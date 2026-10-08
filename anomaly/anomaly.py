"""Nelavi statistical deviation evaluation (Anomaly V1).

Deterministic, descriptive evaluation of ONE observation against an ALREADY
SUPPLIED Baseline V1 reference. It implements the owner-approved decisions
AD-1 to AD-8 in ARCHITECTURE_DECISIONS.md and resolves no frozen contract item.

It reports how far an observed reading is from a reference mean, in units of the
reference's sample standard deviation. It is NOT machine-anomaly detection: the
Record carries no machine telemetry, and nothing here claims machine health,
failure, cause, severity, prediction, probability or confidence. Observation
quality is decided only by the public `classify_observation` of Baseline V1, so
a sensor fault, a null, a non-numeric or non-finite reading, an invalid Record
or a wrong rover can never become `deviates_from_reference`.

Pure and deterministic: no wall clock, randomness, network, file or database
access, no Storage access, and no machine-state grouping. There is no
windowing, persistence, trend, EWMA or sampling-interval logic. Inputs are
never modified.
"""

import math
from dataclasses import dataclass
from types import MappingProxyType

from baseline.baseline import (
    CONTEXT_STATE_UNAVAILABLE,
    STATUS_SUFFICIENT,
    BaselineResult,
    classify_observation,
)

SUPPORTED_SIGNALS = ("air.temperature", "air.humidity", "air.pressure")

STATUS_WITHIN = "within_reference"
STATUS_DEVIATES = "deviates_from_reference"
STATUS_NOT_EVALUATED = "not_evaluated"

# Reasons for not_evaluated. The five observation reasons are those of
# classify_observation (BL-6) and are passed through unchanged.
REASON_INSUFFICIENT_REFERENCE = "insufficient_reference"
REASON_NO_SPREAD = "reference_has_no_spread"  # std is zero or at or below min_spread

COMPARISON = "abs(z_score) > threshold"


@dataclass(frozen=True)
class ReferenceSummary:
    mean: object
    std: object
    count: int
    status: str
    first_id: object
    last_id: object


@dataclass(frozen=True)
class DeviationResult:
    rover_id: str
    signal: str
    record_id: object  # storage id if the observation carried one, else None
    status: str  # within_reference / deviates_from_reference / not_evaluated
    reason: object  # None when evaluated, else the deterministic reason
    observed_value: object
    deviation: object  # observed_value - reference.mean
    z_score: object  # deviation / reference.std, or None when not defined
    reference: ReferenceSummary
    threshold: float
    comparison: object  # the rule applied, or None when not evaluated
    unit: object  # None: the units of the readings are unresolved
    context: object  # read-only mapping; machine state is unavailable
    caveats: tuple  # the reference's caveats, unchanged


def _finite_float(name, value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("%s must be a number" % name)
    try:
        number = float(value)
    except OverflowError:  # an integer too large for a float is not a finite number here
        raise ValueError("%s must be finite" % name) from None
    if not math.isfinite(number):
        raise ValueError("%s must be finite" % name)
    return number


def _checked_number(name, value, allow_zero):
    number = _finite_float(name, value)
    if number < 0 or (number == 0 and not allow_zero):
        raise ValueError(
            "%s must be %s" % (name, "not negative" if allow_zero else "greater than zero")
        )
    return number


def _check_parameters(rover_id, signal, reference, threshold, min_spread):
    if not isinstance(rover_id, str) or rover_id == "":
        raise ValueError("rover_id must be a non-empty string")
    if not isinstance(signal, str) or signal not in SUPPORTED_SIGNALS:
        raise ValueError(
            "signal %r is not supported by anomaly V1; supported: %s"
            % (signal, ", ".join(SUPPORTED_SIGNALS))
        )
    if not isinstance(reference, BaselineResult):
        raise TypeError("reference must be a BaselineResult")
    if reference.rover_id != rover_id:
        raise ValueError("reference belongs to a different rover")
    if reference.signal != signal:
        raise ValueError("reference belongs to a different signal")
    threshold = _checked_number("threshold", threshold, allow_zero=False)
    min_spread = _checked_number("min_spread", min_spread, allow_zero=True)
    if reference.status == STATUS_SUFFICIENT:
        for name in ("mean", "std"):
            try:
                _finite_float(name, getattr(reference, name))
            except ValueError:
                raise ValueError("a sufficient reference needs a finite %s" % name) from None
        if reference.std < 0:
            raise ValueError("reference std must not be negative")
    return threshold, min_spread


def _inside_reference_span(record_id, reference):
    if isinstance(record_id, bool) or not isinstance(record_id, int):
        return False
    ids = (reference.first_id, reference.last_id)
    if any(isinstance(i, bool) or not isinstance(i, int) for i in ids):
        return False
    return min(ids) <= record_id <= max(ids)


def _summary(reference):
    return ReferenceSummary(
        mean=reference.mean,
        std=reference.std,
        count=reference.count,
        status=reference.status,
        first_id=reference.first_id,
        last_id=reference.last_id,
    )


def _evaluate(rover_id, signal, observation, reference, threshold, min_spread):
    outcome = classify_observation(observation, rover_id, signal)
    if outcome.record_id is not None and _inside_reference_span(outcome.record_id, reference):
        raise ValueError(
            "observation storage id %r lies inside the reference id span; the "
            "observation must not be part of its own reference" % (outcome.record_id,)
        )

    status, reason = STATUS_NOT_EVALUATED, None
    observed = deviation = z_score = comparison = None
    if outcome.reason is not None:
        reason = outcome.reason
    else:
        observed = outcome.value
        if reference.status != STATUS_SUFFICIENT:
            reason = REASON_INSUFFICIENT_REFERENCE
        else:
            deviation = observed - reference.mean
            if reference.std <= min_spread:
                reason = REASON_NO_SPREAD  # no spread to measure against; no z-score
            else:
                z_score = deviation / reference.std
                comparison = COMPARISON
                status = STATUS_DEVIATES if abs(z_score) > threshold else STATUS_WITHIN

    return DeviationResult(
        rover_id=rover_id,
        signal=signal,
        record_id=outcome.record_id,
        status=status,
        reason=reason,
        observed_value=observed,
        deviation=deviation,
        z_score=z_score,
        reference=_summary(reference),
        threshold=threshold,
        comparison=comparison,
        unit=None,
        context=MappingProxyType(
            {"machine_state": "unavailable", "reason": CONTEXT_STATE_UNAVAILABLE}
        ),
        caveats=reference.caveats,
    )


def evaluate_observation(rover_id, signal, observation, reference, threshold, min_spread):
    """Evaluate one observation of `signal` against a supplied BaselineResult.

    observation: a Record (dict) or a StoredRecord-like item (`id`, `record`).
    reference: a BaselineResult of the same rover and signal, built by the
        caller from a window that does not contain the observation.
    threshold: required, finite, greater than zero; no default. The observation
        deviates when abs(z_score) > threshold, so equality is within_reference.
    min_spread: required, finite, not negative; no default. A reference whose
        std is at or below it (including zero) is not evaluated.

    Raises ValueError/TypeError for unusable parameters, an unsupported
    signal, a reference of another rover or signal, or an observation whose
    storage id lies inside the reference's first_id..last_id span.
    """
    threshold, min_spread = _check_parameters(rover_id, signal, reference, threshold, min_spread)
    return _evaluate(rover_id, signal, observation, reference, threshold, min_spread)


def evaluate_observations(rover_id, signal, observations, reference, threshold, min_spread):
    """Evaluate each observation independently, in the supplied order.

    No state is carried from one observation to the next: there is no
    persistence, trend or windowing. Returns a tuple of DeviationResult.
    """
    threshold, min_spread = _check_parameters(rover_id, signal, reference, threshold, min_spread)
    if isinstance(observations, (str, bytes, dict)):
        raise TypeError("observations must be an ordered sequence of records")
    try:
        items = tuple(observations)
    except TypeError:
        raise TypeError("observations must be an ordered sequence of records") from None
    return tuple(
        _evaluate(rover_id, signal, item, reference, threshold, min_spread) for item in items
    )
