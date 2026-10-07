"""Nelavi descriptive baseline (V1).

Descriptive statistics for one signal of one rover over a caller-supplied,
ordered window of stored Records. It implements the owner-approved decisions
BL-1 to BL-8 in ARCHITECTURE_DECISIONS.md and resolves no frozen contract item.

The result describes the supplied observations only. It makes no claim of
normal, healthy or unhealthy behaviour, anomaly, failure, prediction or
confidence. Pure and deterministic: no wall clock, randomness, network, file
or database access, and no hardware. The window is processed in the supplied
order and is never sorted: `time` is never parsed and `sequence_number` is
never used. Inputs are never modified.
"""

import math
import statistics
from dataclasses import dataclass
from types import MappingProxyType

from record.validation import SENSOR_READINGS, validate_record

# signal path -> (record section, key, sensor_status key that covers it)
SIGNALS = {
    "%s.%s" % (section, key): (section, key, sensor)
    for sensor, paths in SENSOR_READINGS.items()
    for section, key in paths
}

STATUS_SUFFICIENT = "sufficient"
STATUS_INSUFFICIENT = "insufficient"

NULL_READING = "null_reading"
SENSOR_FAULT = "sensor_fault"
INVALID_RECORD = "invalid_record"
NON_FINITE_OR_NON_NUMERIC = "non_finite_or_non_numeric"
WRONG_ROVER = "wrong_rover"
EXCLUSION_REASONS = (
    NULL_READING,
    SENSOR_FAULT,
    INVALID_RECORD,
    NON_FINITE_OR_NON_NUMERIC,
    WRONG_ROVER,
)

MIN_COUNT_FLOOR = 2  # sample standard deviation needs at least two observations

CONTEXT_STATE_UNAVAILABLE = (
    "Machine state is not part of the Record, so no state context is available; "
    "the baseline is grouped by rover_id and signal only."
)

CAVEAT_SPATIAL_SAMPLING = "possible_spatial_sampling_variation"
CAVEAT_BATTERY_TREND = "battery_voltage_trends_with_usage"


@dataclass(frozen=True)
class Caveat:
    code: str
    message: str


_SOIL_CAVEAT = Caveat(
    CAVEAT_SPATIAL_SAMPLING,
    "The historical mock values may reflect spatial sampling variation: the "
    "Mock Rover synthetic soil field varies with position, and position is not "
    "present in the Record. Descriptive context only; not an anomaly or health judgment.",
)
_BATTERY_CAVEAT = Caveat(
    CAVEAT_BATTERY_TREND,
    "The mock rover battery voltage trends with battery usage, so it is not a "
    "stationary machine-health baseline. Descriptive context only; not an "
    "anomaly or health judgment.",
)
_CAVEATS = {signal: (_SOIL_CAVEAT,) for signal in SIGNALS if signal.startswith("soil.")}
_CAVEATS["battery.voltage_v"] = (_BATTERY_CAVEAT,)


@dataclass(frozen=True)
class BaselineResult:
    rover_id: str
    signal: str
    unit: object  # None: the units of the readings are unresolved
    status: str  # "sufficient" or "insufficient"
    min_count_required: int
    count: int  # included observations
    excluded: object  # read-only mapping: exclusion reason -> count
    mean: object
    median: object
    minimum: object
    maximum: object
    std: object  # sample standard deviation (n - 1)
    first_id: object  # storage id of the first record examined, or None
    last_id: object  # storage id of the last record examined, or None
    records_examined: int
    context: object  # read-only mapping; machine state is unavailable
    caveats: tuple  # tuple of Caveat


def _check_identity(rover_id, signal):
    if not isinstance(rover_id, str) or rover_id == "":
        raise ValueError("rover_id must be a non-empty string")
    if not isinstance(signal, str) or signal not in SIGNALS:
        raise ValueError("unsupported signal: %r" % (signal,))


def _check_arguments(rover_id, signal, min_count, records):
    _check_identity(rover_id, signal)
    if isinstance(min_count, bool) or not isinstance(min_count, int):
        raise ValueError("min_count must be an integer")
    if min_count < MIN_COUNT_FLOOR:
        raise ValueError("min_count must be at least %d" % MIN_COUNT_FLOOR)
    if isinstance(records, (str, bytes, dict)):
        raise TypeError("records must be an ordered sequence of records")
    try:
        return tuple(records)
    except TypeError:
        raise TypeError("records must be an ordered sequence of records") from None


def _unpack(item):
    """Return (record, storage id or None). A StoredRecord-like item has `id` and `record`."""
    if isinstance(item, dict):
        return item, None
    if hasattr(item, "record"):
        item_id = getattr(item, "id", None)
        if isinstance(item_id, bool) or not isinstance(item_id, int):
            item_id = None
        return item.record, item_id
    return None, None


def _sensor_flag(record, sensor):
    status = record.get("sensor_status")
    return status.get(sensor) if isinstance(status, dict) else None


def _classify_invalid(record, signal, sensor, errors):
    """Reason for an observation whose Record failed Record Core validation."""
    if errors and all(error.path == signal for error in errors):
        if _sensor_flag(record, sensor) == "fault":
            return SENSOR_FAULT
        if all(error.code == "wrong_type" for error in errors):
            return NON_FINITE_OR_NON_NUMERIC
        if all(error.code == "inconsistent_status" for error in errors):
            return NULL_READING
    return INVALID_RECORD


def _observe(record, rover_id, signal):
    """Return (value, None) for an included observation, else (None, reason)."""
    section, key, sensor = SIGNALS[signal]
    if not isinstance(record, dict):
        return None, INVALID_RECORD
    if "rover_id" in record and record["rover_id"] != rover_id:
        return None, WRONG_ROVER
    result = validate_record(record)
    if not result.valid:
        return None, _classify_invalid(record, signal, sensor, result.errors)
    if record["sensor_status"][sensor] != "ok":
        return None, SENSOR_FAULT  # a fault always excludes, even with a numeric value
    value = record[section][key]
    if value is None:
        return None, NULL_READING
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None, NON_FINITE_OR_NON_NUMERIC
    return float(value), None


@dataclass(frozen=True)
class ObservationOutcome:
    value: object  # the included finite reading as a float, else None
    reason: object  # None when included, else one of EXCLUSION_REASONS
    record_id: object  # storage id if the item carried one, else None


def classify_observation(item, rover_id, signal):
    """Classify one Record's reading of `signal` for `rover_id`.

    This is the per-observation rule that `compute_baseline` applies to every
    window item (BL-6), exposed unchanged: the rover check precedes validation,
    a sensor fault always excludes, and a validation failure is attributed to
    the signal only when every error is on the signal's own path. `item` is a
    Record (dict) or a StoredRecord-like item with `id` and `record`; anything
    else is an invalid_record. The item is never modified.
    """
    _check_identity(rover_id, signal)
    record, record_id = _unpack(item)
    value, reason = _observe(record, rover_id, signal)
    return ObservationOutcome(value=value, reason=reason, record_id=record_id)


def compute_baseline(rover_id, signal, records, min_count):
    """Descriptive baseline of `signal` for `rover_id` over `records`.

    records: an ordered sequence of Records (dicts) or StoredRecord-like items
    with `id` and `record` attributes. It is examined in the supplied order.
    min_count: required, at least 2, no default.
    """
    window = _check_arguments(rover_id, signal, min_count, records)

    values = []
    excluded = {reason: 0 for reason in EXCLUSION_REASONS}
    for item in window:
        record, _ = _unpack(item)
        value, reason = _observe(record, rover_id, signal)
        if reason is None:
            values.append(value)
        else:
            excluded[reason] += 1

    sufficient = len(values) >= min_count
    stats = (None, None, None, None, None)
    if sufficient:
        stats = (
            statistics.mean(values),
            statistics.median(values),
            min(values),
            max(values),
            statistics.stdev(values),
        )
    mean, median, minimum, maximum, std = stats

    return BaselineResult(
        rover_id=rover_id,
        signal=signal,
        unit=None,
        status=STATUS_SUFFICIENT if sufficient else STATUS_INSUFFICIENT,
        min_count_required=min_count,
        count=len(values),
        excluded=MappingProxyType(excluded),
        mean=mean,
        median=median,
        minimum=minimum,
        maximum=maximum,
        std=std,
        first_id=_unpack(window[0])[1] if window else None,
        last_id=_unpack(window[-1])[1] if window else None,
        records_examined=len(window),
        context=MappingProxyType(
            {"machine_state": "unavailable", "reason": CONTEXT_STATE_UNAVAILABLE}
        ),
        caveats=_CAVEATS.get(signal, ()),
    )
