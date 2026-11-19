"""Scoped reads over imported browsing sessions and content receipts."""

from .storage import StorageReader


class StagedExposureRecords:
    def __init__(self, source):
        self._reader = StorageReader(source)

    def dispatch(self, operation, **request):
        if operation not in ("session", "previous_session", "exposure", "recent", "attempted"):
            raise ValueError("unknown browsing session read")
        return getattr(self, operation)(**request)

    @staticmethod
    def _record(row):
        return ({"scan_id": row[0], "scope_key": row[1], "activity_id": row[2], "platform": row[3]}
                if row is not None else None)

    def session(self, *, scope_key, activity_id):
        row = self._reader.one("""SELECT scan_id, scope_key, activity_id, platform FROM staged_role_exposure_sessions
            WHERE scope_key=? AND activity_id=?""", (scope_key, activity_id))
        return self._record(row)

    def previous_session(self, *, scope_key, activity_id):
        row = self._reader.one("""SELECT scan_id, scope_key, activity_id, platform FROM staged_role_exposure_sessions
            WHERE scope_key=? AND activity_id<>? AND platform<>'unavailable' ORDER BY scan_id DESC LIMIT 1""",
            (scope_key, activity_id))
        return self._record(row)

    def exposure(self, *, scope_key, activity_id, platform, content_id):
        row = self._reader.one("""SELECT * FROM staged_role_exposures WHERE scope_key=? AND activity_id=?
            AND platform=? AND content_id=?""", (scope_key, activity_id, platform, content_id))
        return dict(row) if row else None

    def recent(self, *, scope_key, limit=100):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid browsing history limit")
        rows = self._reader.all("""SELECT * FROM staged_role_exposures WHERE scope_key=?
            ORDER BY fetched_at DESC LIMIT ?""", (scope_key, limit))
        return tuple(dict(row) for row in rows)

    def attempted(self, *, scope_key, platform, content_id, after_activity=None, limit=100):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid browsing attempt limit")
        rows = self._reader.all("""SELECT activity_id FROM staged_role_exposures WHERE scope_key=? AND platform=?
            AND content_id=? AND attempted_at IS NOT NULL AND (? IS NULL OR activity_id>?)
            ORDER BY activity_id LIMIT ?""", (scope_key, platform, content_id, after_activity, after_activity, limit))
        return tuple(row[0] for row in rows)
