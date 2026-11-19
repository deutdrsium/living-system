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
                self.next_check.pop(context_key, None)
            else:
                failures = min(self.no_progress.get(context_key, 0) + 1, 6)
                self.no_progress[context_key] = failures
                self.next_check[context_key] = now + timedelta(seconds=min(30 * 2 ** (failures - 1), 900))
            del self.workers[context_key]

    async def _browse(self, context_key, schedule, next_sleep):
        plan = {"request_id": "social-" + uuid4().hex, "schedule": schedule, "next_sleep": next_sleep}
        activity = await self.capabilities["start_social"](context_key, plan)
        return await self.capabilities["execute_social"](context_key, activity["activity_id"], activity["revision"], schedule)

    def advance(self, contexts, now, sleep_planner):
        self.collect(now)
        for context_key in contexts:
            if context_key in self.workers or now < self.next_check.get(context_key, now):
                continue
            try:
                snapshot = self.capabilities["social_snapshot"](context_key)
                observed = datetime.fromisoformat(snapshot["observed_at"])
                if (snapshot["busy"] or not snapshot["social_available"]
                        or (snapshot["pace_until"] and observed < datetime.fromisoformat(snapshot["pace_until"]))
                        or not browsing_ready(snapshot["records"], observed, snapshot["social_interval_minutes"])):
                    continue
                window = sleep_planner._windows.get(context_key) if snapshot["sleep_enabled"] else None
                if snapshot["sleep_enabled"] and window is None:
                    continue
                next_sleep = window["next_sleep"] if window else None
                schedule = social_window(snapshot["calendar"], observed, observed.astimezone(ZoneInfo(snapshot["timezone"])), next_sleep)
                if not schedule:
                    continue
                self.next_check[context_key] = now + timedelta(minutes=1)
                self.workers[context_key] = asyncio.create_task(
                    self._browse(context_key, schedule, next_sleep.isoformat() if next_sleep else None), name="good-life:browsing")
            except Exception:
                self.next_check[context_key] = now + timedelta(seconds=30)

    async def close(self):
        workers = tuple(self.workers.values())
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        self.workers.clear()
