"""Apply a durably admitted message interruption to the life-owned ledger."""

from dataclasses import replace
from datetime import datetime, timedelta
import json

from companion_v2.conversation.messages import event_from_document
from companion_v2.execution.models import TaskContext, TaskError, digest, encode, stamp
from companion_v2.role_work.life.physiology import sleep_quality_range
from companion_v2.role_work.models import RoleActivityState as State
from companion_v2.role_work.state.ownership import role_owner

from . import sleep_interruption_queue as queue
from .natural_wake_handoff import NaturalWakeHandoff


class SleepInterruptionHandoff(NaturalWakeHandoff):
    def __init__(self, app, routine, provider, intent_id):
        super().__init__(app, routine, provider)
        self.intent_id = intent_id

    def _command_id(self, operation_id, observed_at):
        return "activity-interrupt-" + digest([self.routine.context.key, operation_id, observed_at])

    def _intent(self):
        record = queue.get(self.runtime, self.intent_id)
        try:
            required = {"owner_key", "activity_id", "revision", "sleep_context_key", "event_context", "event_key",
                        "observed_at", "grace_minutes"}
            if (type(record) is not dict or set(record) != required or record["owner_key"] != digest(self.routine.owner)
                    or record["sleep_context_key"] != self.routine.context.key or type(record["revision"]) is not int
                    or record["revision"] < 1 or type(record["grace_minutes"]) is not int
                    or not 0 <= record["grace_minutes"] <= 1440
                    or self.intent_id != queue.PREFIX + digest([record["activity_id"], record["revision"]])):
                raise ValueError("invalid interruption")
            context = TaskContext.restore(record["event_context"])
            row = self.runtime.read_one("SELECT event_json FROM conversation_inputs WHERE event_key=?", (record["event_key"],))
            if row is None:
                raise ValueError("message was not admitted")
            event = event_from_document(json.loads(row["event_json"]), context)
            at = datetime.fromisoformat(record["observed_at"])
            if (at.utcoffset() is None or event.scoped_key != record["event_key"] or event.scope != context.scope
                    or role_owner(context) != self.routine.owner or event.received_at > at):
                raise ValueError("message identity changed")
            return record, at
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise TaskError("life_sleep_interruption_invalid", "original sleep interruption is unavailable") from error

    def _expected(self, activity, at):
        record, expected_at = self._intent()
        if (activity.activity_id != record["activity_id"] or activity.revision != record["revision"]
                or activity.state is not State.ACTIVE or activity.work is None or activity.work.kind != "sleep"
                or at != expected_at or at < activity.updated_at):
            raise TaskError("life_sleep_interruption_invalid", "sleep changed before the original interruption")
        body = self.app.role_state.body_clock
        receipt = activity.sleep or body._legacy_receipt(activity)
        segment = max(0, round((at - activity.updated_at).total_seconds()))
        interruptions, early = receipt.interruptions + 1, receipt.early_interruptions + int(segment < 20 * 60)
        debt = body.profile(self.routine.context, now=at)["sleep_debt_minutes"]
        low, high = sleep_quality_range(receipt.workload_score, debt, interruptions, early)
        sleep = replace(receipt, slept_seconds=min(172800, receipt.slept_seconds + segment),
                        interruptions=interruptions, early_interruptions=early, quality_min=low, quality_max=high,
                        wake_until=at + timedelta(minutes=record["grace_minutes"]))
        return {"state": "paused", "checkpoint": "用户消息打断睡眠；进入清醒宽限后再评估是否续睡",
                "work": None, "sleep": json.loads(encode(sleep)),
                "operation_id": "sleep-interrupt-" + digest([activity.activity_id, activity.revision]),
                "event_consumers": []}, debt

    def _proposal(self, activity, observed_at, debt):
        record, _ = self._intent()
        return self.provider.propose_interrupt_sleep(activity=json.loads(encode(activity)), observed_at=stamp(observed_at),
            sleep_debt_minutes=debt, grace_minutes=record["grace_minutes"])

    async def execute_intent(self):
        self._outside_host_transaction()
        record, at = self._intent()
        operation_id = "sleep-interrupt-" + digest([record["activity_id"], record["revision"]])
        command_id = self._command_id(operation_id, at)
        request = self.provider.request_for_replay(command_id=command_id, owner_key=record["owner_key"],
                                                   context_key=self.routine.context.key)
        if request is not None:
            result = await self.recover(request)
        else:
            await self.routine.preflight()
            request = self.prepare(record["activity_id"], expected_revision=record["revision"], observed_at=at)
            try:
                decision = self.decide(request, observed_at=self.app.now())
            except (TaskError, ValueError):
                self.abort(request, observed_at=self.app.now())
                raise
            result = self.apply(request, decision)
            self.complete(request, decision, observed_at=self.app.now())
            self.acknowledge(request, decision)
            self.app.notify()
        queue.acknowledge(self.runtime, self.intent_id, record)
        return result
