"""Crop context V1: what is known (and not known) about a crop, with where each fact came from.

This is an ADDITIVE internal representation. It does not change the Record format or any frozen
contract: the Record has no crop, location, growth-stage or symptom fields (RECORD_CONTRACT open items
R-13 and R-14), so a context is built AROUND a validated Record and never written back into it. The
layout below is a TEMPORARY INTERNAL CONVENTION; making any of it part of a contract needs the owner.

Design rules, enforced by `validate_context` and tested:
  * Nothing is mandatory. Unknown crop, unknown growth stage and no location are normal, valid states
    and are listed under `missing`; nothing is guessed to fill them.
  * Every fact carries a provenance: sensor_observation, image_analysis, user_provided,
    retrieved_source or inference.
  * Uncertainty is qualitative (`unspecified`, `low`, `medium`, `high`). A numeric confidence is never
    stored, because no source defines what a confidence number would mean (A-10, R-9).
  * A visual symptom is an OBSERVATION. Its interpretations are only ever `possible`. A confirmation
    needs a stated method and source; this code never produces one.
  * Location is `none`, a `region` (text) or `coordinates`. Coordinates need a stated source (a sensor
    or the user) and a declared precision or `None` for unknown. No coordinates are ever invented, and
    the project's hardware documents list no GPS/GNSS receiver.
  * A soil or air observation keeps its original value, unit (or `None` if undefined), timestamp text,
    quality and calibration status. A `calibrated` observation must carry a unit.
  * Disagreements (for example, a user-given crop versus an image-derived candidate) are recorded under
    `conflicts`, not resolved.

Pure and deterministic: no I/O, network, clock or randomness. Inputs are not modified.
"""

import copy
import math
import unicodedata

VERSION = "1"

PROVENANCE = ("sensor_observation", "image_analysis", "user_provided", "retrieved_source", "inference")
UNCERTAINTY = ("unspecified", "low", "medium", "high")
QUALITY = ("ok", "fault", "unknown")
CALIBRATION = ("calibrated", "uncalibrated", "unknown", "not_applicable")
ORIGINS = ("simulated", "hardware", "unknown")
CROP_STATUS = ("unknown", "candidate", "user_provided")
STAGE_STATUS = ("unknown", "candidate", "user_provided")
LOCATION_KINDS = ("none", "region", "coordinates")
DETECTED_PROVENANCE = ("image_analysis", "inference", "retrieved_source")
COORDINATE_SOURCES = ("sensor_observation", "user_provided")

TOP_KEYS = ("version", "origin", "crop", "location", "growth_stage", "symptoms", "observations",
            "missing", "conflicts")
MAX_NAME = 64
MAX_TEXT = 300
MAX_EVIDENCE = 5
MAX_EVIDENCE_CHARS = 200
MAX_CANDIDATES = 10
MAX_SYMPTOMS = 20
MAX_OBSERVATIONS = 100
MAX_UNIT = 16


class ContextError(ValueError):
    """The context is invalid. `problems` is a list of {"path", "code"}; it never holds input values."""

    def __init__(self, problems):
        super().__init__("invalid crop context: %d problem(s)" % len(problems))
        self.problems = problems


def _is_finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _text(value, limit):
    return isinstance(value, str) and value.strip() != "" and len(value) <= limit and not any(
        unicodedata.category(c).startswith("C") for c in value)


def _name(value):
    """A short name: letters, marks, digits, space and a few joiners; no markup or control characters."""
    return _text(value, MAX_NAME) and all(
        c in " -'.," or unicodedata.category(c)[0] in ("L", "M") or unicodedata.category(c) == "Nd"
        for c in value)


# ------------------------------------------------------------------ section builders
def empty_crop():
    return {"status": "unknown", "candidates": []}


def empty_location():
    return {"kind": "none", "region": None, "latitude": None, "longitude": None, "source": None,
            "precision_m": None}


def empty_growth_stage():
    return {"status": "unknown", "candidate": None, "provenance": None, "evidence": [],
            "uncertainty": "unspecified"}


def candidate(name, provenance, evidence=(), uncertainty="unspecified"):
    return {"name": name, "provenance": provenance, "evidence": list(evidence),
            "uncertainty": uncertainty}


def observation(parameter, value, unit, source, timestamp, quality, calibration):
    return {"parameter": parameter, "value": value, "unit": unit, "source": source,
            "timestamp": timestamp, "quality": quality, "calibration": calibration}


def symptom(text, image_ref, provenance, interpretations=(), confirmation=None):
    return {"observation": text, "image_ref": image_ref, "provenance": provenance,
            "possible_interpretations": [{"label": label, "status": "possible"} for label in interpretations],
            "confirmation": confirmation}


# --------------------------------------------------------------------- derived fields
def _missing(context):
    missing = []
    if context["crop"]["status"] == "unknown":
        missing.append("crop_identity")
    if context["growth_stage"]["status"] == "unknown":
        missing.append("growth_stage")
    if context["location"]["kind"] == "none":
        missing.append("location")
    if not context["symptoms"]:
        missing.append("visual_observations")
    if not any(o["parameter"].startswith("soil.") for o in context["observations"]):
        missing.append("soil_observations")
    return missing


def _conflicts(context):
    conflicts = []
    crop = context["crop"]
    # Alternatives to a detected crop are expected and are not conflicts. A conflict is a detected
    # candidate that names a different crop than the one the user gave.
    if crop["status"] == "user_provided" and crop["candidates"]:
        given = crop["candidates"][0]["name"].strip().lower()
        if any(c["name"].strip().lower() != given for c in crop["candidates"][1:]):
            conflicts.append({"code": "crop_user_vs_detected",
                              "detail": "an image-derived candidate names a different crop than the user gave"})
    seen = {}
    for item in context["observations"]:
        key = (item["parameter"], item["source"])
        if key in seen and seen[key] != item["value"]:
            conflicts.append({"code": "observation_values_differ", "detail": item["parameter"]})
        seen.setdefault(key, item["value"])
    return conflicts


def refresh(context):
    """Return a copy of `context` with `missing` and `conflicts` recomputed (not validated)."""
    result = copy.deepcopy(context)
    result["missing"] = _missing(result)
    result["conflicts"] = _conflicts(result)
    return result


# ------------------------------------------------------------------------- validation
def validate_context(context):
    """Return a list of {"path", "code"} problems (empty if the context is valid)."""
    problems = []

    def bad(path, code):
        problems.append({"path": path, "code": code})

    if not isinstance(context, dict) or set(context) != set(TOP_KEYS):
        return [{"path": "", "code": "wrong_shape"}]
    if context["version"] != VERSION:
        bad("version", "unsupported_version")
    if context["origin"] not in ORIGINS:
        bad("origin", "unknown_value")
    _check_crop(context["crop"], bad)
    _check_location(context["location"], bad)
    _check_stage(context["growth_stage"], bad)
    _check_symptoms(context["symptoms"], bad)
    _check_observations(context["observations"], bad)
    for key in ("missing", "conflicts"):
        if not isinstance(context[key], list):
            bad(key, "must_be_a_list")
    if not problems:
        expected = refresh(context)
        if context["missing"] != expected["missing"]:
            bad("missing", "does_not_match_the_content")
        if context["conflicts"] != expected["conflicts"]:
            bad("conflicts", "does_not_match_the_content")
    return problems


def _check_evidence(entry, path, bad):
    evidence = entry.get("evidence")
    if not isinstance(evidence, list) or len(evidence) > MAX_EVIDENCE or not all(
            _text(e, MAX_EVIDENCE_CHARS) for e in evidence):
        bad(path + ".evidence", "must_be_short_text_items")


def _check_candidate(entry, path, bad):
    if not isinstance(entry, dict) or set(entry) != {"name", "provenance", "evidence", "uncertainty"}:
        bad(path, "wrong_shape")
        return
    if not _name(entry["name"]):
        bad(path + ".name", "must_be_a_short_name")
    if entry["provenance"] not in PROVENANCE:
        bad(path + ".provenance", "unknown_value")
    if entry["uncertainty"] not in UNCERTAINTY:
        bad(path + ".uncertainty", "unknown_value")
    _check_evidence(entry, path, bad)


def _check_crop(crop, bad):
    if not isinstance(crop, dict) or set(crop) != {"status", "candidates"}:
        bad("crop", "wrong_shape")
        return
    candidates = crop["candidates"]
    if crop["status"] not in CROP_STATUS:
        bad("crop.status", "unknown_value")
    if not isinstance(candidates, list) or len(candidates) > MAX_CANDIDATES:
        bad("crop.candidates", "must_be_a_short_list")
        return
    for index, entry in enumerate(candidates):
        _check_candidate(entry, "crop.candidates[%d]" % index, bad)
    valid = [c for c in candidates if isinstance(c, dict)]
    if crop["status"] == "unknown" and candidates:
        bad("crop.candidates", "must_be_empty_when_unknown")
    if crop["status"] == "candidate":
        if not valid or valid[0].get("provenance") not in DETECTED_PROVENANCE:
            bad("crop.candidates[0]", "must_be_a_detected_candidate")
    if crop["status"] == "user_provided":
        if not valid or valid[0].get("provenance") != "user_provided":
            bad("crop.candidates[0]", "must_be_user_provided")


def _check_location(location, bad):
    keys = {"kind", "region", "latitude", "longitude", "source", "precision_m"}
    if not isinstance(location, dict) or set(location) != keys:
        bad("location", "wrong_shape")
        return
    kind = location["kind"]
    if kind not in LOCATION_KINDS:
        bad("location.kind", "unknown_value")
        return
    if kind == "none":
        if any(location[k] is not None for k in keys - {"kind"}):
            bad("location", "must_be_empty_when_none")
        return
    if location["source"] not in PROVENANCE:
        bad("location.source", "must_state_a_source")
    precision = location["precision_m"]
    if precision is not None and not (_is_finite(precision) and precision > 0):
        bad("location.precision_m", "must_be_positive_or_unknown")
    if kind == "region":
        if not _name(location["region"]):
            bad("location.region", "must_be_a_short_name")
        if location["latitude"] is not None or location["longitude"] is not None:
            bad("location", "a_region_has_no_coordinates")
    if kind == "coordinates":
        if location["region"] is not None:
            bad("location.region", "must_be_empty_for_coordinates")
        lat, lon = location["latitude"], location["longitude"]
        if not (_is_finite(lat) and -90 <= lat <= 90):
            bad("location.latitude", "out_of_range")
        if not (_is_finite(lon) and -180 <= lon <= 180):
            bad("location.longitude", "out_of_range")
        if location["source"] not in COORDINATE_SOURCES:
            bad("location.source", "coordinates_need_a_sensor_or_the_user")


def _check_stage(stage, bad):
    keys = {"status", "candidate", "provenance", "evidence", "uncertainty"}
    if not isinstance(stage, dict) or set(stage) != keys:
        bad("growth_stage", "wrong_shape")
        return
    if stage["status"] not in STAGE_STATUS:
        bad("growth_stage.status", "unknown_value")
    if stage["uncertainty"] not in UNCERTAINTY:
        bad("growth_stage.uncertainty", "unknown_value")
    _check_evidence(stage, "growth_stage", bad)
    if stage["status"] == "unknown":
        if stage["candidate"] is not None or stage["provenance"] is not None or stage["evidence"]:
            bad("growth_stage", "must_be_empty_when_unknown")
    else:
        if not _name(stage["candidate"]):
            bad("growth_stage.candidate", "must_be_a_short_name")
        if stage["provenance"] not in PROVENANCE:
            bad("growth_stage.provenance", "must_state_a_source")
        if stage["status"] == "user_provided" and stage["provenance"] != "user_provided":
            bad("growth_stage.provenance", "must_be_user_provided")


def _check_symptoms(symptoms, bad):
    if not isinstance(symptoms, list) or len(symptoms) > MAX_SYMPTOMS:
        bad("symptoms", "must_be_a_short_list")
        return
    keys = {"observation", "image_ref", "provenance", "possible_interpretations", "confirmation"}
    for index, entry in enumerate(symptoms):
        path = "symptoms[%d]" % index
        if not isinstance(entry, dict) or set(entry) != keys:
            bad(path, "wrong_shape")
            continue
        if not _text(entry["observation"], MAX_TEXT):
            bad(path + ".observation", "must_be_short_text")
        if entry["image_ref"] is not None and not _text(entry["image_ref"], MAX_NAME):
            bad(path + ".image_ref", "must_be_short_text")
        if entry["provenance"] not in PROVENANCE:
            bad(path + ".provenance", "must_state_a_source")
        interpretations = entry["possible_interpretations"]
        if not isinstance(interpretations, list) or len(interpretations) > MAX_CANDIDATES:
            bad(path + ".possible_interpretations", "must_be_a_short_list")
        else:
            for number, item in enumerate(interpretations):
                if not isinstance(item, dict) or set(item) != {"label", "status"} or not _name(item.get("label")):
                    bad("%s.possible_interpretations[%d]" % (path, number), "wrong_shape")
                elif item["status"] != "possible":
                    # An interpretation is never stated as a finding here.
                    bad("%s.possible_interpretations[%d].status" % (path, number), "must_be_possible")
        confirmation = entry["confirmation"]
        if confirmation is not None and (not isinstance(confirmation, dict)
                                         or set(confirmation) != {"method", "source"}
                                         or not _text(confirmation.get("method"), MAX_TEXT)
                                         or not _text(confirmation.get("source"), MAX_TEXT)):
            bad(path + ".confirmation", "needs_a_method_and_a_source")


def _check_observations(observations, bad):
    if not isinstance(observations, list) or len(observations) > MAX_OBSERVATIONS:
        bad("observations", "must_be_a_short_list")
        return
    keys = {"parameter", "value", "unit", "source", "timestamp", "quality", "calibration"}
    for index, entry in enumerate(observations):
        path = "observations[%d]" % index
        if not isinstance(entry, dict) or set(entry) != keys:
            bad(path, "wrong_shape")
            continue
        if not _text(entry["parameter"], MAX_NAME):
            bad(path + ".parameter", "must_be_short_text")
        if entry["value"] is not None and not _is_finite(entry["value"]):
            bad(path + ".value", "must_be_a_finite_number_or_null")
        if entry["unit"] is not None and not _text(entry["unit"], MAX_UNIT):
            bad(path + ".unit", "must_be_short_text_or_null")
        if entry["source"] not in PROVENANCE:
            bad(path + ".source", "must_state_a_source")
        if entry["timestamp"] is not None and not _text(entry["timestamp"], MAX_NAME):
            bad(path + ".timestamp", "must_be_short_text_or_null")
        if entry["quality"] not in QUALITY:
            bad(path + ".quality", "unknown_value")
        if entry["calibration"] not in CALIBRATION:
            bad(path + ".calibration", "unknown_value")
        if entry["calibration"] == "calibrated" and entry["unit"] is None:
            bad(path + ".calibration", "a_calibrated_value_needs_a_unit")
        if entry["quality"] == "fault" and entry["calibration"] == "calibrated":
            bad(path + ".calibration", "a_faulted_reading_is_not_calibrated")


# --------------------------------------------------------------------------- builder
def build_context(origin="unknown", crop=None, location=None, growth_stage=None, symptoms=(),
                  observations=()):
    """Assemble a context from already-structured parts, recompute `missing` and `conflicts`, validate.

    Raises ContextError if the result is invalid. Every part is optional.
    """
    context = {
        "version": VERSION,
        "origin": origin,
        "crop": copy.deepcopy(crop) if crop is not None else empty_crop(),
        "location": copy.deepcopy(location) if location is not None else empty_location(),
        "growth_stage": copy.deepcopy(growth_stage) if growth_stage is not None else empty_growth_stage(),
        "symptoms": copy.deepcopy(list(symptoms)),
        "observations": copy.deepcopy(list(observations)),
        "missing": [],
        "conflicts": [],
    }
    # Validate the parts first (so a malformed part cannot crash the derived-field code), then derive
    # `missing` and `conflicts` and validate the whole.
    problems = [p for p in validate_context(context) if p["code"] != "does_not_match_the_content"]
    if problems:
        raise ContextError(problems)
    context = refresh(context)
    problems = validate_context(context)
    if problems:
        raise ContextError(problems)
    return context
