"""Nelavi prototype-side historical Record store (SQLite, standard library).

Implements the owner-approved storage decisions ST-1 to ST-3 in
ARCHITECTURE_DECISIONS.md. This is a prototype store. It does not replace the
rover's LittleFS, the phone's IndexedDB, upload synchronisation or any future
production database, and it does not resolve any frozen contract item.

History order is the storage-generated row id. Record time, sequence_number
and rover_id + sequence_number are never used for ordering or uniqueness, so
duplicate Records are allowed. Nothing is ever deleted. Records are validated
by the existing Record Core before they are stored; historical reads do not
revalidate them.
"""

import json
import os
import sqlite3
from dataclasses import dataclass

from record.validation import validate_record

_SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rover_id TEXT NOT NULL,
    record_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS records_rover_id_id ON records (rover_id, id);
"""


class StorageError(Exception):
    """Base class for storage errors."""


class StoreClosedError(StorageError):
    pass


class RecordNotFoundError(StorageError):
    pass


class InvalidRecordError(StorageError):
    """A Record failed Record Core validation and was not stored."""

    def __init__(self, errors):
        super().__init__("record failed validation: %r" % (tuple(errors),))
        self.errors = tuple(errors)


class StorageCorruptionError(StorageError):
    """A stored row cannot be decoded as a JSON object."""

    def __init__(self, row_id, reason):
        super().__init__("storage row %s is corrupted: %s" % (row_id, reason))
        self.row_id = row_id
        self.reason = reason


@dataclass(frozen=True)
class StoredRecord:
    id: int
    record: dict


def _reject_constant(name):
    raise ValueError("%s is not valid JSON" % name)


def _decode(row_id, text):
    try:
        value = json.loads(text, parse_constant=_reject_constant)
    except (ValueError, TypeError) as exc:
        raise StorageCorruptionError(row_id, "stored text is not valid JSON") from exc
    if not isinstance(value, dict):
        raise StorageCorruptionError(row_id, "stored JSON is not an object")
    return value


class RecordStore:
    """Stores validated canonical Records in a caller-supplied SQLite database.

    `path` is a file path or ":memory:". There is no default path.
    """

    def __init__(self, path):
        path = os.fspath(path)
        if not isinstance(path, str):
            raise TypeError("path must be a string or path-like object")
        if path == "":
            raise ValueError("path must not be empty")
        self._conn = sqlite3.connect(path)
        self._conn.executescript(_SCHEMA)

    # ------------------------------------------------------- lifecycle
    def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def _connection(self):
        if self._conn is None:
            raise StoreClosedError("the store is closed")
        return self._conn

    # ----------------------------------------------------------- write
    def add(self, record):
        """Validate and store one Record. Returns its storage id.

        Raises InvalidRecordError (with the validator's errors) for an invalid
        Record; nothing is stored in that case. The input is never modified.
        """
        conn = self._connection()
        result = validate_record(record)
        if not result.valid:
            raise InvalidRecordError(result.errors)
        canonical = result.record  # an independent copy
        text = json.dumps(
            canonical, allow_nan=False, sort_keys=True, separators=(",", ":")
        )
        with conn:  # one atomic transaction
            cursor = conn.execute(
                "INSERT INTO records (rover_id, record_json) VALUES (?, ?)",
                (canonical["rover_id"], text),
            )
        return cursor.lastrowid

    # ------------------------------------------------------------ read
    def get(self, row_id):
        """Return the StoredRecord with this storage id."""
        conn = self._connection()
        row = conn.execute(
            "SELECT id, record_json FROM records WHERE id = ?", (row_id,)
        ).fetchone()
        if row is None:
            raise RecordNotFoundError("no record with storage id %r" % (row_id,))
        return StoredRecord(row[0], _decode(row[0], row[1]))

    def list_records(self, rover_id=None, limit=None, after_id=None):
        """Return StoredRecords in storage-id order (ascending).

        rover_id: only Records of this rover.
        limit: at most this many Records.
        after_id: only Records with a storage id greater than this.
        """
        conn = self._connection()
        clauses, params = [], []
        if rover_id is not None:
            if not isinstance(rover_id, str):
                raise ValueError("rover_id must be a string")
            clauses.append("rover_id = ?")
            params.append(rover_id)
        if after_id is not None:
            if isinstance(after_id, bool) or not isinstance(after_id, int):
                raise ValueError("after_id must be an integer")
            clauses.append("id > ?")
            params.append(after_id)
        sql = "SELECT id, record_json FROM records"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id ASC"
        if limit is not None:
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
                raise ValueError("limit must be a non-negative integer")
            sql += " LIMIT ?"
            params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return [StoredRecord(row_id, _decode(row_id, text)) for row_id, text in rows]

    def latest(self, rover_id=None):
        """Return the most recently stored StoredRecord, or None if there is none."""
        conn = self._connection()
        sql = "SELECT id, record_json FROM records"
        params = []
        if rover_id is not None:
            if not isinstance(rover_id, str):
                raise ValueError("rover_id must be a string")
            sql += " WHERE rover_id = ?"
            params.append(rover_id)
        sql += " ORDER BY id DESC LIMIT 1"
        row = conn.execute(sql, params).fetchone()
        if row is None:
            return None
        return StoredRecord(row[0], _decode(row[0], row[1]))
