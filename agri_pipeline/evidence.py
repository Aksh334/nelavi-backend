"""Source evidence and applicability checks.

A search result is EVIDENCE TO ASSESS, never a recommendation. This module gives retrieved sources a
common structure and decides, deterministically, whether a source may support a recommendation for the
crop, growth stage, region and measurements at hand.

The structure of one evidence entry (all keys always present):
  title, url, organization, publication_date, date_source, retrieved_at, snippet,
  crop, growth_stage, region, supported_claims, limitations, measurement_requirements,
  review_status, provenance.

Entries made from a search result (`evidence_from_research_item`) have `review_status` "unreviewed"
and no crop, stage, region, claims or requirements: a search engine does not tell us those, and this
code does not guess them from the snippet. Only a person can supply them and mark an entry
"reviewed"; nothing in this project does so automatically. `organization` is not derived from the URL.

`check_applicability` returns "applicable" only if the entry was reviewed, declares claims, and names
the crop, growth stage and region it applies to, they match the given context, and every measurement it
requires is present with good quality and, where required, calibration. Anything else is
"not_established" with the unmet requirements listed.

`vet_recommendations` accepts a candidate recommendation only if it cites at least one entry that is
applicable to the context. It cannot judge the candidate's wording, so candidates must come from a
reviewed source or a person, never from a search snippet or a model reply taken at face value.
Pure and deterministic. Inputs are not modified.
"""

import copy

from crop_context.schema import MAX_NAME, MAX_TEXT, PROVENANCE, _text

MAX_SNIPPET = 1000
REVIEW_STATUSES = ("unreviewed", "reviewed")
DATE_SOURCES = (None, "provider_estimate", "reviewer")
ENTRY_KEYS = ("title", "url", "organization", "publication_date", "date_source", "retrieved_at", "snippet",
              "crop", "growth_stage", "region", "supported_claims", "limitations",
              "measurement_requirements", "review_status", "provenance")


def evidence_from_research_item(item, retrieved_at=None):
    """Build an UNREVIEWED evidence entry from a research.tavily.ResearchItem (or a like object)."""
    date = getattr(item, "published_date", None)
    return {
        "title": getattr(item, "title", "") or "",
        "url": getattr(item, "url", ""),
        "organization": None,
        "publication_date": date if isinstance(date, str) and date.strip() else None,
        "date_source": "provider_estimate" if isinstance(date, str) and date.strip() else None,
        "retrieved_at": retrieved_at,
        "snippet": (getattr(item, "snippet", "") or "")[:MAX_SNIPPET],
        "crop": None, "growth_stage": None, "region": None,
        "supported_claims": [], "limitations": [], "measurement_requirements": [],
        "review_status": "unreviewed", "provenance": "retrieved_source",
    }


def validate_evidence(entry):
    """Return a list of {"path", "code"} problems (empty if the entry is well formed)."""
    problems = []

    def bad(path, code):
        problems.append({"path": path, "code": code})

    if not isinstance(entry, dict) or set(entry) != set(ENTRY_KEYS):
        return [{"path": "", "code": "wrong_shape"}]
    if not isinstance(entry["url"], str) or not entry["url"].startswith(("http://", "https://")):
        bad("url", "must_be_a_web_address")
    for key in ("title", "snippet"):
        if not isinstance(entry[key], str):
            bad(key, "must_be_text")
    for key in ("organization", "publication_date", "retrieved_at", "crop", "growth_stage", "region"):
        if entry[key] is not None and not _text(entry[key], MAX_NAME):
            bad(key, "must_be_short_text_or_null")
    if entry["date_source"] not in DATE_SOURCES:
        bad("date_source", "unknown_value")
    if entry["review_status"] not in REVIEW_STATUSES:
        bad("review_status", "unknown_value")
    if entry["provenance"] not in PROVENANCE:
        bad("provenance", "unknown_value")
    for key in ("supported_claims", "limitations"):
        if not isinstance(entry[key], list) or not all(_text(v, MAX_TEXT) for v in entry[key]):
            bad(key, "must_be_a_list_of_short_text")
    requirements = entry["measurement_requirements"]
    if not isinstance(requirements, list) or not all(
            isinstance(r, dict) and set(r) == {"parameter", "calibrated"} and _text(r.get("parameter"), MAX_NAME)
            and isinstance(r.get("calibrated"), bool) for r in requirements):
        bad("measurement_requirements", "must_be_parameter_and_calibrated_pairs")
    return problems


def _same(a, b):
    return isinstance(a, str) and isinstance(b, str) and a.strip().lower() == b.strip().lower()


def check_applicability(entry, context):
    """Return {"status": "applicable" | "not_established", "unmet": [codes]} for one evidence entry."""
    if validate_evidence(entry):
        return {"status": "not_established", "unmet": ["entry_invalid"]}
    unmet = []
    if entry["review_status"] != "reviewed":
        unmet.append("not_reviewed")
    if not entry["supported_claims"]:
        unmet.append("no_supported_claims")
    crop = context["crop"]
    given_crop = crop["candidates"][0]["name"] if crop["status"] == "user_provided" and crop["candidates"] else None
    if entry["crop"] is None:
        unmet.append("crop_not_declared")
    elif given_crop is None:
        unmet.append("crop_not_established")
    elif not _same(entry["crop"], given_crop):
        unmet.append("different_crop")
    stage = context["growth_stage"]
    given_stage = stage["candidate"] if stage["status"] == "user_provided" else None
    if entry["growth_stage"] is None:
        unmet.append("growth_stage_not_declared")
    elif given_stage is None:
        unmet.append("growth_stage_not_established")
    elif not _same(entry["growth_stage"], given_stage):
        unmet.append("different_growth_stage")
    location = context["location"]
    given_region = location["region"] if location["kind"] == "region" else None
    if entry["region"] is None:
        unmet.append("region_not_declared")
    elif given_region is None:
        unmet.append("region_not_established")
    elif not _same(entry["region"], given_region):
        unmet.append("different_region")
    by_parameter = {o["parameter"]: o for o in context["observations"]}
    for requirement in entry["measurement_requirements"]:
        seen = by_parameter.get(requirement["parameter"])
        if seen is None or seen["value"] is None or seen["quality"] != "ok":
            unmet.append("measurement_missing:" + requirement["parameter"])
        elif requirement["calibrated"] and seen["calibration"] != "calibrated":
            unmet.append("measurement_not_calibrated:" + requirement["parameter"])
    return {"status": "not_established" if unmet else "applicable", "unmet": unmet}


def assess_sources(entries, context):
    """Return copies of the entries, each with an `applicability` result added."""
    assessed = []
    for entry in entries:
        copy_ = copy.deepcopy(entry) if isinstance(entry, dict) else entry
        applicability = check_applicability(copy_, context)
        assessed.append(dict(copy_, applicability=applicability) if isinstance(copy_, dict)
                        else {"applicability": applicability})
    return assessed


def vet_recommendations(candidates, entries, context):
    """Split candidate recommendations into (accepted, rejected).

    A candidate is {"text": str, "cites": [url, ...]}. It is accepted only if it has non-empty text and
    cites at least one entry (by URL) that is applicable to the context. A rejection carries a fixed
    code and never the candidate's text.
    """
    applicable = {e["url"]: e for e in entries
                  if isinstance(e, dict) and check_applicability(e, context)["status"] == "applicable"}
    accepted, rejected = [], []
    for index, candidate in enumerate(candidates):
        text = candidate.get("text") if isinstance(candidate, dict) else None
        cites = candidate.get("cites") if isinstance(candidate, dict) else None
        if not _text(text, MAX_TEXT):
            rejected.append({"index": index, "code": "no_usable_text"})
        elif not isinstance(cites, list) or not cites:
            rejected.append({"index": index, "code": "no_source_cited"})
        elif not any(url in applicable for url in cites if isinstance(url, str)):
            rejected.append({"index": index, "code": "no_applicable_source"})
        else:
            sources = [u for u in cites if isinstance(u, str) and u in applicable]
            accepted.append({"text": text, "sources": sources})
    return accepted, rejected
