"""Audit a life-proposed command against an already durable model asset.

Reuses the existing prepare/decide/resolve/confirm barrier. This adapter checks
identity, revision, sources and output bindings; it does not choose spans,
construct activity prompts, parse impressions or plan subsequent activities.
"""
import json

from companion_v2.execution.models import TaskError, digest
from companion_v2.foundation.outcome import Outcome
from companion_v2.model_gateway.life_tasks import PREFIX
from . import activity_transition_authority
from .natural_wake_handoff import NaturalWakeHandoff


def field(document, path):
    for name in path.split("."):
        document = document[name]
    return document


class ModelCompletionHandoff(NaturalWakeHandoff):
    prefix = "life.model-completion.v1:"

    def __init__(self, app, routine, provider, proof):
        super().__init__(app, routine, provider)
        self.proof = proof

    def _parameters(self, request):
        return digest([self.routine.owner, request])

    def _authorize(self, request, proposal, *, observed_at):
        self.routine.authorize()
        task_id, params_digest = self.proof["task_id"], self.proof["params_digest"]
        task = self.app.runtime.read_one("SELECT state FROM execution_tasks WHERE task_id=?", (task_id,))
        row = self.app.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?", (PREFIX + task_id,))
        if task is None or task["state"] != "succeeded" or row is None:
            raise TaskError("life_activity_evidence_required", "original model result is unavailable")
        asset = json.loads(row["value"])
        if asset["params_digest"] != params_digest or asset["context_key"] != self.routine.context.key:
            raise TaskError("life_activity_evidence_required", "model result authority changed")
        original = json.loads(proposal["previous_json"])
        source = original.get("work", {}).get("source")
        if source is not None and not any(json.loads(ref["permission_ref"]) == source for ref in asset["sources"]):
            raise TaskError("life_activity_evidence_required", "model asset lacks the original activity source")
        if self.proof["record_ref"] != {"activity_id": original["activity_id"], "revision": original["revision"]}:
            raise TaskError("life_revision_conflict", "model input revision changed")
        # The submitted input digest binds the expected record to the model
        # request, rather than trusting a newly supplied proof after generation.
        from companion_v2.model_gateway.life_tasks import PREFIX as RESULT_PREFIX
        manifest = self.app.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?",
                                             (RESULT_PREFIX + task_id + ":manifest",))
        if manifest is None or json.loads(manifest["value"]) != self.proof:
            raise TaskError("life_activity_evidence_required", "model output bindings changed")
        result = json.loads(asset["text"])
        proposed = json.loads(proposal["result_json"])
        try:
            if not self.proof["bindings"] or any(field(proposed, target) != field(result, source)
                    for target, source in self.proof["bindings"].items()):
                raise ValueError("model output differs")
        except (KeyError, TypeError, ValueError) as error:
            raise TaskError("life_activity_evidence_required", "activity output differs from the original model result") from error
        return activity_transition_authority.authorize(self.app.tasks, self.app.role_journal.sources,
            self.routine.context, request, proposal, expected_operation_id=request["payload"]["operation_id"],
            expected_consumers=request["payload"]["event_consumers"], observed_at=observed_at,
            verify_completion=lambda *_: Outcome.success(True))
