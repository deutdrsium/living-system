from copy import deepcopy
from dataclasses import asdict, replace
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life")]
from companion_v2.execution.models import TaskError, digest, encode, require, stamp
from companion_v2.foundation.outcome import Outcome
from companion_v2.foundation.refs import OperationRef, RoleOwnerRef, SourceRef, VersionRef
from companion_v2.integration import activity_transition_authority, life_commands
from companion_v2.role_work.models import RoleActivity, RoleActivityState, RoleReadingReceipt
from companion_v2.role_work.work_models import RoleWorkSpec
from companion_v2.sources.models import SourceRecord
from companion_v2.storage.models import OperationState
from companion_v2.storage.repositories import OperationRepository
from good_life.activity_import import stage_activities
from good_life.activity_records import StagedActivityRecords
from good_life.prepared_commands import PreparedCommands
from good_life.staged_activity_commands import StagedActivityTransitions
from good_life.storage import LifeStorage
from tests.test_management_access import NOW
from tests.test_role_state import StateFixture


def wire(value):
    return json.loads(encode(value))


class ActivityTransitionAuthorityTests(StateFixture, unittest.IsolatedAsyncioTestCase):
    async def prepare_reading(self, mode="complete"):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        host, app, context, _manager = self.setup_state()
        self.grant(context.actor, context.scope, "role.life.run")
        self.grant(context.actor, context.scope, "role.reading.record")
        await self.start(host)
        text = "hello world"
        source = SourceRef("source", "library", context.scope, VersionRef("source", 1), NOW)
        app.role_journal.sources.publish(SourceRecord(source, context.audience, digest(text), text), observed_at=NOW).unwrap()
        owner = RoleOwnerRef(context.scope.installation_id, context.scope.bot_id, context.role_binding.role.subject_id)
        work = RoleWorkSpec("reading", context.key, "reading-request", source, digest(text), 0, 5)
        activity = RoleActivity("reading", owner.persona_id, context.scope, NOW, RoleActivityState.ACTIVE, work=work)
        app.role_life.add_activity(activity)
        version = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key='schema_version'", ())["value"]
        stage_activities(self.runtime.database_path, expected_version=version, destination=Path(temporary.name) / "life")
        life = LifeStorage.open(Path(temporary.name) / "life")
        self.addCleanup(life.close)
        commands = PreparedCommands(life)
        commands.install()
        transitions = StagedActivityTransitions(life)
        reading = (RoleReadingReceipt(source, digest(text), len(text), 0, 5, NOW + timedelta(seconds=1), "reading note")
                   if mode == "complete" else None)
        changed_work = replace(work, start_offset=6, end_offset=11) if mode == "skip" else None
        state = "completed" if mode in ("complete", "missing_receipt") else "active" if mode == "skip" else "paused"
        consumers = ["journal"] if state == "completed" else []
        request = {"command_id": "reading-change", "owner_key": digest(owner), "context_key": context.key,
            "kind": "activity.transition", "expected_revision": 1,
            "payload": {"activity_id": activity.activity_id, "operation_id": "reading-operation",
                "previous_hash": hashlib.sha256(encode(activity).encode("utf-8")).hexdigest(),
                "state": state, "observed_at": stamp(NOW + timedelta(seconds=1)), "checkpoint": None,
                "reading": wire(reading), "work": wire(changed_work), "sleep": None, "event_consumers": consumers}}
        transitions.prepare(request, prepared_at=NOW)
        return app, context, source, request, transitions.proposal(request), commands

    def authorize(self, app, context, request, proposal, *, proof=None):
        return activity_transition_authority.authorize(app.tasks, app.role_journal.sources, context, request, proposal,
            expected_operation_id="reading-operation", expected_consumers=("journal",) if request["payload"]["state"] == "completed" else (),
            observed_at=NOW + timedelta(seconds=1), verify_completion=proof)

    async def test_reading_completion_needs_host_evidence_and_current_source(self):
        app, context, source, request, proposal, commands = await self.prepare_reading()
        with self.assertRaises(TaskError) as missing:
            self.authorize(app, context, request, proposal)
        self.assertEqual(missing.exception.code, "life_activity_evidence_required")
        async def delayed_proof(*_args):
            return Outcome.success(True)
        with self.assertRaises(TaskError) as asynchronous:
            self.authorize(app, context, request, proposal, proof=delayed_proof)
        self.assertEqual(asynchronous.exception.code, "life_activity_evidence_required")
        proof = lambda *_: Outcome.success(True)
        self.assertTrue(self.authorize(app, context, request, proposal, proof=proof).is_success)
        forged = deepcopy(proposal)
        result = json.loads(forged["result_json"])
        result["reading"]["reflection"] = "different note"
        forged["result_json"] = encode(result)
        with self.assertRaises(TaskError):
            self.authorize(app, context, request, forged, proof=proof)
        altered = deepcopy(request)
        altered["payload"]["event_consumers"] = ["unrelated-consumer"]
        with self.assertRaises(TaskError):
            self.authorize(app, context, altered, proposal, proof=proof)
        self.revoke_source(app, source)
        with self.assertRaises(TaskError):
            life_commands.decide(self.runtime, context=context, request=request, outcome="commit", observed_at=NOW + timedelta(seconds=1),
                verify_prepared=commands.verify_prepared,
                authorize=lambda value, _at: self.authorize(app, value, request, proposal, proof=proof), apply=lambda *_: None)
        self.assertEqual(commands.pending()[0]["state"], "prepared")

    async def test_resume_cannot_skip_unread_text(self):
        app, context, _source, request, proposal, _commands = await self.prepare_reading("skip")
        with self.assertRaises(TaskError) as skipped:
            self.authorize(app, context, request, proposal)
        self.assertEqual(skipped.exception.code, "life_reading_source_changed")

    async def test_revoked_source_does_not_prevent_pausing_an_activity(self):
        app, context, source, request, proposal, _commands = await self.prepare_reading("pause")
        self.revoke_source(app, source)
        self.assertTrue(self.authorize(app, context, request, proposal).is_success)

    async def test_reading_cannot_complete_without_its_consumed_span(self):
        app, context, _source, request, proposal, _commands = await self.prepare_reading("missing_receipt")
        with self.assertRaises(TaskError) as missing:
            self.authorize(app, context, request, proposal, proof=lambda *_: Outcome.success(True))
        self.assertEqual(missing.exception.code, "life_reading_source_changed")

    async def test_authorized_reading_commits_one_original_receipt_and_event(self):
        app, context, _source, request, proposal, commands = await self.prepare_reading()
        owner = RoleOwnerRef(context.scope.installation_id, context.scope.bot_id, context.role_binding.role.subject_id)
        reading = json.loads(proposal["result_json"])["reading"]
        from companion_v2.role_work.codec import activity_from_json
        reading_record = activity_from_json(proposal["result_json"]).reading
        parameters = [owner, "reading", 1, RoleActivityState.COMPLETED, None, asdict(reading_record)]
        parameter_hash = digest(parameters)
        operation = OperationRef("reading-operation", "role.life", context.scope,
                                 VersionRef("reading-operation", 1), "reading-operation")
        operations = OperationRepository(self.runtime)
        def reserve(_connection, _request):
            require(operations.reserve(operation, parameter_hash=parameter_hash, observed_at=NOW))
        proof = lambda *_: Outcome.success(True)
        decision = life_commands.decide(self.runtime, context=context, request=request, outcome="commit", observed_at=NOW + timedelta(seconds=1),
            verify_prepared=commands.verify_prepared,
            authorize=lambda value, _at: self.authorize(app, value, request, proposal, proof=proof), apply=reserve)
        transitions = StagedActivityTransitions(commands.storage)
        commands.resolve(decision, verify_host_decision=lambda value: life_commands.verify_decision(self.runtime, value),
                         apply=transitions.apply)
        def finalize(_connection, _decision):
            require(operations.transition(operation.operation_id, expected_state=OperationState.ACCEPTED,
                                         next_state=OperationState.COMPLETED, observed_at=NOW + timedelta(seconds=1)))
        life_commands.complete(self.runtime, decision, observed_at=NOW + timedelta(seconds=1),
                               verify_life_commit=transitions.verify_commit, finalize=finalize)
        self.assertTrue(transitions.acknowledge(request["command_id"], verify_host_completion=lambda value:
            life_commands.verify_completion(self.runtime, value) and
            operations.get(operation.operation_id).unwrap().parameter_hash == parameter_hash))
        self.assertFalse(transitions.acknowledge(request["command_id"], verify_host_completion=lambda value:
            life_commands.verify_completion(self.runtime, value)))
        self.assertEqual(json.loads(StagedActivityRecords(commands.storage).dispatch("activity_receipt",
            owner_key=digest(owner), operation_id=operation.operation_id))["reading"], reading)
        self.assertEqual(len(commands.storage.pending_events("journal")), 1)


if __name__ == "__main__":
    unittest.main()
