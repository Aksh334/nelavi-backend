import ast
import copy
import dataclasses
import importlib
import inspect
import re
import unittest
from dataclasses import replace
from unittest import mock

from image_pipeline.reference import (
    CAVEATS,
    STATUS_INVALID_RECORD,
    STATUS_NO_PHOTO_REFERENCE,
    STATUS_PHOTO_REFERENCED,
    STATUSES,
    ReferenceResult,
    reference_photo,
)
from mock_rover.config import RoverConfig
from mock_rover.simulator import MockRover
from record.validation import validate_record
from storage.record_store import RecordStore, StoredRecord

RESULT_FIELDS = (
    "rover_id", "record_id", "sequence_number", "time", "photo_id", "leaf_label", "confidence",
    "versions", "status", "errors", "caveats", "context",
)
CAVEAT_CODES = (
    "no_photo_content", "photo_id_opaque", "photo_relation_unresolved",
    "confidence_semantics_unresolved", "mock_records_unmarked",
)


def mock_record(captured=True, unclear=False, seed=0):
    rover = MockRover(replace(RoverConfig(), seed=seed))
    rover.startup()
    rover.insert_probe(depth_ok=True)
    rover.take_measurement()
    if unclear:
        rover.set_force_unclear(True)
    if captured or unclear:
        rover.capture_image()
    return rover.build_record()


WITH_PHOTO = mock_record(captured=True)
NO_PHOTO = mock_record(captured=False)


def photo(**changes):
    record = copy.deepcopy(WITH_PHOTO)
    record.update(changes)
    return record


def no_photo(**changes):
    record = copy.deepcopy(NO_PHOTO)
    record.update(changes)
    return record


class TestStatuses(unittest.TestCase):
    def test_valid_photo_reference(self):
        result = reference_photo(photo())
        self.assertEqual(result.status, STATUS_PHOTO_REFERENCED)
        self.assertEqual(result.photo_id, WITH_PHOTO["photo_id"])
        self.assertEqual(result.leaf_label, WITH_PHOTO["leaf_label"])
        self.assertEqual(result.confidence, WITH_PHOTO["confidence"])
        self.assertEqual(result.errors, ())

    def test_valid_record_with_no_photo_reference(self):
        result = reference_photo(no_photo())
        self.assertEqual(result.status, STATUS_NO_PHOTO_REFERENCE)
        self.assertEqual((result.photo_id, result.leaf_label, result.confidence), (None, None, None))
        self.assertEqual(result.errors, ())
        self.assertEqual(result.rover_id, NO_PHOTO["rover_id"])  # the rest is still carried

    def test_exactly_three_statuses(self):
        self.assertEqual(STATUSES, ("photo_referenced", "no_photo_reference", "invalid_record"))

    def test_invalid_record(self):
        broken = photo()
        broken["unexpected"] = 1
        result = reference_photo(broken)
        self.assertEqual(result.status, STATUS_INVALID_RECORD)
        self.assertTrue(result.errors)
        self.assertEqual(result.errors[0].code, "unknown_field")

    def test_invalid_record_carries_no_untrusted_fields(self):
        broken = photo()
        broken["unexpected"] = 1
        result = reference_photo(broken)
        for name in ("rover_id", "sequence_number", "time", "photo_id", "leaf_label", "confidence", "versions"):
            self.assertIsNone(getattr(result, name), name)
        self.assertIsNone(result.record_id)

    def test_items_that_are_not_records_do_not_crash(self):
        for item in (None, 5, "record", [1], 3.5, b"bytes", object(), {}, {"photo_id": "x"}):
            result = reference_photo(item)
            self.assertEqual(result.status, STATUS_INVALID_RECORD, repr(item))
            self.assertTrue(result.errors, repr(item))

    def test_missing_fields_do_not_crash(self):
        for key in ("photo_id", "leaf_label", "confidence", "versions", "rover_id", "time", "sequence_number"):
            record = photo()
            del record[key]
            self.assertEqual(reference_photo(record).status, STATUS_INVALID_RECORD, key)

    def test_partial_leaf_triple_is_invalid(self):
        for changes in (
            {"photo_id": None},
            {"leaf_label": None},
            {"confidence": None},
            {"leaf_label": None, "confidence": None},
            {"leaf_label": None, "photo_id": None},
            {"confidence": None, "photo_id": None},
        ):
            result = reference_photo(photo(**changes))
            self.assertEqual(result.status, STATUS_INVALID_RECORD, repr(changes))
            self.assertIn("inconsistent_leaf", [e.code for e in result.errors], repr(changes))

    def test_invalid_confidence_is_rejected_by_record_core(self):
        for bad in (-0.1, 100.1, 150, -1, float("nan"), float("inf"), "90", True):
            result = reference_photo(photo(confidence=bad))
            self.assertEqual(result.status, STATUS_INVALID_RECORD, repr(bad))
            self.assertTrue(all(e.path == "confidence" for e in result.errors), repr(bad))

    def test_confidence_range_boundaries_follow_record_core(self):
        for value in (0, 0.0, 100, 100.0, 55.5):
            result = reference_photo(photo(confidence=value))
            self.assertEqual(result.status, STATUS_PHOTO_REFERENCED, repr(value))
            self.assertEqual(result.confidence, value)

    def test_invalid_records_are_those_record_core_rejects(self):
        for record in (photo(), no_photo(), photo(photo_id=""), photo(leaf_label=""), photo(time=""),
                       photo(sequence_number=True), photo(rover_id=5)):
            valid = validate_record(record).valid
            status = reference_photo(record).status
            self.assertEqual(status != STATUS_INVALID_RECORD, valid)

    def test_unclear_is_an_ordinary_label_not_a_status(self):
        result = reference_photo(mock_record(unclear=True))
        self.assertEqual(result.leaf_label, "unclear")
        self.assertEqual(result.status, STATUS_PHOTO_REFERENCED)
        self.assertNotIn("unclear", STATUSES)
        for label in ("unclear", "UNCLEAR", "mock_disease", "x"):
            self.assertEqual(reference_photo(photo(leaf_label=label)).leaf_label, label)


class TestOpaquePhotoId(unittest.TestCase):
    ODD_IDS = (
        "mock-photo-0-0001", "a/b.jpg", "../../etc/passwd", "photo id with spaces", " ",
        "ünï-✓-写真", "\U0001F600", "x" * 500, "UPPER_lower-123", "id:with;punct!@#$%^&*()",
        "0", "None", "null", "C:\\path\\file.png", "https://example.invalid/photo", "tab\tnewline\n",
    )

    def test_any_non_empty_string_is_carried_unchanged(self):
        for photo_id in self.ODD_IDS:
            result = reference_photo(photo(photo_id=photo_id))
            self.assertEqual(result.status, STATUS_PHOTO_REFERENCED, repr(photo_id))
            self.assertEqual(result.photo_id, photo_id)
            self.assertIs(type(result.photo_id), str)

    def test_empty_or_non_string_ids_are_rejected_only_by_record_core(self):
        for bad in ("", 5, 1.5, ["a"], {"a": 1}, True):
            self.assertEqual(reference_photo(photo(photo_id=bad)).status, STATUS_INVALID_RECORD, repr(bad))

    def test_duplicate_photo_ids_are_not_an_error(self):
        first = reference_photo(StoredRecord(1, photo(photo_id="same-id", sequence_number=1)))
        second = reference_photo(StoredRecord(2, photo(photo_id="same-id", sequence_number=2)))
        third = reference_photo(StoredRecord(3, photo(photo_id="same-id", sequence_number=2)))
        for result in (first, second, third):
            self.assertEqual((result.status, result.photo_id), (STATUS_PHOTO_REFERENCED, "same-id"))
        self.assertEqual([r.record_id for r in (first, second, third)], [1, 2, 3])

    def test_the_same_record_twice_is_not_deduplicated(self):
        record = photo()
        self.assertEqual(reference_photo(record), reference_photo(record))

    def test_the_photo_id_is_not_parsed_or_derived(self):
        a = reference_photo(photo(photo_id="mock-photo-0-0001"))
        b = reference_photo(photo(photo_id="mock-photo-9-9999", sequence_number=0))
        self.assertEqual(a.photo_id, "mock-photo-0-0001")
        self.assertEqual(b.photo_id, "mock-photo-9-9999")
        self.assertEqual(b.sequence_number, 0)  # nothing is derived from the ID


class TestTraceabilityFields(unittest.TestCase):
    def test_record_id_is_preserved_when_supplied(self):
        for record_id in (0, 1, 41, 10**12, -3):
            result = reference_photo(StoredRecord(record_id, photo()))
            self.assertEqual(result.record_id, record_id)

    def test_record_id_is_none_when_not_supplied(self):
        self.assertIsNone(reference_photo(photo()).record_id)
        self.assertIsNone(reference_photo(no_photo()).record_id)

    def test_record_id_is_never_invented(self):
        for bad_id in (True, False, None, "7", 7.0, [7]):
            self.assertIsNone(reference_photo(StoredRecord(bad_id, photo())).record_id, repr(bad_id))
        # an `id` field inside the Record itself is not an id, and is not a valid field
        self.assertIsNone(reference_photo(photo(id=5)).record_id)

    def test_record_id_survives_an_invalid_record(self):
        broken = photo()
        broken["unexpected"] = 1
        result = reference_photo(StoredRecord(9, broken))
        self.assertEqual((result.status, result.record_id), (STATUS_INVALID_RECORD, 9))

    def test_stored_record_like_objects(self):
        class Row:
            id = 12
            record = photo()

        self.assertEqual(reference_photo(Row()).record_id, 12)

    def test_sequence_number_is_copied_unchanged(self):
        for value in (0, 1, -5, 10**12, 7):
            result = reference_photo(photo(sequence_number=value))
            self.assertEqual(result.sequence_number, value)
            self.assertIs(type(result.sequence_number), int)

    def test_time_is_copied_unchanged(self):
        for value in ("sim:12.000", "not-a-time", "2026-10-08T00:00:00Z", "  ", "x"):
            self.assertEqual(reference_photo(photo(time=value)).time, value)

    def test_versions_are_copied_unchanged(self):
        versions = {"firmware": "f-1", "model": "m-?", "rules": "r.2", "calibration": "c"}
        result = reference_photo(photo(versions=versions))
        self.assertEqual(result.versions, versions)
        self.assertEqual(dict(result.versions), versions)
        self.assertEqual(list(result.versions), list(versions))

    def test_leaf_label_is_copied_unchanged(self):
        for label in ("mock_healthy", "mock_disease", "unclear", "anything at all", "Ünï"):
            self.assertEqual(reference_photo(photo(leaf_label=label)).leaf_label, label)

    def test_confidence_is_copied_unchanged(self):
        for value in (0, 55.0, 92.0, 81.5, 100, 70, 20, 70.0000001):
            result = reference_photo(photo(confidence=value))
            self.assertEqual(result.confidence, value)
            self.assertIs(type(result.confidence), type(value))

    def test_rover_id_is_copied_unchanged(self):
        for value in ("nelavi-mock-1", "other", "Ünï rover"):
            self.assertEqual(reference_photo(photo(rover_id=value)).rover_id, value)

    def test_time_sequence_and_versions_are_opaque(self):
        # decreasing, repeated and arbitrary values change nothing about the outcome
        records = [
            photo(sequence_number=9, time="b"), photo(sequence_number=9, time="b"),
            photo(sequence_number=1, time="zzz"), photo(sequence_number=-4, time="a"),
        ]
        results = [reference_photo(r) for r in records]
        self.assertEqual({r.status for r in results}, {STATUS_PHOTO_REFERENCED})
        self.assertEqual([r.sequence_number for r in results], [9, 9, 1, -4])
        self.assertEqual([r.time for r in results], ["b", "b", "zzz", "a"])

    def test_no_interpretation_is_applied_to_confidence(self):
        # the open "unclear" thresholds are not applied: a low or high value keeps its label
        low = reference_photo(photo(leaf_label="mock_healthy", confidence=1))
        high = reference_photo(photo(leaf_label="unclear", confidence=99))
        self.assertEqual((low.leaf_label, low.confidence, low.status), ("mock_healthy", 1, STATUS_PHOTO_REFERENCED))
        self.assertEqual((high.leaf_label, high.confidence, high.status), ("unclear", 99, STATUS_PHOTO_REFERENCED))


class TestResultContract(unittest.TestCase):
    def setUp(self):
        self.result = reference_photo(StoredRecord(5, photo()))

    def test_exact_field_set(self):
        self.assertEqual(tuple(f.name for f in dataclasses.fields(ReferenceResult)), RESULT_FIELDS)

    def test_result_is_frozen(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            self.result.status = STATUS_INVALID_RECORD
        with self.assertRaises(dataclasses.FrozenInstanceError):
            self.result.photo_id = "other"
        with self.assertRaises(TypeError):
            self.result.versions["firmware"] = "changed"
        with self.assertRaises(TypeError):
            self.result.context["photo_content"] = "available"
        self.assertIsInstance(self.result.caveats, tuple)
        self.assertIsInstance(self.result.errors, tuple)

    def test_the_result_does_not_alias_the_input(self):
        record = photo()
        result = reference_photo(record)
        record["versions"]["firmware"] = "changed-after"
        record["photo_id"] = "changed-after"
        self.assertEqual(result.versions["firmware"], WITH_PHOTO["versions"]["firmware"])
        self.assertEqual(result.photo_id, WITH_PHOTO["photo_id"])

    def test_every_result_carries_all_caveats(self):
        broken = photo()
        broken["unexpected"] = 1
        for item in (photo(), no_photo(), broken, None, StoredRecord(3, photo())):
            result = reference_photo(item)
            self.assertEqual(tuple(c.code for c in result.caveats), CAVEAT_CODES)
            self.assertEqual(result.caveats, CAVEATS)

    def test_caveat_content(self):
        text = {c.code: c.message.lower() for c in CAVEATS}
        self.assertIn("no photo content or pixels", text["no_photo_content"])
        self.assertIn("opaque", text["photo_id_opaque"])
        self.assertIn("three photos", text["photo_relation_unresolved"])
        self.assertIn("unresolved", text["photo_relation_unresolved"])
        self.assertIn("beyond the record core range check", text["confidence_semantics_unresolved"])
        self.assertIn("unresolved", text["confidence_semantics_unresolved"])
        self.assertIn("does not mark", text["mock_records_unmarked"])
        self.assertIn("mock", text["mock_records_unmarked"])

    def test_context_states_that_photo_content_is_unavailable(self):
        self.assertEqual(self.result.context["photo_content"], "unavailable")
        self.assertIn("not available", self.result.context["reason"])

    def test_the_result_has_no_judgment_or_provider_fields(self):
        for name in ("score", "probability", "severity", "health", "diagnosis", "failure", "prediction",
                     "provider", "bytes", "pixels", "image", "path", "url", "size", "width", "height"):
            self.assertFalse(hasattr(self.result, name), name)


class TestPurityAndDeterminism(unittest.TestCase):
    def test_repeated_calls_are_identical(self):
        for item in (photo(), no_photo(), StoredRecord(4, photo()), {"bad": 1}, None):
            self.assertEqual(reference_photo(item), reference_photo(item))

    def test_inputs_are_not_modified(self):
        broken = photo()
        broken["unexpected"] = 1
        items = [photo(), no_photo(), StoredRecord(4, photo()), broken, StoredRecord(5, broken)]
        snapshots = copy.deepcopy(items)
        for item in items:
            reference_photo(item)
        self.assertEqual(items, snapshots)

    def test_no_clock_randomness_network_files_or_database_are_used(self):
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
            result = reference_photo(photo())
        self.assertEqual(result.status, STATUS_PHOTO_REFERENCED)


class TestScopeProtection(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = importlib.import_module("image_pipeline.reference")
        cls.source = inspect.getsource(cls.module)
        cls.tree = ast.parse(cls.source)

    def test_module_imports_only_what_it_needs(self):
        imported = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module.split(".")[0])
        self.assertEqual(imported, {"dataclasses", "types", "record"})
        for banned in ("time", "datetime", "random", "secrets", "socket", "threading", "asyncio",
                       "sqlite3", "os", "json", "pathlib", "base64", "io", "urllib", "http", "ssl",
                       "mock_rover", "storage", "baseline", "anomaly", "machine_state", "energy",
                       "calibration", "PIL", "numpy", "cv2", "requests", "openai"):
            self.assertFalse(hasattr(self.module, banned), banned)

    def test_no_image_transport_or_provider_vocabulary_in_the_code(self):
        # The module docstring is the scope statement and says what is NOT done, so it is
        # left out; every identifier, string value and other docstring is checked.
        tree = ast.parse(self.source)
        tree.body = tree.body[1:]
        self.assertIsInstance(ast.parse(self.source).body[0].value, ast.Constant)
        text = ast.unparse(tree).lower()
        for term in ("jpeg", "jpg", "png", "base64", "resize", "normaliz", "dimension", "thumbnail",
                     "nvidia", "nebius", "tavily", "nemotron", "diagnos", "disease", "health",
                     "severity", "probab", "predict", "failure", "retriev", "upload", "download",
                     "transport", "websocket", "endpoint", "api key", "credential", "token"):
            self.assertNotIn(term, text, term)
        self.assertIsNone(re.search(r"\brul\b", text))

    def test_no_numeric_constants_exist_so_no_threshold_or_conversion_can(self):
        constants = [n.value for n in ast.walk(self.tree)
                     if isinstance(n, ast.Constant) and isinstance(n.value, (int, float))
                     and not isinstance(n.value, bool)]
        self.assertEqual(constants, [])

    def test_no_file_bytes_or_dynamic_code_names_are_used(self):
        names = {n.id for n in ast.walk(self.tree) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(self.tree) if isinstance(n, ast.Attribute)}
        for banned in ("open", "bytes", "bytearray", "memoryview", "eval", "exec", "compile",
                       "__import__", "read", "write", "encode", "decode", "sorted", "sort",
                       "strptime", "fromisoformat", "match", "split"):
            self.assertNotIn(banned, names, banned)

    def test_public_names_carry_no_unrelated_concepts(self):
        public = {name for name in dir(self.module) if not name.startswith("_")}
        for word in ("image", "pixel", "resize", "fetch", "send", "upload", "provider", "score",
                     "diagnos", "predict", "health", "vote", "majority", "threshold"):
            self.assertFalse(any(word in name.lower() for name in public), word)

    def test_statuses_and_context_do_not_name_unapproved_states(self):
        for word in ("fail", "camera", "transfer", "retriev", "upload", "storage", "capture"):
            for status in STATUSES:
                self.assertNotIn(word, status)

    def test_no_three_photo_logic_exists(self):
        # one Record gives one result with one photo_id; there is no list of photos anywhere
        result = reference_photo(photo())
        self.assertIsInstance(result.photo_id, str)
        self.assertNotIn("photos", {f.name for f in dataclasses.fields(ReferenceResult)})


class TestMockRoverIntegration(unittest.TestCase):
    ROWS = 24

    @classmethod
    def build(cls):
        rover = MockRover(replace(RoverConfig(), seed=5))
        rover.startup()
        store = RecordStore(":memory:")
        kinds = []
        for index in range(cls.ROWS):
            rover.drive(1.0, 1.0)
            rover.advance(1.0)
            rover.stop()
            rover.insert_probe(depth_ok=True)
            rover.take_measurement()
            kind = ("captured", "uncaptured", "unclear")[index % 3]
            if kind == "uncaptured":
                rover.clear_capture()
            else:
                rover.set_force_unclear(kind == "unclear")
                rover.capture_image()
            kinds.append(kind)
            store.add(rover.build_record())
            rover.retract_probe()
        return store, kinds

    def setUp(self):
        self.store, self.kinds = self.build()
        self.addCleanup(self.store.close)
        self.history = self.store.list_records()

    def test_there_are_enough_records_of_every_kind(self):
        self.assertGreaterEqual(len(self.history), 20)
        for kind in ("captured", "uncaptured", "unclear"):
            self.assertGreaterEqual(self.kinds.count(kind), 5)

    def test_every_mock_record_is_referenced_as_expected(self):
        for stored, kind in zip(self.history, self.kinds):
            record = stored.record
            result = reference_photo(stored)
            self.assertEqual(result.record_id, stored.id)
            self.assertEqual(result.errors, ())
            for name in ("rover_id", "sequence_number", "time", "photo_id", "leaf_label", "confidence"):
                self.assertEqual(getattr(result, name), record[name], name)
            self.assertEqual(result.versions, record["versions"])
            if kind == "uncaptured":
                self.assertEqual(result.status, STATUS_NO_PHOTO_REFERENCE)
                self.assertIsNone(result.photo_id)
            else:
                self.assertEqual(result.status, STATUS_PHOTO_REFERENCED)
                self.assertTrue(result.photo_id.startswith("mock-photo-5-"))
            if kind == "unclear":
                self.assertEqual(result.leaf_label, "unclear")
                self.assertEqual(result.status, STATUS_PHOTO_REFERENCED)

    def test_status_counts(self):
        statuses = [reference_photo(s).status for s in self.history]
        self.assertEqual(statuses.count(STATUS_NO_PHOTO_REFERENCE), self.kinds.count("uncaptured"))
        self.assertEqual(statuses.count(STATUS_PHOTO_REFERENCED), self.ROWS - self.kinds.count("uncaptured"))
        self.assertEqual(statuses.count(STATUS_INVALID_RECORD), 0)

    def test_plain_records_work_without_storage_ids(self):
        for stored in self.history:
            result = reference_photo(stored.record)
            self.assertIsNone(result.record_id)
            self.assertEqual(result.photo_id, stored.record["photo_id"])

    def test_the_store_and_the_records_are_not_changed(self):
        before = self.store.list_records()
        snapshots = copy.deepcopy(before)
        for stored in before:
            reference_photo(stored)
        self.assertEqual(before, snapshots)
        self.assertEqual(self.store.list_records(), before)

    def test_no_image_data_is_involved(self):
        for stored in self.history:
            result = reference_photo(stored)
            self.assertIsInstance(result.photo_id, (str, type(None)))
            self.assertNotIsInstance(result.photo_id, (bytes, bytearray))


if __name__ == "__main__":
    unittest.main()
