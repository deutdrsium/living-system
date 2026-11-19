"""Internal read access to staged activity data; not an authorization boundary."""

from __future__ import annotations

from .storage import LifeStorage, StorageReader


class ActivityTransitionPending(Exception):
    code = "life_activity_pending"


class StagedActivityRecords:
    """Return original JSON and receipt metadata for an already authorized host."""

    OPERATIONS = frozenset({"activity", "activity_revision", "activity_revision_evidence", "activity_revision_coverage", "activity_receipt",
                            "scan_activity_receipts", "recent_activity_receipts", "activity_revisions",
                            "activities_for_role", "context_activities", "work_activities", "artifact", "artifacts",
                            "timeline_activity_ceiling", "timeline_activity_candidates"})

    def __init__(self, source):
        self._reader = StorageReader(source)
        self._storage = source if isinstance(source, LifeStorage) else None

    def snapshot(self, operation):
        if operation != "snapshot" or self._storage is None:
            raise ValueError("activity snapshot is unavailable")
        return self._storage.read_snapshot()

    def dispatch(self, operation, **request):
        if operation not in self.OPERATIONS:
            raise ValueError("unknown activity read operation")
        # The raw methods are also used by offline checks and command validation.
        # The provider boundary must not expose a revision before host audit
        # completion; keep the fence and query under the same life-store lock.
        with self._reader.locked():
            present = self._reader.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                       ("life_prepared_commands",))
            if present:
                if operation == "scan_activity_receipts":
                    pending = self._reader.one("""SELECT 1 FROM life_prepared_commands
                        WHERE kind IN ('activity.transition','activity.delete') AND state='committed' LIMIT 1""", ())
                else:
                    pending = self._reader.one("""SELECT 1 FROM life_prepared_commands
                        WHERE kind IN ('activity.transition','activity.create','activity.delete') AND state='committed' AND owner_key=? LIMIT 1""",
                        (request.get("owner_key"),))
                if pending:
                    raise ActivityTransitionPending("activity transition awaits host confirmation")
            return getattr(self, operation)(**request)

    @staticmethod
    def _page(limit):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("activity page size must be between 1 and 100")

    def activity(self, *, owner_key, activity_id):
        row = self._reader.one("""SELECT record_json FROM staged_role_activities
            WHERE owner_key=? AND activity_id=?""", (owner_key, activity_id))
        if row is None:
            raise KeyError(activity_id)
        return row[0]

    def activity_revision(self, *, owner_key, activity_id, revision):
        if type(revision) is not int or revision < 1:
            raise ValueError("activity revision must be positive")
        row = self._reader.one("""SELECT record_json FROM staged_role_activity_revisions
            WHERE owner_key=? AND activity_id=? AND revision=?""", (owner_key, activity_id, revision))
        if row is None:
            raise KeyError((activity_id, revision))
        return row[0]

    def activity_revision_evidence(self, *, owner_key, activity_id, revision):
        row = self._reader.one("""SELECT provenance, operation_id FROM staged_role_activity_revisions
            WHERE owner_key=? AND activity_id=? AND revision=?""", (owner_key, activity_id, revision))
        return {"provenance": row[0], "operation_id": row[1]} if row is not None else None

    def activity_revision_coverage(self, *, owner_key, activity_id, revision):
        row = self._reader.one("""SELECT provenance, updated_at,
            (SELECT COUNT(*) FROM staged_role_activity_revisions p WHERE p.owner_key = r.owner_key
             AND p.activity_id = r.activity_id AND p.revision < r.revision) AS prior_count
            FROM staged_role_activity_revisions r WHERE owner_key = ? AND activity_id = ? AND revision = ?""",
            (owner_key, activity_id, revision))
        return {"provenance": row[0], "updated_at": row[1], "prior_count": row[2]} if row is not None else None

    def timeline_activity_ceiling(self, *, owner_key, scope_json):
        row = self._reader.one("""SELECT COALESCE(MAX(rowid), 0) FROM staged_role_activity_revisions
            WHERE owner_key = ? AND json_extract(record_json, '$.scope') = json(?)""", (owner_key, scope_json))
        return row[0]

    def timeline_activity_candidates(self, *, owner_key, scope_json, ceiling, end, start, after, limit):
        if type(ceiling) is not int or ceiling < 0 or type(limit) is not int or not 1 <= limit <= 65:
            raise ValueError("invalid timeline activity page")
        sql = """SELECT rowid, updated_at, activity_id, revision, record_json
            FROM staged_role_activity_revisions WHERE owner_key = ? AND json_extract(record_json, '$.scope') = json(?)
            AND rowid <= ? AND updated_at <= ?"""
        args = [owner_key, scope_json, ceiling, end]
        if start is not None:
            sql += " AND updated_at >= ?"
            args.append(start)
        if after is not None:
            sql += " AND (updated_at, 'activity', revision, activity_id) < (?, ?, ?, ?)"
            args.extend(after)
        sql += " ORDER BY updated_at DESC, revision DESC, activity_id DESC LIMIT ?"
        rows = self._reader.all(sql, (*args, limit))
        return tuple({"scan_id": row[0], "occurred_at": row[1], "kind": "activity", "identifier": row[2],
                      "revision": row[3], "record_json": row[4]} for row in rows)

    def activity_receipt(self, *, owner_key, operation_id):
        row = self._reader.one("""SELECT result_json FROM staged_role_life_operations
            WHERE owner_key=? AND operation_id=?""", (owner_key, operation_id))
        return row[0] if row is not None else None

    def activities_for_role(self, *, owner_key, state=None, limit=50, after_id=None):
        self._page(limit)
        rows = self._reader.all("""SELECT record_json FROM staged_role_activities
            WHERE owner_key=? AND (? IS NULL OR state=?) AND (? IS NULL OR activity_id > ?)
            ORDER BY activity_id LIMIT ?""", (owner_key, state, state, after_id, after_id, limit))
        return tuple(row[0] for row in rows)

    def context_activities(self, *, owner_key, scope_json, observed_at, limit=32):
        self._page(limit)
        rows = self._reader.all("""SELECT record_json FROM staged_role_activities
            WHERE owner_key=? AND json_extract(record_json, '$.scope') = json(?) AND updated_at <= ?
            ORDER BY CASE state WHEN 'active' THEN 0 WHEN 'paused' THEN 1
                WHEN 'completed' THEN 2 WHEN 'planned' THEN 3 ELSE 4 END,
                updated_at DESC, activity_id DESC LIMIT ?""", (owner_key, scope_json, observed_at, limit))
        return tuple(row[0] for row in rows)

    def work_activities(self, *, owner_key, states, kind, limit, oldest_first, context_key=None):
        if (type(limit) is not int or not 1 <= limit <= 200 or type(oldest_first) is not bool
                or type(states) not in (tuple, list)
                or any(type(state) is not str or not state for state in states)
                or (kind is not None and (type(kind) is not str or not kind))
                or (context_key is not None and (type(context_key) is not str or not context_key))):
            raise ValueError("invalid work activity query")
        state_clause = "" if not states else " AND state IN (" + ",".join("?" for _ in states) + ")"
        kind_clause = "" if kind is None else " AND json_extract(record_json, '$.work.kind') = ?"
        context_clause = "" if context_key is None else " AND json_extract(record_json, '$.work.context_key') = ?"
        order = " ASC" if oldest_first else " DESC"
        rows = self._reader.all("""SELECT record_json FROM staged_role_activities
            WHERE owner_key = ? AND json_extract(record_json, '$.scope.kind') = 'private'"""
            + state_clause + kind_clause + context_clause + " ORDER BY updated_at" + order + ", activity_id" + order + " LIMIT ?",
            (owner_key, *states, *((kind,) if kind else ()), *((context_key,) if context_key else ()), limit))
        return tuple(row[0] for row in rows)

    def artifact(self, *, owner_key, artifact_id):
        row = self._reader.one("""SELECT record_json FROM staged_role_artifacts
            WHERE owner_key=? AND artifact_id=?""", (owner_key, artifact_id))
        if row is None:
            raise KeyError(artifact_id)
        return row[0]

    def artifacts(self, *, owner_key, scope_key=None, limit=50, after_id=None):
        self._page(limit)
        rows = self._reader.all("""SELECT record_json FROM staged_role_artifacts
            WHERE owner_key=? AND (? IS NULL OR scope_key=?) AND (? IS NULL OR artifact_id > ?)
            ORDER BY artifact_id LIMIT ?""", (owner_key, scope_key, scope_key, after_id, after_id, limit))
        return tuple(row[0] for row in rows)

    def scan_activity_receipts(self, *, after, limit):
        if type(after) is not int or after < 0:
            raise ValueError("activity receipt cursor must be nonnegative")
        self._page(limit)
        rows = self._reader.all("""SELECT scan_id, operation_id, owner_key FROM staged_role_life_operations
            WHERE scan_id > ? ORDER BY scan_id LIMIT ?""", (after, limit))
        return tuple({"scan_id": row[0], "operation_id": row[1], "owner_key": row[2]} for row in rows)

    def recent_activity_receipts(self, *, owner_key, limit):
        self._page(limit)
        rows = self._reader.all("""SELECT operation_id FROM staged_role_life_operations
            WHERE owner_key=? ORDER BY scan_id DESC LIMIT ?""", (owner_key, limit))
        return tuple({"operation_id": row[0]} for row in rows)

    def activity_revisions(self, *, owner_key, after, at, before=None, limit=50):
        self._page(limit)
        params = [owner_key, after, at]
        continuation = ""
        if before is not None:
            continuation = " AND (updated_at, activity_id, revision) < (?, ?, ?)"
            params.extend(before)
        rows = self._reader.all("""SELECT owner_key, activity_id, revision, updated_at,
            provenance, operation_id, record_json FROM staged_role_activity_revisions
            WHERE owner_key=? AND updated_at > ? AND updated_at <= ?""" + continuation + """
            ORDER BY updated_at DESC, activity_id DESC, revision DESC LIMIT ?""", (*params, limit))
        names = ("owner_key", "activity_id", "revision", "updated_at", "provenance", "operation_id", "record_json")
        return tuple(dict(zip(names, row)) for row in rows)
