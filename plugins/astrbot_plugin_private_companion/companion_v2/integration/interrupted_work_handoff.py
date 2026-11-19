"""Pause reading or browsing left active when the life executor restarts."""

from datetime import datetime
import json

from companion_v2.execution.models import TaskError, digest, encode, stamp
from companion_v2.role_work.codec import activity_from_json
from companion_v2.role_work.models import RoleActivityState as State

from . import activity_transition_authority
from .natural_wake_handoff import NaturalWakeHandoff


class InterruptedWorkHandoff(NaturalWakeHandoff):
    prefix = "life.activity.interrupted-work.v1:"

    def _command_id(self, operation_id, observed_at):
        return "activity-interrupted-" + digest([self.routine.context.key, operation_id, observed_at])

    def _expected(self, activity, at):
        if (activity.state is not State.ACTIVE or activity.work is None
                or activity.work.kind not in ("reading", "social") or at < activity.updated_at):
            raise TaskError("life_interrupted_invalid", "activity is not interrupted reading or browsing")
        kind = activity.work.kind
        checkpoint = ("interrupted; inspect the original model result before retrying" if kind == "reading"
                      else "浏览中断；不会重复结果不明的互动")
        return {"state": "paused", "checkpoint": checkpoint, "work": None, "sleep": None,
                "operation_id": "recover-" + kind + "-" + digest([activity.activity_id, activity.revision]),
                "event_consumers": []}, None

    def _proposal(self, activity, observed_at, _unused):
        return self.provider.propose_interrupted_work(activity=json.loads(encode(activity)), observed_at=stamp(observed_at))

    def _authorize(self, request, proposal, *, observed_at):
        self.routine.authorize()
        if not self.routine.settings or not self.routine.settings["enabled"]:
            raise TaskError("life_disabled", "the original life routine is disabled")
        previous = activity_from_json(proposal["previous_json"])
        payload = request["payload"]
        at = datetime.fromisoformat(payload["observed_at"])
        expected, _ = self._expected(previous, at)
        if (any(payload[key] != expected[key] for key in
                ("state", "checkpoint", "work", "sleep", "operation_id", "event_consumers"))
                or request["command_id"] != self._command_id(expected["operation_id"], at)):
            raise TaskError("life_activity_transition_invalid", "interrupted activity outcome changed")
        return activity_transition_authority.authorize(self.app.tasks, self.app.role_journal.sources,
            self.routine.context, request, proposal, expected_operation_id=expected["operation_id"],
            expected_consumers=[], observed_at=observed_at)
