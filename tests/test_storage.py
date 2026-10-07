import copy
import json
import os
import pathlib
import sqlite3
import tempfile
import unittest
from dataclasses import replace

from mock_rover.config import RoverConfig
from mock_rover.simulator import MockRover
from record.validation import ValidationError, validate_record
from storage.record_store import (
    InvalidRecordError,
    RecordNotFoundError,
    RecordStore,
    StorageCorruptionError,
    StoredRecord,
    StoreClosedError,
)


def mock_record(seed=0, fault=None, unclear=False, capture=False):
    rover = MockRover(replace(RoverConfig(), seed=seed))
    rover.startup()
    if fault:
        rover.inject_fault(fault)
    rover.insert_probe(depth_ok=True)
    rover.take_measurement()
    if unclear:
        rover.set_force_unclear(True)
    if capture or unclear:
        rover.capture_image()
    return rover.build_record()


def variant(record, **changes):
    changed = copy.deepcopy(record)
    changed.update(changes)
    return changed


class StoreBehaviour:
    """Behaviour every backend must show. Subclasses provide open_store()."""

    def open_store(self):
        raise NotImplementedError

    def setUp(self):
        self.store = self.open_store()

    # --- write and read --------------------------------------------------
    def test_valid_record_round_trip(self):
        record = mock_record(seed=1, capture=True)
        row_id = self.store.add(record)
        stored = self.store.get(row_id)
        self.assertIsInstance(stored, StoredRecord)
        self.assertEqual(stored.id, row_id)
        self.assertEqual(stored.record, record)
        self.assertIs(type(stored.record["sequence_number"]), int)
        self.assertIsInstance(stored.record["soil"]["nitrogen"], float)

    def test_returned_records_are_independent_copies(self):
        record = mock_record()
        row_id = self.store.add(record)
        got = self.store.get(row_id).record
        got["rover_id"] = "changed"
        self.assertEqual(self.store.get(row_id).record, record)

    def test_invalid_record_is_rejected_with_the_validator_errors(self):
        bad = variant(mock_record(), confidence=500)
        with self.assertRaises(InvalidRecordError) as caught:
            self.store.add(bad)
        errors = caught.exception.errors
        self.assertTrue(errors)
        self.assertTrue(all(isinstance(e, ValidationError) for e in errors))
        self.assertIn(("confidence", "out_of_range"), {(e.path, e.code) for e in errors})

    def test_non_object_and_unknown_field_records_are_rejected(self):
        for bad in (None, [], "record", variant(mock_record(), extra=1)):
            with self.assertRaises(InvalidRecordError):
                self.store.add(bad)

    def test_rejected_record_is_not_stored(self):
        with self.assertRaises(InvalidRecordError):
            self.store.add(variant(mock_record(), confidence=500))
        self.assertEqual(self.store.list_records(), [])
        self.assertIsNone(self.store.latest())
        good_id = self.store.add(mock_record())
        self.assertEqual(good_id, 1)  # the rejected write consumed no storage id

    def test_input_record_is_not_mutated(self):
        record = mock_record(capture=True)
        snapshot = copy.deepcopy(record)
        self.store.add(record)
        self.assertEqual(record, snapshot)
        bad = variant(record, confidence=500)
        bad_snapshot = copy.deepcopy(bad)
        with self.assertRaises(InvalidRecordError):
            self.store.add(bad)
        self.assertEqual(bad, bad_snapshot)

    def test_get_by_storage_id(self):
        first, second = mock_record(seed=1), mock_record(seed=2)
        id_first, id_second = self.store.add(first), self.store.add(second)
        self.assertEqual(self.store.get(id_second).record, second)
        self.assertEqual(self.store.get(id_first).record, first)

    def test_get_unknown_id_raises(self):
        with self.assertRaises(RecordNotFoundError):
            self.store.get(999)

    # --- ordering ------------------------------------------------------------
    def test_history_is_in_insertion_order_not_time_or_sequence_order(self):
        base = mock_record()
        records = [
            variant(base, sequence_number=seq, time=stamp)
            for seq, stamp in ((5, "sim:900"), (3, "sim:100"), (9, "sim:500"), (1, "sim:999"), (7, "sim:2"))
        ]
        ids = [self.store.add(record) for record in records]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(set(ids)), 5)
        self.assertEqual([s.record for s in self.store.list_records()], records)
        self.assertEqual([s.id for s in self.store.list_records()], ids)

    def test_duplicate_records_are_allowed(self):
        record = mock_record()
        first, second = self.store.add(record), self.store.add(record)
        self.assertNotEqual(first, second)
        listed = self.store.list_records()
        self.assertEqual(len(listed), 2)
        self.assertEqual(listed[0].record, listed[1].record)
        same_identity = [
            variant(record, rover_id="same", sequence_number=4) for _ in range(3)
        ]
        for item in same_identity:
            self.store.add(item)
        self.assertEqual(len(self.store.list_records(rover_id="same")), 3)

    # --- latest, filtering, paging ------------------------------------------
    def test_latest_record(self):
        self.assertIsNone(self.store.latest())
        base = mock_record()
        records = [variant(base, sequence_number=n) for n in range(3)]
        for record in records:
            self.store.add(record)
        self.assertEqual(self.store.latest().record, records[-1])

    def test_latest_record_for_a_rover(self):
        base = mock_record()
        a1 = variant(base, rover_id="a", sequence_number=1)
        b1 = variant(base, rover_id="b", sequence_number=1)
        a2 = variant(base, rover_id="a", sequence_number=2)
        for record in (a1, b1, a2):
            self.store.add(record)
        self.assertEqual(self.store.latest(rover_id="b").record, b1)
        self.assertEqual(self.store.latest(rover_id="a").record, a2)
        self.assertIsNone(self.store.latest(rover_id="nobody"))

    def test_rover_id_filter(self):
        base = mock_record()
        records = [
            variant(base, rover_id="a" if n % 2 == 0 else "b", sequence_number=n)
            for n in range(6)
        ]
        for record in records:
            self.store.add(record)
        a_only = [s.record for s in self.store.list_records(rover_id="a")]
        self.assertEqual(a_only, [r for r in records if r["rover_id"] == "a"])
        self.assertEqual(self.store.list_records(rover_id="nobody"), [])

    def test_limit(self):
        base = mock_record()
        records = [variant(base, sequence_number=n) for n in range(5)]
        for record in records:
            self.store.add(record)
        self.assertEqual([s.record for s in self.store.list_records(limit=2)], records[:2])
        self.assertEqual(self.store.list_records(limit=0), [])
        self.assertEqual(len(self.store.list_records(limit=50)), 5)

    def test_invalid_limit_is_rejected(self):
        for bad in (-1, True, "2", 1.5):
            with self.assertRaises(ValueError):
                self.store.list_records(limit=bad)

    def test_after_id_paging(self):
        base = mock_record()
        records = [variant(base, sequence_number=n) for n in range(5)]
        ids = [self.store.add(record) for record in records]
        rest = self.store.list_records(after_id=ids[1])
        self.assertEqual([s.record for s in rest], records[2:])
        self.assertEqual(self.store.list_records(after_id=ids[-1]), [])
        collected, cursor = [], 0
        while True:
            page = self.store.list_records(limit=2, after_id=cursor)
            if not page:
                break
            collected.extend(page)
            cursor = page[-1].id
        self.assertEqual([s.record for s in collected], records)
        self.assertEqual([s.id for s in collected], ids)

    def test_invalid_after_id_and_rover_id_are_rejected(self):
        for bad in (True, "1", 1.5):
            with self.assertRaises(ValueError):
                self.store.list_records(after_id=bad)
        for bad in (1, b"a", ["a"]):
            with self.assertRaises(ValueError):
                self.store.list_records(rover_id=bad)
            with self.assertRaises(ValueError):
                self.store.latest(rover_id=bad)

    def test_filters_combine(self):
        base = mock_record()
        ids = {}
        for n in range(6):
            rover = "a" if n % 2 == 0 else "b"
            ids[n] = self.store.add(variant(base, rover_id=rover, sequence_number=n))
        found = self.store.list_records(rover_id="a", after_id=ids[0], limit=1)
        self.assertEqual([s.record["sequence_number"] for s in found], [2])

    # --- Mock Rover Records ----------------------------------------------------
    def test_mock_rover_normal_record_round_trip(self):
        record = mock_record(seed=11, capture=True)
        row_id = self.store.add(record)
        self.assertEqual(self.store.get(row_id).record, record)

    def test_mock_rover_sensor_fault_record_round_trip(self):
        record = mock_record(seed=12, fault="moisture")
        self.assertEqual(record["sensor_status"]["moisture"], "fault")
        self.assertIsNone(record["soil"]["moisture"])
        row_id = self.store.add(record)
        stored = self.store.get(row_id).record
        self.assertEqual(stored, record)
        self.assertIsNone(stored["soil"]["moisture"])

    def test_mock_rover_unclear_record_round_trip(self):
        record = mock_record(seed=13, unclear=True)
        self.assertEqual(record["leaf_label"], "unclear")
        row_id = self.store.add(record)
        self.assertEqual(self.store.get(row_id).record, record)

    def test_stored_records_are_still_valid_record_core_records(self):
        row_id = self.store.add(mock_record(seed=14, capture=True))
        self.assertTrue(validate_record(self.store.get(row_id).record).valid)

    # --- lifecycle ---------------------------------------------------------------
    def test_closed_store_refuses_operations_and_close_is_repeatable(self):
        row_id = self.store.add(mock_record())
        self.store.close()
        self.store.close()  # a second close is harmless
        for call in (
            lambda: self.store.add(mock_record()),
            lambda: self.store.get(row_id),
            lambda: self.store.list_records(),
            lambda: self.store.latest(),
        ):
            with self.assertRaises(StoreClosedError):
                call()


class TestMemoryStore(StoreBehaviour, unittest.TestCase):
    def open_store(self):
        store = RecordStore(":memory:")
        self.addCleanup(store.close)
        return store

    def test_memory_stores_are_independent(self):
        other = self.open_store()
        self.store.add(mock_record())
        self.assertEqual(other.list_records(), [])
        self.assertEqual(len(self.store.list_records()), 1)


class TestFileStore(StoreBehaviour, unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)  # runs after the store is closed
        self.path = os.path.join(tmp.name, "history.db")
        super().setUp()

    def open_store(self):
        store = RecordStore(self.path)
        self.addCleanup(store.close)
        return store


class TestFilePersistenceAndCorruption(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = tmp.name
        self.path = os.path.join(self.directory, "history.db")

    def open_store(self):
        store = RecordStore(self.path)
        self.addCleanup(store.close)
        return store

    def raw(self, sql, params=()):
        connection = sqlite3.connect(self.path)
        try:
            with connection:
                return connection.execute(sql, params).fetchall()
        finally:
            connection.close()

    def tamper(self, row_id, text):
        self.raw("UPDATE records SET record_json = ? WHERE id = ?", (text, row_id))

    def filled_store(self, count=3):
        store = self.open_store()
        base = mock_record()
        records = [variant(base, sequence_number=n) for n in range(count)]
        ids = [store.add(record) for record in records]
        store.close()
        return records, ids

    # --- construction ------------------------------------------------------------
    def test_a_path_is_required_and_validated(self):
        with self.assertRaises(TypeError):
            RecordStore()
        with self.assertRaises(ValueError):
            RecordStore("")
        for bad in (None, 5, b"history.db"):
            with self.assertRaises(TypeError):
                RecordStore(bad)

    def test_path_like_objects_are_accepted(self):
        store = RecordStore(pathlib.Path(self.path))
        self.addCleanup(store.close)
        store.add(mock_record())
        self.assertTrue(os.path.exists(self.path))

    def test_only_the_supplied_database_file_is_created(self):
        self.assertEqual(os.listdir(self.directory), [])
        store = self.open_store()
        store.add(mock_record())
        store.close()
        self.assertEqual(os.listdir(self.directory), ["history.db"])

    def test_context_manager_closes_the_store(self):
        with RecordStore(self.path) as store:
            store.add(mock_record())
        with self.assertRaises(StoreClosedError):
            store.latest()

    # --- schema and stored text ------------------------------------------------------
    def test_schema_is_minimal(self):
        self.open_store().close()
        columns = [row[1] for row in self.raw("PRAGMA table_info(records)")]
        self.assertEqual(columns, ["id", "rover_id", "record_json"])
        indexes = {row[1] for row in self.raw("PRAGMA index_list(records)")}
        self.assertIn("records_rover_id_id", indexes)
        index_columns = [row[2] for row in self.raw("PRAGMA index_info(records_rover_id_id)")]
        self.assertEqual(index_columns, ["rover_id", "id"])

    def test_stored_text_is_strict_canonical_json(self):
        store = self.open_store()
        record = mock_record(capture=True)
        row_id = store.add(record)
        store.close()
        text = self.raw("SELECT record_json FROM records WHERE id = ?", (row_id,))[0][0]
        self.assertEqual(text, json.dumps(record, sort_keys=True, separators=(",", ":")))
        self.assertNotIn("NaN", text)
        self.assertNotIn("Infinity", text)
        rover_id = self.raw("SELECT rover_id FROM records WHERE id = ?", (row_id,))[0][0]
        self.assertEqual(rover_id, record["rover_id"])

    # --- persistence ----------------------------------------------------------------
    def test_records_persist_after_close_and_reopen(self):
        records, ids = self.filled_store(3)
        reopened = self.open_store()
        listed = reopened.list_records()
        self.assertEqual([s.record for s in listed], records)
        self.assertEqual([s.id for s in listed], ids)
        self.assertEqual(reopened.latest().record, records[-1])
        new_id = reopened.add(variant(records[0], sequence_number=99))
        self.assertGreater(new_id, ids[-1])
        reopened.close()
        again = self.open_store()
        self.assertEqual(len(again.list_records()), 4)

    # --- corruption ---------------------------------------------------------------------
    def test_invalid_json_is_reported_with_the_row_id(self):
        _, ids = self.filled_store(3)
        self.tamper(ids[1], "{not json")
        store = self.open_store()
        with self.assertRaises(StorageCorruptionError) as caught:
            store.get(ids[1])
        self.assertEqual(caught.exception.row_id, ids[1])
        self.assertIn(str(ids[1]), str(caught.exception))
        with self.assertRaises(StorageCorruptionError) as caught:
            store.list_records()
        self.assertEqual(caught.exception.row_id, ids[1])
        self.assertEqual(store.get(ids[0]).id, ids[0])  # other rows are still readable

    def test_corrupted_latest_row_is_reported(self):
        _, ids = self.filled_store(2)
        self.tamper(ids[-1], "###")
        with self.assertRaises(StorageCorruptionError) as caught:
            self.open_store().latest()
        self.assertEqual(caught.exception.row_id, ids[-1])

    def test_json_that_is_not_an_object_is_corruption(self):
        _, ids = self.filled_store(1)
        store = self.open_store()
        for text in ("[]", "[1, 2]", "5", "1.5", '"text"', "null", "true", ""):
            self.tamper(ids[0], text)
            with self.assertRaises(StorageCorruptionError, msg=text):
                store.get(ids[0])

    def test_nan_and_infinity_literals_are_corruption(self):
        _, ids = self.filled_store(1)
        store = self.open_store()
        for text in ("NaN", "-Infinity", '{"x": NaN}', '{"x": Infinity}'):
            self.tamper(ids[0], text)
            with self.assertRaises(StorageCorruptionError, msg=text):
                store.get(ids[0])

    def test_corrupted_rows_are_not_skipped_or_repaired(self):
        _, ids = self.filled_store(3)
        self.tamper(ids[1], "{broken")
        store = self.open_store()
        with self.assertRaises(StorageCorruptionError):
            store.list_records()
        stored = self.raw("SELECT record_json FROM records WHERE id = ?", (ids[1],))[0][0]
        self.assertEqual(stored, "{broken")  # untouched
        self.assertEqual(self.raw("SELECT COUNT(*) FROM records")[0][0], 3)

    def test_historical_reads_do_not_revalidate(self):
        _, ids = self.filled_store(2)
        self.tamper(ids[0], '{"legacy": true}')
        store = self.open_store()
        self.assertEqual(store.get(ids[0]).record, {"legacy": True})
        self.assertFalse(validate_record(store.get(ids[0]).record).valid)
        listed = store.list_records()
        self.assertEqual(listed[0].record, {"legacy": True})
        self.assertEqual(len(listed), 2)


if __name__ == "__main__":
    unittest.main()
