"""Start supervised browsing from the life plugin's available time."""

import asyncio
from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from .social_schedule import browsing_ready, social_window


class SocialScheduler:
    def __init__(self, capabilities):
        self.capabilities = capabilities
        self.workers, self.next_check, self.no_progress = {}, {}, {}

    def collect(self, now):
        for context_key, worker in tuple(self.workers.items()):
            if not worker.done():
                continue
            if worker.cancelled():
                result = None
            else:
                error = worker.exception()
                result = None if error else worker.result()
            if isinstance(result, dict) and result.get("state") == "completed":
                self.no_progress.pop(context_key, None)
            else:
                failures = min(self.no_progress.get(context_key, 0) + 1, 6)
                self.no_progress[context_key] = failures
                self.next_check[context_key] = now + timedelta(seconds=min(30 * 2 ** (failures - 1), 900))
            del self.workers[context_key]

    async def _browse(self, context_key, schedule, next_sleep):
        plan = {"request_id": "social-" + uuid4().hex, "schedule": schedule, "next_sleep": next_sleep}
        activity = await self.capabilities["start_social"](context_key, plan)
        return await self.capabilities["execute_social"](context_key, activity["activity_id"], activity["revision"], schedule)

    def snapshot_if_due(self, context_key, now):
        if now < self.next_check.get(context_key, now):
            return None
        self.next_check[context_key] = now + timedelta(seconds=30)
        try:
            return self.capabilities["social_snapshot"](context_key)
        except Exception:
            return None

    def advance(self, context_key, snapshot, sleep_planner):
        try:
            observed = datetime.fromisoformat(snapshot["observed_at"])
            if (snapshot["busy"] or not snapshot["social_available"]
                    or (snapshot["pace_until"] and observed < datetime.fromisoformat(snapshot["pace_until"]))
                    or not browsing_ready(snapshot["records"], observed, snapshot["social_interval_minutes"])):
                return
            window = sleep_planner._windows.get(context_key) if snapshot["sleep_enabled"] else None
            if snapshot["sleep_enabled"] and window is None:
                return
            next_sleep = window["next_sleep"] if window else None
            schedule = social_window(snapshot["calendar"], observed, observed.astimezone(ZoneInfo(snapshot["timezone"])), next_sleep)
            if schedule:
                self.workers[context_key] = asyncio.create_task(
                    self._browse(context_key, schedule, next_sleep.isoformat() if next_sleep else None), name="good-life:browsing")
        except Exception:
            return

    async def close(self):
        workers = tuple(self.workers.values())
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        self.workers.clear()
