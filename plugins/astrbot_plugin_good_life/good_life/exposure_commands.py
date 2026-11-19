"""Persist real browsing receipts while the originating activity is active."""

from datetime import datetime
import json


def _name(value, *, allow_empty=False, limit=512):
    if type(value) is not str or len(value) > limit or (not allow_empty and not value.strip()):
        raise ValueError("invalid browsing identity or text")
    return value


def _time(value):
    if type(value) is not str or datetime.fromisoformat(value).utcoffset() is None:
        raise ValueError("browsing timestamp must include a timezone")
    return value


class StagedExposureCommands:
    def __init__(self, storage):
        self.storage = storage

    def dispatch(self, operation, *, owner_key, context_key, scope_key, activity_id, **request):
        if operation not in ("save_batch", "selected", "mark_exposed", "mark_attempt"):
            raise ValueError("unknown browsing command")
        for value in (owner_key, context_key, scope_key, activity_id):
            _name(value)
        with self.storage.transaction() as db:
            activity = db.execute("SELECT state,record_json FROM staged_role_activities WHERE owner_key=? AND activity_id=?",
                                  (owner_key, activity_id)).fetchone()
            record = json.loads(activity["record_json"]) if activity is not None else None
            if (record is None or activity["state"] != "active" or record.get("work", {}).get("kind") != "social"
                    or record["work"].get("context_key") != context_key):
                raise ValueError("browsing activity is not active for this role and session")
            return getattr(self, "_" + operation)(db, scope_key, activity_id, **request)

    @staticmethod
    def _save_batch(db, scope, activity, *, platform, items, observed_at):
        _name(platform, limit=100)
        _time(observed_at)
        if type(items) is not list or len(items) > 100:
            raise ValueError("invalid browsing batch")
        records = []
        for item in items:
            if type(item) is not dict or set(item) != {"id", "title", "summary", "url"}:
                raise ValueError("invalid browsing card")
            records.append((scope, activity, platform, _name(item["id"]),
                _name(item["title"], allow_empty=True, limit=200), _name(item["summary"], allow_empty=True, limit=500),
                _name(item["url"], allow_empty=True, limit=500), observed_at))
        db.execute("""INSERT OR IGNORE INTO staged_role_exposure_sessions
            (scope_key,activity_id,platform,selected_id,selection_made) VALUES (?,?,?,'',0)""", (scope, activity, platform))
        db.executemany("""INSERT OR IGNORE INTO staged_role_exposures
            (scope_key,activity_id,platform,content_id,title,summary,source_url,fetched_at,exposed_at,attempted_at,coverage,random_attempt)
            VALUES (?,?,?,?,?,?,?,?,NULL,NULL,'',0)""", records)
        return {"saved": True}

    @staticmethod
    def _selected(db, scope, activity, *, choice):
        _name(choice, allow_empty=True)
        row = db.execute("SELECT selected_id,selection_made FROM staged_role_exposure_sessions WHERE scope_key=? AND activity_id=?",
                         (scope, activity)).fetchone()
        if row is None:
            raise ValueError("missing browsing session")
        if not row["selection_made"]:
            db.execute("UPDATE staged_role_exposure_sessions SET selected_id=?,selection_made=1 WHERE scope_key=? AND activity_id=?",
                       (choice, scope, activity))
            return choice
        return row["selected_id"]

    @staticmethod
    def _mark_exposed(db, scope, activity, *, platform, ids, skipped, observed_at):
        _name(platform, limit=100)
        _time(observed_at)
        if type(ids) is not list or type(skipped) is not list or len(ids) > 100 or any(item not in ids for item in skipped):
            raise ValueError("invalid exposed cards")
        for ident in ids:
            _name(ident)
            db.execute("""UPDATE staged_role_exposures SET exposed_at=?,
                coverage=CASE WHEN ? AND attempted_at IS NULL AND coverage='' THEN 'skipped' ELSE coverage END
                WHERE scope_key=? AND activity_id=? AND platform=? AND content_id=? AND exposed_at IS NULL""",
                (observed_at, int(ident in skipped), scope, activity, platform, ident))
        return {"saved": True}

    @staticmethod
    def _mark_attempt(db, scope, activity, *, platform, ident, coverage, observed_at, random_attempt):
        _name(platform, limit=100)
        _name(ident)
        _time(observed_at)
        if coverage not in ("text", "image", "image_description") or type(random_attempt) is not bool:
            raise ValueError("invalid browsing attempt")
        db.execute("""UPDATE staged_role_exposures SET attempted_at=COALESCE(attempted_at,?),coverage=?,
            random_attempt=MAX(random_attempt,?) WHERE scope_key=? AND activity_id=? AND platform=? AND content_id=?
            AND attempted_at IS NULL""", (observed_at, coverage, int(random_attempt), scope, activity, platform, ident))
        return {"saved": True}
