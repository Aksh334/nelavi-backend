import ast
import copy
import dataclasses
import importlib
import inspect
import math
import unittest
from dataclasses import replace
from unittest import mock

from anomaly.anomaly import (
    COMPARISON,
    REASON_INSUFFICIENT_REFERENCE,
    REASON_NO_SPREAD,
    STATUS_DEVIATES,
    STATUS_NOT_EVALUATED,
    STATUS_WITHIN,
    SUPPORTED_SIGNALS,
    DeviationResult,
    ReferenceSummary,
    evaluate_observation,
    evaluate_observations,
)
from baseline.baseline import ObservationOutcome, compute_baseline
from mock_rover.config import RoverConfig
from mock_rover.simulator import MockRover
from storage.record_store import RecordStore, StoredRecord

ROVER = "nelavi-mock-1"
OTHER = "other-rover"
AIR = ("air.temperature", "air.humidity", "air.pressure")
UNSUPPORTED = (
    "soil.nitrogen", "soil.phosphorus", "soil.potassium", "soil.moisture", "soil.temperature",
    "battery.voltage_v", "light", "battery.pct", "vibration", "rpm", "current", "power",
    "production_quantity", "load", "air", "air.temperature ", "AIR.TEMPERATURE", "", None, 5,
)
RESULT_FIELDS = (
    "rover_id", "signal", "record_id", "status", "reason", "observed_value", "deviation",
    "z_score", "reference", "threshold", "comparison", "unit", "context", "caveats",
)
REFERENCE_FIELDS = ("mean", "std", "count", "status", "first_id", "last_id")
OBSERVATION_REASONS = (
    "sensor_fault", "null_reading", "non_finite_or_non_numeric", "invalid_record", "wrong_rover",
)


def mock_record(seed=0):
    rover = MockRover(replace(RoverConfig(), seed=seed))
    rover.startup()
    rover.insert_probe(depth_ok=True)
    rover.take_measurement()
    return rover.build_record()


BASE = mock_record()


def make(signal, value, status="ok", rover_id=ROVER, **changes):
    record = copy.deepcopy(BASE)
    section, key = signal.split(".")
    record[section][key] = value
    record["sensor_status"]["air"] = status
    record["rover_id"] = rover_id
    record.update(changes)
    return record


def stored(records, first_id=1):
    return [StoredRecord(first_id + i, r) for i, r in enumerate(records)]


def reference(values, signal="air.temperature", rover_id=ROVER, min_count=2, first_id=None):
    records = [make(signal, v, rover_id=rover_id) for v in values]
    window = stored(records, first_id) if first_id is not None else records
    return compute_baseline(rover_id, signal, window, min_count)


def evaluate(observation, ref, threshold=2.0, min_spread=0.0, signal="air.temperature", rover_id=ROVER):
    return evaluate_observation(rover_id, signal, observation, ref, threshold, min_spread)


REF = reference([10, 12, 14])  # mean 12.0, sample std 2.0


class TestEvaluation(unittest.TestCase):
    def test_reference_is_as_expected(self):
        self.assertEqual((REF.mean, REF.std, REF.count, REF.status), (12.0, 2.0, 3, "sufficient"))

    def test_within_reference(self):
        result = evaluate(make("air.temperature", 13.0), REF)
        self.assertEqual(result.status, STATUS_WITHIN)
        self.assertIsNone(result.reason)
        self.assertEqual(result.observed_value, 13.0)
        self.assertEqual(result.deviation, 1.0)
        self.assertEqual(result.z_score, 0.5)
        self.assertEqual(result.comparison, COMPARISON)

    def test_deviates_above_and_below(self):
        high = evaluate(make("air.temperature", 17.0), REF)
        low = evaluate(make("air.temperature", 7.0), REF)
        self.assertEqual((high.status, high.deviation, high.z_score), (STATUS_DEVIATES, 5.0, 2.5))
        self.assertEqual((low.status, low.deviation, low.z_score), (STATUS_DEVIATES, -5.0, -2.5))
        self.assertIsNone(high.reason)

    def test_value_equal_to_the_mean(self):
        result = evaluate(make("air.temperature", 12.0), REF)
        self.assertEqual((result.status, result.deviation, result.z_score), (STATUS_WITHIN, 0.0, 0.0))

    def test_threshold_equality_is_within_reference(self):
        at = evaluate(make("air.temperature", 15.0), REF, threshold=1.5)  # z is exactly 1.5
        self.assertEqual(at.z_score, 1.5)
        self.assertEqual(at.status, STATUS_WITHIN)
        negative = evaluate(make("air.temperature", 9.0), REF, threshold=1.5)  # z is exactly -1.5
        self.assertEqual(negative.status, STATUS_WITHIN)
        above = evaluate(make("air.temperature", 15.0), REF, threshold=1.4999)
        self.assertEqual(above.status, STATUS_DEVIATES)
        below = evaluate(make("air.temperature", 9.0), REF, threshold=1.4999)
        self.assertEqual(below.status, STATUS_DEVIATES)

    def test_integer_threshold_and_reading_are_accepted(self):
        result = evaluate(make("air.temperature", 16), REF, threshold=1, min_spread=0)
        self.assertEqual(result.status, STATUS_DEVIATES)
        self.assertEqual(result.threshold, 1.0)

    def test_gradual_drift_is_evaluated_observation_by_observation(self):
        values = (12.0, 13.0, 14.0, 15.0, 16.0, 17.0)  # z = 0, .5, 1, 1.5, 2, 2.5
        observations = [make("air.temperature", v) for v in values]
        results = evaluate_observations(ROVER, "air.temperature", observations, REF, 1.5, 0.0)
        self.assertEqual(
            [r.status for r in results],
            [STATUS_WITHIN] * 4 + [STATUS_DEVIATES] * 2,
        )
        for observation, result in zip(observations, results):
            self.assertEqual(result, evaluate(observation, REF, threshold=1.5))
        for name in ("trend", "drift", "slope", "persistence", "run_length", "ewma"):
            self.assertFalse(hasattr(results[0], name), name)

    def test_result_reports_reference_and_context(self):
        result = evaluate(make("air.temperature", 13.0), reference([10, 12, 14], first_id=5))
        self.assertEqual(
            result.reference,
            ReferenceSummary(mean=12.0, std=2.0, count=3, status="sufficient", first_id=5, last_id=7),
        )
        self.assertEqual(result.rover_id, ROVER)
        self.assertEqual(result.signal, "air.temperature")
        self.assertEqual(result.threshold, 2.0)
        self.assertIsNone(result.unit)
        self.assertEqual(result.caveats, ())
        self.assertEqual(result.context["machine_state"], "unavailable")
        self.assertIn("not part of the Record", result.context["reason"])

    def test_record_id_is_reported_when_supplied_and_never_invented(self):
        self.assertIsNone(evaluate(make("air.temperature", 13.0), REF).record_id)
        self.assertEqual(evaluate(StoredRecord(40, make("air.temperature", 13.0)), REF).record_id, 40)

    def test_multiple_supported_signals(self):
        for signal in AIR:
            ref = reference([10, 12, 14], signal=signal)
            result = evaluate(make(signal, 17.0), ref, signal=signal)
            self.assertEqual((result.signal, result.status, result.z_score), (signal, STATUS_DEVIATES, 2.5))

    def test_multiple_rovers(self):
        ref_a = reference([10, 12, 14], rover_id=ROVER)
        ref_b = reference([100, 102, 104], rover_id=OTHER)
        record_a = make("air.temperature", 13.0, rover_id=ROVER)
        record_b = make("air.temperature", 103.0, rover_id=OTHER)
        self.assertEqual(evaluate(record_a, ref_a).status, STATUS_WITHIN)
        self.assertEqual(
            evaluate(record_b, ref_b, rover_id=OTHER).status, STATUS_WITHIN
        )
        crossed = evaluate(record_a, ref_b, rover_id=OTHER)
        self.assertEqual((crossed.status, crossed.reason), (STATUS_NOT_EVALUATED, "wrong_rover"))
        # a reference of another rover is rejected outright
        with self.assertRaises(ValueError):
            evaluate(record_a, ref_b)


class TestNotEvaluated(unittest.TestCase):
    def assertNotEvaluated(self, result, reason):
        self.assertEqual(result.status, STATUS_NOT_EVALUATED)
        self.assertEqual(result.reason, reason)
        self.assertIsNone(result.z_score)
        self.assertIsNone(result.comparison)

    def test_sensor_fault_never_deviates(self):
        for value in (None, 12.0, 9999.0, -9999.0):
            result = evaluate(make("air.temperature", value, status="fault"), REF)
            self.assertNotEvaluated(result, "sensor_fault")
            self.assertIsNone(result.observed_value)
            self.assertIsNone(result.deviation)

    def test_null_reading(self):
        self.assertNotEvaluated(evaluate(make("air.temperature", None), REF), "null_reading")

    def test_fault_takes_precedence_over_a_bad_reading(self):
        for bad in ("12", float("nan"), None):
            self.assertNotEvaluated(
                evaluate(make("air.temperature", bad, status="fault"), REF), "sensor_fault"
            )

    def test_non_finite_reading(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            self.assertNotEvaluated(
                evaluate(make("air.temperature", bad), REF), "non_finite_or_non_numeric"
            )

    def test_non_numeric_reading(self):
        for bad in ("12", True, [1]):
            self.assertNotEvaluated(
                evaluate(make("air.temperature", bad), REF), "non_finite_or_non_numeric"
            )

    def test_invalid_record(self):
        broken = make("air.temperature", 99.0)
        broken["unexpected"] = 1
        self.assertNotEvaluated(evaluate(broken, REF), "invalid_record")
        for item in (None, 5, "record", {"unexpected": 1}):
            self.assertNotEvaluated(evaluate(item, REF), "invalid_record")

    def test_wrong_rover(self):
        self.assertNotEvaluated(
            evaluate(make("air.temperature", 99.0, rover_id=OTHER), REF), "wrong_rover"
        )

    def test_insufficient_reference(self):
        ref = reference([10, 12, 14], min_count=5)
        self.assertEqual(ref.status, "insufficient")
        result = evaluate(make("air.temperature", 99.0), ref)
        self.assertNotEvaluated(result, REASON_INSUFFICIENT_REFERENCE)
        self.assertEqual(result.observed_value, 99.0)
        self.assertIsNone(result.deviation)
        self.assertIsNone(result.reference.mean)
        self.assertEqual(result.reference.status, "insufficient")

    def test_empty_reference_is_insufficient(self):
        ref = compute_baseline(ROVER, "air.temperature", [], 2)
        self.assertNotEvaluated(evaluate(make("air.temperature", 1.0), ref), REASON_INSUFFICIENT_REFERENCE)

    def test_observation_quality_is_decided_before_the_reference(self):
        ref = reference([10, 12, 14], min_count=5)  # insufficient
        self.assertNotEvaluated(
            evaluate(make("air.temperature", 5.0, status="fault"), ref), "sensor_fault"
        )

    def test_reference_with_zero_spread(self):
        ref = reference([5, 5, 5])
        self.assertEqual(ref.std, 0.0)
        for value, deviation in ((5.0, 0.0), (6.0, 1.0), (1000.0, 995.0)):
            result = evaluate(make("air.temperature", value), ref)
            self.assertNotEvaluated(result, REASON_NO_SPREAD)
            self.assertEqual(result.deviation, deviation)  # reported, but no z-score
            self.assertEqual(result.observed_value, value)

    def test_reference_with_spread_at_or_below_min_spread(self):
        observation = make("air.temperature", 99.0)
        self.assertNotEvaluated(evaluate(observation, REF, min_spread=2.0), REASON_NO_SPREAD)  # equal
        self.assertNotEvaluated(evaluate(observation, REF, min_spread=3.0), REASON_NO_SPREAD)
        evaluated = evaluate(observation, REF, min_spread=1.999)
        self.assertEqual(evaluated.status, STATUS_DEVIATES)

    def test_min_spread_zero_still_rejects_zero_spread(self):
        self.assertNotEvaluated(
            evaluate(make("air.temperature", 6.0), reference([5, 5]), min_spread=0), REASON_NO_SPREAD
        )

    def test_no_epsilon_is_hidden_in_the_comparison(self):
        tiny = reference([1.0, 1.0 + 1e-9])  # a very small but nonzero spread
        self.assertGreater(tiny.std, 0)
        result = evaluate(make("air.temperature", 2.0), tiny, min_spread=0.0)
        self.assertEqual(result.status, STATUS_DEVIATES)

    def test_a_not_evaluated_result_is_never_a_deviation(self):
        cases = [
            make("air.temperature", 1e9, status="fault"), make("air.temperature", None),
            make("air.temperature", float("nan")), make("air.temperature", "x"),
            {"unexpected": 1}, make("air.temperature", 1e9, rover_id=OTHER),
        ]
        for case in cases:
            self.assertNotEqual(evaluate(case, REF).status, STATUS_DEVIATES)

    def test_mixed_valid_and_faulted_history(self):
        history = [make("air.temperature", v) for v in (10, 12, 14)] + [
            make("air.temperature", 9999.0, status="fault"),
            make("air.temperature", None, status="fault"),
        ]
        ref = compute_baseline(ROVER, "air.temperature", history, 2)
        self.assertEqual((ref.count, ref.mean, ref.std), (3, 12.0, 2.0))
        self.assertEqual(ref.excluded["sensor_fault"], 2)
        self.assertEqual(evaluate(make("air.temperature", 17.0), ref).status, STATUS_DEVIATES)
        self.assertNotEvaluated(
            evaluate(make("air.temperature", 17.0, status="fault"), ref), "sensor_fault"
        )

    def test_shared_air_fault_applies_to_all_air_signals(self):
        for signal in AIR:
            ref = reference([10, 12, 14], signal=signal)
            record = make(signal, 50.0, status="fault")
            self.assertNotEvaluated(evaluate(record, ref, signal=signal), "sensor_fault")


class TestParametersAndScope(unittest.TestCase):
    OBSERVATION = make("air.temperature", 13.0)

    def test_supported_signals_are_exactly_the_three_air_signals(self):
        self.assertEqual(SUPPORTED_SIGNALS, AIR)

    def test_unsupported_signals_are_rejected(self):
        for signal in UNSUPPORTED:
            with self.assertRaises(ValueError, msg=repr(signal)):
                evaluate_observation(ROVER, signal, self.OBSERVATION, REF, 2.0, 0.0)
            with self.assertRaises(ValueError, msg=repr(signal)):
                evaluate_observations(ROVER, signal, [self.OBSERVATION], REF, 2.0, 0.0)

    def test_soil_and_battery_are_rejected_even_with_a_matching_reference(self):
        for signal in ("soil.nitrogen", "soil.moisture", "battery.voltage_v"):
            ref = compute_baseline(ROVER, signal, [], 2)
            with self.assertRaises(ValueError, msg=signal):
                evaluate_observation(ROVER, signal, self.OBSERVATION, ref, 2.0, 0.0)

    def test_invalid_threshold(self):
        for bad in (0, 0.0, -1, -0.5, float("nan"), float("inf"), float("-inf"), True, False, "2", None, [2]):
            with self.assertRaises(ValueError, msg=repr(bad)):
                evaluate(self.OBSERVATION, REF, threshold=bad)

    def test_invalid_min_spread(self):
        for bad in (-1, -0.001, float("nan"), float("inf"), float("-inf"), True, False, "0", None, [0]):
            with self.assertRaises(ValueError, msg=repr(bad)):
                evaluate(self.OBSERVATION, REF, min_spread=bad)

    def test_huge_integers_are_rejected_with_value_error_not_overflow_error(self):
        huge = 10**400
        with self.assertRaises(ValueError):
            evaluate(self.OBSERVATION, REF, threshold=huge)
        with self.assertRaises(ValueError):
            evaluate(self.OBSERVATION, REF, threshold=-huge)
        with self.assertRaises(ValueError):
            evaluate(self.OBSERVATION, REF, min_spread=huge)
        with self.assertRaises(ValueError):
            evaluate(self.OBSERVATION, REF, min_spread=-huge)
        with self.assertRaises(ValueError):
            evaluate(self.OBSERVATION, replace(REF, mean=huge))
        with self.assertRaises(ValueError):
            evaluate(self.OBSERVATION, replace(REF, std=huge))
        with self.assertRaises(ValueError):
            evaluate_observations(ROVER, "air.temperature", [self.OBSERVATION], REF, huge, 0.0)
        # just above the largest float is also rejected, while a large float-sized integer is not
        with self.assertRaises(ValueError):
            evaluate(self.OBSERVATION, REF, threshold=10**309)
        self.assertEqual(evaluate(self.OBSERVATION, REF, threshold=10**300).status, STATUS_WITHIN)

    def test_threshold_and_min_spread_have_no_defaults(self):
        parameters = inspect.signature(evaluate_observation).parameters
        self.assertIs(parameters["threshold"].default, inspect.Parameter.empty)
        self.assertIs(parameters["min_spread"].default, inspect.Parameter.empty)
        with self.assertRaises(TypeError):
            evaluate_observation(ROVER, "air.temperature", self.OBSERVATION, REF, 2.0)
        with self.assertRaises(TypeError):
            evaluate_observation(ROVER, "air.temperature", self.OBSERVATION, REF)

    def test_bad_rover_id(self):
        for bad in ("", None, 5):
            with self.assertRaises(ValueError):
                evaluate_observation(bad, "air.temperature", self.OBSERVATION, REF, 2.0, 0.0)

    def test_reference_must_be_a_matching_baseline_result(self):
        for bad in (None, {"mean": 12.0}, "ref", 5):
            with self.assertRaises(TypeError):
                evaluate(self.OBSERVATION, bad)
        with self.assertRaises(ValueError):
            evaluate(self.OBSERVATION, reference([10, 12, 14], rover_id=OTHER))
        with self.assertRaises(ValueError):
            evaluate(self.OBSERVATION, reference([10, 12, 14], signal="air.humidity"))

    def test_a_sufficient_reference_must_carry_usable_statistics(self):
        for changes in ({"mean": float("nan")}, {"std": float("inf")}, {"std": -1.0},
                        {"mean": None}, {"std": None}, {"mean": True}):
            with self.assertRaises(ValueError, msg=repr(changes)):
                evaluate(self.OBSERVATION, replace(REF, **changes))

    def test_batch_validates_its_parameters_even_when_empty(self):
        self.assertEqual(evaluate_observations(ROVER, "air.temperature", [], REF, 2.0, 0.0), ())
        with self.assertRaises(ValueError):
            evaluate_observations(ROVER, "air.temperature", [], REF, 0, 0.0)
        for bad in ("text", b"bytes", {"a": 1}, 5, None):
            with self.assertRaises(TypeError):
                evaluate_observations(ROVER, "air.temperature", bad, REF, 2.0, 0.0)


class TestOwnWindowGuard(unittest.TestCase):
    def ref(self, first_id=10, count=5):
        return reference([10, 12, 14, 11, 13][:count], first_id=first_id)  # ids first..first+count-1

    def observation(self, record_id):
        return StoredRecord(record_id, make("air.temperature", 13.0))

    def test_ids_inside_the_span_are_rejected(self):
        ref = self.ref()  # first_id 10, last_id 14
        self.assertEqual((ref.first_id, ref.last_id), (10, 14))
        for record_id in (10, 11, 12, 14):
            with self.assertRaises(ValueError, msg=record_id):
                evaluate(self.observation(record_id), ref)

    def test_ids_outside_the_span_are_accepted(self):
        ref = self.ref()
        for record_id in (9, 15, 1, 1000):
            self.assertEqual(evaluate(self.observation(record_id), ref).status, STATUS_WITHIN)

    def test_the_span_is_taken_from_the_smaller_and_larger_id(self):
        items = [StoredRecord(i, make("air.temperature", v)) for i, v in ((14, 10), (11, 12), (10, 14))]
        ref = compute_baseline(ROVER, "air.temperature", items, 2)
        self.assertEqual((ref.first_id, ref.last_id), (14, 10))
        with self.assertRaises(ValueError):
            evaluate(self.observation(12), ref)
        self.assertEqual(evaluate(self.observation(20), ref).status, STATUS_WITHIN)

    def test_the_guard_applies_whatever_the_observation_quality(self):
        ref = self.ref()
        faulted = StoredRecord(12, make("air.temperature", None, status="fault"))
        with self.assertRaises(ValueError):
            evaluate(faulted, ref)

    def test_no_guard_without_ids(self):
        ref = self.ref()
        self.assertEqual(evaluate(make("air.temperature", 13.0), ref).status, STATUS_WITHIN)
        self.assertEqual(evaluate(make("air.temperature", 13.0), REF).status, STATUS_WITHIN)
        plain_reference = reference([10, 12, 14])  # no ids at all
        self.assertIsNone(plain_reference.first_id)
        self.assertEqual(evaluate(self.observation(1), plain_reference).status, STATUS_WITHIN)

    def test_the_batch_rejects_an_observation_inside_the_span(self):
        ref = self.ref()
        items = [self.observation(20), self.observation(12)]
        with self.assertRaises(ValueError):
            evaluate_observations(ROVER, "air.temperature", items, ref, 2.0, 0.0)


class TestPublicClassificationIsUsed(unittest.TestCase):
    def test_observation_outcome_comes_from_classify_observation(self):
        target = "anomaly.anomaly.classify_observation"
        junk = object()  # not a Record: only the patched classifier decides
        with mock.patch(target, return_value=ObservationOutcome(None, "null_reading", 7)) as patched:
            result = evaluate(junk, REF)
        patched.assert_called_once_with(junk, ROVER, "air.temperature")
        self.assertEqual((result.status, result.reason, result.record_id), (STATUS_NOT_EVALUATED, "null_reading", 7))
        with mock.patch(target, return_value=ObservationOutcome(15.0, None, None)):
            result = evaluate(junk, REF, threshold=1.0)
        self.assertEqual((result.status, result.observed_value, result.z_score), (STATUS_DEVIATES, 15.0, 1.5))
        with mock.patch(target, return_value=ObservationOutcome(None, "wrong_rover", None)):
            self.assertEqual(evaluate(junk, REF).reason, "wrong_rover")

    def test_every_classifier_reason_is_passed_through(self):
        for reason in OBSERVATION_REASONS:
            with mock.patch(
                "anomaly.anomaly.classify_observation", return_value=ObservationOutcome(None, reason, None)
            ):
                result = evaluate(object(), REF)
            self.assertEqual((result.status, result.reason), (STATUS_NOT_EVALUATED, reason))

    def test_the_module_does_not_recreate_the_exclusion_logic(self):
        module = importlib.import_module("anomaly.anomaly")
        source = inspect.getsource(module)
        for name in ("validate_record", "_observe", "_classify_invalid", "_unpack", "sensor_status",
                     "SENSOR_READINGS"):
            self.assertNotIn(name, source, name)
        tree = ast.parse(source)
        imported = [
            (node.module, [alias.name for alias in node.names])
            for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        ]
        self.assertIn("classify_observation", dict(imported)["baseline.baseline"])


class TestOutputContract(unittest.TestCase):
    def setUp(self):
        self.result = evaluate(make("air.temperature", 13.0), REF)

    def test_exact_field_sets(self):
        self.assertEqual(tuple(f.name for f in dataclasses.fields(DeviationResult)), RESULT_FIELDS)
        self.assertEqual(tuple(f.name for f in dataclasses.fields(ReferenceSummary)), REFERENCE_FIELDS)

    def test_results_are_frozen(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            self.result.status = STATUS_DEVIATES
        with self.assertRaises(dataclasses.FrozenInstanceError):
            self.result.reference.mean = 0.0
        with self.assertRaises(TypeError):
            self.result.context["machine_state"] = "running"

    def test_status_and_reason_vocabulary(self):
        self.assertEqual(
            (STATUS_WITHIN, STATUS_DEVIATES, STATUS_NOT_EVALUATED),
            ("within_reference", "deviates_from_reference", "not_evaluated"),
        )
        self.assertEqual((REASON_INSUFFICIENT_REFERENCE, REASON_NO_SPREAD),
                         ("insufficient_reference", "reference_has_no_spread"))

    def test_no_health_failure_or_prediction_vocabulary(self):
        words = ("health", "fail", "severity", "confidence", "probab", "cause", "maintenance",
                 "alert", "predict", "anomal", "trend", "persist", "ewma", "diagnos", "rul")
        names = [f.name for f in dataclasses.fields(DeviationResult)]
        names += [f.name for f in dataclasses.fields(ReferenceSummary)]
        values = [STATUS_WITHIN, STATUS_DEVIATES, STATUS_NOT_EVALUATED, REASON_NO_SPREAD,
                  REASON_INSUFFICIENT_REFERENCE, COMPARISON, *OBSERVATION_REASONS]
        for text in names + values:
            for word in words:
                self.assertNotIn(word, text.lower(), text)
        for banned in ("confidence", "probability", "severity", "cause", "health", "alert",
                       "prediction", "score", "persistence"):
            self.assertFalse(hasattr(self.result, banned), banned)

    def test_comparison_is_a_neutral_rule(self):
        self.assertEqual(self.result.comparison, "abs(z_score) > threshold")


class TestMockRoverIntegration(unittest.TestCase):
    ROWS = 24
    REFERENCE_ROWS = 16
    FAULT_ROWS = (3, 4, 18)  # two inside the reference window, one an observation

    ROW_SECONDS = 65.0  # the mock air values change once per 60 s of simulated time

    def build(self, row_seconds):
        store = RecordStore(":memory:")
        self.addCleanup(store.close)
        rover = MockRover(replace(RoverConfig(), seed=3))
        rover.startup()
        for index in range(self.ROWS):
            rover.drive(1.0, 1.0)
            rover.advance(row_seconds)
            rover.stop()
            if index in self.FAULT_ROWS:
                rover.inject_fault("air")
            else:
                rover.clear_fault("air")
            rover.insert_probe(depth_ok=True)
            rover.take_measurement()
            store.add(rover.build_record())
            rover.retract_probe()
        return store

    def setUp(self):
        self.store = self.build(self.ROW_SECONDS)
        self.history = self.store.list_records(rover_id=ROVER)
        self.window = self.history[: self.REFERENCE_ROWS]
        self.observations = self.history[self.REFERENCE_ROWS:]

    def test_a_reference_from_one_constant_mock_air_bucket_has_no_spread(self):
        store = self.build(2.0)  # 24 rows in under two 60 s buckets: air values repeat
        history = store.list_records(rover_id=ROVER)
        ref = compute_baseline(ROVER, "air.temperature", history[:16], 5)
        self.assertEqual((ref.status, ref.std), ("sufficient", 0.0))
        result = evaluate(history[20], ref, threshold=3.0)
        self.assertEqual((result.status, result.reason), (STATUS_NOT_EVALUATED, REASON_NO_SPREAD))
        self.assertIsNone(result.z_score)

    def test_history_is_long_enough_and_contains_a_fault(self):
        self.assertGreaterEqual(len(self.history), 20)
        self.assertEqual(self.history[18].record["sensor_status"]["air"], "fault")

    def test_every_air_signal_end_to_end(self):
        for signal in AIR:
            ref = compute_baseline(ROVER, signal, self.window, 5)
            self.assertEqual(ref.status, "sufficient")
            self.assertEqual(ref.count, self.REFERENCE_ROWS - 2)
            results = evaluate_observations(ROVER, signal, self.observations, ref, 3.0, 0.0)
            self.assertEqual(len(results), len(self.observations))
            for stored_record, result in zip(self.observations, results):
                self.assertEqual(result.record_id, stored_record.id)
                if stored_record.record["sensor_status"]["air"] == "fault":
                    self.assertEqual((result.status, result.reason), (STATUS_NOT_EVALUATED, "sensor_fault"))
                    self.assertIsNone(result.observed_value)
                else:
                    self.assertEqual(result.status, STATUS_WITHIN, signal)
                    self.assertIsNone(result.reason)

    def test_an_injected_deviation_is_flagged_and_a_fault_is_not(self):
        ref = compute_baseline(ROVER, "air.temperature", self.window, 5)
        shifted = copy.deepcopy(self.observations[0])
        shifted.record["air"]["temperature"] = ref.mean + 50.0
        self.assertEqual(evaluate(shifted, ref, threshold=3.0).status, STATUS_DEVIATES)
        faulted = copy.deepcopy(shifted)
        faulted.record["sensor_status"]["air"] = "fault"
        self.assertEqual(evaluate(faulted, ref, threshold=3.0).status, STATUS_NOT_EVALUATED)

    def test_faulted_reference_rows_never_enter_the_reference(self):
        ref = compute_baseline(ROVER, "air.temperature", self.window, 5)
        self.assertEqual(ref.excluded["sensor_fault"], 2)
        healthy = [s.record["air"]["temperature"] for i, s in enumerate(self.window) if i not in (3, 4)]
        self.assertAlmostEqual(ref.mean, sum(healthy) / len(healthy))

    def test_the_reference_window_itself_is_rejected_as_an_observation(self):
        ref = compute_baseline(ROVER, "air.temperature", self.window, 5)
        with self.assertRaises(ValueError):
            evaluate(self.window[5], ref)

    def test_the_store_is_not_changed(self):
        ref = compute_baseline(ROVER, "air.temperature", self.window, 5)
        before = self.store.list_records()
        evaluate_observations(ROVER, "air.temperature", self.observations, ref, 3.0, 0.0)
        self.assertEqual(self.store.list_records(), before)


class TestPurityAndDeterminism(unittest.TestCase):
    def observations(self):
        return stored(
            [make("air.temperature", v) for v in (13.0, 17.0)]
            + [make("air.temperature", None, status="fault"), {"unexpected": 1}],
            first_id=100,
        )

    def test_repeated_evaluation_is_identical(self):
        observation = make("air.temperature", 17.0)
        self.assertEqual(evaluate(observation, REF), evaluate(observation, REF))
        items = self.observations()
        first = evaluate_observations(ROVER, "air.temperature", items, REF, 2.0, 0.0)
        second = evaluate_observations(ROVER, "air.temperature", items, REF, 2.0, 0.0)
        self.assertEqual(first, second)

    def test_inputs_are_not_modified(self):
        items = self.observations()
        plain = make("air.temperature", 17.0)
        ref = reference([10, 12, 14], first_id=1)
        items_copy, plain_copy = copy.deepcopy(items), copy.deepcopy(plain)
        evaluate_observations(ROVER, "air.temperature", items, ref, 2.0, 0.0)
        evaluate(plain, ref)
        self.assertEqual(items, items_copy)
        self.assertEqual(plain, plain_copy)
        self.assertEqual(ref, reference([10, 12, 14], first_id=1))  # the reference is unchanged
        self.assertEqual(len(items), 4)

    def test_batch_results_are_independent_and_in_supplied_order(self):
        items = self.observations()
        results = evaluate_observations(ROVER, "air.temperature", items, REF, 2.0, 0.0)
        self.assertEqual([r.record_id for r in results], [100, 101, 102, 103])
        self.assertEqual(
            [r.status for r in results],
            [STATUS_WITHIN, STATUS_DEVIATES, STATUS_NOT_EVALUATED, STATUS_NOT_EVALUATED],
        )
        self.assertEqual(results, evaluate_observations(ROVER, "air.temperature", items[::-1], REF, 2.0, 0.0)[::-1])
        self.assertEqual(evaluate_observations(ROVER, "air.temperature", iter(items), REF, 2.0, 0.0), results)

    def test_no_clock_randomness_or_io_is_used(self):
        def forbidden(*args, **kwargs):
            raise AssertionError("a forbidden facility was used")

        with mock.patch("time.time", forbidden), \
                mock.patch("time.time_ns", forbidden), \
                mock.patch("time.monotonic", forbidden), \
                mock.patch("time.perf_counter", forbidden), \
                mock.patch("time.sleep", forbidden), \
                mock.patch("random.random", forbidden), \
                mock.patch("random.randint", forbidden), \
                mock.patch("random.choice", forbidden), \
                mock.patch("os.urandom", forbidden), \
                mock.patch("builtins.open", forbidden), \
                mock.patch("socket.socket", forbidden), \
                mock.patch("sqlite3.connect", forbidden):
            result = evaluate(make("air.temperature", 17.0), REF)
        self.assertEqual(result.status, STATUS_DEVIATES)

    def test_module_imports_only_what_it_needs(self):
        module = importlib.import_module("anomaly.anomaly")
        tree = ast.parse(inspect.getsource(module))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module.split(".")[0])
        self.assertEqual(imported, {"math", "dataclasses", "types", "baseline"})
        for banned in ("time", "datetime", "random", "secrets", "socket", "threading", "asyncio",
                       "sqlite3", "os", "json", "pathlib", "statistics", "storage", "mock_rover",
                       "machine_state", "energy", "calibration", "record", "numpy", "sklearn", "scipy"):
            self.assertFalse(hasattr(module, banned), banned)

    def test_no_machine_learning_or_unrelated_methods_exist(self):
        module = importlib.import_module("anomaly.anomaly")
        public = {name for name in dir(module) if not name.startswith("_")}
        for word in ("fit", "train", "predict", "ewma", "trend", "persist", "window", "forest", "svm"):
            self.assertFalse(any(word in name.lower() for name in public), word)

    def test_results_hold_only_plain_finite_numbers(self):
        result = evaluate(make("air.temperature", 17.0), REF)
        for value in (result.observed_value, result.deviation, result.z_score, result.threshold):
            self.assertIsInstance(value, float)
            self.assertTrue(math.isfinite(value))


if __name__ == "__main__":
    unittest.main()
