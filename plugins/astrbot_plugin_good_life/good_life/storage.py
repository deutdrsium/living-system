"""Plugin-owned storage and a durable, per-consumer life-event outbox."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from threading import RLock, get_ident


VERSION = "good-life.storage.v1"
STAGED_ACTIVITY_RECENT_INDEX = """CREATE INDEX IF NOT EXISTS staged_role_activities_recent_idx
    ON staged_role_activities(owner_key, json_extract(record_json, '$.scope'),
        json_extract(record_json, '$.work.kind'), updated_at DESC, activity_id DESC)"""


class LifeStorage:
    def __init__(self, connection):
        self._connection = connection
        self._lock = RLock()
        self._writer_thread_id = None
        self._closed = False

    @classmethod
    def open(cls, directory):
        root = Path(directory).resolve()
        root.mkdir(parents=True, exist_ok=True)
        path = root / "good-life.sqlite3"
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            created = False
        else:
            os.close(fd)
            created = True
        connection = sqlite3.connect(path, isolation_level=None, timeout=5.0, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            if created:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("CREATE TABLE life_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                connection.execute("INSERT INTO life_metadata VALUES ('schema_version', ?)", (VERSION,))
                connection.execute("""CREATE TABLE life_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE, owner_key TEXT NOT NULL, context_key TEXT NOT NULL, kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL, content_hash TEXT NOT NULL, occurred_at TEXT NOT NULL
                )""")
                connection.execute("""CREATE TABLE life_event_deliveries (
                    sequence INTEGER NOT NULL REFERENCES life_events(sequence),
                    consumer TEXT NOT NULL, acknowledged_at TEXT,
                    PRIMARY KEY(sequence, consumer)
                )""")
                connection.execute("COMMIT")
            else:
                row = connection.execute(
                    "SELECT value FROM life_metadata WHERE key = 'schema_version'").fetchone()
                if row is None or row["value"] != VERSION:
                    raise ValueError("unrecognized good-life database version")
                required = {"life_metadata", "life_events", "life_event_deliveries"}
                found = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not required <= found:
                    raise ValueError("good-life database schema is incomplete")
                if "staged_role_activities" in found:
                    connection.execute(STAGED_ACTIVITY_RECENT_INDEX)
            return cls(connection)
        except (sqlite3.Error, ValueError) as error:
            if connection.in_transaction:
                connection.rollback()
            connection.close()
            raise ValueError("cannot open the good-life database") from error

    @contextmanager
    def transaction(self):
        with self._lock:
            if self._connection.in_transaction:
                raise ValueError("life transaction is already active")
            self._connection.execute("BEGIN IMMEDIATE")
            self._writer_thread_id = get_ident()
            try:
                yield self._connection
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
            finally:
                self._writer_thread_id = None

    @contextmanager
    def read_snapshot(self):
        """Pin life reads while a synchronous host snapshot is assembled."""
        with self._lock:
            if self._connection.in_transaction:
                raise ValueError("life transaction is already active")
            self._connection.execute("BEGIN")
            try:
                self._connection.execute("SELECT value FROM life_metadata WHERE key='schema_version'").fetchone()
                yield
            finally:
                self._connection.rollback()

    @staticmethod
    def _name(value):
        if type(value) is not str or not value.strip() or len(value) > 256:
            raise ValueError("event identifiers must be nonempty strings of at most 256 characters")
        return value

    def record_event(self, *, event_id, owner_key, context_key, kind, payload, occurred_at, consumers):
        if self._writer_thread_id != get_ident() or not self._connection.in_transaction:
            raise ValueError("a life event must be recorded in its activity transaction")
        event_id, owner_key, context_key, kind = map(self._name, (event_id, owner_key, context_key, kind))
        if type(payload) is not dict or not isinstance(occurred_at, datetime) or occurred_at.utcoffset() is None:
            raise ValueError("event payload and timezone-aware occurrence time are required")
        if not isinstance(consumers, (tuple, list)) or not consumers:
            raise ValueError("event needs at least one consumer")
        recipients = sorted({self._name(consumer) for consumer in consumers})
        if len(recipients) != len(consumers):
            raise ValueError("event consumers must be unique")
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        timestamp = occurred_at.astimezone(timezone.utc).isoformat()
        identity = json.dumps([event_id, owner_key, context_key, kind, payload_json, timestamp, recipients],
                              ensure_ascii=False, separators=(",", ":"))
        content_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        existing = self._connection.execute(
            "SELECT sequence, content_hash FROM life_events WHERE event_id = ?", (event_id,)).fetchone()
        if existing is not None:
            if existing["content_hash"] != content_hash:
                raise ValueError("event identifier conflicts with an existing event")
            return existing["sequence"]
        cursor = self._connection.execute("""INSERT INTO life_events
            (event_id, owner_key, context_key, kind, payload_json, content_hash, occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (event_id, owner_key, context_key, kind, payload_json, content_hash, timestamp))
        for consumer in recipients:
            self._connection.execute("INSERT INTO life_event_deliveries(sequence, consumer) VALUES (?, ?)",
                                     (cursor.lastrowid, consumer))
        return cursor.lastrowid

    def pending_events(self, consumer, *, limit=50, context_key=None, after_sequence=0):
        self._name(consumer)
        if context_key is not None:
            self._name(context_key)
        if (type(limit) is not int or not 1 <= limit <= 100
                or type(after_sequence) is not int or after_sequence < 0):
            raise ValueError("event page size must be between 1 and 100")
        with self._lock:
            rows = self._connection.execute("""SELECT e.sequence, e.event_id, e.owner_key, e.context_key, e.kind,
                    e.payload_json, e.content_hash, e.occurred_at
                FROM life_events e JOIN life_event_deliveries d ON d.sequence = e.sequence
                WHERE d.consumer = ? AND d.acknowledged_at IS NULL AND (? IS NULL OR e.context_key = ?)
                    AND e.sequence > ? ORDER BY e.sequence LIMIT ?""",
                (consumer, context_key, context_key, after_sequence, limit)).fetchall()
            return tuple(self._event(row) for row in rows)

    def _event(self, row):
        data = dict(row)
        data["payload"] = json.loads(data.pop("payload_json"))
        data["consumers"] = [item[0] for item in self._connection.execute(
            "SELECT consumer FROM life_event_deliveries WHERE sequence = ? ORDER BY consumer", (row["sequence"],))]
        return data

    def event_for_consumer(self, event_id, consumer):
        self._name(event_id)
        self._name(consumer)
        with self._lock:
            row = self._connection.execute("""SELECT e.sequence, e.event_id, e.owner_key, e.context_key, e.kind,
                    e.payload_json, e.content_hash, e.occurred_at FROM life_events e
                JOIN life_event_deliveries d ON d.sequence = e.sequence
                WHERE e.event_id = ? AND d.consumer = ?""", (event_id, consumer)).fetchone()
            return self._event(row) if row is not None else None

    def acknowledge(self, *, event_id, consumer, acknowledged_at):
        if self._writer_thread_id != get_ident() or not self._connection.in_transaction:
            raise ValueError("event acknowledgement requires a transaction")
        self._name(event_id)
        self._name(consumer)
        if not isinstance(acknowledged_at, datetime) or acknowledged_at.utcoffset() is None:
            raise ValueError("acknowledgement time must be timezone-aware")
        row = self._connection.execute("""SELECT d.sequence, d.acknowledged_at
            FROM life_event_deliveries d JOIN life_events e ON e.sequence = d.sequence
            WHERE e.event_id = ? AND d.consumer = ?""", (event_id, consumer)).fetchone()
        if row is None:
            return False
        if row["acknowledged_at"] is None:
            self._connection.execute("UPDATE life_event_deliveries SET acknowledged_at = ? WHERE sequence = ? AND consumer = ?",
                (acknowledged_at.astimezone(timezone.utc).isoformat(), row["sequence"], consumer))
        return True

    def close(self):
        with self._lock:
            if self._closed:
                return
            if self._connection.in_transaction:
                raise ValueError("cannot close life storage during an active transaction or snapshot")
            self._connection.close()
            self._closed = True


class StorageReader:
    """Serialize reads with the owning plugin transaction when one exists."""

    def __init__(self, source):
        if isinstance(source, LifeStorage):
            self._connection, self._lock = source._connection, source._lock
        else:
            self._connection, self._lock = source, RLock()

    @contextmanager
    def locked(self):
        with self._lock:
            yield

    def one(self, statement, parameters):
        with self._lock:
            return self._connection.execute(statement, parameters).fetchone()

    def all(self, statement, parameters):
        with self._lock:
            return self._connection.execute(statement, parameters).fetchall()
