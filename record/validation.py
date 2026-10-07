"""Nelavi record validation core.

Validates a candidate record (already-parsed JSON) against the canonical shape
set by the owner-approved implementation decisions MD-0 to MD-6 in
ARCHITECTURE_DECISIONS.md. The frozen contracts are not changed and their open
items remain open; see docs/contracts/RECORD_CONTRACT.md for the source items.

Pure and deterministic: no I/O, no network, no clock, no calibration. The
candidate is never modified.
"""

import copy
from dataclasses import dataclass

TOP_LEVEL_KEYS = (
    "time",
    "sequence_number",
    "rover_id",
    "battery",
    "soil",
    "air",
    "sensor_status",
    "leaf_label",
    "confidence",
    "photo_id",
    "versions",
)
SOIL_KEYS = ("nitrogen", "phosphorus", "potassium", "moisture", "temperature")
AIR_KEYS = ("temperature", "humidity", "pressure")
BATTERY_KEYS = ("voltage_v",)
STATUS_KEYS = ("npk", "moisture", "soil_temperature", "air", "battery")
VERSION_KEYS = ("firmware", "model", "rules", "calibration")
LEAF_KEYS = ("leaf_label", "confidence", "photo_id")

STATUS_VALUES = ("ok", "fault")
CONFIDENCE_MIN = 0
CONFIDENCE_MAX = 100

# Which readings each sensor status flag covers (MD-4). A sensor flagged "ok"
# must have finite numeric readings (null is invalid). A sensor flagged "fault"
# may have null or finite numeric readings (both valid) and is never by itself
# a reason to reject the record.
SENSOR_READINGS = {
    "npk": (("soil", "nitrogen"), ("soil", "phosphorus"), ("soil", "potassium")),
    "moisture": (("soil", "moisture"),),
    "soil_temperature": (("soil", "temperature"),),
    "air": (("air", "temperature"), ("air", "humidity"), ("air", "pressure")),
    "battery": (("battery", "voltage_v"),),
}


@dataclass(frozen=True)
class ValidationError:
    path: str
    code: str
    message: str


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    errors: tuple
    record: object  # a copy of the candidate when valid, otherwise None


def _join(parent, key):
    return key if not parent else parent + "." + key


def _is_number(value):
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return value == value and value not in (float("inf"), float("-inf"))
    return False


def _check_keys(obj, expected, parent, errors):
    for key in expected:
        if key not in obj:
            errors.append(
                ValidationError(_join(parent, key), "missing", "required field is missing")
            )
    for key in obj:
        if key not in expected:
            errors.append(
                ValidationError(
                    _join(parent, str(key)), "unknown_field", "field is not part of the record"
                )
            )


def _check_text(obj, key, parent, errors, nullable=False):
    if key not in obj:
        return
    value = obj[key]
    if value is None and nullable:
        return
    if not isinstance(value, str) or value == "":
        suffix = " or null" if nullable else ""
        errors.append(
            ValidationError(
                _join(parent, key), "wrong_type", "must be a non-empty string" + suffix
            )
        )


def _check_number(obj, key, parent, errors, nullable=False):
    if key not in obj:
        return
    value = obj[key]
    if value is None and nullable:
        return
    if not _is_number(value):
        suffix = " or null" if nullable else ""
        errors.append(
            ValidationError(
                _join(parent, key), "wrong_type", "must be a finite number" + suffix
            )
        )


def _section(obj, key, expected, errors):
    if key not in obj:
        return None
    value = obj[key]
    if not isinstance(value, dict):
        errors.append(ValidationError(key, "wrong_type", "must be an object"))
        return None
    _check_keys(value, expected, key, errors)
    return value


def validate_record(candidate):
    """Validate a candidate record and return a ValidationResult."""
    if not isinstance(candidate, dict):
        return ValidationResult(
            False,
            (ValidationError("", "wrong_type", "record must be an object"),),
            None,
        )

    errors = []
    _check_keys(candidate, TOP_LEVEL_KEYS, "", errors)

    _check_text(candidate, "time", "", errors)
    _check_text(candidate, "rover_id", "", errors)
    if "sequence_number" in candidate:
        value = candidate["sequence_number"]
        if isinstance(value, bool) or not isinstance(value, int):
            errors.append(
                ValidationError("sequence_number", "wrong_type", "must be an integer")
            )

    sections = {
        "soil": _section(candidate, "soil", SOIL_KEYS, errors),
        "air": _section(candidate, "air", AIR_KEYS, errors),
        "battery": _section(candidate, "battery", BATTERY_KEYS, errors),
    }
    for name, keys in (("soil", SOIL_KEYS), ("air", AIR_KEYS), ("battery", BATTERY_KEYS)):
        if sections[name] is not None:
            for key in keys:
                _check_number(sections[name], key, name, errors, nullable=True)

    status = _section(candidate, "sensor_status", STATUS_KEYS, errors)
    if status is not None:
        for key in STATUS_KEYS:
            if key in status and not (
                isinstance(status[key], str) and status[key] in STATUS_VALUES
            ):
                errors.append(
                    ValidationError(
                        "sensor_status." + key, "wrong_value", 'must be "ok" or "fault"'
                    )
                )
        for sensor, paths in SENSOR_READINGS.items():
            flag = status.get(sensor)
            if not (isinstance(flag, str) and flag in STATUS_VALUES):
                continue
            for section_name, key in paths:
                section = sections[section_name]
                if section is None or key not in section:
                    continue
                value = section[key]
                path = _join(section_name, key)
                if flag == "ok" and value is None:
                    errors.append(
                        ValidationError(
                            path, "inconsistent_status", 'sensor is "ok" but the reading is null'
                        )
                    )

    versions = _section(candidate, "versions", VERSION_KEYS, errors)
    if versions is not None:
        for key in VERSION_KEYS:
            _check_text(versions, key, "versions", errors)

    _check_text(candidate, "leaf_label", "", errors, nullable=True)
    _check_number(candidate, "confidence", "", errors, nullable=True)
    _check_text(candidate, "photo_id", "", errors, nullable=True)
    confidence = candidate.get("confidence")
    if _is_number(confidence) and not (CONFIDENCE_MIN <= confidence <= CONFIDENCE_MAX):
        errors.append(
            ValidationError(
                "confidence",
                "out_of_range",
                "must be between %d and %d inclusive" % (CONFIDENCE_MIN, CONFIDENCE_MAX),
            )
        )
    leaf_present = [key for key in LEAF_KEYS if key in candidate]
    leaf_null = [key for key in leaf_present if candidate[key] is None]
    if 0 < len(leaf_null) < len(leaf_present):
        errors.append(
            ValidationError(
                "leaf",
                "inconsistent_leaf",
                "leaf_label, confidence and photo_id must be all values or all null",
            )
        )

    if errors:
        return ValidationResult(False, tuple(errors), None)
    return ValidationResult(True, (), copy.deepcopy(candidate))
