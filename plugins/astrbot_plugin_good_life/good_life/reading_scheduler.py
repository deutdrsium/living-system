"""Choose books and launch the original supervised reading from life time."""

import asyncio
from datetime import datetime, timedelta
import json
from time import monotonic
from zoneinfo import ZoneInfo

from .reading_choices import _digest, candidates, choice_slot, reading_window


class ReadingScheduler:
    def __init__(self, storage, capabilities, *, executor=None, monotonic_clock=None):
        self.storage, self.capabilities = storage, capabilities
        self.monotonic_clock = monotonic_clock or monotonic
        from .reading_execution import ReadingExecutionStore
        self.store = ReadingExecutionStore(storage)
        self.execute_executor = self.recovery_executor = executor
        self.coordination_ports = getattr(executor, "coordination_ports", None)
        self.workers, self.slots, self.next_check, self.activity_ids = {}, {}, {}, {}
        self._next_snapshot_check = {}
        self.life_workers = set()

    @property
    def executor(self):
        return self.execute_executor or self.recovery_executor

    @executor.setter
    def executor(self, executor):
        self.execute_executor = self.recovery_executor = executor

    def _choice(self, context_key, slot):
        key = "role.reading.choice:" + _digest([context_key, slot])
        with self.storage._lock:
            row = self.storage._connection.execute("SELECT value FROM staged_life_runtime_metadata WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else None

    def _save_choice(self, context_key, slot, value):
        if (type(value) is not dict or set(value) != {"source_id", "reason", "starts", "next_at"}
                or (value["source_id"] is not None and (type(value["source_id"]) is not str or not value["source_id"]))
                or type(value["reason"]) is not str or len(value["reason"]) > 1000
                or value["starts"] != 0 or value["next_at"] is not None):
            raise ValueError("reading choice evidence is invalid")
        key = "role.reading.choice:" + _digest([context_key, slot])
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self.storage.transaction() as db:
            row = db.execute("SELECT value FROM staged_life_runtime_metadata WHERE key=?", (key,)).fetchone()
            if row is not None and json.loads(row[0]) != value:
                raise ValueError("reading choice changed")
            if row is None:
                db.execute("INSERT INTO staged_life_runtime_metadata VALUES (?,?)", (key, encoded))

    @staticmethod
    def _completed_choice_value(checkpoint):
        if checkpoint is not None and checkpoint.get("phase") == "completed":
            return checkpoint.get("value")
        return None

    @staticmethod
    def _retry(state, now):
        if state["next_retry"] is None:
            state["no_choice"] = min(state["no_choice"] + 1, 6)
            state["next_retry"] = now + timedelta(seconds=min(30 * 2 ** (state["no_choice"] - 1), 900))
        if now < state["next_retry"]:
            return False
        state["retry"] += 1
        state["next_retry"] = None
        return True

    def _choice_due(self, context_key, now):
        state = self.slots.get(context_key)
        if state is None:
            return False
        retry_at = state["next_retry"]
        if retry_at is not None:
            due = now >= retry_at
            check = (state["retry"], retry_at)
        elif self.recovery_executor is not None:
            # A pre-due snapshot after restart identifies the current slot,
            # but must not move its result check to the next periodic scan.
            from .reading_selection import choice_key, result_check_due
            choice = self.store.get(context_key, choice_key(state["base"] + ":" + str(state["retry"])))
            if choice is None or choice["phase"] not in ("submitted", "unknown"):
                return False
            due = result_check_due(choice, now)
            check = (state["retry"], choice["phase"], choice.get("result_check_after"))
        else:
            return False
        # If the early snapshot still finds the context busy, resume polling
        # on the usual cadence instead of taking another snapshot next tick.
        if not due or state.get("early_check") == check:
            return False
        state["early_check"] = check
        return True

    def _choice_slot_decision(self, context_key, state, now):
        from .reading_selection import choice_key, result_check_due, MAX_RESULT_CHECKS, fail_result
        while True:
            slot = state["base"] + ":" + str(state["retry"])
            decision = self._choice(context_key, slot)
            if decision is None:
                own_choice = self.store.get(context_key, choice_key(slot))
                value = self._completed_choice_value(own_choice)
                if value is not None:
                    evidence = {"state": "committed", "value": value}
                elif own_choice is not None:
                    executor = self.recovery_executor
                    if executor is None and own_choice["phase"] != "failed":
                        return None
                    if own_choice["phase"] == "unknown":
                        if own_choice.get("result_check_attempts", 0) >= MAX_RESULT_CHECKS:
                            fail_result(executor, own_choice, "result_check_exhausted")
                        elif not result_check_due(own_choice, now):
                            return None
                    evidence = {"state": "failed" if own_choice["phase"] == "failed" else "absent"}
                else:
                    evidence = self.capabilities["reading_choice_result"](context_key, slot)
                if evidence["state"] == "committed":
                    self._save_choice(context_key, slot, evidence["value"])
                    decision = evidence["value"]
                elif evidence["state"] == "failed":
                    if self._retry(state, now):
                        continue
                    return None
                elif evidence["state"] != "absent":
                    return None
            if decision is not None and not decision.get("source_id"):
                if self._retry(state, now):
                    continue
                return None
            return slot, decision

    async def _choose(self, context_key, identifiers, slot, schedule, next_sleep):
        from .reading_selection import choice_key, choose
        cached = self.store.get(context_key, choice_key(slot))
        # Terminal A3 evidence can be handled locally after host rollback.
        if cached and cached.get("phase") == "failed":
            return {"state": "failed"}
        value = self._completed_choice_value(cached)
        if value is not None:
            self._save_choice(context_key, slot, value)
            return {"state": "committed", "value": value}
        executor = self.recovery_executor if cached is not None else self.execute_executor
        if cached is not None and executor is None:
            return {"state": "paused"}
        if executor is not None and (executor.enabled or cached is not None):
            self.life_workers.add(context_key)
            snapshot = self.capabilities["reading_snapshot"](context_key)
            observed = datetime.fromisoformat(snapshot["observed_at"])
            local = observed.astimezone(ZoneInfo(snapshot["timezone"]))
            window = reading_window(snapshot["calendar"], observed, local,
                                    datetime.fromisoformat(next_sleep) if next_sleep else None)
            if snapshot["busy"] or not snapshot["reading_enabled"] or window != schedule:
                return {"state": "paused"}
            records = {item["activity_id"]: item for item in snapshot["records"]}
            result = await choose(executor, context_key, [records[identifier] for identifier in identifiers], slot, schedule)
        else:
            result = await self.capabilities["choose_reading"](context_key, identifiers, slot, schedule, next_sleep)
        if result.get("state") == "committed":
            self._save_choice(context_key, slot, result["value"])
        return result

    async def _owner(self, context_key, activity):
        existing = self.store.get(context_key, activity["activity_id"])
        if existing is not None:
            return existing["execution_owner"]
        if self.execute_executor is None or not self.execute_executor.enabled:
            return "host_bridge"
        from .reading_execution import digest
        scope = await self.execute_executor._scope(context_key)
        original_id = "reading-task-" + digest([context_key, activity["activity_id"], activity["work"]["request_id"]])
        # Read-only until start succeeds; old attempts never acquire an A3 row.
        return "host_bridge" if await self.execute_executor.inspect(scope, original_id, "") is not None else "life"

    async def _read(self, context_key, activity, next_sleep, *, resume=False):
        existing = self.store.get(context_key, activity["activity_id"])
        owner = await self._owner(context_key, activity)
        executor = self.recovery_executor if existing is not None else self.execute_executor
        if owner == "life" and executor is None:
            return {"state": "paused"}
        started = await self.capabilities["start_reading"](context_key, activity["activity_id"], activity["revision"], next_sleep, resume)
        if owner == "life":
            executor.claim(context_key, started["activity_id"], owner="life")
            return await self._run_life(context_key, started["activity_id"], started["revision"], executor=executor)
        return await self.capabilities["execute_reading"](context_key, started["activity_id"], started["revision"])

    async def _run_life(self, context_key, activity_id, revision=None, *, executor=None):
        executor = executor or self.recovery_executor
        self.life_workers.add(context_key)
        source_unavailable = False
        try:
            result = await executor.execute(context_key, activity_id, revision)
        except ValueError as error:
            code = getattr(error, "code", "")
            if code not in ("source_unavailable", "stale_version") and not code.startswith(("source_", "reading_source_")):
                raise
            state = executor.store.get(context_key, activity_id)
            if state.get("command"):
                with self.storage._lock:
                    row = self.storage._connection.execute("SELECT state FROM life_prepared_commands WHERE command_id=?",
                                                          (state["command"]["command_id"],)).fetchone()
                # A decision may already exist, or events may still need delivery.
                # Keep the command/proof until the original commit reconciles;
                # only a verified abort permits the source-error pause path.
                if (row is None or row[0] != "aborted" or "command_coordination" in state
                        and state["command_coordination"].get("confirmed_decision") != "abort"):
                    raise
            source_unavailable = True
            result = {"state": "paused"}
        if result.get("state") == "paused" and callable(self.capabilities.get("pause_interrupted_work")):
            scope = await executor._scope(context_key)
            activity = json.loads(executor.records.dispatch("activity", owner_key=scope.owner_key, activity_id=activity_id))
            if activity["state"] == "active":
                await self.capabilities["pause_interrupted_work"](context_key, activity_id, activity["revision"])
            state = executor.store.get(context_key, activity_id)
            state["phase"] = activity["state"] if activity["state"] in ("completed", "cancelled") else "paused"
            if source_unavailable:
                state.pop("command", None)
                state.pop("proof", None)
            executor.store.save(context_key, activity_id, state)
        return result

    async def _manual(self, context_key, intent):
        state = self.store.get(context_key, intent["activity_id"])
        executor = self.recovery_executor if state is not None else self.execute_executor
        if state is not None and state["execution_owner"] == "life" and executor is None:
            return {"state": "paused"}
        if (state is not None and state["execution_owner"] != "life") or (state is None and (executor is None or not executor.enabled)):
            return await self.capabilities["execute_manual_reading"](context_key, intent["operation_id"])
        from .contracts import models as m, ports as p
        from .reading_execution import digest, signed
        scope = await executor._scope(context_key)
        if state is None:
            original_id = "reading-task-" + digest([context_key, intent["activity_id"], intent["operation_id"]])
            if await executor.inspect(scope, original_id, "") is not None:
                return await self.capabilities["execute_manual_reading"](context_key, intent["operation_id"])
        # The host validates missing/stale activities and settles rejected intents.
        # No life-ledger read or owner write precedes successful start.
        self.life_workers.add(context_key)
        audience = m.AudienceBound("context:" + digest([scope.owner_key, scope.context_key]), (scope.context_key,))
        payload = {"intent_ref": "life.reading.manual.v1:" + intent["operation_id"]}
        receipt = await executor.ports.execute_platform_action(signed("platform_action", p.PlatformActionRequest(
            scope, intent["operation_id"], "", "life.intent.start", payload, (), audience)))
        if receipt.status != m.ReceiptStatus.SUCCEEDED:
            return {"state": "paused"}
        executor.claim(context_key, intent["activity_id"], owner="life")
        return await self._run_life(context_key, intent["activity_id"], executor=executor)

    def collect(self, now):
        for context_key, worker in tuple(self.workers.items()):
            if worker.done():
                if not worker.cancelled():
                    error = worker.exception()
                    result = None if error else worker.result()
                    if (context_key not in self.activity_ids and type(result) is dict
                            and result.get("state") == "committed"):
                        # A choice has finished; hand off using a fresh snapshot
                        # rather than waiting for the next background scan.
                        self._next_snapshot_check.pop(context_key, None)
                    if (error or type(result) is dict and (result.get("state") in ("absent", "failed", "paused")
                            or result.get("state") in ("unknown", "pending", "running", "commit_unresolved") and context_key in self.life_workers)):
                        self.next_check[context_key] = now + timedelta(seconds=30)
                self.life_workers.discard(context_key)
                del self.workers[context_key]
                self.activity_ids.pop(context_key, None)

    def cancel_activity(self, context_key, activity_id):
        worker = self.workers.get(context_key)
        if self.activity_ids.get(context_key) != activity_id or worker is None or worker.done():
            return False
        worker.cancel()
        return True

    def advance(self, contexts, now, sleep_planner):
        self.collect(now)
        pending = {}
        if all(callable(self.capabilities.get(name)) for name in ("reading_manual_pending", "execute_manual_reading")):
            for item in self.capabilities["reading_manual_pending"]():
                if contexts.get(item.get("context_key")) == item.get("owner_key"):
                    pending.setdefault(item["context_key"], item)
        for context_key in contexts:
            if context_key in self.workers or now < self.next_check.get(context_key, now):
                continue
            self.activity_ids.pop(context_key, None)
            try:
                executor = self.recovery_executor
                if executor is not None:
                    from .reading_execution import result_check_due
                    resumable = executor.store.pending(context_key,
                        include_unresolved=executor.coordination_ports is not None)
                    for state in resumable:
                        if state.get("phase") in ("unknown", "commit_unresolved") and not result_check_due(state, now):
                            local, decision = executor._local_commit(state) if state.get("command") else (None, None)
                            confirmed_abort = ("command_coordination" not in state or
                                state["command_coordination"].get("confirmed_decision") == "abort")
                            new_ack = (local == "acknowledged" and decision is not None and
                                ("commit_decision" not in state or not state.get("commit_acknowledged", True)))
                            if not (new_ack or local == "aborted" and confirmed_abort):
                                continue
                        if state.get("phase") == "starting":
                            activity = json.loads(executor.records.dispatch("activity", owner_key=contexts[context_key], activity_id=state["activity_id"]))
                            if activity["state"] != "active":
                                continue
                        self.activity_ids[context_key] = state["activity_id"]
                        self.workers[context_key] = asyncio.create_task(
                            self._run_life(context_key, state["activity_id"], executor=executor), name="good-life:reading")
                        break
                    if context_key in self.workers:
                        continue
                if context_key in pending:
                    self.activity_ids[context_key] = pending[context_key]["activity_id"]
                    self.workers[context_key] = asyncio.create_task(
                        self._manual(context_key, pending[context_key]), name="good-life:manual-reading")
                    continue
                if (self.monotonic_clock() < self._next_snapshot_check.get(context_key, 0)
                        and not self._choice_due(context_key, now)):
                    continue
                self._next_snapshot_check[context_key] = self.monotonic_clock() + 30
                snapshot = self.capabilities["reading_snapshot"](context_key)
                observed = datetime.fromisoformat(snapshot["observed_at"])
                if not snapshot["reading_enabled"] or snapshot["busy"]:
                    continue
                records = snapshot["records"]
                local = observed.astimezone(ZoneInfo(snapshot["timezone"]))
                day = local.date().isoformat()
                window = sleep_planner._windows.get(context_key) if snapshot["sleep_enabled"] else None
                if snapshot["sleep_enabled"] and window is None:
                    continue
                next_sleep = window["next_sleep"] if window else None
                schedule = reading_window(snapshot["calendar"], observed, local, next_sleep)
                if not schedule:
                    continue
                next_sleep_raw = next_sleep.isoformat() if next_sleep else None
                approved = set(snapshot["approved_paused"])
                paused = next((item for item in records if item["activity_id"] in approved), None)
                if paused is not None:
                    self.activity_ids[context_key] = paused["activity_id"]
                    self.workers[context_key] = asyncio.create_task(self._read(context_key, paused, next_sleep_raw, resume=True),
                                                                    name="good-life:reading")
                    continue
                if snapshot["pace_until"] and observed < datetime.fromisoformat(snapshot["pace_until"]):
                    continue
                base = choice_slot(records, day)
                state = self.slots.get(context_key)
                if state is None or state["base"] != base:
                    state = {"base": base, "retry": 0, "no_choice": 0, "next_retry": None}
                    self.slots[context_key] = state
                slot_decision = self._choice_slot_decision(context_key, state, now)
                if slot_decision is None:
                    continue
                slot, decision = slot_decision
                allowed = set(snapshot["authorized_sources"])
                choices = candidates(records, day, selected_source=decision.get("source_id") if decision else None,
                                     authorize_source=lambda source: source["source_id"] in allowed)
                if not choices:
                    if decision is not None:
                        state["retry"] += 1
                    continue
                if decision is None:
                    operation = self._choose(context_key, [item["activity_id"] for item in choices], slot, schedule, next_sleep_raw)
                else:
                    self.activity_ids[context_key] = choices[0]["activity_id"]
                    operation = self._read(context_key, choices[0], next_sleep_raw)
                self.workers[context_key] = asyncio.create_task(operation, name="good-life:reading")
            except Exception:
                self.next_check[context_key] = now + timedelta(seconds=30)

    async def close(self):
        workers = tuple(self.workers.values())
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        self.workers.clear()
        self.activity_ids.clear()
