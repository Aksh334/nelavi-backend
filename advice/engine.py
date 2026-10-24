"""Deterministic rules engine (V1): documented rules applied to a validated Record.

Source (docs/cropsync_coding_overview.docx, section 6): "Rules live in a versioned data file with
condition, message, priority and source. Write a unit test for every rule. Thresholds come from
official recommendations only, and NPK probe values pass through the calibration correction before
the rules see them." Section 8 gives the correction `Lab = a*Probe + c*Moisture + b`, with a, b and c
fitted on lab samples and kept in a config file.

What this module does NOT contain, because no authoritative source supplies it:
  * any rule, threshold, crop, nutrient target or fertilizer dose (the ruleset is supplied by the
    caller; `EMPTY_RULESET` is the only one shipped, and it has no rules),
  * calibration coefficients (a, b, c are supplied by the caller; none are defined here),
  * the units of the readings (the Record schema defines none except battery volts; R-13),
  * any staleness rule (the Record's time has no defined source, R-2),
  * any use of leaf confidence, AI output or the second opinion.

The rule-file layout below (field names `id`, `when`, `message`, `priority`, `source`, and the
condition operators) is a TEMPORARY INTERNAL CONVENTION. The source names the four ideas but not a
format. `priority` is carried through unchanged and is NOT used to order or filter anything, because
the source does not say what it means. Rules are reported in file order.

Behaviour, in order:
  1. The Record must pass Record Core validation, else the outcome is `invalid_record`.
  2. The ruleset must be well formed, else the outcome is `invalid_ruleset`.
  3. A condition is only assessed if its sensor's status flag is "ok" and the reading is present
     (contract principle P4). Otherwise it is reported under `not_assessed`, never as "not met".
  4. A condition on soil nitrogen, phosphorus or potassium uses the calibration-corrected value, and
     is not assessed unless calibration coefficients were supplied and the moisture reading is usable.
  5. A rule fires only when every one of its conditions was assessed and holds.
  6. `no_rule_fired` means exactly that; it is not a statement that the soil or crop is fine.

Pure and deterministic: no I/O, no network, no clock, no randomness. Inputs are never modified.
"""

import math

from calibration.npk_correction import correct_npk
from record.validation import SENSOR_READINGS, validate_record

EMPTY_RULESET = {"version": "none", "rules": []}

OUTCOME_ADVICE = "advice"
OUTCOME_NO_RULE_FIRED = "no_rule_fired"
OUTCOME_INSUFFICIENT = "insufficient_evidence"
OUTCOME_INVALID_RECORD = "invalid_record"
OUTCOME_INVALID_RULESET = "invalid_ruleset"

REASON_NO_RULES = "no_rules"
REASON_NOTHING_ASSESSABLE = "nothing_could_be_assessed"
NOT_ASSESSED_SENSOR_FAULT = "sensor_fault"
NOT_ASSESSED_READING_MISSING = "reading_missing"
NOT_ASSESSED_NO_CALIBRATION = "calibration_not_configured"
NOT_ASSESSED_MOISTURE_UNUSABLE = "moisture_unusable_for_calibration"
NOT_ASSESSED_CROP_UNKNOWN = "crop_not_established"
NOT_ASSESSED_STAGE_UNKNOWN = "growth_stage_not_established"
NOT_APPLICABLE_CROP = "different_crop"
NOT_APPLICABLE_STAGE = "different_growth_stage"
REASON_NOTHING_APPLICABLE = "no_rule_applies_to_this_context"
OPTIONAL_RULE_KEYS = ("crop", "growth_stage")

OPERATORS = {
    "lt": lambda value, threshold: value < threshold,
    "le": lambda value, threshold: value <= threshold,
    "gt": lambda value, threshold: value > threshold,
    "ge": lambda value, threshold: value >= threshold,
}
NPK_NUTRIENTS = ("nitrogen", "phosphorus", "potassium")
MOISTURE_PATH = "soil.moisture"

# What a user must provide before advice can exist. Fixed wording; it never states or implies a
# result about the crop or soil.
NEEDS = {
    "agronomic_rules": "A versioned ruleset in which every rule names its official source. None is loaded.",
    "crop_and_growth_stage": "The crop and its growth stage. The Record carries neither, and official "
                             "recommendations usually depend on them.",
    "calibrated_lab_values": "NPK values calibrated against lab samples (coefficients a, b and c fitted on "
                             "your own lab samples), in the units the rules use. Probe readings are not lab values.",
    "reading_units": "The units of the readings, which the Record does not define (open item R-13).",
    NOT_ASSESSED_SENSOR_FAULT: "A working sensor: the reading's status flag is not \"ok\".",
    NOT_ASSESSED_READING_MISSING: "A reading value; the Record has none for this sensor.",
    NOT_ASSESSED_NO_CALIBRATION: "Calibration coefficients (a, b and c) for the nutrient; none were supplied.",
    NOT_ASSESSED_MOISTURE_UNUSABLE: "A usable soil moisture reading, which the NPK correction needs.",
    NOT_ASSESSED_CROP_UNKNOWN: "The crop, stated by the user. The rule names the crops it applies to, and "
                               "an image-based guess is not used to select rules.",
    NOT_ASSESSED_STAGE_UNKNOWN: "The growth stage, stated by the user. The rule names the stages it applies to.",
    "rules_for_this_context": "Rules, each with an official source, for this crop and growth stage. The "
                              "loaded rules name other crops or stages.",
}
NEEDS_WHEN_NO_RULES = ("agronomic_rules", "crop_and_growth_stage", "calibrated_lab_values", "reading_units")

CAVEATS = (
    {"code": "units_unresolved",
     "message": "The Record defines no units for its readings except battery volts (open item R-13), "
                "so a threshold's unit cannot be checked against the Record."},
    {"code": "staleness_not_assessed",
     "message": "The Record's time has no defined source (open item R-2), so reading age is not checked."},
    {"code": "rules_are_not_a_diagnosis",
     "message": "Messages come only from the supplied rules. This is not a diagnosis, a dose or a "
                "substitute for the agriculture officer."},
)


def _all_reading_paths():
    paths = {}
    for sensor, pairs in SENSOR_READINGS.items():
        for section, key in pairs:
            paths["%s.%s" % (section, key)] = (sensor, section, key)
    return paths


READING_PATHS = _all_reading_paths()


def _is_finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _non_empty_text(value):
    return isinstance(value, str) and value.strip() != ""


def validate_ruleset(ruleset):
    """Return a list of `{"path", "code"}` problems (empty if the ruleset is well formed)."""
    problems = []

    def bad(path, code):
        problems.append({"path": path, "code": code})

    if not isinstance(ruleset, dict) or set(ruleset) != {"version", "rules"}:
        return [{"path": "", "code": "wrong_shape"}]
    if not _non_empty_text(ruleset["version"]):
        bad("version", "must_be_text")
    rules = ruleset["rules"]
    if not isinstance(rules, list):
        bad("rules", "must_be_a_list")
        return problems
    seen = set()
    for index, rule in enumerate(rules):
        here = "rules[%d]" % index
        required = {"id", "when", "message", "priority", "source"}
        if not isinstance(rule, dict) or not required <= set(rule) <= required | set(OPTIONAL_RULE_KEYS):
            bad(here, "wrong_shape")
            continue
        for key in OPTIONAL_RULE_KEYS:
            if key in rule:
                names = rule[key]
                if not isinstance(names, list) or not names or not all(_non_empty_text(n) for n in names):
                    bad("%s.%s" % (here, key), "must_be_a_non_empty_list_of_names")
        if not _non_empty_text(rule["id"]):
            bad(here + ".id", "must_be_text")
        elif rule["id"] in seen:
            bad(here + ".id", "duplicate")
        else:
            seen.add(rule["id"])
        if not _non_empty_text(rule["message"]):
            bad(here + ".message", "must_be_text")
        if not _non_empty_text(rule["source"]):
            bad(here + ".source", "must_name_a_source")
        if isinstance(rule["priority"], bool) or not isinstance(rule["priority"], int):
            bad(here + ".priority", "must_be_an_integer")
        when = rule["when"]
        if not isinstance(when, list) or not when:
            bad(here + ".when", "must_be_a_non_empty_list")
            continue
        for number, condition in enumerate(when):
            there = "%s.when[%d]" % (here, number)
            if not isinstance(condition, dict) or set(condition) != {"reading", "op", "value"}:
                bad(there, "wrong_shape")
                continue
            if not isinstance(condition["reading"], str) or condition["reading"] not in READING_PATHS:
                bad(there + ".reading", "unknown_reading")
            if not isinstance(condition["op"], str) or condition["op"] not in OPERATORS:
                bad(there + ".op", "unknown_operator")
            if not _is_finite_number(condition["value"]):
                bad(there + ".value", "must_be_a_finite_number")
    return problems


def _calibration_is_usable(calibration, nutrient):
    if not isinstance(calibration, dict):
        return False
    coefficients = calibration.get("coefficients")
    entry = coefficients.get(nutrient) if isinstance(coefficients, dict) else None
    return (_non_empty_text(calibration.get("version")) and isinstance(entry, dict)
            and set(entry) == {"a", "b", "c"} and all(_is_finite_number(v) for v in entry.values()))


def _reading(record, path):
    """Return (value, sensor, status) for a reading path of a validated Record."""
    sensor, section, key = READING_PATHS[path]
    return record[section][key], sensor, record["sensor_status"][sensor]


def _assess_condition(record, condition, calibration):
    """Return (observed_value, calibrated, not_assessed_code). Exactly one of the last two forms."""
    path = condition["reading"]
    value, sensor, status = _reading(record, path)
    if status != "ok":
        return None, False, NOT_ASSESSED_SENSOR_FAULT
    if value is None:
        return None, False, NOT_ASSESSED_READING_MISSING
    nutrient = path.split(".", 1)[1] if path.startswith("soil.") else None
    if nutrient in NPK_NUTRIENTS:
        if not _calibration_is_usable(calibration, nutrient):
            return None, False, NOT_ASSESSED_NO_CALIBRATION
        moisture, _, moisture_status = _reading(record, MOISTURE_PATH)
        if moisture_status != "ok" or moisture is None:
            return None, False, NOT_ASSESSED_MOISTURE_UNUSABLE
        entry = calibration["coefficients"][nutrient]
        corrected = correct_npk(value, moisture, entry["a"], entry["b"], entry["c"])
        if not _is_finite_number(corrected):
            return None, False, NOT_ASSESSED_NO_CALIBRATION
        return corrected, True, None
    return value, False, None


def _context_name(value):
    return value.strip().lower() if isinstance(value, str) and value.strip() else None


def _context_check(rule, crop, growth_stage):
    """Return ("apply", None), ("unknown", code) or ("different", code) for a rule's optional
    crop and growth-stage limits. A rule without those keys applies to any context."""
    for key, given, unknown_code, different_code in (
            ("crop", crop, NOT_ASSESSED_CROP_UNKNOWN, NOT_APPLICABLE_CROP),
            ("growth_stage", growth_stage, NOT_ASSESSED_STAGE_UNKNOWN, NOT_APPLICABLE_STAGE)):
        if key not in rule:
            continue
        name = _context_name(given)
        if name is None:
            return "unknown", unknown_code
        if name not in {_context_name(n) for n in rule[key]}:
            return "different", different_code
    return "apply", None


def evaluate(record, ruleset, calibration=None, crop=None, growth_stage=None):
    """Apply `ruleset` to `record` and return a JSON-serialisable dict.

    `calibration` is None or `{"version": str, "coefficients": {nutrient: {"a", "b", "c"}}}`.
    `crop` and `growth_stage` are None or the name the USER stated; a rule that names crops or stages
    is assessed only when the matching name is given, and is reported as not applicable when it is a
    different one. An image-based guess must not be passed here.
    Keys of the result: outcome, reason, rules_version, calibration_version, items, not_assessed,
    not_applicable, errors, missing_information (fixed-wording statements of what the user must
    provide, never a statement about the crop or soil), caveats.
    """
    result = {
        "outcome": None, "reason": None,
        "rules_version": None, "calibration_version": None,
        "items": [], "not_assessed": [], "not_applicable": [], "errors": [], "missing_information": [],
        "caveats": [dict(c) for c in CAVEATS],
    }
    checked = validate_record(record)
    if not checked.valid:
        result["outcome"] = OUTCOME_INVALID_RECORD
        result["errors"] = [{"path": e.path, "code": e.code} for e in checked.errors]
        return result
    problems = validate_ruleset(ruleset)
    if problems:
        result["outcome"] = OUTCOME_INVALID_RULESET
        result["errors"] = problems
        return result
    result["rules_version"] = ruleset["version"]
    if isinstance(calibration, dict) and _non_empty_text(calibration.get("version")):
        result["calibration_version"] = calibration["version"]
    if not ruleset["rules"]:
        result["outcome"] = OUTCOME_INSUFFICIENT
        result["reason"] = REASON_NO_RULES
        result["missing_information"] = _needs(NEEDS_WHEN_NO_RULES)
        return result

    record = checked.record  # the validated copy; the caller's record is never touched
    assessed_rules = 0
    for rule in ruleset["rules"]:
        verdict, code = _context_check(rule, crop, growth_stage)
        if verdict == "different":
            result["not_applicable"].append({"rule_id": rule["id"], "code": code})
            continue
        if verdict == "unknown":
            result["not_assessed"].append({"rule_id": rule["id"], "reading": None, "code": code})
            continue
        evidence, missing, holds = [], [], True
        for condition in rule["when"]:
            observed, calibrated, code = _assess_condition(record, condition, calibration)
            if code is not None:
                missing.append({"rule_id": rule["id"], "reading": condition["reading"], "code": code})
                continue
            met = OPERATORS[condition["op"]](observed, condition["value"])
            holds = holds and met
            _, sensor, status = _reading(record, condition["reading"])
            evidence.append({
                "reading": condition["reading"], "observed": observed, "calibrated": calibrated,
                "op": condition["op"], "threshold": condition["value"],
                "sensor": sensor, "sensor_status": status, "met": met,
            })
        if missing:
            result["not_assessed"].extend(missing)
            continue
        assessed_rules += 1
        if holds:
            result["items"].append({
                "rule_id": rule["id"], "message": rule["message"],
                "priority": rule["priority"], "source": rule["source"], "evidence": evidence,
            })
    if result["items"]:
        result["outcome"] = OUTCOME_ADVICE
    elif assessed_rules:
        result["outcome"] = OUTCOME_NO_RULE_FIRED
    elif not result["not_assessed"]:
        # Every rule named a different crop or stage than the one given.
        result["outcome"] = OUTCOME_INSUFFICIENT
        result["reason"] = REASON_NOTHING_APPLICABLE
        result["missing_information"] = _needs(("rules_for_this_context",))
    else:
        result["outcome"] = OUTCOME_INSUFFICIENT
        result["reason"] = REASON_NOTHING_ASSESSABLE
        codes = []
        for entry in result["not_assessed"]:
            if entry["code"] not in codes:
                codes.append(entry["code"])
        result["missing_information"] = _needs(codes)
    return result


def _needs(codes):
    return [{"code": code, "needs": NEEDS[code]} for code in codes]
