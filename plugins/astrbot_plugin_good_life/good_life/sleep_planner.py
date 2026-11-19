"""Choose the same nightly and nap triggers as the original life routine."""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from .sleep_physiology import nap_ready, night_sleep_ready
from .sleep_schedule import next_bedtime


class SleepPlanner:
    def __init__(self):
        self._windows = {}

    def _schedule(self, context_key, owner, settings, config_revision, now, held_bedtime):
        current = self._windows.get(context_key)
        signature = (config_revision, owner["installation_id"], owner["bot_id"], owner["persona_id"])
        if current is None or current["signature"] != signature:
            next_sleep = held_bedtime or next_bedtime(now, owner, settings["sleep_timezone"],
                time.fromisoformat(settings["sleep_time"]), settings["sleep_variation_minutes"])
            current = {"signature": signature, "next_sleep": next_sleep}
            self._windows[context_key] = current
        return current

    def plan(self, *, context_key, owner, settings, config_revision, now, profile, busy,
             selected_executor, active_reading, napped_today, held_bedtime=None):
        if (type(context_key) is not str or not context_key or type(owner) is not dict
                or set(owner) != {"installation_id", "bot_id", "persona_id"} or type(settings) is not dict
                or not isinstance(now, datetime) or now.utcoffset() is None
                or any(type(value) is not bool for value in (busy, selected_executor, active_reading, napped_today))):
            raise ValueError("invalid sleep planner snapshot")
        if settings.get("enabled") is not True or settings.get("sleep_enabled") is not True:
            self._windows.pop(context_key, None)
            return None
        current = self._schedule(context_key, owner, settings, config_revision, now, held_bedtime)
        due = current["next_sleep"]
        if due <= now and not busy and selected_executor:
            if now >= due + timedelta(minutes=settings["sleep_minutes"]):
                current["next_sleep"] = next_bedtime(now + timedelta(microseconds=1), owner, settings["sleep_timezone"],
                    time.fromisoformat(settings["sleep_time"]), settings["sleep_variation_minutes"])
            elif not active_reading:
                window_closed = now >= due + timedelta(minutes=settings["sleep_window_minutes"])
                if night_sleep_ready(profile) or window_closed:
                    day = due.astimezone(ZoneInfo(settings["sleep_timezone"])).date().isoformat()
                    return {"kind": "night", "request_id": "daily-sleep-" + day, "minutes": settings["sleep_minutes"],
                            "scheduled_at": due.isoformat(), "context_key": context_key}
        local = now.astimezone(ZoneInfo(settings["sleep_timezone"]))
        if (not busy and not napped_today and nap_ready(profile, local_minutes=local.hour * 60 + local.minute)):
            return {"kind": "nap", "request_id": "daily-nap-" + local.date().isoformat(), "minutes": 25,
                    "scheduled_at": None, "context_key": context_key}
        return None

    def confirm(self, plan, *, owner, settings, now):
        """Advance a nightly window only after the life creation is confirmed."""
        if plan["kind"] != "night":
            return
        current = self._windows.get(plan["context_key"])
        if current is None or current["next_sleep"].isoformat() != plan["scheduled_at"]:
            raise ValueError("sleep schedule changed before confirmation")
        current["next_sleep"] = next_bedtime(now + timedelta(microseconds=1), owner, settings["sleep_timezone"],
            time.fromisoformat(settings["sleep_time"]), settings["sleep_variation_minutes"])
