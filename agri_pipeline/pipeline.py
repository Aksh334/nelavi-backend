"""The reusable agricultural pipeline: one crop-aware path from a Record to a farmer-facing report.

Stages, each small and independently testable (each reports "unavailable" instead of inventing):
  1. ingestion           validate the Record; turn its readings into context observations
  2. user context        crop, region/coordinates and growth stage STATED BY THE USER
  3. image findings      crop / growth-stage / symptom observations from an image provider, if any
  4. context assembly    one validated crop context (see crop_context.schema), with missing and conflicts
  5. source assessment   retrieved sources checked for applicability (agri_pipeline.evidence)
  6. advice              deterministic rules (advice.engine); only the USER's stated crop and stage
                         select crop-specific rules, never an image-based guess
  7. recommendations     rule results, plus candidate statements only if an applicable source backs them
  8. report              observed / inferred / measured / retrieved / supported / uncertain / next steps

There is one pipeline for every crop. No crop is built in; "tomato" or any other crop appears only when a
user or a test supplies it. Nothing here calls a network. Providers are passed in (or absent).

What a caller gets back is a JSON-serialisable dict. `status` is "ok", "invalid_record" or
"invalid_input"; the other statuses carry fixed codes only, never input values.
"""

from advice.engine import EMPTY_RULESET
from advice.engine import evaluate as evaluate_advice
from agri_pipeline.evidence import assess_sources, evidence_from_research_item, vet_recommendations
from agri_pipeline.findings import (
    ERROR_RESPONSE_MALFORMED,
    STATUS_FINDINGS_ERROR,
    STATUS_FINDINGS_OK,
    STATUS_FINDINGS_UNAVAILABLE,
    FindingsResult,
    validate_findings,
)
from crop_context import schema as cc
from crop_context.schema import MAX_NAME, ContextError, build_context
from record.validation import SENSOR_READINGS, validate_record

VERSION = "1"
USER_KEYS = ("crop", "region", "growth_stage", "coordinates")
COORDINATE_KEYS = ("latitude", "longitude", "precision_m")
# The only reading with a documented unit (ARCHITECTURE_DECISIONS.md, MD-3).
READING_UNITS = {"battery.voltage_v": "V"}

ORIGIN_NOTES = {
    "simulated": "SIMULATED: the rover, its sensors and any photo reference come from local simulation code. "
                 "No hardware was used and nothing here was measured in a field.",
    "hardware": "Reported by rover hardware. This project has not verified that hardware.",
    "unknown": "The origin of these observations was not stated.",
}
NEXT_STEPS = {
    "crop_identity": "State the crop. Crop-specific rules and sources are only applied to a crop you state; "
                     "an image-based guess is shown but not used to select them.",
    "growth_stage": "State the growth stage. Many recommendations depend on it and it is not guessed.",
    "location": "Add a region, or coordinates with their source and precision. None is available from the rover.",
    "visual_observations": "Add a clear photo of the affected plant part. A configured image model is also "
                           "needed before any visual finding exists.",
    "soil_observations": "Take a soil measurement with the probe.",
    "npk_calibration": "NPK readings are raw probe values. Coefficients fitted on lab samples are needed "
                       "before they can be treated as nutrient values.",
    "units": "Define the units of the readings (open contract item R-13).",
    "crop_unconfirmed": "Confirm the crop by stating it. The image-based candidate is shown but is not used "
                        "to select rules or sources.",
    "growth_stage_unconfirmed": "Confirm the growth stage by stating it. The image-based candidate is shown "
                                "but is not used to select rules or sources.",
}
UNCONFIRMED_TEXT = {
    "crop_unconfirmed": "The crop is only an image-based candidate and has not been confirmed.",
    "growth_stage_unconfirmed": "The growth stage is only an image-based candidate and has not been confirmed.",
}
UNAVAILABLE_TEXT = {
    "crop_identity": "The crop is not known.",
    "growth_stage": "The growth stage is not known.",
    "location": "No location was provided.",
    "visual_observations": "No visual observations are available.",
    "soil_observations": "No soil observations are available.",
}


def observations_from_record(record):
    """Context observations for a Record that passed validation. Units, quality and calibration are
    kept as stated: unit None means the Record defines none."""
    observations = []
    stamp = record["time"] if cc._text(record["time"], MAX_NAME) else None
    for sensor, pairs in SENSOR_READINGS.items():
        for section, key in pairs:
            path = "%s.%s" % (section, key)
            observations.append(cc.observation(
                path, record[section][key], READING_UNITS.get(path), "sensor_observation", stamp,
                record["sensor_status"][sensor],
                # No calibration coefficients exist in this project, so probe values are raw.
                "uncalibrated" if sensor == "npk" else "unknown"))
    return observations


def _user_sections(user):
    """Return (crop_name, region, stage_name, coordinates) from a user dict, or raise ValueError."""
    if user is None:
        return None, None, None, None
    if not isinstance(user, dict) or not set(user) <= set(USER_KEYS):
        raise ValueError("user")
    coordinates = user.get("coordinates")
    if coordinates is not None and (not isinstance(coordinates, dict)
                                    or not set(coordinates) <= set(COORDINATE_KEYS)
                                    or "latitude" not in coordinates or "longitude" not in coordinates):
        raise ValueError("coordinates")
    if user.get("region") is not None and coordinates is not None:
        raise ValueError("region_and_coordinates")
    return user.get("crop"), user.get("region"), user.get("growth_stage"), coordinates


def _blank(value):
    return value is None or (isinstance(value, str) and value.strip() == "")


def _crop_section(user_crop, findings):
    detected = []
    if findings:
        detected = [cc.candidate(c["name"], "image_analysis", c["evidence"], c["uncertainty"])
                    for c in findings["crop"]["candidates"]]
    if not _blank(user_crop):
        return {"status": "user_provided",
                "candidates": [cc.candidate(user_crop.strip() if isinstance(user_crop, str) else user_crop,
                                            "user_provided")] + detected}
    if detected:
        return {"status": "candidate", "candidates": detected}
    return cc.empty_crop()


def _stage_section(user_stage, findings):
    if not _blank(user_stage):
        stage = cc.empty_growth_stage()
        stage.update(status="user_provided", provenance="user_provided",
                     candidate=user_stage.strip() if isinstance(user_stage, str) else user_stage)
        return stage
    if findings and findings["growth_stage"]["candidate"] is not None:
        found = findings["growth_stage"]
        return {"status": "candidate", "candidate": found["candidate"], "provenance": "image_analysis",
                "evidence": list(found["evidence"]), "uncertainty": found["uncertainty"]}
    return cc.empty_growth_stage()


def _location_section(region, coordinates):
    location = cc.empty_location()
    if not _blank(region):
        location.update(kind="region", region=region.strip() if isinstance(region, str) else region,
                        source="user_provided")
    elif coordinates is not None:
        location.update(kind="coordinates", latitude=coordinates["latitude"], longitude=coordinates["longitude"],
                        source="user_provided", precision_m=coordinates.get("precision_m"))
    return location


def _symptoms(findings, image_ref):
    if not findings:
        return []
    return [cc.symptom(s["observation"], image_ref, "image_analysis", s["possible_interpretations"])
            for s in findings["symptoms"]]


def _image_section(findings_result):
    if findings_result is None:
        return {"status": "not_run", "reason": "no_image_in_this_run", "error_code": None, "findings": None,
                "note": "No image was analysed in this run, so nothing was inferred from an image."}
    base = {"status": findings_result.status, "reason": findings_result.reason,
            "error_code": findings_result.error_code, "findings": None, "model": findings_result.model}
    if findings_result.status == STATUS_FINDINGS_OK:
        base["findings"] = findings_result.findings
        base["note"] = ("Model-inferred observations, not verified facts. A symptom is an observation and its "
                        "interpretations are only possibilities; none is a confirmed result.")
    elif findings_result.status == STATUS_FINDINGS_UNAVAILABLE:
        base["note"] = "No image model is configured, so nothing was inferred from an image."
    else:
        base["note"] = "The image analysis failed, so nothing was inferred from an image."
    return base


# What a malformed or unrecognised `findings_result` is reported as. It uses the existing error vocabulary
# (the same code a malformed model reply gets) and carries no findings, so nothing is made up.
_MALFORMED_FINDINGS = FindingsResult(status=STATUS_FINDINGS_ERROR, error_code=ERROR_RESPONSE_MALFORMED,
                                     reason=None, findings=None, model=None)


def _checked_findings(findings_result):
    """Return (findings_result, findings) for the pipeline to use.

    A well-formed result passes through unchanged: status "ok" with findings that satisfy
    `agri_pipeline.findings.validate_findings`, or status "unavailable" / "error" with the attributes the report
    reads. Anything else (not a result object, an unrecognised status, "ok" with missing or non-conforming
    findings) is replaced by an honest `response_malformed` error with no findings, like the research path does
    for an odd object. Exceptions are caught only for a missing attribute."""
    if findings_result is None:
        return None, None
    try:
        status = findings_result.status
        if status == STATUS_FINDINGS_OK:
            if validate_findings(findings_result.findings):
                return findings_result, findings_result.findings
        elif status in (STATUS_FINDINGS_UNAVAILABLE, STATUS_FINDINGS_ERROR):
            findings_result.reason, findings_result.error_code, findings_result.model  # the report reads these
            return findings_result, None
    except AttributeError:
        pass
    return _MALFORMED_FINDINGS, None


def _research_parts(research_result):
    """Turn a research result (duck-typed: status, error_code, items, retrieved_at, dropped, query,
    caveats) into (evidence entries, info dict). A missing or malformed result is reported as an
    error, never as sources."""
    if research_result is None:
        return [], {"status": "not_run", "error_code": None, "query": None, "retrieved_at": None, "dropped": 0,
                    "caveats": []}
    try:
        status = research_result.status
        items = list(research_result.items) if status == "ok" else []
        retrieved_at = research_result.retrieved_at
        entries = [evidence_from_research_item(item, retrieved_at) for item in items]
        caveats = [{"code": c[0], "message": c[1]} for c in research_result.caveats]
        info = {"status": "ok" if status == "ok" and entries else "error",
                "error_code": None if status == "ok" and entries else (
                    research_result.error_code if status != "ok" else "no_results"),
                "query": research_result.query if isinstance(research_result.query, str) else None,
                "retrieved_at": retrieved_at if isinstance(retrieved_at, str) else None,
                "dropped": research_result.dropped if isinstance(research_result.dropped, int) else 0,
                "caveats": caveats}
        if info["status"] == "error" and not isinstance(info["error_code"], str):
            info["error_code"] = "malformed_research_result"
        return entries, info
    except Exception:  # an odd object must not crash the report or become sources
        return [], {"status": "error", "error_code": "malformed_research_result", "query": None,
                    "retrieved_at": None, "dropped": 0, "caveats": []}


RESEARCH_NOTE = ("Search results are evidence to assess, not recommendations. A result supports a "
                 "recommendation only if a person has reviewed it and it is applicable to this crop, growth "
                 "stage, region and measurements.")


def research_report(research_result, user=None):
    """The advisory report: retrieved sources, assessed for applicability to what the user stated.

    It needs no Record. It never produces a recommendation: `recommendations.supported` is always empty
    here, because nothing retrieved has been reviewed. Returns a JSON-serialisable dict."""
    problems = validate_user(user)
    if problems:
        return {"version": VERSION, "status": "invalid_input", "errors": problems, "sources": []}
    crop, region, stage, coordinates = _user_sections(user)
    context = build_context(origin="unknown", crop=_crop_section(crop, None),
                            location=_location_section(region, coordinates),
                            growth_stage=_stage_section(stage, None))
    entries, info = _research_parts(research_result)
    # Only what bears on judging a source: the crop, growth stage and place. Photos and soil readings
    # belong to the farm report, not to a research search.
    steps = [NEXT_STEPS[code] for code in context["missing"] if code in ("crop_identity", "growth_stage", "location")]
    steps.append("A person must review a source for this crop, growth stage and region before it can support "
                 "a recommendation.")
    return {
        "version": VERSION, "status": info["status"], "error_code": info["error_code"], "kind": "research_evidence_only",
        "query": info["query"], "retrieved_at": info["retrieved_at"], "dropped": info["dropped"],
        "sources": assess_sources(entries, context), "caveats": info["caveats"], "note": RESEARCH_NOTE,
        "recommendations": {"supported": [], "note": "None. Retrieved sources are not recommendations."},
        "context_missing": list(context["missing"]), "next_steps": steps, "errors": [],
    }


def validate_user(user):
    """Return a list of {"path", "code"} problems for the user's stated context (empty if usable).
    It never contains the submitted values."""
    try:
        crop, region, stage, coordinates = _user_sections(user)
        build_context(origin="unknown", crop=_crop_section(crop, None), location=_location_section(region, coordinates),
                      growth_stage=_stage_section(stage, None))
    except ValueError as exc:
        if isinstance(exc, ContextError):
            return exc.problems
        return [{"path": str(exc), "code": "not_allowed"}]
    return []


def _invalid(status, origin, codes):
    return {
        "version": VERSION, "status": status, "origin": origin, "origin_note": ORIGIN_NOTES.get(origin, ""),
        "errors": codes, "context": None, "advice": None, "sections": None,
    }


def run(record, origin="unknown", user=None, findings_result=None, evidence=(), ruleset=EMPTY_RULESET,
        calibration=None, image_ref=None, recommendation_candidates=(), research_result=None):
    """Run the pipeline for one Record and return the report dict (see the module docstring).

    `research_result` is an optional research result (see `research_report`); its sources join `evidence`
    as unreviewed entries and its status is shown honestly in the report."""
    if origin not in cc.ORIGINS:
        return _invalid("invalid_input", "unknown", [{"path": "origin", "code": "unknown_value"}])
    checked = validate_record(record)
    if not checked.valid:
        return _invalid("invalid_record", origin, [{"path": e.path, "code": e.code} for e in checked.errors])
    record = checked.record
    try:
        user_crop, region, user_stage, coordinates = _user_sections(user)
    except ValueError as exc:
        return _invalid("invalid_input", origin, [{"path": str(exc), "code": "not_allowed"}])
    findings_result, findings = _checked_findings(findings_result)
    try:
        context = build_context(
            origin=origin,
            crop=_crop_section(user_crop, findings),
            location=_location_section(region, coordinates),
            growth_stage=_stage_section(user_stage, findings),
            symptoms=_symptoms(findings, image_ref),
            observations=observations_from_record(record))
    except ContextError as exc:
        return _invalid("invalid_input", origin, exc.problems)

    # Only the user's own statements select crop-specific rules; a model's guess never does.
    stated_crop = context["crop"]["candidates"][0]["name"] if context["crop"]["status"] == "user_provided" else None
    stated_stage = context["growth_stage"]["candidate"] if context["growth_stage"]["status"] == "user_provided" else None
    advice = evaluate_advice(record, ruleset, calibration, crop=stated_crop, growth_stage=stated_stage)

    research_entries, research_info = _research_parts(research_result)
    all_evidence = list(evidence) + research_entries
    sources = assess_sources(all_evidence, context)
    accepted, rejected = vet_recommendations(list(recommendation_candidates), all_evidence, context)
    supported = [{"kind": "rule", "text": item["message"], "source": item["source"], "rule_id": item["rule_id"],
                  "based_on": item["evidence"]} for item in advice["items"]]
    supported += [{"kind": "sourced_statement", "text": c["text"], "source": ", ".join(c["sources"]),
                   "rule_id": None, "based_on": []} for c in accepted]

    unavailable = [{"code": code, "text": UNAVAILABLE_TEXT[code]} for code in context["missing"]]
    unavailable += [{"code": c["code"], "text": c["detail"]} for c in context["conflicts"]]
    # A crop or stage that is only an image-based guess is shown, but it is not confirmed and is not
    # used to select rules or sources.
    unconfirmed = [code for code, section in (("crop_unconfirmed", context["crop"]),
                                              ("growth_stage_unconfirmed", context["growth_stage"]))
                   if section["status"] == "candidate"]
    for code in unconfirmed:
        unavailable.append({"code": code, "text": UNCONFIRMED_TEXT[code]})
    unavailable.append({"code": "npk_uncalibrated", "text": NEXT_STEPS["npk_calibration"]})
    unavailable.append({"code": "units_undefined", "text": NEXT_STEPS["units"]})
    if research_info["status"] == "error":
        unavailable.append({"code": "research_failed",
                            "text": "The research search returned no usable sources (code: %s)."
                                    % research_info["error_code"]})
    elif not sources:
        unavailable.append({"code": "no_sources_retrieved",
                            "text": "No agricultural sources were retrieved in this run."})
    if not supported:
        unavailable.append({"code": "no_supported_recommendation",
                            "text": "No recommendation is supported. This does not mean the crop is healthy or "
                                    "that no action is needed."})

    steps, seen = [], set()
    for code in context["missing"]:
        steps.append(NEXT_STEPS[code])
    for code in unconfirmed:
        steps.append(NEXT_STEPS[code])
    steps.append(NEXT_STEPS["npk_calibration"])
    steps.append(NEXT_STEPS["units"])
    for need in advice["missing_information"]:
        if need["needs"] not in seen:
            seen.add(need["needs"])
            steps.append(need["needs"])
    if findings_result is not None and findings:
        steps += list(findings["needs_more"])
    steps = list(dict.fromkeys(steps))

    record_time = record["time"]
    return {
        "version": VERSION, "status": "ok", "origin": origin, "origin_note": ORIGIN_NOTES[origin],
        "errors": [], "context": context, "advice": advice,
        "sections": {
            "rover_observed": {
                "time": record_time if isinstance(record_time, str) else None,
                "photo_reference": record["photo_id"],
                "leaf_label": record["leaf_label"],
                "note": "The photo reference and leaf label are the rover's own fields. In a simulation they "
                        "are placeholders: no image exists and nothing was analysed." if origin == "simulated"
                        else "The photo reference and leaf label are the rover's own fields, as reported.",
            },
            "image_inference": _image_section(findings_result),
            "sensors_measured": {
                "observations": context["observations"],
                "note": "Original values with their units (None means the Record defines none), quality and "
                        "calibration status. NPK values are raw probe readings, not laboratory values.",
            },
            "retrieved_sources": {
                "status": research_info["status"], "error_code": research_info["error_code"],
                "retrieved_at": research_info["retrieved_at"], "dropped": research_info["dropped"],
                # The search's own caveats, in the same {"code", "message"} form as `research_report`; empty
                # when no search ran or its result was unusable. Nothing is added here.
                "caveats": research_info["caveats"],
                "entries": sources,
                "note": "Search results are evidence to assess, not recommendations. An entry supports a "
                        "recommendation only if it is applicable to this crop, stage, region and measurements.",
            },
            "recommendations": {"supported": supported, "rejected": rejected,
                                "advice_outcome": advice["outcome"], "advice_reason": advice["reason"]},
            "uncertain_or_unavailable": unavailable,
            "next_steps": steps,
        },
    }
