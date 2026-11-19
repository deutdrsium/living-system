"""Run the original calendar planning opportunities from life time."""

import asyncio
from datetime import timedelta


class CalendarScheduler:
    def __init__(self, capabilities):
        self.capabilities = capabilities
        self.worker = None
        self.next_checks = {}
        self.manual_next = {}
        self.manual_id = None
        self.cursor = 0

    def advance(self, now):
        if self.worker is not None:
            if not self.worker.done():
                return
            if not self.worker.cancelled() and self.worker.exception() is not None and self.manual_id is not None:
                self.manual_next[self.manual_id] = now + timedelta(seconds=30)
            self.worker = None
            self.manual_id = None
        if all(callable(self.capabilities.get(name)) for name in ("planning_manual_pending", "planning_manual_run")):
            for item in self.capabilities["planning_manual_pending"]():
                if (type(item) is dict and set(item) == {"operation_id", "context_key", "owner_key"}
                        and all(type(value) is str and value for value in item.values())
                        and now >= self.manual_next.get(item["operation_id"], now)):
                    self.manual_id = item["operation_id"]
                    self.worker = asyncio.create_task(self.capabilities["planning_manual_run"](item["context_key"], item["operation_id"]),
                                                      name="good-life:manual-calendar-planning")
                    return
        bindings = self.capabilities["planning_bindings"]()
        if not isinstance(bindings, (tuple, list)) or len(bindings) > 10000:
            raise ValueError("invalid calendar planning bindings")
        bindings = sorted(bindings, key=lambda item: item["role_key"])
        for offset in range(min(16, len(bindings))):
            index = (self.cursor + offset) % len(bindings)
            item = bindings[index]
            if (type(item) is not dict or set(item) != {"role_key", "context_key"}
                    or any(type(item[name]) is not str or not item[name] for name in item)):
                raise ValueError("invalid calendar planning identity")
            role_key, context_key = item["role_key"], item["context_key"]
            if now < self.next_checks.get(role_key, now):
                continue
            self.next_checks[role_key] = now + timedelta(seconds=30)
            self.cursor = index + 1
            try:
                opportunity = self.capabilities["planning_check"](context_key)
                if opportunity is not None:
                    self.worker = asyncio.create_task(self.capabilities["planning_run"](context_key, opportunity),
                                                      name="good-life:calendar-planning")
                    return
            except Exception:
                continue

    async def close(self):
        if self.worker is not None:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
            self.worker = None
