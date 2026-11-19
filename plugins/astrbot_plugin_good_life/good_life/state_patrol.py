"""The existing daily, model-free state patrol, owned by the life database."""

from datetime import date, datetime, time, timedelta, timezone
import json
from zoneinfo import ZoneInfo

from .state_scheduler import _digest, _stamp


PREFIX = "role.state.patrol.v1:"
MAX_REPORTS = 64


def _document(row):
    try:
        value = json.loads(row[0]) if row else None
    except (TypeError, ValueError):
        value = None
    if type(value) is not dict or value.get("version") != 1:
        return {"version": 1, "day": None, "reports": []}
    reports = value.get("reports")
    if type(reports) is not list or any(type(item) is not dict for item in reports):
        reports = []
    return {"version": 1, "day": value.get("day") if isinstance(value.get("day"), str) else None,
            "reports": reports[-MAX_REPORTS:]}


def _local(settings, now):
    try:
        zone = ZoneInfo(str(settings.get("planning_timezone") or "Asia/Shanghai"))
    except Exception:
        zone = ZoneInfo("Asia/Shanghai")
    return now.astimezone(timezone.utc).astimezone(zone)


class StatePatrol:
    def __init__(self, storage):
        self.storage = storage

    @staticmethod
    def key(owner):
        return PREFIX + _digest(owner)

    def due(self, owner, settings, now):
        if not settings.get("daily_state_patrol_enabled", True):
            return False
        local = _local(settings, now)
        try:
            raw = " ".join(str(settings.get("daily_state_patrol_time", "04:00") or "").split())
            target = time.fromisoformat(raw)
            if len(raw) != 5 or target.tzinfo is not None or target.second or target.microsecond:
                return False
        except (TypeError, ValueError):
            return False
        with self.storage._lock:
            row = self.storage._connection.execute("SELECT value FROM staged_state_metadata WHERE key=?",
                                                   (self.key(owner),)).fetchone()
        return (_document(row)["day"] != local.date().isoformat()
                and local >= datetime.combine(local.date(), target, tzinfo=local.tzinfo))

    def record(self, owner, settings, now, state):
        if state.get("state") == "disabled":
            return {"state": "disabled", "day": None, "kept": 0, "pruned": 0}
        local = _local(settings, now)
        retention = settings.get("daily_state_patrol_retention_days", 30)
        entry = {"day": local.date().isoformat(), "at": _stamp(now), "revision": int(state.get("revision") or 0),
                 "state": str(state.get("state") or "unknown"), "live_conditions": int(state.get("live_conditions") or 0),
                 "expired_conditions": int(state.get("expired_conditions") or 0), "retention_days": retention,
                 "role_timezone": local.tzinfo.key}
        key = self.key(owner)
        with self.storage.transaction() as db:
            current = _document(db.execute("SELECT value FROM staged_state_metadata WHERE key=?", (key,)).fetchone())
            if current["day"] == entry["day"]:
                return {"state": "already_recorded", "day": entry["day"]}
            reports = [item for item in current["reports"] if item.get("day") != entry["day"]]
            reports.append(entry)
            kept = []
            for item in reports:
                try:
                    day = date.fromisoformat(item.get("day", ""))
                except (TypeError, ValueError):
                    continue
                if day >= local.date() - timedelta(days=retention):
                    kept.append(item)
            kept.sort(key=lambda item: (item["day"], str(item.get("at") or "")))
            kept = kept[-MAX_REPORTS:]
            encoded = json.dumps({"version": 1, "day": entry["day"], "reports": kept},
                                 ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
            db.execute("""INSERT INTO staged_state_metadata(key,value) VALUES (?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (key, encoded))
        return {"state": "recorded", "day": entry["day"], "kept": len(kept),
                "pruned": len(reports) - len(kept), "entry": entry}
