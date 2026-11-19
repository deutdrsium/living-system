from datetime import timedelta
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life"), str(ROOT / "tools")]

from astrbot_host.life_host_capabilities import capabilities_for
from companion_v2.execution.models import digest
from companion_v2.foundation.refs import RoleOwnerRef, SourceRef, VersionRef
from companion_v2.integration.life_activity_control_proxy import LifeActivityCreationProxy, LifeActivityTransitionProxy
from companion_v2.integration.life_event_provider_proxy import LifeEventProviderProxy
from companion_v2.integration.life_ledger_proxy import LifeLedgerReadProxy
from companion_v2.integration.life_ledger_router import LIFE_ACTIVITY_OWNER_KEY
from companion_v2.role_work.models import RoleActivity, RoleActivityState as State
from companion_v2.role_work.work_models import RoleWorkSpec
from companion_v2.sources.models import SourceRecord
from companion_v2.storage.models import OperationState
from companion_v2.storage.repositories import OperationRepository
from good_life.activity_import import stage_activities
from good_life.prepared_commands import PreparedCommands
from good_life.sleep_scheduler import DueSleepScheduler
from good_life.storage import LifeStorage
from rehearse_activity_rollback import rehearse
from test_manual_sleep_handoff import LocalLease
from tests.test_management_access import NOW
from tests.test_role_state import StateFixture


class InterruptedWorkHandoffTests(StateFixture, unittest.IsolatedAsyncioTestCase):
    async def test_restart_pauses_unknown_reading_and_browsing_and_recovers_a_commit(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        host, app, context, _ = self.setup_state()
        self.grant(context.actor, context.scope, "role.life.run")
        await self.start(host)
        routine = app.life_runtime.bind(context, "test persona")
        routine.settings = {"enabled": True}
        routine.refresh = lambda: None
        owner = RoleOwnerRef(context.scope.installation_id, context.scope.bot_id, context.role_binding.role.subject_id)
        text = "the original reading span"
        source = SourceRef("book-source", "library", context.scope, VersionRef("book-source", 1), NOW)
        app.role_journal.sources.publish(SourceRecord(source, context.audience, digest(text), text), observed_at=NOW).unwrap()
        reading = RoleActivity("book", owner.persona_id, context.scope, NOW - timedelta(minutes=1), State.ACTIVE,
            work=RoleWorkSpec("reading", context.key, "reading-attempt", source, digest(text), 0, len(text)))
        browsing = RoleActivity("browse", owner.persona_id, context.scope, NOW - timedelta(minutes=1), State.ACTIVE,
            work=RoleWorkSpec("social", context.key, "social-attempt"))
        app.role_life.add_activity(reading)
        app.role_life.add_activity(browsing)
        version = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key='schema_version'", ())["value"]
        root = Path(temporary.name) / "life"
        stage_activities(self.runtime.database_path, expected_version=version, destination=root)
        life = LifeStorage.open(root)
        self.addCleanup(life.close)
        PreparedCommands(life).install()
        lease = LocalLease(life)
        app.role_life.bind_reader(LifeLedgerReadProxy(self.runtime, lease),
            creation_provider=LifeActivityCreationProxy(lease), transition_provider=LifeActivityTransitionProxy(lease),
            events_provider=LifeEventProviderProxy(self.runtime, lease))
        with self.runtime.transaction() as db:
            db.execute("INSERT INTO runtime_metadata VALUES (?, 'good_life')", (LIFE_ACTIVITY_OWNER_KEY,))

        controller = SimpleNamespace(_terminated=False, session=SimpleNamespace(application=app))
        registry = SimpleNamespace(owns_registration=lambda registration, plugin: True)
        host_caps = capabilities_for(controller, registry, plugin=object(), registration_id="test")
        caps = {key: host_caps[key] for key in ("api_version", "contexts", "wake", "recover",
                                                  "pause_interrupted_work", "recover_interrupted_work")}
        scheduler = DueSleepScheduler(life, caps)
        self.addAsyncCleanup(scheduler.close)
        lease.interrupt_wake_after_resolve = True
        await scheduler.tick(now=self.clock.now())
        self.assertTrue(PreparedCommands(life).pending())
        result = await scheduler.tick(now=self.clock.now())
        self.assertEqual(result["recovered"], 1)
        for original in (reading, browsing):
            current = app.role_life.activity(original.activity_id, owner=owner)
            self.assertEqual((current.state, current.revision, current.work), (State.PAUSED, 2, original.work))
            operation = "recover-" + original.work.kind + "-" + digest([original.activity_id, original.revision])
            self.assertIs(OperationRepository(self.runtime).get(operation).unwrap().state, OperationState.COMPLETED)
        self.assertEqual(app.role_life.activity("book", owner=owner).work.end_offset, len(text))
        self.assertFalse(PreparedCommands(life).pending())
        await scheduler.tick(now=self.clock.now())
        self.assertEqual(app.role_life.activity("book", owner=owner).revision, 2)
        self.assertEqual(len(self.sent), 0)
        self.assertEqual(rehearse(self.runtime.database_path, root / "good-life.sqlite3", expected_version=version)["activities"], 2)


if __name__ == "__main__":
    unittest.main()
