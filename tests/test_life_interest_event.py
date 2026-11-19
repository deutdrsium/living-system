import asyncio
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life"), str(ROOT / "tests")]

from companion_v2.configuration.registry import FieldRegistry
from companion_v2.execution.models import digest
from companion_v2.foundation.refs import SourceRef, VersionRef
from companion_v2.integration.life_activity_control_proxy import LifeActivityCreationProxy, LifeActivityTransitionProxy
from companion_v2.integration.life_event_inbox import inbox_key, receive_pending
from companion_v2.integration.life_event_provider_proxy import LifeEventProviderProxy
from companion_v2.integration.life_ledger_proxy import LifeLedgerReadProxy
from companion_v2.integration.life_ledger_router import LIFE_ACTIVITY_OWNER_KEY
from companion_v2.model_gateway.routing import configuration_fields
from companion_v2.role_work.interest_tasks import interest_profile
from companion_v2.role_work.life.management import RoleLifeManagement
from companion_v2.role_work.models import RoleActivity, RoleActivityState
from companion_v2.role_work.work_models import RoleWorkSpec
from companion_v2.sources.models import SourceRecord
from good_life.activity_import import stage_activities
from good_life.prepared_commands import PreparedCommands
from good_life.storage import LifeStorage
from test_manual_sleep_handoff import LocalLease
from tests.test_management_access import NOW
from tests.test_role_state import StateFixture
from tests.test_role_tool_loop import answered


class LifeInterestEventTests(StateFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.profiles += (interest_profile("primary", max_output_tokens=100),)
        self.registry = FieldRegistry(version="interest-event-tests", fields=configuration_fields(self.profiles))

    async def test_completed_reading_reaches_interest_once_after_inbox_interruption(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        host, app, context, _ = self.setup_state()
        self.grant(context.actor, context.scope, "role.life.run")
        self.grant(context.actor, context.scope, "role.reading.record")
        await self.start(host)
        routine = app.life_runtime.bind(context, "test persona")
        manager = RoleLifeManagement(app, context)
        status = manager.status(self.core)
        configured = await manager.command(self.core, {"action": "configure", "operation_key": "enable-life",
            "view_token": status["view_token"], "edit_token": status["edit_token"],
            "changes": {"enabled": True, "diary_enabled": False}})
        self.assertTrue(configured.is_success, configured)
        routine.refresh()
        text = "实际阅读的原文"
        source = SourceRef("book", "library", context.scope, VersionRef("book", 1), NOW)
        app.role_journal.sources.publish(SourceRecord(source, context.audience, digest(text), text), observed_at=NOW).unwrap()
        activity = RoleActivity("reading", routine.owner.persona_id, context.scope, NOW, RoleActivityState.ACTIVE,
            description="book", work=RoleWorkSpec("reading", context.key, "read", source, digest(text), 0, len(text)))
        app.role_life.add_activity(activity)
        self.clock.advance(timedelta(seconds=1))
        completed = app.role_reading.record_completion(context, activity_id=activity.activity_id, source=source,
            content_hash=digest(text), start_offset=0, end_offset=len(text), completed_at=self.clock.now(),
            expected_revision=1, operation_id="test-reading-complete", reflection="这是实际片段的感想").unwrap()
        version = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key='schema_version'", ())["value"]
        directory = Path(temporary.name) / "life"
        stage_activities(self.runtime.database_path, expected_version=version, destination=directory)
        life = LifeStorage.open(directory)
        self.addCleanup(life.close)
        PreparedCommands(life).install()
        lease = LocalLease(life)
        app.role_life.bind_reader(LifeLedgerReadProxy(self.runtime, lease),
            creation_provider=LifeActivityCreationProxy(lease), transition_provider=LifeActivityTransitionProxy(lease),
            events_provider=LifeEventProviderProxy(self.runtime, lease))
        with self.runtime.transaction() as db:
            db.execute("INSERT INTO runtime_metadata VALUES (?, 'good_life')", (LIFE_ACTIVITY_OWNER_KEY,))
        event_id = "life-activity-" + hashlib.sha256(b"test-reading-complete").hexdigest()
        with life.transaction():
            life.record_event(event_id=event_id, owner_key=digest(routine.owner), context_key=context.key,
                kind="activity.completed", payload={"activity_id": activity.activity_id, "revision": completed.revision,
                    "operation_id": "test-reading-complete"}, occurred_at=completed.updated_at, consumers=["interest"])
        receive_pending(app, context, consumer="interest", observed_at=self.clock.now())
        key = inbox_key(context, "interest", event_id)
        self.model_behavior = lambda _env: asyncio.sleep(0, result=answered('{"observations":[]}'))
        runner = app.life_runtime.events
        original_settle = runner._settle
        def interrupted_settle(event_key, document, *, state, result):
            if result == "processed":
                raise RuntimeError("simulated interruption after the model commit")
            return original_settle(event_key, document, state=state, result=result)
        with patch.object(runner, "_settle", side_effect=interrupted_settle):
            runner.advance((routine,))
            await asyncio.gather(*tuple(runner._workers.values()), return_exceptions=True)
            await asyncio.sleep(0)
        self.assertTrue(app.role_interests.store.processed(context, activity.activity_id))
        self.assertEqual(len(self.called), 1)
        runner._retry_at.clear()
        runner.advance((routine,))
        await asyncio.gather(*tuple(runner._workers.values()), return_exceptions=True)
        await asyncio.sleep(0)
        saved = json.loads(self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?", (key,))["value"])
        self.assertEqual((saved["state"], saved["result"], len(self.called)), ("completed", "already_processed", 1))
        self.assertEqual(len(self.sent), 0)


if __name__ == "__main__":
    unittest.main()
