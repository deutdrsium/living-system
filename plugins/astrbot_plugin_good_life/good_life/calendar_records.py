"""Internal read access to staged calendar data; not an authorization boundary."""

from __future__ import annotations

import json

from .storage import StorageReader


class CalendarTransitionPending(Exception):
    code = "life_calendar_pending"


class StagedCalendarRecords:
    def __init__(self, source, *, spending=None, classes=None):
        self._reader = StorageReader(source)
        self.spending = spending
        self.classes = classes

    def dispatch(self, operation, **request):
        if operation == "planned_spending":
            return self.spending.read(**request) if self.spending is not None else None
        if operation == "class_sessions":
            return self.classes(**request) if self.classes is not None else None
        if operation not in {"get", "get_raw", "list", "version", "receipt", "by_reminder", "memory_snapshot"}:
            raise ValueError("unknown calendar read operation")
        keys = request.get("owner_keys", ()) if operation == "by_reminder" else (request.get("owner_key"),)
        keys = tuple(dict.fromkeys(keys))
        if operation == "by_reminder":
            request["owner_keys"] = keys
        with self._reader.locked():
            present = self._reader.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                       ("life_prepared_commands",))
            if operation == "memory_snapshot":
                if request:
                    raise ValueError("memory snapshot takes no arguments")
                if present and self._reader.one("""SELECT 1 FROM life_prepared_commands
                    WHERE state='committed' AND kind IN ('calendar.change','calendar.batch','state.change') LIMIT 1""", ()):
                    raise CalendarTransitionPending("life change awaits host confirmation")
                return self.memory_snapshot()
            if present and keys:
                placeholders = ",".join("?" for _ in keys)
                pending = self._reader.one(f"""SELECT 1 FROM life_prepared_commands WHERE state='committed' AND (
                    (kind='calendar.change' AND json_extract(payload_json, '$.storage_key') IN ({placeholders})) OR
                    (kind='calendar.batch' AND EXISTS (SELECT 1 FROM json_each(payload_json, '$.storage_keys')
                        WHERE value IN ({placeholders})))) LIMIT 1""", (*keys, *keys))
                if pending:
                    raise CalendarTransitionPending("calendar change awaits host confirmation")
            return getattr(self, operation)(**request)

    def memory_snapshot(self):
        # This preserves the existing installation-wide memory-page contract;
        # only the trusted companion host can call the in-process provider.
        records = self._reader.all("SELECT record_json FROM staged_calendar_records", ())
        states = self._reader.all("SELECT record_json FROM staged_role_state_records ORDER BY updated_at DESC LIMIT 1", ())
        return {"records": [dict(row) for row in records], "states": [dict(row) for row in states]}

    def get(self, *, owner_key, identifier):
        raw = self.get_raw(owner_key=owner_key, identifier=identifier)
        return json.loads(raw) if raw is not None else None

    def get_raw(self, *, owner_key, identifier):
        row = self._reader.one("""SELECT record_json FROM staged_calendar_records
            WHERE owner_key=? AND record_id=?""", (owner_key, identifier))
        return row[0] if row is not None else None

    def list(self, *, owner_key, after_id="", limit=100, kinds=None):
        if type(limit) is not int or not 1 <= limit <= 100 or type(after_id) is not str:
            raise ValueError("invalid calendar page")
        predicates, params = ["owner_key = ?", "record_id > ?"], [owner_key, after_id]
        if kinds is not None:
            kinds = tuple(kinds)
            if not kinds or any(type(kind) is not str or not kind for kind in kinds):
                raise ValueError("invalid calendar kind filter")
            predicates.append("json_extract(record_json, '$.kind') IN (" + ", ".join("?" for _ in kinds) + ")")
            params.extend(kinds)
        params.append(limit)
        rows = self._reader.all("SELECT record_json FROM staged_calendar_records WHERE "
            + " AND ".join(predicates) + " ORDER BY record_id LIMIT ?", tuple(params))
        return tuple(json.loads(row[0]) for row in rows)

    def version(self, *, owner_key, identifier, revision):
        row = self._reader.one("""SELECT record_json FROM staged_calendar_history
            WHERE owner_key=? AND record_id=? AND revision=?""", (owner_key, identifier, revision))
        if row is None:
            raise KeyError((identifier, revision))
        return json.loads(row[0])

    def receipt(self, *, owner_key, operation_id):
        row = self._reader.one("""SELECT result_json FROM staged_calendar_receipts
            WHERE owner_key=? AND operation_id=?""", (owner_key, operation_id))
        if row is None:
            raise KeyError(operation_id)
        return json.loads(row[0])

    def by_reminder(self, *, owner_keys, reminder_id):
        keys = tuple(dict.fromkeys(owner_keys))
        if not keys:
            return ()
        placeholders = ",".join("?" for _ in keys)
        rows = self._reader.all("SELECT record_json FROM staged_calendar_records WHERE owner_key IN ("
            + placeholders + ") AND json_extract(record_json, '$.reminder_id') = ?", (*keys, reminder_id))
        return tuple(json.loads(row[0]) for row in rows)
