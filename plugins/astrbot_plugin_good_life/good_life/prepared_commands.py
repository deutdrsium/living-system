"""Internal, durable prepare/decision journal for future life-ledger writes.

The host must supply a verified, immutable decision.  This module does not
authorize users, dispatch external effects, or connect the journal to a live
executor.  Its callbacks belong to the life plugin and must only touch its DB.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import inspect
import json

from .storage import StorageReader


VERSION = "good-life.prepared-commands.v1"


def _name(value):
    if type(value) is not str or not value.strip() or value != value.strip() or len(value) > 256:
        raise ValueError("command identifiers must be nonempty strings of at most 256 characters")
    return value


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _synchronous(value):
    if inspect.isawaitable(value):
        if inspect.iscoroutine(value):
            value.close()
        raise ValueError("command callbacks must complete synchronously")
    return value


def _stored_request(row):
    return {"command_id": row["command_id"], "owner_key": row["owner_key"],
            "context_key": row["context_key"], "kind": row["kind"],
            "expected_revision": row["expected_revision"], "payload": json.loads(row["payload_json"])}


class _ValidationView:
    """Offer internal validators reads, without handing them a writable cursor."""

    def __init__(self, reader):
        self._reader = reader

    @staticmethod
    def _statement(statement):
        if type(statement) is not str or not statement.lstrip().upper().startswith("SELECT "):
            raise ValueError("prepare validation may only read")
        return statement

    def one(self, statement, parameters=()):
        return self._reader.one(self._statement(statement), parameters)

    def all(self, statement, parameters=()):
        return self._reader.all(self._statement(statement), parameters)


class PreparedCommands:
    """Hold each role until its local command and host receipt agree."""

    def __init__(self, storage):
        self.storage = storage
        self.reader = StorageReader(storage)

    def install(self):
        """Explicitly initialize the journal; never overwrite an unknown schema."""
        with self.storage.transaction() as connection:
            version = connection.execute("SELECT value FROM life_metadata WHERE key=?",
                                         ("prepared_commands_version",)).fetchone()
            present = connection.execute("SELECT 1 FROM sqlite_master WHERE name='life_prepared_commands'").fetchone()
            if version is None:
                if present:
                    raise ValueError("unclaimed prepared command table already exists")
                connection.execute("""CREATE TABLE life_prepared_commands (
                    command_id TEXT PRIMARY KEY, owner_key TEXT NOT NULL, context_key TEXT NOT NULL,
                    kind TEXT NOT NULL, expected_revision INTEGER NOT NULL CHECK(expected_revision >= 0),
                    payload_json TEXT NOT NULL, request_hash TEXT NOT NULL, prepared_at TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('prepared', 'committed', 'acknowledged', 'aborted')),
                    decision_id TEXT UNIQUE, decision_hash TEXT, result_json TEXT)""")
                connection.execute("""CREATE UNIQUE INDEX life_one_prepared_command_per_role
                    ON life_prepared_commands(owner_key) WHERE state IN ('prepared', 'committed')""")
                connection.execute("INSERT INTO life_metadata VALUES (?, ?)", ("prepared_commands_version", VERSION))
            elif version[0] != VERSION or not present:
                raise ValueError("unsupported prepared command schema")
            else:
                fields = tuple(row[1] for row in connection.execute("PRAGMA table_info(life_prepared_commands)"))
                expected = ("command_id", "owner_key", "context_key", "kind", "expected_revision", "payload_json",
                            "request_hash", "prepared_at", "state", "decision_id", "decision_hash", "result_json")
                index = connection.execute("""SELECT sql FROM sqlite_master WHERE type='index'
                    AND name='life_one_prepared_command_per_role'""").fetchone()
                index_list = {row[1]: row for row in connection.execute("PRAGMA index_list(life_prepared_commands)")}
                role_index = index_list.get("life_one_prepared_command_per_role")
                index_fields = tuple(row[2] for row in connection.execute("PRAGMA index_info(life_one_prepared_command_per_role)"))
                expected_sql = ("create unique index life_one_prepared_command_per_role "
                                "on life_prepared_commands(owner_key) where state in ('prepared', 'committed')")
                if (fields != expected or index is None or role_index is None or not role_index[2] or not role_index[4]
                        or index_fields != ("owner_key",) or " ".join(index[0].lower().split()) != expected_sql):
                    raise ValueError("prepared command schema is incomplete")

    def prepare(self, *, command_id, owner_key, context_key, kind, expected_revision, payload, prepared_at, validate):
        command_id, owner_key, context_key, kind = map(_name, (command_id, owner_key, context_key, kind))
        if type(expected_revision) is not int or expected_revision < 0 or type(payload) is not dict:
            raise ValueError("command requires a revision and object payload")
        if not isinstance(prepared_at, datetime) or prepared_at.utcoffset() is None or not callable(validate):
            raise ValueError("command requires an aware timestamp and local validation")
        request = {"command_id": command_id, "owner_key": owner_key, "context_key": context_key,
                   "kind": kind, "expected_revision": expected_revision, "payload": payload}
        request_hash, payload_json = _hash(request), _json(payload)
        timestamp = prepared_at.astimezone(timezone.utc).isoformat()
        with self.storage.transaction() as connection:
            existing = connection.execute("SELECT request_hash, state FROM life_prepared_commands WHERE command_id=?",
                                          (command_id,)).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise ValueError("command identifier conflicts with the original request")
                return {"request_hash": request_hash, "state": existing["state"]}
            pending = connection.execute("""SELECT 1 FROM life_prepared_commands
                WHERE owner_key=? AND state IN ('prepared', 'committed')""",
                                         (owner_key,)).fetchone()
            if pending is not None:
                raise ValueError("another command for this role awaits a decision")
            # Validation is read-only and receives a detached copy of the request.
            if _synchronous(validate(_ValidationView(self.reader), json.loads(_json(request)))) is not True:
                raise ValueError("life command validation failed")
            connection.execute("""INSERT INTO life_prepared_commands
                (command_id, owner_key, context_key, kind, expected_revision, payload_json, request_hash, prepared_at, state)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'prepared')""",
                (command_id, owner_key, context_key, kind, expected_revision, payload_json, request_hash, timestamp))
        return {"request_hash": request_hash, "state": "prepared"}

    def resolve(self, decision, *, verify_host_decision, apply):
        """Apply a verified commit or abort once; local writes share the receipt transaction."""
        fields = {"command_id", "request_hash", "decision", "decision_id"}
        if type(decision) is not dict or set(decision) != fields:
            raise ValueError("invalid command decision")
        _name(decision["command_id"])
        _name(decision["decision_id"])
        if (type(decision["request_hash"]) is not str or len(decision["request_hash"]) != 64
                or decision["decision"] not in ("commit", "abort")
                or not callable(verify_host_decision) or not callable(apply)):
            raise ValueError("invalid command decision")
        # The host decision must already be durable and immutable.  Check it
        # outside the life transaction to avoid calling the host under our lock.
        if _synchronous(verify_host_decision(dict(decision))) is not True:
            raise ValueError("host decision is unavailable or unverified")
        decision_hash = _hash(decision)
        with self.storage.transaction() as connection:
            row = connection.execute("SELECT * FROM life_prepared_commands WHERE command_id=?",
                                     (decision["command_id"],)).fetchone()
            if row is None or row["request_hash"] != decision["request_hash"]:
                raise ValueError("decision does not match the prepared command")
            request = _stored_request(row)
            if _hash(request) != row["request_hash"]:
                raise ValueError("prepared command content changed")
            if row["state"] != "prepared":
                if row["decision_hash"] != decision_hash:
                    raise ValueError("command was already resolved by another decision")
                return {"state": row["state"], "result": json.loads(row["result_json"]) if row["result_json"] else None}
            used = connection.execute("SELECT 1 FROM life_prepared_commands WHERE decision_id=?",
                                      (decision["decision_id"],)).fetchone()
            if used is not None:
                raise ValueError("host decision identifier is already in use")
            result = None
            if decision["decision"] == "commit":
                result = _synchronous(apply(connection, request))
                if type(result) is not dict:
                    raise ValueError("local command effect must return an object")
            result_json = _json(result) if result is not None else None
            state = "committed" if decision["decision"] == "commit" else "aborted"
            connection.execute("""UPDATE life_prepared_commands SET state=?, decision_id=?, decision_hash=?, result_json=?
                WHERE command_id=? AND state='prepared'""",
                (state, decision["decision_id"], decision_hash, result_json, decision["command_id"]))
        return {"state": state, "result": result}

    def pending(self, *, limit=50, after=None):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("command page size must be between 1 and 100")
        if after is not None and (not isinstance(after, (tuple, list)) or len(after) != 2
                or any(type(value) is not str or not value for value in after)):
            raise ValueError("invalid command page cursor")
        sql = """SELECT command_id, owner_key, context_key, kind, expected_revision, request_hash,
            prepared_at, state, decision_id FROM life_prepared_commands
            WHERE state IN ('prepared', 'committed')"""
        args = []
        if after is not None:
            sql += " AND (prepared_at, command_id) > (?, ?)"
            args.extend(after)
        sql += " ORDER BY prepared_at, command_id LIMIT ?"
        rows = self.reader.all(sql, (*args, limit))
        names = ("command_id", "owner_key", "context_key", "kind", "expected_revision", "request_hash", "prepared_at",
                 "state", "decision_id")
        return tuple(dict(zip(names, row)) for row in rows)

    def verify_prepared(self, request):
        """Confirm the host's exact request is still reserved in this database."""
        fields = {"command_id", "owner_key", "context_key", "kind", "expected_revision", "payload"}
        if type(request) is not dict or set(request) != fields:
            return False
        try:
            request_hash = _hash(request)
            row = self.reader.one("SELECT * FROM life_prepared_commands WHERE command_id=?", (request["command_id"],))
            return (row is not None and row["state"] == "prepared" and row["request_hash"] == request_hash
                    and _hash(_stored_request(row)) == request_hash)
        except (KeyError, TypeError, ValueError):
            return False

    def recovery_request(self, *, command_id, owner_key, context_key, request_hash):
        """Recover the exact pending request for its original host context."""
        if any(type(value) is not str or not value or len(value) > 256
               for value in (command_id, owner_key, context_key, request_hash)):
            return None
        row = self.reader.one("SELECT * FROM life_prepared_commands WHERE command_id=?", (command_id,))
        if (row is None or row["state"] not in ("prepared", "committed") or row["owner_key"] != owner_key
                or row["context_key"] != context_key or row["request_hash"] != request_hash):
            return None
        try:
            request = _stored_request(row)
            return request if _hash(request) == request_hash else None
        except (KeyError, TypeError, ValueError):
            return None

    def verify_commit(self, decision):
        """Confirm a host decision has a durable local result before host completion."""
        fields = {"command_id", "request_hash", "decision", "decision_id"}
        if type(decision) is not dict or set(decision) != fields or decision.get("decision") != "commit":
            return False
        try:
            row = self.reader.one("SELECT * FROM life_prepared_commands WHERE command_id=?", (decision["command_id"],))
            return (row is not None and row["state"] in ("committed", "acknowledged")
                    and row["request_hash"] == decision["request_hash"] and row["decision_hash"] == _hash(decision)
                    and _hash(_stored_request(row)) == row["request_hash"])
        except (KeyError, TypeError, ValueError):
            return False

    def acknowledge(self, command_id, *, verify_host_completion, after_ack=None):
        """Release a committed role only after the host confirms its receipt."""
        _name(command_id)
        if not callable(verify_host_completion) or (after_ack is not None and not callable(after_ack)):
            raise ValueError("host completion verification is required")
        row = self.reader.one("""SELECT request_hash, state, decision_id FROM life_prepared_commands
            WHERE command_id=?""", (command_id,))
        if row is None or row["state"] not in ("committed", "acknowledged"):
            raise ValueError("command has no committed local result")
        evidence = {"command_id": command_id, "request_hash": row["request_hash"], "decision_id": row["decision_id"]}
        if _synchronous(verify_host_completion(dict(evidence))) is not True:
            raise ValueError("host completion is unavailable or unverified")
        with self.storage.transaction() as connection:
            current = connection.execute("SELECT * FROM life_prepared_commands WHERE command_id=?", (command_id,)).fetchone()
            if (current is None or current["request_hash"] != evidence["request_hash"]
                    or current["decision_id"] != evidence["decision_id"]
                    or current["state"] not in ("committed", "acknowledged")):
                raise ValueError("command changed before host completion")
            if current["state"] == "acknowledged":
                return False
            request = _stored_request(current)
            if _hash(request) != current["request_hash"]:
                raise ValueError("prepared command content changed")
            if after_ack is not None:
                if _synchronous(after_ack(connection, request, json.loads(current["result_json"]))) is not None:
                    raise ValueError("acknowledgement effect must return None")
            connection.execute("UPDATE life_prepared_commands SET state='acknowledged' WHERE command_id=?", (command_id,))
        return True
