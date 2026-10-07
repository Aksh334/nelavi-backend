import copy
import unittest

from record.validation import validate_record

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
VERSION_KEYS = ("firmware", "model", "rules", "calibration")
STATUS_KEYS = ("npk", "moisture", "soil_temperature", "air", "battery")
LEAF_KEYS = ("leaf_label", "confidence", "photo_id")


def make_valid_record():
    return {
        "time": "t-1",
        "sequence_number": 0,
        "rover_id": "rover-a",
        "battery": {"voltage_v": 11.8},
        "soil": {
            "nitrogen": 10,
            "phosphorus": 20.5,
            "potassium": 30,
            "moisture": 40,
            "temperature": 22.5,
        },
        "air": {"temperature": 25, "humidity": 60, "pressure": 1000},
        "sensor_status": {
            "npk": "ok",
            "moisture": "ok",
            "soil_temperature": "ok",
            "air": "ok",
            "battery": "ok",
        },
        "leaf_label": "healthy",
        "confidence": 85,
        "photo_id": "photo-1",
        "versions": {
            "firmware": "fw-1",
            "model": "model-1",
            "rules": "rules-1",
            "calibration": "cal-1",
        },
    }


def error_pairs(result):
    return {(e.path, e.code) for e in result.errors}


class TestValidRecord(unittest.TestCase):
    def test_valid_canonical_record(self):
        result = validate_record(make_valid_record())
        self.assertTrue(result.valid)
        self.assertEqual(result.errors, ())
        self.assertEqual(result.record, make_valid_record())

    def test_canonical_top_level_keys_are_exactly_the_approved_set(self):
        self.assertEqual(set(make_valid_record()), set(TOP_LEVEL_KEYS))


class TestNonObjectInput(unittest.TestCase):
    def test_non_object_inputs_are_rejected(self):
        for candidate in (None, [], "record", 5, 1.5, True):
            result = validate_record(candidate)
            self.assertFalse(result.valid)
            self.assertIsNone(result.record)
            self.assertEqual(error_pairs(result), {("", "wrong_type")})


class TestTopLevelStructure(unittest.TestCase):
    def test_each_top_level_field_is_required(self):
        for key in TOP_LEVEL_KEYS:
            record = make_valid_record()
            del record[key]
            result = validate_record(record)
            self.assertFalse(result.valid, key)
            self.assertIn((key, "missing"), error_pairs(result))

    def test_unknown_top_level_field_is_rejected(self):
        record = make_valid_record()
        record["extra"] = 1
        result = validate_record(record)
        self.assertFalse(result.valid)
        self.assertEqual(error_pairs(result), {("extra", "unknown_field")})

    def test_sequence_number_must_be_an_integer(self):
        for bad in ("1", 1.5, None, [], {}):
            record = make_valid_record()
            record["sequence_number"] = bad
            self.assertIn(
                ("sequence_number", "wrong_type"), error_pairs(validate_record(record)), bad
            )

    def test_boolean_is_rejected_as_sequence_number(self):
        for bad in (True, False):
            record = make_valid_record()
            record["sequence_number"] = bad
            self.assertIn(
                ("sequence_number", "wrong_type"), error_pairs(validate_record(record)), bad
            )

    def test_sequence_number_has_no_sign_or_range_rule(self):
        for ok in (0, 1, -1, 10**12):
            record = make_valid_record()
            record["sequence_number"] = ok
            self.assertTrue(validate_record(record).valid, ok)

    def test_empty_strings_are_rejected_for_required_string_fields(self):
        paths = (("time",), ("rover_id",)) + tuple(("versions", k) for k in VERSION_KEYS)
        for path in paths:
            record = make_valid_record()
            target = record
            for part in path[:-1]:
                target = target[part]
            target[path[-1]] = ""
            self.assertIn(
                (".".join(path), "wrong_type"), error_pairs(validate_record(record)), path
            )

    def test_non_string_values_are_rejected_for_required_string_fields(self):
        for key in ("time", "rover_id"):
            for bad in (5, 1.5, True, None, [], {}):
                record = make_valid_record()
                record[key] = bad
                self.assertIn((key, "wrong_type"), error_pairs(validate_record(record)), bad)

    def test_sections_must_be_objects(self):
        record = make_valid_record()
        record["soil"] = []
        record["versions"] = "v1"
        self.assertEqual(
            error_pairs(validate_record(record)),
            {("soil", "wrong_type"), ("versions", "wrong_type")},
        )


class TestVersions(unittest.TestCase):
    def test_each_version_is_required(self):
        for key in VERSION_KEYS:
            record = make_valid_record()
            del record["versions"][key]
            self.assertEqual(
                error_pairs(validate_record(record)), {("versions." + key, "missing")}
            )

    def test_unknown_version_field_is_rejected(self):
        record = make_valid_record()
        record["versions"]["hardware"] = "x"
        self.assertEqual(
            error_pairs(validate_record(record)), {("versions.hardware", "unknown_field")}
        )

    def test_versions_have_exactly_the_approved_keys(self):
        self.assertEqual(set(make_valid_record()["versions"]), set(VERSION_KEYS))


class TestTimeHasNoFormatRule(unittest.TestCase):
    def test_any_non_empty_string_is_accepted_as_time(self):
        for value in ("t-1", "not a time", "12345", "2026-10-07T10:00:00+05:30", "yesterday"):
            record = make_valid_record()
            record["time"] = value
            self.assertTrue(validate_record(record).valid, value)


class TestReadings(unittest.TestCase):
    def test_canonical_reading_fields(self):
        record = make_valid_record()
        self.assertEqual(
            set(record["soil"]),
            {"nitrogen", "phosphorus", "potassium", "moisture", "temperature"},
        )
        self.assertEqual(set(record["air"]), {"temperature", "humidity", "pressure"})
        self.assertEqual(set(record["battery"]), {"voltage_v"})

    def test_each_canonical_npk_field_is_required(self):
        for key in ("nitrogen", "phosphorus", "potassium"):
            record = make_valid_record()
            del record["soil"][key]
            self.assertEqual(error_pairs(validate_record(record)), {("soil." + key, "missing")})

    def test_battery_voltage_v_is_required_and_voltage_is_unknown(self):
        record = make_valid_record()
        record["battery"] = {"voltage": 11.8}
        self.assertEqual(
            error_pairs(validate_record(record)),
            {("battery.voltage_v", "missing"), ("battery.voltage", "unknown_field")},
        )

    def test_integer_float_zero_and_negative_readings_are_accepted(self):
        record = make_valid_record()
        record["soil"]["nitrogen"] = 0
        record["soil"]["temperature"] = -5.5
        record["air"]["pressure"] = 1013.25
        record["battery"]["voltage_v"] = 9
        self.assertTrue(validate_record(record).valid)

    def test_no_range_rule_is_applied_to_readings(self):
        record = make_valid_record()
        record["soil"]["moisture"] = 1e9
        record["air"]["humidity"] = -1e9
        self.assertTrue(validate_record(record).valid)

    def test_invalid_measurement_values_are_rejected(self):
        bad_values = ("12", True, False, [], {}, float("nan"), float("inf"), float("-inf"))
        for bad in bad_values:
            record = make_valid_record()
            record["soil"]["moisture"] = bad
            result = validate_record(record)
            self.assertFalse(result.valid, repr(bad))
            self.assertIn(("soil.moisture", "wrong_type"), error_pairs(result))

    def test_nan_and_infinity_are_rejected_in_every_section(self):
        for section, key in (("soil", "nitrogen"), ("air", "temperature"), ("battery", "voltage_v")):
            for bad in (float("nan"), float("inf"), float("-inf")):
                record = make_valid_record()
                record[section][key] = bad
                self.assertIn(
                    (section + "." + key, "wrong_type"), error_pairs(validate_record(record))
                )

    def test_calibrated_npk_fields_are_not_part_of_the_record(self):
        for key in ("nitrogen_kg_ha", "calibrated", "nitrogen_calibrated"):
            record = make_valid_record()
            record["soil"][key] = 1
            self.assertEqual(
                error_pairs(validate_record(record)), {("soil." + key, "unknown_field")}
            )
        record = make_valid_record()
        record["calibrated"] = {"nitrogen": 1}
        self.assertEqual(error_pairs(validate_record(record)), {("calibrated", "unknown_field")})

    def test_light_and_bh1750_fields_are_rejected(self):
        for path in (("light",), ("bh1750",), ("air", "light"), ("soil", "light"),
                     ("air", "lux"), ("sensor_status", "bh1750"), ("sensor_status", "light")):
            record = make_valid_record()
            target = record
            for part in path[:-1]:
                target = target[part]
            target[path[-1]] = 1
            self.assertEqual(
                error_pairs(validate_record(record)), {(".".join(path), "unknown_field")}, path
            )

    def test_battery_temperature_fields_are_rejected(self):
        for path in (("battery_temperature",), ("battery", "temperature"),
                     ("sensor_status", "battery_temperature")):
            record = make_valid_record()
            target = record
            for part in path[:-1]:
                target = target[part]
            target[path[-1]] = 1
            self.assertEqual(
                error_pairs(validate_record(record)), {(".".join(path), "unknown_field")}, path
            )


class TestSensorStatus(unittest.TestCase):
    def test_status_keys_are_exactly_the_approved_set(self):
        self.assertEqual(set(make_valid_record()["sensor_status"]), set(STATUS_KEYS))

    def test_both_valid_status_values_are_accepted_for_every_sensor(self):
        for sensor in STATUS_KEYS:
            record = make_valid_record()
            record["sensor_status"][sensor] = "fault"
            # a faulty sensor's readings may be null
            for section, key in {
                "npk": [("soil", "nitrogen"), ("soil", "phosphorus"), ("soil", "potassium")],
                "moisture": [("soil", "moisture")],
                "soil_temperature": [("soil", "temperature")],
                "air": [("air", "temperature"), ("air", "humidity"), ("air", "pressure")],
                "battery": [("battery", "voltage_v")],
            }[sensor]:
                record[section][key] = None
            self.assertTrue(validate_record(record).valid, sensor)
            record["sensor_status"][sensor] = "ok"
            result = validate_record(record)
            self.assertFalse(result.valid, sensor)  # ok sensor with null readings

    def test_invalid_status_values_are_rejected(self):
        for bad in ("OK", "Fault", "unavailable", "unknown", "degraded", "", True, None, 1):
            record = make_valid_record()
            record["sensor_status"]["moisture"] = bad
            self.assertIn(
                ("sensor_status.moisture", "wrong_value"), error_pairs(validate_record(record)), bad
            )

    def test_missing_status_object_and_missing_status_key(self):
        record = make_valid_record()
        del record["sensor_status"]
        self.assertIn(("sensor_status", "missing"), error_pairs(validate_record(record)))
        for key in STATUS_KEYS:
            record = make_valid_record()
            del record["sensor_status"][key]
            self.assertEqual(
                error_pairs(validate_record(record)), {("sensor_status." + key, "missing")}
            )

    def test_unknown_status_field_is_rejected(self):
        record = make_valid_record()
        record["sensor_status"]["camera"] = "ok"
        self.assertEqual(
            error_pairs(validate_record(record)), {("sensor_status.camera", "unknown_field")}
        )

    def test_fault_with_null_readings_is_valid(self):
        record = make_valid_record()
        record["sensor_status"]["npk"] = "fault"
        record["soil"]["nitrogen"] = None
        record["soil"]["phosphorus"] = None
        record["soil"]["potassium"] = None
        self.assertTrue(validate_record(record).valid)

    def test_all_sensors_faulted_is_still_a_valid_record(self):
        record = make_valid_record()
        for key in record["sensor_status"]:
            record["sensor_status"][key] = "fault"
        for key in record["soil"]:
            record["soil"][key] = None
        for key in record["air"]:
            record["air"][key] = None
        record["battery"]["voltage_v"] = None
        self.assertTrue(validate_record(record).valid)

    def test_a_fault_flag_never_by_itself_invalidates_the_record(self):
        record = make_valid_record()
        record["sensor_status"]["battery"] = "fault"
        self.assertTrue(validate_record(record).valid)

    def test_ok_sensor_with_null_reading_is_inconsistent(self):
        record = make_valid_record()
        record["air"]["humidity"] = None
        self.assertEqual(
            error_pairs(validate_record(record)),
            {("air.humidity", "inconsistent_status")},
        )

    def test_status_flag_covers_only_its_own_readings(self):
        record = make_valid_record()
        record["sensor_status"]["npk"] = "fault"
        record["soil"]["nitrogen"] = None
        record["soil"]["phosphorus"] = None
        record["soil"]["potassium"] = None
        record["soil"]["moisture"] = None  # moisture sensor is still "ok"
        self.assertEqual(
            error_pairs(validate_record(record)), {("soil.moisture", "inconsistent_status")}
        )


SENSOR_READING_PATHS = {
    "npk": (("soil", "nitrogen"), ("soil", "phosphorus"), ("soil", "potassium")),
    "moisture": (("soil", "moisture"),),
    "soil_temperature": (("soil", "temperature"),),
    "air": (("air", "temperature"), ("air", "humidity"), ("air", "pressure")),
    "battery": (("battery", "voltage_v"),),
}


class TestOkAndFaultReadingMatrix(unittest.TestCase):
    """MD-4: an "ok" sensor needs finite numeric readings; a "fault" sensor's
    readings may be null or a finite number, and never invalidate the record."""

    def make_record(self, sensor, status, value):
        record = make_valid_record()
        record["sensor_status"][sensor] = status
        for section, key in SENSOR_READING_PATHS[sensor]:
            record[section][key] = value
        return record

    def test_ok_sensor_with_numeric_readings_is_valid(self):
        for sensor in STATUS_KEYS:
            for value in (0, 12.5, -3):
                result = validate_record(self.make_record(sensor, "ok", value))
                self.assertTrue(result.valid, (sensor, value))

    def test_ok_sensor_with_null_readings_is_invalid(self):
        for sensor in STATUS_KEYS:
            result = validate_record(self.make_record(sensor, "ok", None))
            self.assertFalse(result.valid, sensor)
            expected = {
                (section + "." + key, "inconsistent_status")
                for section, key in SENSOR_READING_PATHS[sensor]
            }
            self.assertEqual(error_pairs(result), expected, sensor)

    def test_fault_sensor_with_null_readings_is_valid(self):
        for sensor in STATUS_KEYS:
            result = validate_record(self.make_record(sensor, "fault", None))
            self.assertTrue(result.valid, sensor)

    def test_fault_sensor_with_numeric_readings_is_valid(self):
        for sensor in STATUS_KEYS:
            for value in (0, 12.5, -3):
                result = validate_record(self.make_record(sensor, "fault", value))
                self.assertTrue(result.valid, (sensor, value))

    def test_non_finite_readings_are_invalid_for_both_statuses(self):
        for sensor in STATUS_KEYS:
            for status in ("ok", "fault"):
                for bad in (float("nan"), float("inf"), float("-inf")):
                    result = validate_record(self.make_record(sensor, status, bad))
                    self.assertFalse(result.valid, (sensor, status, bad))
                    for section, key in SENSOR_READING_PATHS[sensor]:
                        self.assertIn(
                            (section + "." + key, "wrong_type"), error_pairs(result)
                        )

    def test_fault_sensors_do_not_invalidate_the_record(self):
        # every sensor faulty, mixing null and numeric readings
        record = make_valid_record()
        for index, sensor in enumerate(STATUS_KEYS):
            record["sensor_status"][sensor] = "fault"
            for section, key in SENSOR_READING_PATHS[sensor]:
                record[section][key] = None if index % 2 == 0 else 1.5
        result = validate_record(record)
        self.assertTrue(result.valid)
        self.assertEqual(result.errors, ())
        self.assertEqual(result.record, record)


class TestConfidence(unittest.TestCase):
    def test_minimum_boundary_is_valid(self):
        record = make_valid_record()
        record["confidence"] = 0
        self.assertTrue(validate_record(record).valid)

    def test_maximum_boundary_is_valid(self):
        record = make_valid_record()
        record["confidence"] = 100
        self.assertTrue(validate_record(record).valid)

    def test_fractional_values_inside_the_range_are_valid(self):
        for ok in (0.5, 99.9, 70):
            record = make_valid_record()
            record["confidence"] = ok
            self.assertTrue(validate_record(record).valid, ok)

    def test_values_outside_the_range_are_rejected(self):
        for bad in (-0.1, -1, 100.1, 101, 1000):
            record = make_valid_record()
            record["confidence"] = bad
            self.assertEqual(
                error_pairs(validate_record(record)), {("confidence", "out_of_range")}, bad
            )

    def test_non_numeric_confidence_is_rejected(self):
        for bad in ("85", True, False, float("nan"), float("inf"), [], {}):
            record = make_valid_record()
            record["confidence"] = bad
            self.assertIn(("confidence", "wrong_type"), error_pairs(validate_record(record)))

    def test_null_confidence_is_accepted_only_with_null_label_and_photo(self):
        record = make_valid_record()
        record["leaf_label"] = None
        record["confidence"] = None
        record["photo_id"] = None
        self.assertTrue(validate_record(record).valid)

        record = make_valid_record()
        record["confidence"] = None
        self.assertEqual(error_pairs(validate_record(record)), {("leaf", "inconsistent_leaf")})

        record = make_valid_record()
        record["leaf_label"] = None
        record["photo_id"] = None
        self.assertEqual(error_pairs(validate_record(record)), {("leaf", "inconsistent_leaf")})


class TestLeafFields(unittest.TestCase):
    def test_all_three_populated_is_valid(self):
        self.assertTrue(validate_record(make_valid_record()).valid)

    def test_all_three_null_is_valid(self):
        record = make_valid_record()
        for key in LEAF_KEYS:
            record[key] = None
        self.assertTrue(validate_record(record).valid)

    def test_only_one_populated_is_rejected(self):
        for populated in LEAF_KEYS:
            record = make_valid_record()
            for key in LEAF_KEYS:
                if key != populated:
                    record[key] = None
            self.assertEqual(
                error_pairs(validate_record(record)), {("leaf", "inconsistent_leaf")}, populated
            )

    def test_only_two_populated_is_rejected(self):
        for null_key in LEAF_KEYS:
            record = make_valid_record()
            record[null_key] = None
            self.assertEqual(
                error_pairs(validate_record(record)), {("leaf", "inconsistent_leaf")}, null_key
            )

    def test_unclear_is_an_ordinary_label(self):
        record = make_valid_record()
        record["leaf_label"] = "unclear"
        record["confidence"] = 55
        result = validate_record(record)
        self.assertTrue(result.valid)
        self.assertEqual(result.record["leaf_label"], "unclear")

    def test_unclear_with_boundary_confidence_is_valid(self):
        for confidence in (0, 100):
            record = make_valid_record()
            record["leaf_label"] = "unclear"
            record["confidence"] = confidence
            self.assertTrue(validate_record(record).valid, confidence)

    def test_empty_or_non_string_leaf_label_and_photo_id_are_rejected(self):
        for key in ("leaf_label", "photo_id"):
            for bad in ("", 3, True, [], {}):
                record = make_valid_record()
                record[key] = bad
                self.assertIn((key, "wrong_type"), error_pairs(validate_record(record)), bad)

    def test_no_camera_status_field_exists(self):
        record = make_valid_record()
        record["camera_status"] = "ok"
        self.assertEqual(
            error_pairs(validate_record(record)), {("camera_status", "unknown_field")}
        )


class TestUnknownFieldsAndErrorCollection(unittest.TestCase):
    def test_unknown_fields_are_rejected_at_every_level(self):
        record = make_valid_record()
        record["extra"] = 1
        record["soil"]["light"] = 5
        record["air"]["wind"] = 5
        record["battery"]["temperature"] = 5
        record["versions"]["hardware"] = "x"
        record["sensor_status"]["camera"] = "ok"
        result = validate_record(record)
        self.assertFalse(result.valid)
        self.assertEqual(
            error_pairs(result),
            {
                ("extra", "unknown_field"),
                ("soil.light", "unknown_field"),
                ("air.wind", "unknown_field"),
                ("battery.temperature", "unknown_field"),
                ("versions.hardware", "unknown_field"),
                ("sensor_status.camera", "unknown_field"),
            },
        )

    def test_multiple_errors_are_collected(self):
        record = make_valid_record()
        del record["rover_id"]
        record["confidence"] = 500
        record["extra"] = True
        record["sequence_number"] = True
        record["soil"]["moisture"] = "wet"
        self.assertEqual(
            error_pairs(validate_record(record)),
            {
                ("rover_id", "missing"),
                ("confidence", "out_of_range"),
                ("extra", "unknown_field"),
                ("sequence_number", "wrong_type"),
                ("soil.moisture", "wrong_type"),
            },
        )

    def test_rejected_result_has_no_record(self):
        record = make_valid_record()
        record["extra"] = 1
        result = validate_record(record)
        self.assertFalse(result.valid)
        self.assertIsNone(result.record)


class TestImmutabilityAndDeterminism(unittest.TestCase):
    def test_candidate_is_not_modified(self):
        for mutate in (
            lambda r: None,
            lambda r: r.update(extra=1),
            lambda r: r.pop("time"),
            lambda r: r["soil"].update(moisture=float("nan")),
            lambda r: r.update(confidence=500),
        ):
            record = make_valid_record()
            mutate(record)
            snapshot = copy.deepcopy(record)
            validate_record(record)
            self.assertEqual(repr(record), repr(snapshot))

    def test_valid_result_is_an_independent_copy(self):
        record = make_valid_record()
        result = validate_record(record)
        self.assertIsNot(result.record, record)
        self.assertIsNot(result.record["soil"], record["soil"])
        result.record["soil"]["moisture"] = 99
        self.assertEqual(record["soil"]["moisture"], 40)

    def test_repeated_validation_is_deterministic(self):
        record = make_valid_record()
        record["confidence"] = 200
        record["extra"] = 1
        self.assertEqual(validate_record(record), validate_record(record))
        valid = make_valid_record()
        self.assertEqual(validate_record(valid), validate_record(valid))


if __name__ == "__main__":
    unittest.main()
