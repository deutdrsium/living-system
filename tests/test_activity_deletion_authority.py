from copy import deepcopy
from datetime import timedelta
import hashlib
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life"), str(ROOT / "tools")]
from companion_v2.execution.models import TaskError, digest, encode, stamp
from companion_v2.foundation.refs import RoleOwnerRef, SourceRef, VersionRef
from companion_v2.identity_policy.models import AuthorizationTier
from companion_v2.integration import activity_deletion_authority, life_commands
from companion_v2.integration.activity_deletion_handoff import ActivityDeletionHandoff
from companion_v2.integration.life_ledger_router import LIFE_ACTIVITY_OWNER_KEY
from companion_v2.role_work.life.management import RoleLifeManagement
from companion_v2.role_work.models import RoleActivity, RoleActivityState as State
from companion_v2.role_work.work_models import RoleWorkSpec
from companion_v2.sources.models import SourceRecord
from companion_v2.storage.models import OperationState
from companion_v2.storage.repositories import OperationRepository
from good_life.activity_import import stage_activities
from good_life.activity_records import ActivityTransitionPending, StagedActivityRecords
from good_life.prepared_commands import PreparedCommands
from good_life.staged_activity_deletions import StagedActivityDeletions
from good_life.storage import LifeStorage
from rehearse_activity_rollback import rehearse
from tests.test_management_access import NOW
from tests.test_role_state import StateFixture


class ActivityDeletionAuthorityTests(StateFixture, unittest.IsolatedAsyncioTestCase):
    async def prepare_removal(self, *, prepared=True, related=False, related_state=State.PLANNED, legacy_request=False,
                              target_state=State.CANCELLED):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        host, app, context, _ = self.setup_state()
        await self.start(host)
        app.life_runtime.bind(context, "test persona")
        manager = RoleLifeManagement(app, context)
        owner = RoleOwnerRef(context.scope.installation_id, context.scope.bot_id, context.role_binding.role.subject_id)
        source = SourceRef("source", "library", context.scope, VersionRef("source", 1), NOW)
        app.role_journal.sources.publish(SourceRecord(source, context.audience, digest("hello"), "hello"), observed_at=NOW).unwrap()
        work = RoleWorkSpec("reading", context.key, "reading-request", source, digest("hello"), 0, 5)
        activity = RoleActivity("book", owner.persona_id, context.scope, NOW, target_state, work=work)
        app.role_life.add_activity(activity)
        if related:
            app.role_life.add_activity(RoleActivity("related", owner.persona_id, context.scope, NOW, related_state, work=work))
        version = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key='schema_version'", ())["value"]
        stage_activities(self.runtime.database_path, expected_version=version, destination=Path(temporary.name) / "life")
        life = LifeStorage.open(Path(temporary.name) / "life")
        self.addCleanup(life.close)
        PreparedCommands(life).install()
        deletion = StagedActivityDeletions(life)
        access = manager.authorize(self.core)
        data = {"action": "remove", "activity_id": "book", "expected_revision": 1, "operation_key": "remove-book",
                "view_token": manager.token(self.core, access, "book", 1)}
        proposal = deletion.describe(owner_key=digest(owner), activity_id="book")
        request = {"command_id": "deletion", "owner_key": digest(owner), "context_key": context.key,
            "kind": "activity.delete", "expected_revision": 1,
            "payload": {"activity_id": "book", "management_operation_id": manager.operation_id("life-command-", self.core, "remove-book"),
                "management_parameter_hash": digest(data), "rows_hash": proposal["rows_hash"],
                "source_hash": digest(activity.work.source),
                "observed_at": stamp(NOW), "related": [],
                "previous_hash": hashlib.sha256(encode(activity).encode("utf-8")).hexdigest()}}
        if legacy_request:
            del request["payload"]["observed_at"]
            del request["payload"]["related"]
        if prepared:
            deletion.prepare(request, prepared_at=NOW + timedelta(seconds=1))
            proposal = deletion.proposal(request)
        return manager, data, request, proposal, deletion, source, life, Path(temporary.name) / "life"

    async def test_host_checks_management_operator_token_and_original_deletion(self):
        manager, data, request, proposal, _, _, _, _ = await self.prepare_removal()
        self.assertTrue(activity_deletion_authority.authorize(manager, self.core, data, request, proposal).is_success)
        with self.assertRaises(TaskError):
            activity_deletion_authority.authorize(manager, self.important, data, request, proposal)
        stale = {**data, "view_token": "stale"}
        with self.assertRaises(TaskError):
            activity_deletion_authority.authorize(manager, self.core, stale, request, proposal)
        forged = deepcopy(request)
        forged["payload"]["management_operation_id"] = "another-operation"
        with self.assertRaises(TaskError):
            activity_deletion_authority.authorize(manager, self.core, data, forged, proposal)
        altered = {**proposal, "previous_json": proposal["previous_json"].replace('"role_id":', '"role_id": ')}
        with self.assertRaises(TaskError):
            activity_deletion_authority.authorize(manager, self.core, data, request, altered)

    async def test_handoff_revokes_source_and_recovers_after_both_restarts(self):
        manager, data, _, _, deletion, source, life, life_path = await self.prepare_removal(prepared=False)
        at = NOW + timedelta(seconds=1)
        handoff = ActivityDeletionHandoff(manager, deletion)
        request = handoff.prepare(self.core, data, observed_at=at)
        decision = handoff.decide(self.core, data, request, observed_at=at)
        self.assertFalse(manager.app.role_journal.sources.validate_reference(source, observed_at=at).is_success)
        self.assertEqual(handoff.decide(self.core, data, request, observed_at=at), decision)
        life.close()
        reopened = LifeStorage.open(life_path)
        self.addCleanup(reopened.close)
        handoff = ActivityDeletionHandoff(manager, StagedActivityDeletions(reopened))
        recovered = life_commands.lookup(self.runtime, context_key=manager.context.key,
            command_id=request["command_id"], request_hash=decision["request_hash"])
        self.assertEqual(recovered, decision)
        self.assertEqual(handoff.apply(request, recovered)["state"], "committed")
        reopened.close()
        reopened_again = LifeStorage.open(life_path)
        self.addCleanup(reopened_again.close)
        handoff = ActivityDeletionHandoff(manager, StagedActivityDeletions(reopened_again))
        self.assertTrue(handoff.complete(request, decision, observed_at=at))
        self.assertFalse(handoff.complete(request, decision, observed_at=at))
        self.assertTrue(handoff.acknowledge(request, decision))
        self.assertFalse(handoff.acknowledge(request, decision))
        self.assertEqual(handoff.prepare(self.core, data, observed_at=at), request)
        self.assertEqual(handoff.decide(self.core, data, request, observed_at=at), decision)
        self.assertEqual(handoff.apply(request, decision)["state"], "acknowledged")
        with self.assertRaises(TaskError):
            handoff.prepare(self.core, {**data, "unexpected": "changed"}, observed_at=at)
        with self.assertRaises(KeyError):
            from good_life.activity_records import StagedActivityRecords
            StagedActivityRecords(reopened_again).dispatch("activity", owner_key=request["owner_key"], activity_id="book")
        version = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key='schema_version'", ())["value"]
        result = rehearse(self.runtime.database_path, life_path / "good-life.sqlite3", expected_version=version)
        self.assertEqual(result["activities"], 0)
        with self.runtime.transaction() as connection:
            connection.execute("UPDATE sources SET state='active' WHERE source_id=?", (source.source_id,))
        with self.assertRaisesRegex(ValueError, "deletion evidence"):
            rehearse(self.runtime.database_path, life_path / "good-life.sqlite3", expected_version=version)
        self.assertEqual(len(self.sent), 0)

    async def test_handoff_batches_planned_reading_with_original_operation_audit(self):
        manager, data, _, _, deletion, source, life, life_path = await self.prepare_removal(prepared=False, related=True)
        at = NOW + timedelta(seconds=1)
        handoff = ActivityDeletionHandoff(manager, deletion)
        request = handoff.prepare(self.core, data, observed_at=at)
        self.assertEqual([item["activity_id"] for item in request["payload"]["related"]], ["related"])
        decision = handoff.decide(self.core, data, request, observed_at=at)
        operation = request["payload"]["related"][0]["operation_id"]
        self.assertEqual(OperationRepository(self.runtime).get(operation).unwrap().state, OperationState.ACCEPTED)
        handoff.apply(request, decision)
        with self.assertRaises(ActivityTransitionPending):
            StagedActivityRecords(life).dispatch("scan_activity_receipts", after=0, limit=100)
        with self.runtime.transaction() as connection:
            connection.execute("UPDATE operations SET commit_state='unknown' WHERE operation_id=?", (operation,))
        with self.assertRaises(TaskError):
            handoff.complete(request, decision, observed_at=at)
        with self.runtime.transaction() as connection:
            connection.execute("UPDATE operations SET commit_state='committed' WHERE operation_id=?", (operation,))
        self.assertTrue(handoff.complete(request, decision, observed_at=at))
        self.assertTrue(handoff.acknowledge(request, decision))
        self.assertEqual(OperationRepository(self.runtime).get(operation).unwrap().state, OperationState.COMPLETED)
        raw = StagedActivityRecords(life).dispatch("activity", owner_key=request["owner_key"], activity_id="related")
        from companion_v2.role_work.codec import activity_from_json
        self.assertEqual(activity_from_json(raw).state, State.CANCELLED)
        self.assertFalse(manager.app.role_journal.sources.validate_reference(source, observed_at=at).is_success)
        version = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key='schema_version'", ())["value"]
        result = rehearse(self.runtime.database_path, life_path / "good-life.sqlite3", expected_version=version)
        self.assertEqual((result["activities"], result["receipts"]), (1, 1))

    async def test_prepared_previous_format_finishes_with_real_host_authority(self):
        manager, data, request, _, deletion, source, _, life_path = await self.prepare_removal(legacy_request=True)
        at = NOW + timedelta(seconds=1)
        handoff = ActivityDeletionHandoff(manager, deletion)
        decision = handoff.decide(self.core, data, request, observed_at=at)
        handoff.apply(request, decision)
        self.assertTrue(handoff.complete(request, decision, observed_at=at))
        self.assertTrue(handoff.acknowledge(request, decision))
        self.assertFalse(manager.app.role_journal.sources.validate_reference(source, observed_at=at).is_success)
        version = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key='schema_version'", ())["value"]
        self.assertEqual(rehearse(self.runtime.database_path, life_path / "good-life.sqlite3", expected_version=version)["activities"], 0)

    async def test_reactivated_source_blocks_recovery(self):
        manager, data, _, _, deletion, source, _, _ = await self.prepare_removal(prepared=False)
        at = NOW + timedelta(seconds=1)
        handoff = ActivityDeletionHandoff(manager, deletion)
        request = handoff.prepare(self.core, data, observed_at=at)
        decision = handoff.decide(self.core, data, request, observed_at=at)
        with self.runtime.transaction() as connection:
            connection.execute("UPDATE sources SET state='active' WHERE source_id=?", (source.source_id,))
        with self.assertRaises(TaskError):
            handoff.apply(request, decision)

    async def test_denied_preparation_can_abort_without_withdrawing_source(self):
        manager, data, _, _, deletion, source, _, _ = await self.prepare_removal(prepared=False)
        at = NOW + timedelta(seconds=1)
        handoff = ActivityDeletionHandoff(manager, deletion)
        request = handoff.prepare(self.core, data, observed_at=at)
        with self.assertRaises(TaskError):
            handoff.decide(self.important, data, request, observed_at=at)
        self.assertTrue(manager.app.role_journal.sources.validate_reference(source, observed_at=at).is_success)
        self.assertEqual((await handoff.recover(request))["state"], "aborted")
        self.assertEqual(handoff.abort(request, observed_at=at)["state"], "aborted")
        self.assertEqual(len(deletion.commands.pending()), 0)

    async def test_commit_decision_cannot_be_replaced_by_abort(self):
        manager, data, _, _, deletion, _, _, _ = await self.prepare_removal(prepared=False)
        at = NOW + timedelta(seconds=1)
        handoff = ActivityDeletionHandoff(manager, deletion)
        request = handoff.prepare(self.core, data, observed_at=at)
        handoff.decide(self.core, data, request, observed_at=at)
        with self.assertRaises(TaskError):
            handoff.abort(request, observed_at=at)

    def bind_provider(self, manager, deletion, life, *, fail_first_resolve=False, after_prepare=None, stop_reading=None):
        from importlib.util import find_spec
        if find_spec("astrbot") is None:
            self.skipTest("AstrBot is required for the real plugin facade")
        try:
            from astrbot_host.integration_registry import UnifiedCompanionExtensionAPI
            from astrbot_host.life_provider_registry import API_VERSION, HANDLERS, PROVIDER_ID
        except ModuleNotFoundError as error:
            if error.name == "astrbot" or (error.name and error.name.startswith("astrbot.")):
                self.skipTest("AstrBot is required for the real plugin facade")
            raise
        from types import SimpleNamespace
        from good_life.activity_control import ActivityControl
        from good_life.activity_records import StagedActivityRecords
        plugin = object()
        star = SimpleNamespace(root_dir_name=PROVIDER_ID, star_cls=plugin, activated=True)
        controller = SimpleNamespace(context=SimpleNamespace(get_all_stars=lambda: [star]), _terminated=False,
                                     session=SimpleNamespace(application=manager.app))
        api = UnifiedCompanionExtensionAPI(controller)
        self.addCleanup(api.close_memory_providers)
        self.addCleanup(api.close_life_providers)
        handlers = {name: (lambda *_args, **_kwargs: None) for name in HANDLERS}
        handlers["activity_read"] = StagedActivityRecords(life).dispatch
        activity_control = ActivityControl(life, stop_reading=stop_reading)
        activity_control._handlers["reading_deletion"] = deletion
        failures = [fail_first_resolve]
        def control(operation, *args, **kwargs):
            if operation == "reading_deletion.resolve" and failures[0]:
                failures[0] = False
                raise ValueError("simulated interruption before local commit")
            result = activity_control.dispatch(operation, *args, **kwargs)
            if operation == "reading_deletion.prepare" and after_prepare is not None:
                after_prepare()
            return result
        handlers["activity_control"] = control
        api.register_life_provider(plugin=plugin, api_version=API_VERSION, handlers=handlers)
        with self.runtime.transaction() as connection:
            connection.execute("INSERT INTO runtime_metadata VALUES (?, 'good_life')", (LIFE_ACTIVITY_OWNER_KEY,))
        return api

    async def test_management_route_recovers_real_authorized_deletion_after_interruption(self):
        from good_life.sleep_scheduler import DueSleepScheduler
        manager, data, _, _, deletion, source, life, _ = await self.prepare_removal(
            prepared=False, related=True, related_state=State.ACTIVE)
        stopped = []
        api = self.bind_provider(manager, deletion, life, fail_first_resolve=True,
                                 stop_reading=lambda context_key, activity_id: stopped.append((context_key, activity_id)) or True)
        registration = api._life_providers._entry
        host = api.life_host_capabilities(plugin=registration.plugin, registration_id=registration.token)
        scheduler = DueSleepScheduler(life, {name: host[name] for name in
            ("api_version", "contexts", "calendar_contexts", "wake", "recover", "recover_reading_deletion")})
        self.addAsyncCleanup(scheduler.close)
        interrupted = await manager.command(self.core, data)
        self.assertFalse(interrupted.is_success)
        self.assertFalse(manager.app.role_journal.sources.validate_reference(source, observed_at=NOW).is_success)
        self.assertEqual((await scheduler.tick(now=self.clock.now()))["recovered"], 1, scheduler.last_failure)
        self.assertEqual(stopped, [(manager.context.key, "related")])
        self.assertEqual(manager.app.role_life.activity("related", owner=manager.owner).state, State.CANCELLED)
        completed = await manager.command(self.core, data)
        self.assertTrue(completed.is_success, completed)
        self.assertEqual(completed.unwrap()["removed"], "book")
        replayed = await manager.command(self.core, data)
        self.assertTrue(replayed.is_success, replayed)
        self.assertTrue(replayed.unwrap()["replayed"])
        with self.assertRaises(KeyError):
            manager.app.role_life.activity("book", owner=manager.owner)
        self.assertEqual(len(self.sent), 0)

    async def test_management_route_stops_active_related_reading_on_source_withdrawal(self):
        manager, data, _, _, deletion, source, life, _ = await self.prepare_removal(
            prepared=False, related=True, related_state=State.ACTIVE)
        stopped = []
        self.bind_provider(manager, deletion, life, stop_reading=lambda context_key, activity_id:
                           stopped.append((context_key, activity_id)) or True)
        outcome = await manager.command(self.core, data)
        self.assertTrue(outcome.is_success, outcome)
        self.assertFalse(manager.app.role_journal.sources.validate_reference(source, observed_at=NOW).is_success)
        self.assertEqual(manager.app.role_life.activity("related", owner=manager.owner).state, State.CANCELLED)
        self.assertEqual(stopped, [(manager.context.key, "related")])

    async def test_source_withdrawal_keeps_records_and_recovers_all_cancellations(self):
        from good_life.sleep_scheduler import DueSleepScheduler
        manager, data, _, _, deletion, source, life, life_path = await self.prepare_removal(
            prepared=False, related=True, target_state=State.ACTIVE)
        data = {**data, "action": "withdraw", "operation_key": "withdraw-book"}
        stopped = []
        api = self.bind_provider(manager, deletion, life, fail_first_resolve=True,
                                 stop_reading=lambda context_key, activity_id: stopped.append((context_key, activity_id)) or True)
        registration = api._life_providers._entry
        host = api.life_host_capabilities(plugin=registration.plugin, registration_id=registration.token)
        scheduler = DueSleepScheduler(life, {name: host[name] for name in
            ("api_version", "contexts", "calendar_contexts", "wake", "recover", "recover_reading_deletion")})
        self.addAsyncCleanup(scheduler.close)
        self.assertFalse((await manager.command(self.core, data)).is_success)
        self.assertFalse(manager.app.role_journal.sources.validate_reference(source, observed_at=NOW).is_success)
        self.assertEqual((await scheduler.tick(now=self.clock.now()))["recovered"], 1, scheduler.last_failure)
        self.assertEqual({manager.app.role_life.activity(name, owner=manager.owner).state for name in ("book", "related")},
                         {State.CANCELLED})
        self.assertEqual({item[1] for item in stopped}, {"book", "related"})
        self.assertTrue((await manager.command(self.core, data)).unwrap()["replayed"])
        version = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key='schema_version'", ())["value"]
        self.assertEqual(rehearse(self.runtime.database_path, life_path / "good-life.sqlite3", expected_version=version)["activities"], 2)
        self.assertEqual(len(self.sent), 0)

    async def test_management_route_cancels_planned_related_reading_in_same_batch(self):
        manager, data, _, _, deletion, source, life, life_path = await self.prepare_removal(prepared=False, related=True)
        self.bind_provider(manager, deletion, life)
        outcome = await manager.command(self.core, data)
        self.assertTrue(outcome.is_success, outcome)
        self.assertEqual(manager.app.role_life.activity("related", owner=manager.owner).state, State.CANCELLED)
        self.assertFalse(manager.app.role_journal.sources.validate_reference(source, observed_at=NOW).is_success)
        version = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key='schema_version'", ())["value"]
        result = rehearse(self.runtime.database_path, life_path / "good-life.sqlite3", expected_version=version)
        self.assertEqual((result["activities"], result["receipts"]), (1, 1))
        access = manager.authorize(self.core)
        second = {"action": "remove", "activity_id": "related", "expected_revision": 2,
                  "operation_key": "remove-related", "view_token": manager.token(self.core, access, "related", 2)}
        self.assertTrue((await manager.command(self.core, second)).is_success)
        replay = await manager.command(self.core, data)
        self.assertTrue(replay.is_success, replay)
        self.assertTrue(replay.unwrap()["replayed"])
        result = rehearse(self.runtime.database_path, life_path / "good-life.sqlite3", expected_version=version)
        self.assertEqual((result["activities"], result["receipts"]), (0, 1))

    async def test_management_route_aborts_if_authority_changes_before_decision(self):
        manager, data, _, _, deletion, source, life, _ = await self.prepare_removal(prepared=False)
        def revoke():
            self.policy.set_tier(self.core.subject, AuthorizationTier.STANDARD,
                                 expected_revision=self.policy.revision, observed_at=NOW)
        self.bind_provider(manager, deletion, life, after_prepare=revoke)
        outcome = await manager.command(self.core, data)
        self.assertFalse(outcome.is_success)
        self.assertEqual(deletion.commands.pending(), ())
        self.assertTrue(manager.app.role_journal.sources.validate_reference(source, observed_at=NOW).is_success)


if __name__ == "__main__":
    unittest.main()
