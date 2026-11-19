"""Resume host-owned follow-up work from durable life events."""

import asyncio
from datetime import datetime, timedelta
import json
from time import monotonic
from zoneinfo import ZoneInfo

from companion_v2.execution.models import TaskError
from companion_v2.foundation.refs import RoleOwnerRef
from companion_v2.references.live_wardrobe import binding
from companion_v2.references.service import owner_key
from companion_v2.role_work.interest_tasks import KIND as INTEREST_KIND
from companion_v2.role_work.interests import scope_key

from .life_event_inbox import PREFIX, accept, receive_pending


class LifeEventRunner:
    def __init__(self, app):
        self.app = app
        self._workers = {}
        self._worker_contexts = {}
        self._retry_at = {}
        self._cursors = {}
        self._interest_contexts = set()
        self._closed = False

    @property
    def active_handles(self):
        return tuple("life-event-" + key[len(PREFIX):] for key in self._workers)

    def advance(self, routines):
        if self._closed:
            return
        contexts = {routine.context.key: routine for routine in routines
                    if getattr(routine, "context", None) is not None and getattr(routine, "instruction", None) is not None
                    and isinstance(getattr(routine, "settings", None), dict) and routine.settings.get("enabled") is True
                    and not getattr(routine, "_closed", False) and not getattr(routine, "_retired", False)}
        for key, context_key in tuple(self._worker_contexts.items()):
            if context_key not in contexts and key in self._workers:
                self._workers[key].cancel()
        for routine in contexts.values():
            for consumer in ("journal", "dream", "interest", "wardrobe"):
                try:
                    cursor_key = (routine.context.key, consumer)
                    result = receive_pending(self.app, routine.context, consumer=consumer, observed_at=self.app.now(),
                                             limit=10, after_sequence=self._cursors.get(cursor_key, 0))
                    self._cursors[cursor_key] = result["next_after"]
                except (TaskError, ValueError, KeyError, TypeError):
                    break
            for consumer, handler in (("dream", self._dream), ("wardrobe", self._wardrobe), ("interest", self._interest)):
                rows = self.app.runtime.read_all("""SELECT key, value FROM runtime_metadata
                    WHERE key LIKE ? AND json_valid(value)
                        AND json_extract(value, '$.state') = 'pending'
                        AND json_extract(value, '$.consumer') = ?
                        AND json_extract(value, '$.event.context_key') = ?
                    ORDER BY json_extract(value, '$.event.sequence') LIMIT 10""", (PREFIX + "%", consumer, routine.context.key))
                for row in rows:
                    if consumer == "interest" and routine.context.key in self._interest_contexts:
                        break
                    if row["key"] in self._workers or monotonic() < self._retry_at.get(row["key"], 0):
                        continue
                    self._retry_at.pop(row["key"], None)
                    document = json.loads(row["value"])
                    worker = asyncio.create_task(handler(routine, row["key"], document), name="companion:life-event:" + consumer)
                    self._workers[row["key"]] = worker
                    self._worker_contexts[row["key"]] = routine.context.key
                    if consumer == "interest":
                        self._interest_contexts.add(routine.context.key)
                        worker.add_done_callback(lambda _done, context_key=routine.context.key: self._interest_contexts.discard(context_key))
                    worker.add_done_callback(lambda done, key=row["key"]: self._finished(key, done))

    def _finished(self, key, worker):
        if not worker.cancelled():
            if worker.exception() is not None or worker.result() is False:
                self._retry_at.setdefault(key, monotonic() + 1)
            else:
                self._retry_at.pop(key, None)
        if self._workers.get(key) is worker:
            self._workers.pop(key, None)
            self._worker_contexts.pop(key, None)
        self.app.notify()

    async def _dream(self, routine, key, document):
        event = document["event"]
        try:
            with self.app.role_life.consistent_snapshot():
                accept(self.app, routine.context, event, consumer="dream", observed_at=self.app.now())
                scope = routine.context.scope
                owner = RoleOwnerRef(scope.installation_id, scope.bot_id, routine.context.role_binding.role.subject_id)
                activity = self.app.role_life.activity_revision(event["payload"]["activity_id"],
                                                                 event["payload"]["revision"], owner=owner)
            result = await routine.dream(activity)
            if not result.is_success:
                return False
            receipt = result.unwrap()
            if receipt.state == "accepted":
                return False
            states = {"written": "completed", "active": "completed", "deleted": "completed",
                      "unknown": "unknown", "failed": "failed"}
            if receipt.state not in states:
                return False
            state = states[receipt.state]
            with self.app.runtime.transaction() as connection:
                row = connection.execute("SELECT value FROM runtime_metadata WHERE key=?", (key,)).fetchone()
                if row is None or json.loads(row["value"]) != document:
                    return True
                document = {**document, "state": state, "result": receipt.state,
                            "failure_code": receipt.failure_code}
                connection.execute("UPDATE runtime_metadata SET value=? WHERE key=?",
                    (json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")), key))
            return True
        except (TaskError, ValueError, KeyError, TypeError):
            # Pending work remains retryable; original journal receipts prevent duplicate model calls.
            return False

    def _settle(self, key, document, *, state, result):
        with self.app.runtime.transaction() as connection:
            row = connection.execute("SELECT value FROM runtime_metadata WHERE key=?", (key,)).fetchone()
            if row is None or json.loads(row["value"]) != document:
                return
            updated = {**document, "state": state, "result": result}
            connection.execute("UPDATE runtime_metadata SET value=? WHERE key=?",
                (json.dumps(updated, ensure_ascii=False, sort_keys=True, separators=(",", ":")), key))

    async def _wardrobe(self, routine, key, document):
        try:
            event = document["event"]
            with self.app.role_life.consistent_snapshot():
                accept(self.app, routine.context, event, consumer="wardrobe", observed_at=self.app.now())
                scope = routine.context.scope
                owner = RoleOwnerRef(scope.installation_id, scope.bot_id, routine.context.role_binding.role.subject_id)
                activity = self.app.role_life.activity_revision(event["payload"]["activity_id"],
                                                                 event["payload"]["revision"], owner=owner)
            wardrobe = self.app.role_wardrobe
            identity = owner_key(routine.context)
            zone = ZoneInfo(routine.settings["sleep_timezone"])
            day = activity.updated_at.astimezone(zone).date().isoformat()
            now = self.app.now()
            synced = binding(self.app.runtime, identity)
            if (day != now.astimezone(zone).date().isoformat() or synced is None
                    or activity.updated_at < datetime.fromisoformat(synced["activated_at"])):
                self._settle(key, document, state="completed", result="not_eligible")
                return True
            saved = wardrobe._read_choice(identity, day)
            if saved and saved.get("state") in ("applied", "superseded"):
                self._settle(key, document, state="completed", result=saved["state"])
                return True
            retry_key = wardrobe._key(identity, day)
            if wardrobe.retries.get(retry_key, (0, None))[0] >= 3:
                self._settle(key, document, state="failed", result="retry_limit")
                return True
            try:
                self.app.calendar.prepare_life_snapshot_read()
            except (TaskError, ValueError):
                pass
            factory = self.app.role_life.snapshot_factory()
            with self.app.runtime.coordinated_snapshot(factory):
                await wardrobe.consume_life_event(routine, activity, now)
            saved = wardrobe._read_choice(identity, day)
            if saved and saved.get("state") in ("applied", "superseded"):
                self._settle(key, document, state="completed", result=saved["state"])
                return True
            return False
        except (TaskError, ValueError, KeyError, TypeError):
            return False

    async def _interest(self, routine, key, document):
        try:
            event, now = document["event"], self.app.now()
            with self.app.role_life.consistent_snapshot():
                accept(self.app, routine.context, event, consumer="interest", observed_at=now)
                scope = routine.context.scope
                owner = RoleOwnerRef(scope.installation_id, scope.bot_id, routine.context.role_binding.role.subject_id)
                activity = self.app.role_life.activity_revision(event["payload"]["activity_id"],
                                                                 event["payload"]["revision"], owner=owner)
                if self.app.role_interests.store.processed(routine.context, activity.activity_id):
                    self._settle(key, document, state="completed", result="already_processed")
                    return True
                if ((activity.work.kind == "reading" and (activity.reading is None or not activity.reading.reflection))
                        or (activity.work.kind == "social" and
                            self.app.role_social.exposure.session_platform(routine.context, activity.activity_id) is not None)):
                    self._settle(key, document, state="completed", result="not_eligible")
                    return True
                busy = (any(not worker.done() for worker in routine._workers.values())
                        or any(item.state.value == "active" for item in routine.activities())
                        or bool(self.app.role_state.body_clock.pending_sleep(routine.context)))
            if (INTEREST_KIND not in self.app.gateway.router.profiles or busy
                    or ("interest_request" not in document and routine._next_interest_check is not None
                        and now < routine._next_interest_check)):
                self._retry_at[key] = monotonic() + 60
                return False
            if "interest_request" not in document:
                day = now.astimezone(ZoneInfo(routine.settings["timezone"])).date().isoformat()
                daily_key = "role.interests.daily:" + scope_key(routine.context) + ":" + day
                with self.app.runtime.transaction() as connection:
                    row = connection.execute("SELECT value FROM runtime_metadata WHERE key=?", (key,)).fetchone()
                    if row is None or json.loads(row["value"]) != document:
                        return True
                    count = connection.execute("SELECT value FROM runtime_metadata WHERE key=?", (daily_key,)).fetchone()
                    count = int(count["value"]) if count else 0
                    if count >= 2:
                        self._retry_at[key] = monotonic() + 60
                        return False
                    document = {**document, "interest_request": event["event_id"]}
                    connection.execute("UPDATE runtime_metadata SET value=? WHERE key=?",
                        (json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")), key))
                    connection.execute("""INSERT INTO runtime_metadata(key,value) VALUES (?,?)
                        ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (daily_key, str(count + 1)))
                routine._next_interest_check = now + timedelta(hours=2)
            self.app.calendar.prepare_life_snapshot_read()
            with self.app.runtime.coordinated_snapshot(self.app.role_life.snapshot_factory()):
                result = await self.app.role_interests.consume(routine, activity, request_id=document["interest_request"])
            if not result.is_success or result.unwrap().task is None:
                return False
            task = result.unwrap().task
            if task.state == "succeeded":
                self._settle(key, document, state="completed", result="processed")
                return True
            if task.state in ("uncertain", "failed", "cancelled", "expired", "rejected"):
                self._settle(key, document, state="unknown" if task.effect == "unknown" or task.state == "uncertain" else "failed",
                             result=task.state)
                return True
            self._retry_at[key] = monotonic() + 60
            return False
        except (TaskError, ValueError, KeyError, TypeError):
            return False

    def cancel_running(self):
        for worker in tuple(self._workers.values()):
            worker.cancel()

    def cancel_context(self, context_key):
        for key, worker in tuple(self._workers.items()):
            if self._worker_contexts.get(key) == context_key:
                worker.cancel()

    async def close(self):
        self._closed = True
        workers = tuple(self._workers.values())
        self.cancel_running()
        if workers:
            await asyncio.wait(workers, timeout=0.25)
