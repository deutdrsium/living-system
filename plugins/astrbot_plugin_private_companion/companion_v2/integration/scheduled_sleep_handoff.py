"""Authorize a scheduled sleep proposed by the life plugin, without sharing the host DB."""

from datetime import datetime, time, timedelta
import json
from threading import get_ident
from zoneinfo import ZoneInfo

from companion_v2.execution.models import TaskError, digest, encode, stamp
from companion_v2.foundation.outcome import Outcome
from companion_v2.role_work.codec import activity_from_json
from companion_v2.role_work.life.physiology import nap_ready, night_sleep_ready, recommended_sleep_minutes, sleep_receipt
from companion_v2.role_work.life.scheduling import bedtime_at
from companion_v2.role_work.models import RoleActivity, RoleActivityState
from companion_v2.role_work.work_models import RoleWorkSpec

from . import activity_creation_authority, life_commands


SLEEP_SETTINGS = ("enabled", "sleep_enabled", "sleep_timezone", "sleep_time", "sleep_variation_minutes",
                  "sleep_window_minutes", "sleep_minutes")


class ScheduledSleepHandoff:
    def __init__(self, app, routine, provider):
        self.app, self.routine, self.provider, self.runtime = app, routine, provider, app.runtime

    def _outside_host_transaction(self):
        diagnostic = getattr(getattr(self.runtime, "_read_snapshot_diagnostics", None), "current", None)
        if getattr(self.runtime, "_write_owner_thread_id", None) == get_ident() or diagnostic is not None:
            raise TaskError("life_activity_transaction_active", "scheduled sleep needs an independent host transaction")

    def snapshot(self, *, at=None):
        self._outside_host_transaction()
        if self.app.role_life.activity_owner() != "good_life":
            raise TaskError("life_activity_owner_invalid", "scheduled sleep belongs to the life plugin")
        self.routine.refresh()
        self.routine.authorize()
        if not self.routine.settings:
            raise TaskError("life_disabled", "the original life routine is disabled")
        at = at or self.app.now()
        with self.app.role_life.consistent_snapshot():
            return self._snapshot_locked(at)

    def _snapshot_locked(self, at):
        settings = {name: self.routine.settings[name] for name in SLEEP_SETTINGS}
        body = self.app.role_state.body_clock
        records = self.routine.activities()
        local = at.astimezone(ZoneInfo(settings["sleep_timezone"]))
        pending = body.pending_sleep(self.routine.context)
        busy = (any(not worker.done() for worker in self.routine._workers.values())
                or any(item.state is RoleActivityState.ACTIVE for item in records) or bool(pending))
        return {"context_key": self.routine.context.key, "owner": json.loads(encode(self.routine.owner)),
                "settings": settings, "config_revision": digest(settings), "observed_at": stamp(at),
                "profile": body.profile(self.routine.context, now=at), "busy": bool(busy),
                "selected_executor": self.app.life_runtime.owns_sleep_schedule(self.routine),
                "active_reading": bool(body.active(self.routine.context, "reading")),
                "napped_today": any(item.sleep and item.sleep.kind == "nap"
                    and item.started_at.astimezone(ZoneInfo(settings["sleep_timezone"])).date() == local.date()
                    for item in body.episodes(self.routine.context))}

    def _validate_plan(self, plan, snapshot, *, at):
        if (type(plan) is not dict or set(plan) != {"kind", "request_id", "minutes", "scheduled_at", "context_key"}
                or plan["context_key"] != snapshot["context_key"] or plan["kind"] not in ("night", "nap")):
            raise TaskError("life_sleep_invalid", "scheduled sleep plan is invalid")
        settings, profile = snapshot["settings"], snapshot["profile"]
        if (not settings["enabled"] or not settings["sleep_enabled"] or snapshot["busy"]
                or snapshot["active_reading"]):
            raise TaskError("life_sleep_invalid", "sleep is no longer available")
        local = at.astimezone(ZoneInfo(settings["sleep_timezone"]))
        if plan["kind"] == "nap":
            if (plan["scheduled_at"] is not None or plan["minutes"] != 25
                    or plan["request_id"] != "daily-nap-" + local.date().isoformat() or snapshot["napped_today"]
                    or not nap_ready(profile, local_minutes=local.hour * 60 + local.minute)):
                raise TaskError("life_sleep_invalid", "nap is no longer due")
            return
        try:
            due = datetime.fromisoformat(plan["scheduled_at"])
            if due.utcoffset() is None:
                raise ValueError("naive scheduled time")
            day = due.astimezone(ZoneInfo(settings["sleep_timezone"])).date()
            expected = bedtime_at(self.routine.owner, day, settings["sleep_timezone"],
                                  time.fromisoformat(settings["sleep_time"]), settings["sleep_variation_minutes"])
        except (TypeError, ValueError) as error:
            raise TaskError("life_sleep_invalid", "night sleep time is invalid") from error
        if (not snapshot["selected_executor"] or due != expected or due > at
                or at >= due + timedelta(minutes=settings["sleep_minutes"])
                or plan["minutes"] != settings["sleep_minutes"]
                or plan["request_id"] != "daily-sleep-" + day.isoformat()
                or (at < due + timedelta(minutes=settings["sleep_window_minutes"]) and not night_sleep_ready(profile))):
            raise TaskError("life_sleep_invalid", "night sleep is no longer due")

    def _expected(self, plan, snapshot, at, proposed_wake):
        settings, profile = snapshot["settings"], snapshot["profile"]
        base = settings["sleep_minutes"]
        target = 25 if plan["kind"] == "nap" else recommended_sleep_minutes(base, profile["sleep_debt_minutes"], profile["workload_score"])
        due = at + timedelta(minutes=target)
        if plan["kind"] == "night" and at < proposed_wake < due:
            # Life may shorten a night episode to fit a class alarm. The base need still accrues debt.
            due = proposed_wake
            target = max(1, (due - at) // timedelta(minutes=1))
        receipt = sleep_receipt(kind=plan["kind"], base_need_minutes=base, target_minutes=target,
                                workload_score=profile["workload_score"], sleep_debt_minutes=profile["sleep_debt_minutes"])
        context = self.routine.context
        work = RoleWorkSpec("sleep", context.key, plan["request_id"], due_at=due)
        return RoleActivity("sleep-" + digest([context.key, plan["request_id"]]), self.routine.owner.persona_id,
            context.scope, at, RoleActivityState.ACTIVE,
            description="角色短暂午休" if plan["kind"] == "nap" else "角色开始一段睡眠", work=work, sleep=receipt)

    def prepare(self, plan, *, observed_at):
        snapshot = self.snapshot(at=observed_at)
        self._validate_plan(plan, snapshot, at=observed_at)
        profile = snapshot["profile"]
        proposal = self.provider.propose_scheduled_sleep(owner=snapshot["owner"], scope=json.loads(encode(self.routine.context.scope)),
            context_key=self.routine.context.key, request_id=plan["request_id"], observed_at=stamp(observed_at), kind=plan["kind"],
            base_need_minutes=snapshot["settings"]["sleep_minutes"], workload_score=profile["workload_score"],
            sleep_debt_minutes=profile["sleep_debt_minutes"])
        proposed_wake = datetime.fromisoformat(proposal["work"]["due_at"])
        expected = self._expected(plan, snapshot, observed_at, proposed_wake)
        if proposal != json.loads(encode(expected)):
            raise TaskError("life_activity_creation_invalid", "scheduled sleep proposal changed")
        request = {"command_id": "activity-scheduled-" + digest([self.routine.context.key, plan["request_id"], observed_at]),
            "owner_key": digest(self.routine.owner), "context_key": self.routine.context.key,
            "kind": "activity.create", "expected_revision": 0,
            "payload": {"owner": snapshot["owner"], "activity": proposal,
                        "schedule": {"kind": plan["kind"], "planned_at": plan["scheduled_at"],
                                     "config_hash": snapshot["config_revision"]}}}
        self.provider.prepare(request, prepared_at=observed_at)
        return request

    def _authorize(self, request, snapshot, *, observed_at):
        activity_creation_authority.authorize_identity(self.app.tasks, self.routine.context, request, observed_at=observed_at)
        activity = activity_from_json(encode(request["payload"]["activity"]))
        schedule = request["payload"]["schedule"]
        plan = {"kind": schedule["kind"], "request_id": activity.work.request_id,
                "minutes": 25 if schedule["kind"] == "nap" else snapshot["settings"]["sleep_minutes"],
                "scheduled_at": schedule["planned_at"], "context_key": self.routine.context.key}
        self._validate_plan(plan, snapshot, at=activity.started_at)
        if (schedule["config_hash"] != snapshot["config_revision"] or activity != self._expected(plan, snapshot, activity.started_at, activity.work.due_at)
                or request["command_id"] != "activity-scheduled-" + digest([self.routine.context.key, plan["request_id"], activity.started_at])):
            raise TaskError("life_activity_creation_invalid", "scheduled sleep inputs changed")
        return Outcome.success(True)

    def decide(self, request, *, observed_at):
        self._outside_host_transaction()
        _, request_hash = life_commands._request(self.routine.context, request)
        decision = life_commands.lookup(self.runtime, context_key=self.routine.context.key,
                                        command_id=request["command_id"], request_hash=request_hash)
        if decision is not None:
            return decision
        self.routine.refresh()
        self.routine.authorize()
        started_at = activity_from_json(encode(request["payload"]["activity"])).started_at
        with self.app.role_life.consistent_snapshot():
            snapshot = self._snapshot_locked(started_at)
            return life_commands.decide(self.runtime, context=self.routine.context, request=request, outcome="commit",
                observed_at=observed_at, verify_prepared=self.provider.verify_prepared,
                authorize=lambda _context, at: self._authorize(request, snapshot, observed_at=at), apply=lambda *_: None)

    def abort(self, request, *, observed_at):
        decision = life_commands.decide(self.runtime, context=self.routine.context, request=request, outcome="abort",
            observed_at=observed_at, verify_prepared=self.provider.verify_prepared, authorize=lambda *_: None, apply=lambda *_: None)
        return self.provider.resolve(decision, verify_host_decision=lambda value: life_commands.verify_decision(self.runtime, value))

    def finish(self, request, decision, *, observed_at):
        _, request_hash = life_commands._request(self.routine.context, request)
        if (decision.get("decision") != "commit" or decision.get("command_id") != request["command_id"]
                or decision.get("request_hash") != request_hash or not life_commands.verify_decision(self.runtime, decision)):
            raise TaskError("life_command_invalid", "scheduled sleep decision is unavailable")
        result = self.provider.resolve(decision, verify_host_decision=lambda value: life_commands.verify_decision(self.runtime, value))
        life_commands.complete(self.runtime, decision, observed_at=observed_at, verify_life_commit=self.provider.verify_commit)
        self.provider.acknowledge(decision["command_id"], verify_host_completion=lambda evidence: life_commands.verify_completion(self.runtime, evidence))
        self.app.notify()
        return result

    async def execute(self, plan):
        await self.routine.preflight()
        at = self.app.now()
        snapshot = self.snapshot(at=at)
        self._validate_plan(plan, snapshot, at=at)
        identifier = "sleep-" + digest([self.routine.context.key, plan["request_id"]])
        try:
            self.routine.activity(identifier)
        except TaskError as error:
            if error.code != "life_activity_unavailable":
                raise
        else:
            return {"state": "already_exists"}
        request = self.prepare(plan, observed_at=at)
        try:
            decision = self.decide(request, observed_at=at)
        except (TaskError, ValueError):
            self.abort(request, observed_at=at)
            raise
        return self.finish(request, decision, observed_at=at)

    async def recover(self, request):
        self._outside_host_transaction()
        _, request_hash = life_commands._request(self.routine.context, request)
        decision = life_commands.lookup(self.runtime, context_key=self.routine.context.key,
                                        command_id=request["command_id"], request_hash=request_hash)
        if decision is None:
            self.abort(request, observed_at=self.app.now())
            return {"state": "aborted"}
        return self.finish(request, decision, observed_at=self.app.now())
