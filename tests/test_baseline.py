import ast
import copy
import dataclasses
import importlib
import inspect
import math
import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from unittest import mock

from baseline.baseline import (
    CAVEAT_BATTERY_TREND,
    CAVEAT_SPATIAL_SAMPLING,
    EXCLUSION_REASONS,
    SIGNALS,
    BaselineResult,
    compute_baseline,
)
from mock_rover.config import RoverConfig
from mock_rover.simulator import MockRover
from storage.record_store import RecordStore, StorageCorruptionError, StoredRecord

ROVER = "nelavi-mock-1"
ALL_SIGNALS = (
    "soil.nitrogen",
    "soil.phosphorus",
    "soil.potassium",
    "soil.moisture",
    "soil.temperature",
    "air.temperature",
    "air.humidity",
    "air.pressure",
    "battery.voltage_v",
)
APPROVED_FIELDS = (
    "rover_id", "signal", "unit", "status", "min_count_required", "count", "excluded",
    "mean", "median", "minimum", "maximum", "std", "first_id", "last_id",
    "records_examined", "context", "caveats",
)
STATUS_KEY = {"soil.nitrogen": "npk", "soil.phosphorus": "npk", "soil.potassium": "npk",
              "soil.moisture": "moisture", "soil.temperature": "soil_temperature",
              "air.temperature": "air", "air.humidity": "air", "air.pressure": "air",
              "battery.voltage_v": "battery"}


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
    record["sensor_status"][STATUS_KEY[signal]] = status
    record["rover_id"] = rover_id
    record.update(changes)
    return record


def nitrogen(values):
    return [make("soil.nitrogen", v) for v in values]


def stored(records, first_id=1):
    return [StoredRecord(first_id + i, r) for i, r in enumerate(records)]


class TestBasicStatistics(unittest.TestCase):
    def test_known_sample(self):
        result = compute_baseline(ROVER, "soil.nitrogen", nitrogen([1, 2, 3, 4]), 2)
        self.assertEqual(result.status, "sufficient")
        self.assertEqual(result.count, 4)
        self.assertEqual(result.mean, 2.5)
        self.assertEqual(result.median, 2.5)
        self.assertEqual(result.minimum, 1.0)
        self.assertEqual(result.maximum, 4.0)
        self.assertAlmostEqual(result.std, math.sqrt(5 / 3))

    def test_second_known_sample_uses_n_minus_one(self):
        result = compute_baseline(ROVER, "soil.nitrogen", nitrogen([2, 4, 4, 4, 5, 5, 7, 9]), 2)
        self.assertEqual(result.count, 8)
        self.assertEqual(result.mean, 5.0)
        self.assertEqual(result.median, 4.5)
        self.assertEqual((result.minimum, result.maximum), (2.0, 9.0))
        self.assertAlmostEqual(result.std, math.sqrt(32 / 7))

    def test_two_observations(self):
        result = compute_baseline(ROVER, "soil.nitrogen", nitrogen([10, 20]), 2)
        self.assertEqual((result.mean, result.median), (15.0, 15.0))
        self.assertAlmostEqual(result.std, math.sqrt(50))

    def test_every_signal_is_supported(self):
        for signal in ALL_SIGNALS:
            records = [make(signal, 1.5), make(signal, 2.5)]
            result = compute_baseline(ROVER, signal, records, 2)
            self.assertEqual(result.signal, signal)
            self.assertEqual(result.mean, 2.0, signal)

    def test_integer_readings_are_included(self):
        result = compute_baseline(ROVER, "air.pressure", [make("air.pressure", 1000), make("air.pressure", 1010)], 2)
        self.assertEqual(result.mean, 1005.0)


class TestSufficiency(unittest.TestCase):
    def test_below_min_count_is_insufficient(self):
        result = compute_baseline(ROVER, "soil.nitrogen", nitrogen([1, 2, 3]), 4)
        self.assertEqual(result.status, "insufficient")
        self.assertEqual(result.count, 3)
        self.assertEqual(result.min_count_required, 4)
        for name in ("mean", "median", "minimum", "maximum", "std"):
            self.assertIsNone(getattr(result, name), name)

    def test_equal_to_min_count_is_sufficient(self):
        result = compute_baseline(ROVER, "soil.nitrogen", nitrogen([1, 2, 3, 4]), 4)
        self.assertEqual(result.status, "sufficient")
        self.assertIsNotNone(result.std)

    def test_empty_window_is_insufficient(self):
        result = compute_baseline(ROVER, "soil.nitrogen", [], 2)
        self.assertEqual(result.status, "insufficient")
        self.assertEqual((result.count, result.records_examined), (0, 0))
        self.assertIsNone(result.first_id)
        self.assertIsNone(result.last_id)

    def test_invalid_min_count_is_rejected(self):
        for bad in (1, 0, -1, -5, True, False, 2.0, 2.5, "2", None, [2]):
            with self.assertRaises(ValueError, msg=repr(bad)):
                compute_baseline(ROVER, "soil.nitrogen", nitrogen([1, 2]), bad)

    def test_min_count_has_no_default(self):
        with self.assertRaises(TypeError):
            compute_baseline(ROVER, "soil.nitrogen", nitrogen([1, 2]))
        parameter = inspect.signature(compute_baseline).parameters["min_count"]
        self.assertIs(parameter.default, inspect.Parameter.empty)

    def test_bad_rover_id_and_records_are_rejected(self):
        for bad in ("", None, 5):
            with self.assertRaises(ValueError):
                compute_baseline(bad, "soil.nitrogen", [], 2)
        for bad in ("text", b"bytes", {"a": 1}, 5, None):
            with self.assertRaises(TypeError):
                compute_baseline(ROVER, "soil.nitrogen", bad, 2)


class TestExclusions(unittest.TestCase):
    def excluded_one(self, record, signal="soil.nitrogen"):
        result = compute_baseline(ROVER, signal, [record] + nitrogen([1, 2]) if signal == "soil.nitrogen" else [record], 2)
        return result

    def only(self, result, reason):
        for name in EXCLUSION_REASONS:
            self.assertEqual(result.excluded[name], 1 if name == reason else 0, name)

    def test_reasons_are_all_present_with_zero_counts(self):
        result = compute_baseline(ROVER, "soil.nitrogen", nitrogen([1, 2]), 2)
        self.assertEqual(dict(result.excluded), {name: 0 for name in EXCLUSION_REASONS})
        self.assertEqual(
            EXCLUSION_REASONS,
            ("null_reading", "sensor_fault", "invalid_record", "non_finite_or_non_numeric", "wrong_rover"),
        )

    def test_null_reading(self):
        result = self.excluded_one(make("soil.nitrogen", None))
        self.only(result, "null_reading")
        self.assertEqual(result.count, 2)

    def test_sensor_fault_with_null(self):
        result = self.excluded_one(make("soil.nitrogen", None, status="fault"))
        self.only(result, "sensor_fault")

    def test_sensor_fault_with_numeric_value(self):
        result = self.excluded_one(make("soil.nitrogen", 99.0, status="fault"))
        self.only(result, "sensor_fault")
        self.assertEqual(result.count, 2)
        self.assertEqual(result.maximum, 2.0)  # the faulted 99.0 never contributed

    def test_invalid_record(self):
        broken = make("soil.nitrogen", 5.0)
        broken["unexpected"] = 1
        self.only(self.excluded_one(broken), "invalid_record")
        missing = make("soil.nitrogen", 5.0)
        del missing["air"]
        self.only(self.excluded_one(missing), "invalid_record")

    def test_item_that_is_not_a_record(self):
        for item in (None, 5, "record", [1], {"record": 1}):
            result = compute_baseline(ROVER, "soil.nitrogen", [item], 2)
            self.assertEqual(result.excluded["invalid_record"], 1, repr(item))
        result = compute_baseline(ROVER, "soil.nitrogen", [StoredRecord(1, "not a dict")], 2)
        self.assertEqual(result.excluded["invalid_record"], 1)

    def test_non_finite_value(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            result = self.excluded_one(make("soil.nitrogen", bad))
            self.only(result, "non_finite_or_non_numeric")
            self.assertEqual(result.count, 2)

    def test_non_numeric_value(self):
        for bad in ("12", True, [1], {"v": 1}):
            result = self.excluded_one(make("soil.nitrogen", bad))
            self.only(result, "non_finite_or_non_numeric")

    def test_wrong_rover(self):
        result = self.excluded_one(make("soil.nitrogen", 5.0, rover_id="other-rover"))
        self.only(result, "wrong_rover")
        self.assertEqual(result.count, 2)

    def test_unrelated_validation_failures_are_never_signal_specific(self):
        # the selected reading is non-numeric, but the Record also has an unrelated defect
        record = make("soil.nitrogen", "12")
        record["unexpected"] = 1
        self.only(self.excluded_one(record), "invalid_record")
        # a neighbouring reading under the same shared flag is null while the sensor is "ok"
        record = make("soil.phosphorus", 5.0)
        record["soil"]["nitrogen"] = None
        result = compute_baseline(ROVER, "soil.phosphorus", [record], 2)
        self.assertEqual(result.excluded["invalid_record"], 1)
        self.assertEqual(result.excluded["null_reading"], 0)
        # a non-numeric selected reading plus another invalid reading
        record = make("soil.nitrogen", "12")
        record["air"]["humidity"] = "wet"
        self.only(self.excluded_one(record), "invalid_record")
        # an unrelated defect on a faulted sensor Record is still not a signal-specific reason
        record = make("soil.nitrogen", None, status="fault")
        record["confidence"] = 500
        self.only(self.excluded_one(record), "invalid_record")

    def test_a_missing_selected_reading_key_is_invalid_record(self):
        record = make("soil.nitrogen", 5.0)
        del record["soil"]["nitrogen"]
        self.only(self.excluded_one(record), "invalid_record")

    def test_rover_is_checked_before_validation(self):
        invalid_other = make("soil.nitrogen", None, rover_id="other-rover")  # ok + null: invalid
        invalid_other["unexpected"] = 1
        self.only(self.excluded_one(invalid_other), "wrong_rover")
        faulted_other = make("soil.nitrogen", 9.0, status="fault", rover_id="other-rover")
        self.only(self.excluded_one(faulted_other), "wrong_rover")
        # an invalid Record of the requested rover is invalid_record, not wrong_rover
        invalid_own = make("soil.nitrogen", 5.0)
        invalid_own["unexpected"] = 1
        self.only(self.excluded_one(invalid_own), "invalid_record")
        # without a rover_id there is nothing to compare, so validation decides
        no_rover = make("soil.nitrogen", 5.0)
        del no_rover["rover_id"]
        self.only(self.excluded_one(no_rover), "invalid_record")

    def test_counts_accumulate_per_reason(self):
        records = nitrogen([1, 2, 3]) + [
            make("soil.nitrogen", None),
            make("soil.nitrogen", None, status="fault"),
            make("soil.nitrogen", 9.0, status="fault"),
            make("soil.nitrogen", float("nan")),
            make("soil.nitrogen", 4.0, rover_id="other"),
            {"unexpected": 1},
        ]
        result = compute_baseline(ROVER, "soil.nitrogen", records, 2)
        self.assertEqual(result.count, 3)
        self.assertEqual(result.records_examined, 9)
        self.assertEqual(
            dict(result.excluded),
            {"null_reading": 1, "sensor_fault": 2, "invalid_record": 1,
             "non_finite_or_non_numeric": 1, "wrong_rover": 1},
        )
        self.assertEqual(result.count + sum(result.excluded.values()), result.records_examined)


class TestFaultSemantics(unittest.TestCase):
    def test_faulted_readings_never_contribute(self):
        records = nitrogen([1, 2, 3]) + [make("soil.nitrogen", 1000.0, status="fault")] * 5
        result = compute_baseline(ROVER, "soil.nitrogen", records, 2)
        self.assertEqual(result.count, 3)
        self.assertEqual(result.mean, 2.0)
        self.assertEqual(result.excluded["sensor_fault"], 5)

    def test_faults_do_not_invalidate_the_baseline(self):
        records = nitrogen([1, 2, 3]) + [make("soil.nitrogen", None, status="fault")]
        result = compute_baseline(ROVER, "soil.nitrogen", records, 3)
        self.assertEqual(result.status, "sufficient")

    def test_all_faulted_gives_insufficient_not_healthy(self):
        records = [make("soil.nitrogen", None, status="fault") for _ in range(10)]
        result = compute_baseline(ROVER, "soil.nitrogen", records, 2)
        self.assertEqual(result.status, "insufficient")
        self.assertEqual(result.count, 0)
        self.assertEqual(result.excluded["sensor_fault"], 10)

    def test_shared_npk_fault_excludes_n_p_k(self):
        record = copy.deepcopy(BASE)
        record["sensor_status"]["npk"] = "fault"
        for key in ("nitrogen", "phosphorus", "potassium"):
            record["soil"][key] = 50.0  # numeric values on a faulted sensor
        for signal in ("soil.nitrogen", "soil.phosphorus", "soil.potassium"):
            result = compute_baseline(ROVER, signal, [record, record], 2)
            self.assertEqual(result.count, 0, signal)
            self.assertEqual(result.excluded["sensor_fault"], 2, signal)

    def test_npk_fault_does_not_exclude_moisture(self):
        record = copy.deepcopy(BASE)
        record["sensor_status"]["npk"] = "fault"
        result = compute_baseline(ROVER, "soil.moisture", [record, record], 2)
        self.assertEqual(result.count, 2)

    def test_shared_air_fault_excludes_all_air_signals(self):
        record = copy.deepcopy(BASE)
        record["sensor_status"]["air"] = "fault"
        for signal in ("air.temperature", "air.humidity", "air.pressure"):
            result = compute_baseline(ROVER, signal, [record, record], 2)
            self.assertEqual(result.excluded["sensor_fault"], 2, signal)


class TestOrderingAndWindow(unittest.TestCase):
    def test_supplied_order_and_ids_are_kept(self):
        items = [StoredRecord(5, make("soil.nitrogen", 1.0)),
                 StoredRecord(3, make("soil.nitrogen", 2.0)),
                 StoredRecord(9, make("soil.nitrogen", 3.0))]
        result = compute_baseline(ROVER, "soil.nitrogen", items, 2)
        self.assertEqual((result.first_id, result.last_id), (5, 9))  # not sorted
        reversed_result = compute_baseline(ROVER, "soil.nitrogen", items[::-1], 2)
        self.assertEqual((reversed_result.first_id, reversed_result.last_id), (9, 5))
        self.assertEqual(result.mean, reversed_result.mean)

    def test_ids_with_a_single_record(self):
        result = compute_baseline(ROVER, "soil.nitrogen", [StoredRecord(7, make("soil.nitrogen", 1.0))], 2)
        self.assertEqual((result.first_id, result.last_id), (7, 7))

    def test_ids_are_none_for_plain_records(self):
        result = compute_baseline(ROVER, "soil.nitrogen", nitrogen([1, 2]), 2)
        self.assertIsNone(result.first_id)
        self.assertIsNone(result.last_id)

    def test_ids_cover_excluded_records_too(self):
        items = stored([make("soil.nitrogen", None, status="fault")] + nitrogen([1, 2]), first_id=10)
        result = compute_baseline(ROVER, "soil.nitrogen", items, 2)
        self.assertEqual((result.first_id, result.last_id), (10, 12))
        self.assertEqual(result.records_examined, 3)

    def test_duplicates_are_separate_observations(self):
        record = make("soil.nitrogen", 5.0)
        result = compute_baseline(ROVER, "soil.nitrogen", [record, record, record], 3)
        self.assertEqual(result.count, 3)
        self.assertEqual(result.std, 0.0)

    def test_a_generator_window_is_accepted_in_order(self):
        result = compute_baseline(ROVER, "soil.nitrogen", (r for r in stored(nitrogen([1, 2, 3]))), 2)
        self.assertEqual((result.first_id, result.last_id, result.count), (1, 3, 3))

    def test_time_and_sequence_number_are_not_used(self):
        times = ("not-a-time", "sim:999.000", "2000-01-01", "x")
        plain = [make("soil.nitrogen", v) for v in (1.0, 2.0, 3.0, 4.0)]
        scrambled = [
            make("soil.nitrogen", v, time=t, sequence_number=s)
            for v, t, s in zip((1.0, 2.0, 3.0, 4.0), times, (42, -7, 42, 0))
        ]
        self.assertEqual(
            compute_baseline(ROVER, "soil.nitrogen", plain, 2),
            compute_baseline(ROVER, "soil.nitrogen", scrambled, 2),
        )
        # records with duplicated or decreasing sequence numbers are all kept
        result = compute_baseline(ROVER, "soil.nitrogen", stored(scrambled), 2)
        self.assertEqual((result.first_id, result.last_id, result.count), (1, 4, 4))


class TestOutput(unittest.TestCase):
    def setUp(self):
        self.result = compute_baseline(ROVER, "soil.nitrogen", nitrogen([1, 2, 3]), 2)

    def test_result_is_frozen(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            self.result.count = 99
        with self.assertRaises(TypeError):
            self.result.excluded["sensor_fault"] = 5
        with self.assertRaises(TypeError):
            self.result.context["machine_state"] = "running"

    def test_exact_field_set(self):
        names = tuple(field.name for field in dataclasses.fields(BaselineResult))
        self.assertEqual(names, APPROVED_FIELDS)

    def test_no_judgment_fields_exist(self):
        for banned in ("anomaly", "anomaly_score", "confidence", "health", "alert", "prediction",
                       "timestamp", "z_score", "normal", "score"):
            self.assertFalse(hasattr(self.result, banned), banned)
        for name in APPROVED_FIELDS:
            for word in ("anomal", "confidence", "health", "alert", "predict", "time"):
                self.assertNotIn(word, name)

    def test_basic_fields(self):
        self.assertIsNone(self.result.unit)
        self.assertEqual(self.result.rover_id, ROVER)
        self.assertEqual(self.result.signal, "soil.nitrogen")
        self.assertEqual(self.result.min_count_required, 2)
        self.assertEqual(self.result.records_examined, 3)

    def test_unit_is_none_for_every_signal(self):
        for signal in ALL_SIGNALS:
            self.assertIsNone(compute_baseline(ROVER, signal, [], 2).unit)

    def test_context_states_machine_state_is_unavailable(self):
        self.assertEqual(self.result.context["machine_state"], "unavailable")
        self.assertIn("not part of the Record", self.result.context["reason"])
        for signal in ALL_SIGNALS:
            self.assertEqual(compute_baseline(ROVER, signal, [], 2).context["machine_state"], "unavailable")

    def test_soil_caveats(self):
        for signal in ("soil.nitrogen", "soil.phosphorus", "soil.potassium", "soil.moisture", "soil.temperature"):
            caveats = compute_baseline(ROVER, signal, [], 2).caveats
            self.assertEqual([c.code for c in caveats], [CAVEAT_SPATIAL_SAMPLING], signal)
            self.assertIn("position", caveats[0].message)
            self.assertIn("spatial", caveats[0].message)

    def test_battery_voltage_caveat(self):
        caveats = compute_baseline(ROVER, "battery.voltage_v", [], 2).caveats
        self.assertEqual([c.code for c in caveats], [CAVEAT_BATTERY_TREND])
        self.assertIn("not a stationary machine-health baseline", caveats[0].message)

    def test_air_signals_have_no_caveat(self):
        for signal in ("air.temperature", "air.humidity", "air.pressure"):
            self.assertEqual(compute_baseline(ROVER, signal, [], 2).caveats, ())

    def test_caveats_are_present_even_when_insufficient(self):
        result = compute_baseline(ROVER, "soil.nitrogen", [], 2)
        self.assertEqual(result.status, "insufficient")
        self.assertEqual(len(result.caveats), 1)


class TestScopeProtection(unittest.TestCase):
    def test_supported_signals_are_exactly_the_nine(self):
        self.assertEqual(tuple(SIGNALS), ALL_SIGNALS)

    def test_unsupported_signals_are_rejected(self):
        for signal in ("light", "battery.pct", "battery.pct_", "battery.temperature", "vibration",
                       "rpm", "current", "machine_voltage", "real_power", "apparent_power",
                       "power_factor", "frequency", "power", "production_quantity", "load",
                       "machine_load", "soil", "soil.nitrogen ", "SOIL.NITROGEN", "", None, 5):
            with self.assertRaises(ValueError, msg=repr(signal)):
                compute_baseline(ROVER, signal, nitrogen([1, 2]), 2)


class TestMockRoverIntegration(unittest.TestCase):
    ROWS = 24
    FAULT_ROWS = range(10, 15)  # rows measured while the air sensor is faulted

    @classmethod
    def build_history(cls, store):
        rover = MockRover(replace(RoverConfig(), seed=3))
        rover.startup()
        for index in range(cls.ROWS):
            rover.drive(1.0, 1.0)
            rover.advance(2.0)
            rover.stop()
            if index in cls.FAULT_ROWS:
                rover.inject_fault("air")
            else:
                rover.clear_fault("air")
            rover.insert_probe(depth_ok=True)
            rover.take_measurement()
            store.add(rover.build_record())
            rover.retract_probe()

    def setUp(self):
        self.store = RecordStore(":memory:")
        self.addCleanup(self.store.close)
        self.build_history(self.store)
        self.history = self.store.list_records(rover_id=ROVER)

    def test_history_has_enough_records(self):
        self.assertGreaterEqual(len(self.history), 20)

    def test_every_signal_over_the_history(self):
        for signal in ALL_SIGNALS:
            result = compute_baseline(ROVER, signal, self.history, 5)
            self.assertEqual(result.records_examined, self.ROWS)
            self.assertEqual((result.first_id, result.last_id), (self.history[0].id, self.history[-1].id))
            self.assertEqual(result.count + sum(result.excluded.values()), self.ROWS)
            if signal.startswith("air."):
                self.assertEqual(result.count, self.ROWS - len(self.FAULT_ROWS), signal)
                self.assertEqual(result.excluded["sensor_fault"], len(self.FAULT_ROWS), signal)
            else:
                self.assertEqual(result.count, self.ROWS, signal)
                self.assertEqual(result.excluded["sensor_fault"], 0, signal)
            self.assertEqual(result.status, "sufficient")
            self.assertLessEqual(result.minimum, result.mean)
            self.assertLessEqual(result.mean, result.maximum)

    def test_faulted_air_values_never_enter_the_statistics(self):
        healthy = [s.record["air"]["temperature"] for i, s in enumerate(self.history) if i not in self.FAULT_ROWS]
        result = compute_baseline(ROVER, "air.temperature", self.history, 5)
        self.assertEqual(result.minimum, min(healthy))
        self.assertEqual(result.maximum, max(healthy))
        self.assertAlmostEqual(result.mean, sum(healthy) / len(healthy))

    def test_soil_values_follow_the_stored_records(self):
        values = [s.record["soil"]["nitrogen"] for s in self.history]
        result = compute_baseline(ROVER, "soil.nitrogen", self.history, 5)
        self.assertEqual((result.minimum, result.maximum), (min(values), max(values)))
        self.assertGreater(result.std, 0)  # the mock soil field varies with position

    def test_other_rover_filter_and_wrong_rover(self):
        result = compute_baseline("another-rover", "soil.nitrogen", self.history, 2)
        self.assertEqual(result.count, 0)
        self.assertEqual(result.excluded["wrong_rover"], self.ROWS)

    def test_a_partial_window_is_described_by_its_ids(self):
        window = self.store.list_records(rover_id=ROVER, after_id=self.history[4].id, limit=10)
        result = compute_baseline(ROVER, "soil.nitrogen", window, 2)
        self.assertEqual(result.records_examined, 10)
        self.assertEqual((result.first_id, result.last_id), (window[0].id, window[-1].id))

    def test_the_store_is_not_changed(self):
        before = self.store.list_records()
        compute_baseline(ROVER, "air.temperature", self.history, 2)
        self.assertEqual(self.store.list_records(), before)


class TestStorageErrorPropagation(unittest.TestCase):
    def test_corruption_from_the_callers_read_path_is_not_swallowed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "history.db")
            store = RecordStore(path)
            ids = [store.add(make("soil.nitrogen", float(n), sequence_number=n)) for n in range(3)]
            store.close()
            connection = sqlite3.connect(path)
            with connection:
                connection.execute("UPDATE records SET record_json = ? WHERE id = ?", ("{broken", ids[1]))
            connection.close()
            store = RecordStore(path)
            try:
                with self.assertRaises(StorageCorruptionError):
                    compute_baseline(ROVER, "soil.nitrogen", store.list_records(rover_id=ROVER), 2)
            finally:
                store.close()

    def test_an_error_raised_while_the_window_is_consumed_propagates(self):
        def window():
            yield make("soil.nitrogen", 1.0)
            raise StorageCorruptionError(2, "stored text is not valid JSON")

        with self.assertRaises(StorageCorruptionError):
            compute_baseline(ROVER, "soil.nitrogen", window(), 2)


class TestPurityAndDeterminism(unittest.TestCase):
    def window(self):
        return stored(nitrogen([1, 2, 3]) + [make("soil.nitrogen", None, status="fault")])

    def test_repeated_calls_are_identical(self):
        window = self.window()
        self.assertEqual(
            compute_baseline(ROVER, "soil.nitrogen", window, 2),
            compute_baseline(ROVER, "soil.nitrogen", window, 2),
        )

    def test_inputs_are_not_modified(self):
        window = self.window()
        plain = nitrogen([4, 5]) + [{"unexpected": 1}, make("soil.nitrogen", 9.0, rover_id="other")]
        window_copy, plain_copy = copy.deepcopy(window), copy.deepcopy(plain)
        compute_baseline(ROVER, "soil.nitrogen", window, 2)
        compute_baseline(ROVER, "soil.nitrogen", plain, 2)
        self.assertEqual(window, window_copy)
        self.assertEqual(plain, plain_copy)
        self.assertEqual(len(window), 4)
        self.assertEqual(len(plain), 4)

    def test_no_wall_clock_is_used(self):
        def forbidden(*args, **kwargs):
            raise AssertionError("the wall clock was used")

        with mock.patch("time.time", forbidden), \
                mock.patch("time.time_ns", forbidden), \
                mock.patch("time.monotonic", forbidden), \
                mock.patch("time.perf_counter", forbidden), \
                mock.patch("time.sleep", forbidden):
            result = compute_baseline(ROVER, "soil.nitrogen", self.window(), 2)
        self.assertEqual(result.count, 3)

    def test_no_randomness_is_used(self):
        def forbidden(*args, **kwargs):
            raise AssertionError("randomness was used")

        with mock.patch("random.random", forbidden), \
                mock.patch("random.randint", forbidden), \
                mock.patch("random.choice", forbidden), \
                mock.patch("random.shuffle", forbidden), \
                mock.patch("os.urandom", forbidden):
            compute_baseline(ROVER, "soil.nitrogen", self.window(), 2)

    def test_module_imports_only_what_it_needs(self):
        module = importlib.import_module("baseline.baseline")
        tree = ast.parse(inspect.getsource(module))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module.split(".")[0])
        self.assertEqual(imported, {"math", "statistics", "dataclasses", "types", "record"})
        for banned in ("time", "datetime", "random", "secrets", "socket", "threading", "asyncio",
                       "sqlite3", "os", "json", "pathlib", "storage", "mock_rover", "machine_state",
                       "energy", "calibration"):
            self.assertFalse(hasattr(module, banned), banned)


if __name__ == "__main__":
    unittest.main()
