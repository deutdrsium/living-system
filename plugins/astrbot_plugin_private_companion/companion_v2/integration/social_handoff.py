"""Authorize a supervised browsing outcome in the life-owned ledger."""

from datetime import datetime, timedelta
import hashlib
import json
from zoneinfo import ZoneInfo

from companion_v2.execution.models import TaskError, digest, encode, stamp
from companion_v2.foundation.outcome import Outcome
from companion_v2.role_work.codec import activity_from_json
from companion_v2.role_work.models import RoleActivity, RoleActivityState as State
from companion_v2.role_work.work_models import RoleWorkSpec

from . import activity_creation_authority, activity_transition_authority, life_commands
from .natural_wake_handoff import NaturalWakeHandoff
from .scheduled_sleep_handoff import ScheduledSleepHandoff


class SocialStartHandoff(ScheduledSleepHandoff):
    def __init__(self, app, routine, provider, snapshot, plan):
        super().__init__(app, routine, provider)
        self.snapshot_callback, self.plan = snapshot, plan

    def _validate_social(self, snapshot):
        from companion_v2.role_work.life.smart_social import social_window
        plan = self.plan
        if (type(plan) is not dict or set(plan) != {"request_id", "schedule", "next_sleep"}
                or type(plan["request_id"]) is not str or not plan["request_id"].startswith("social-")
                or len(plan["request_id"]) > 128 or type(plan["schedule"]) is not str):
            raise TaskError("life_social_invalid", "browsing plan is invalid")
        now = datetime.fromisoformat(snapshot["observed_at"])
        next_sleep = datetime.fromisoformat(plan["next_sleep"]) if plan["next_sleep"] else None
        if next_sleep is not None and next_sleep.utcoffset() is None:
            raise TaskError("life_social_invalid", "sleep boundary needs a timezone")
        last = [datetime.fromisoformat(item["updated_at"]) for item in snapshot["records"]
                if item["work"]["kind"] == "social" and item["state"] in ("completed", "paused", "cancelled")]
        if (snapshot["busy"] or not snapshot["social_available"]
                or (snapshot["pace_until"] and now < datetime.fromisoformat(snapshot["pace_until"]))
                or (last and now < max(last) + timedelta(minutes=snapshot["social_interval_minutes"]))
                or social_window(snapshot["calendar"], now, now.astimezone(ZoneInfo(snapshot["timezone"])),
                                 next_sleep if snapshot["sleep_enabled"] else None) != plan["schedule"]):
            raise TaskError("life_social_invalid", "browsing is no longer due")

    def prepare(self, *, observed_at):
        snapshot = self.snapshot_callback()
        self._validate_social(snapshot)
        context, plan = self.routine.context, self.plan
        expected = RoleActivity("social-activity-" + digest([context.key, plan["request_id"]]),
            self.routine.owner.persona_id, context.scope, observed_at, State.ACTIVE, description="自主浏览外部内容",
            work=RoleWorkSpec("social", context.key, plan["request_id"]))
        owner = json.loads(encode(self.routine.owner))
        proposal = self.provider.propose_social(owner=owner, scope=json.loads(encode(context.scope)), context_key=context.key,
                                                request_id=plan["request_id"], observed_at=stamp(observed_at))
        if proposal != json.loads(encode(expected)):
            raise TaskError("life_activity_creation_invalid", "browsing proposal changed")
        request = {"command_id": "activity-social-start-" + digest([context.key, plan["request_id"], observed_at]),
            "owner_key": digest(self.routine.owner), "context_key": context.key, "kind": "activity.create",
            "expected_revision": 0, "payload": {"owner": owner, "activity": proposal}}
        self.provider.prepare(request, prepared_at=observed_at)
        return request

    def decide(self, request, *, observed_at):
        self._outside_host_transaction()
        _, request_hash = life_commands._request(self.routine.context, request)
        decision = life_commands.lookup(self.runtime, context_key=self.routine.context.key,
                                        command_id=request["command_id"], request_hash=request_hash)
        if decision is not None:
            return decision
        snapshot = self.snapshot_callback()
        self._validate_social(snapshot)
        activity = activity_from_json(encode(request["payload"]["activity"]))
        if (activity.work.request_id != self.plan["request_id"] or request["command_id"] !=
                "activity-social-start-" + digest([self.routine.context.key, self.plan["request_id"], activity.started_at])):
            raise TaskError("life_activity_creation_invalid", "browsing start identity changed")
        def authorize(_context, at):
            return activity_creation_authority.authorize_identity(self.app.tasks, self.routine.context, request, observed_at=at)
        with self.app.role_life.consistent_snapshot():
            return life_commands.decide(self.runtime, context=self.routine.context, request=request, outcome="commit",
                observed_at=observed_at, verify_prepared=self.provider.verify_prepared, authorize=authorize, apply=lambda *_: None)

    async def execute(self):
        await self.routine.preflight()
        at = self.app.now()
        request = self.prepare(observed_at=at)
        try:
            decision = self.decide(request, observed_at=at)
        except (TaskError, ValueError):
            self.abort(request, observed_at=at)
            raise
        return self.finish(request, decision, observed_at=at)


class SocialCompletionHandoff(NaturalWakeHandoff):
    prefix = "life.activity.social-completion.v1:"

    def __init__(self, app, routine, provider, *, run=None, job=None, checkpoint=None, completed=None, effect_started=False):
        super().__init__(app, routine, provider)
        self.run, self.job = run, job
        self.checkpoint, self.completed, self.effect_started = checkpoint, completed, effect_started

    def _command_id(self, operation_id, observed_at):
        return "activity-social-complete-" + digest([self.routine.context.key, operation_id, observed_at])

    def _authorize(self, request, proposal, *, observed_at):
        if self.run is None or self.job is None or type(self.completed) is not bool:
            raise TaskError("life_activity_evidence_required", "browsing execution evidence is unavailable")
        self.routine.authorize()
        previous = activity_from_json(proposal["previous_json"])
        payload = request["payload"]
        operation_id = "social-complete-" + digest(previous.activity_id)
        at = datetime.fromisoformat(payload["observed_at"])
        consumers = ["journal", "interest"] if self.completed else []
        if (previous != self.job.activity or previous.state is not State.ACTIVE or previous.work.kind != "social"
                or self.job.context != self.routine.context or self.job.instruction != self.routine.instruction
                or payload["state"] != ("completed" if self.completed else "cancelled")
                or payload["checkpoint"] != self.checkpoint or payload["reading"] is not None
                or payload["work"] is not None or payload["sleep"] is not None
                or request["command_id"] != self._command_id(operation_id, at)):
            raise TaskError("life_activity_transition_invalid", "browsing outcome differs from its supervised execution")
        authorized = activity_transition_authority.authorize(self.app.tasks, self.app.role_journal.sources,
            self.routine.context, request, proposal, expected_operation_id=operation_id, expected_consumers=consumers,
            observed_at=observed_at, verify_completion=lambda *_: Outcome.success(True))
        self.run.validate()
        if not self.effect_started:
            self.run.begin_effect()
            self.effect_started = True
        return authorized

    def execute_completion(self, activity, *, observed_at):
        self._outside_host_transaction()
        if self.app.role_life.activity_owner() != "good_life" or self.run is None:
            raise TaskError("life_activity_owner_invalid", "browsing has no active executor")
        with self.app.role_life.consistent_snapshot():
            if self.routine.activity(activity.activity_id) != activity:
                raise TaskError("life_revision_conflict", "browsing activity changed")
        operation_id = "social-complete-" + digest(activity.activity_id)
        request = {"command_id": self._command_id(operation_id, observed_at), "owner_key": digest(self.routine.owner),
            "context_key": self.routine.context.key, "kind": "activity.transition", "expected_revision": activity.revision,
            "payload": {"activity_id": activity.activity_id, "operation_id": operation_id,
                "previous_hash": hashlib.sha256(encode(activity).encode("utf-8")).hexdigest(),
                "state": "completed" if self.completed else "cancelled", "observed_at": stamp(observed_at),
                "checkpoint": self.checkpoint, "reading": None, "work": None, "sleep": None,
                "event_consumers": ["journal", "interest"] if self.completed else []}}
        self.provider.prepare(request, prepared_at=observed_at)
        try:
            decision = self.decide(request, observed_at=observed_at)
        except (TaskError, ValueError):
            self.abort(request, observed_at=observed_at)
            raise
        result = self.apply(request, decision)
        self.complete(request, decision, observed_at=observed_at)
        self.acknowledge(request, decision)
        self.app.notify()
        return activity_from_json(result["result"]["record_json"])

    async def recover(self, request):
        self._outside_host_transaction()
        _, request_hash = life_commands._request(self.routine.context, request)
        decision = life_commands.lookup(self.runtime, context_key=self.routine.context.key,
                                        command_id=request["command_id"], request_hash=request_hash)
        if decision is None:
            self.abort(request, observed_at=self.app.now())
            return {"state": "aborted"}
        return await super().recover(request)


def complete_supervised_social(app, run, job, checkpoint, completed, effect_started):
    routine = app.life_runtime.routines.get(job.context.key)
    if (routine is None or routine.context != job.context or routine.instruction != job.instruction
            or routine._closed or routine._retired):
        raise TaskError("life_context_changed", "the original browsing context is unavailable")
    return SocialCompletionHandoff(app, routine, app.role_life.transition_provider(), run=run, job=job,
        checkpoint=checkpoint, completed=completed, effect_started=effect_started).execute_completion(job.activity, observed_at=app.now())
