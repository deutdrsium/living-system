import asyncio
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life"), str(ROOT / "tools")]

from astrbot_host.life_provider_registry import API_VERSION, HANDLERS, PROVIDER_ID, LifeProviderRegistry
from companion_v2.execution.models import TaskError
from companion_v2.execution.models import digest
from companion_v2.foundation.refs import RoleOwnerRef
from companion_v2.integration.exposure_session_proxy import ExposureSessionReadProxy
from companion_v2.integration.life_activity_control_proxy import LifeActivityTransitionProxy
from companion_v2.integration.life_event_inbox import accept, inbox_key, receive_pending, verify_received
from companion_v2.integration.life_event_provider_proxy import LifeEventProviderProxy
from companion_v2.integration.life_ledger_proxy import LifeLedgerReadProxy
from companion_v2.integration.life_ledger_router import LIFE_ACTIVITY_OWNER_KEY
from companion_v2.role_work.journal.models import JournalKind
from good_life.activity_control import ActivityControl
from good_life.activity_import import stage_activities
from good_life.activity_records import StagedActivityRecords
from good_life.event_delivery import LifeEventDelivery
from good_life.exposure_import import stage_exposures
from good_life.exposure_records import StagedExposureRecords
from good_life.storage import LifeStorage
from rehearse_activity_rollback import _events_confirmed
from tests.test_role_life_runtime import LifeFixture


class JournalJointSnapshotTests(LifeFixture, unittest.IsolatedAsyncioTestCase):
    async def setup_life_snapshot(self, *, derive=False, reading=False):
        await self.setup_life()
        if derive:
            await self.configure(derive_enabled=True)
        if reading:
            book = (await self.add_text())[0]
            (await self.command("read", book)).unwrap()
            await self.settle()
        routine = self.manager.life
        sleep = routine.start_sleep("joint-snapshot-sleep", 1)
        self.clock.advance(timedelta(minutes=2))
        completed = routine.wake(sleep.activity_id, expected_revision=sleep.revision, operation_id="joint-snapshot-wake")
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name) / "life"
        version = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key='schema_version'", ())["value"]
        stage_activities(self.runtime.database_path, expected_version=version, destination=directory)
        stage_exposures(self.runtime.database_path, expected_version=version, destination=directory,
                        validate_source=lambda _connection: {"state": "matched"})
        life = LifeStorage.open(directory)
        self.addCleanup(life.close)
        self.snapshot_life = life
        records, event_delivery = StagedActivityRecords(life), LifeEventDelivery(life)
        self.event_delivery = event_delivery
        plugin = object()
        star = SimpleNamespace(root_dir_name=PROVIDER_ID, star_cls=plugin, activated=True)
        registry = LifeProviderRegistry(SimpleNamespace(get_all_stars=lambda: [star]), owner=object(), host_active=lambda: True)
        self.addCleanup(registry.close)
        self.snapshot_registry = registry
        handlers = {name: (lambda *_args, **_kwargs: None) for name in HANDLERS}
        handlers["activity_read"], handlers["activity_history"] = records.dispatch, event_delivery.dispatch
        handlers["activity_control"] = ActivityControl(life).dispatch
        handlers["browsing_read"] = StagedExposureRecords(life).dispatch
        self.snapshot_plugin, self.snapshot_handlers = plugin, handlers
        self.snapshot_token = registry.register(plugin=plugin, api_version=API_VERSION, handlers=handlers)
        lease = registry.lease()
        self.snapshot_reader = LifeLedgerReadProxy(self.runtime, lease)
        browsing = ExposureSessionReadProxy(self.runtime, lease, life=self.app.role_life,
                                            visible=self.app.role_social._visible_contact,
                                            writable=self.app.role_social._active_contact)
        self.app.role_social.exposure.session_reader = self.app.role_life.browsing_reader = browsing
        self.app.role_life.bind_reader(self.snapshot_reader, transition_provider=LifeActivityTransitionProxy(lease),
                                       events_provider=LifeEventProviderProxy(self.runtime, lease))
        with self.runtime.transaction() as db:
            db.execute("INSERT INTO runtime_metadata VALUES (?, 'good_life')", (LIFE_ACTIVITY_OWNER_KEY,))
        return routine, completed

    def publish_completion(self, activity, operation_id, consumers):
        owner = RoleOwnerRef(self.task_context.scope.installation_id, self.task_context.scope.bot_id,
                             self.task_context.role_binding.role.subject_id)
        event_id = "life-activity-" + hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
        with self.snapshot_life.transaction():
            self.snapshot_life.record_event(event_id=event_id, owner_key=digest(owner), context_key=self.task_context.key,
                kind="activity.completed", payload={"activity_id": activity.activity_id,
                    "revision": activity.revision, "operation_id": operation_id},
                occurred_at=activity.updated_at, consumers=consumers)
        return self.snapshot_life.event_for_consumer(event_id, consumers[0])

    async def test_collection_reads_completed_sleep_under_both_snapshots(self):
        routine, completed = await self.setup_life_snapshot()
        zone = routine.settings["timezone"]
        day = completed.updated_at.astimezone(ZoneInfo(zone)).date().isoformat()
        target = self.app.journal_triggers.target(self.task_context, JournalKind.DREAM, day, zone)
        collected = self.app.journal_triggers.collector.for_day(self.task_context, target)
        self.assertTrue(any(item.activity and item.activity.lineage_id == completed.activity_id
                            for item in collected.materials))
        self.assertEqual(self.called, [])

    async def test_existing_management_reads_transferred_sleep(self):
        _routine, completed = await self.setup_life_snapshot()
        status = self.manager.status(self.core)
        self.assertTrue(any(item["activity_id"] == completed.activity_id for item in status["activities"]))
        self.assertEqual(self.sent, [])

    async def test_verified_completion_is_durably_queued_once(self):
        _routine, completed = await self.setup_life_snapshot()
        event = self.publish_completion(completed, "joint-snapshot-wake", ["dream"])
        self.assertFalse(verify_received(self.runtime, self.task_context, event, consumer="dream"))
        with self.assertRaises(TaskError) as absent:
            accept(self.app, self.task_context, {**event, "sequence": event["sequence"] + 1},
                   consumer="dream", observed_at=self.clock.now())
        self.assertEqual(absent.exception.code, "life_event_unconfirmed")
        with self.runtime.transaction() as db:
            db.execute("UPDATE operations SET commit_state='unknown' WHERE operation_id='joint-snapshot-wake'")
        with self.assertRaises(TaskError) as unconfirmed:
            accept(self.app, self.task_context, event, consumer="dream", observed_at=self.clock.now())
        self.assertEqual(unconfirmed.exception.code, "life_event_unconfirmed")
        with self.runtime.transaction() as db:
            db.execute("UPDATE operations SET commit_state='committed' WHERE operation_id='joint-snapshot-wake'")
        self.assertTrue(accept(self.app, self.task_context, event, consumer="dream", observed_at=self.clock.now()))
        self.assertTrue(verify_received(self.runtime, self.task_context, event, consumer="dream"))
        self.assertFalse(accept(self.app, self.task_context, event, consumer="dream", observed_at=self.clock.now()))
        row = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?",
                                    (inbox_key(self.task_context, "dream", event["event_id"]),))
        self.assertIn('"state":"pending"', row["value"])
        self.assertEqual(self.called, [])
        self.assertEqual(self.sent, [])

    async def test_delivery_requires_host_receipt_and_acknowledges_each_consumer(self):
        _routine, completed = await self.setup_life_snapshot()
        event = self.publish_completion(completed, "joint-snapshot-wake", ["dream", "journal"])
        channel = self.app.role_life.events_provider()
        with self.runtime.transaction():
            with self.assertRaises(TaskError) as nested:
                channel.dispatch("pending", consumer="dream")
            self.assertEqual(nested.exception.code, "life_event_transaction_active")
        self.assertEqual(channel.dispatch("pending", consumer="dream", context_key="another-context"), ())
        self.assertEqual(channel.dispatch("pending", consumer="dream", context_key=self.task_context.key), (event,))
        verify = lambda item, consumer: verify_received(self.runtime, self.task_context, item, consumer=consumer)
        with self.assertRaisesRegex(ValueError, "receipt"):
            channel.dispatch("acknowledge", event_id=event["event_id"], consumer="dream",
                             acknowledged_at=self.clock.now(), verify_host_receipt=verify)
        self.assertTrue(accept(self.app, self.task_context, event, consumer="dream", observed_at=self.clock.now()))
        self.assertFalse(accept(self.app, self.task_context, event, consumer="dream", observed_at=self.clock.now()))
        self.assertTrue(channel.dispatch("acknowledge", event_id=event["event_id"], consumer="dream",
                                         acknowledged_at=self.clock.now(), verify_host_receipt=verify))
        self.assertTrue(channel.dispatch("acknowledge", event_id=event["event_id"], consumer="dream",
                                         acknowledged_at=self.clock.now(), verify_host_receipt=verify))
        self.assertEqual(channel.dispatch("pending", consumer="dream"), ())
        self.assertEqual(len(channel.dispatch("pending", consumer="journal")), 1)
        journal = receive_pending(self.app, self.task_context, consumer="journal", observed_at=self.clock.now())
        self.assertEqual((journal["received"], journal["acknowledged"]), (1, 1))
        saved = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?",
                                      (inbox_key(self.task_context, "journal", event["event_id"]),))
        self.assertEqual(json.loads(saved["value"])["state"], "completed")
        self.assertEqual(self.called, [])

    async def test_pending_delivery_recovers_after_interrupted_acknowledgement(self):
        _routine, completed = await self.setup_life_snapshot()
        event = self.publish_completion(completed, "joint-snapshot-wake", ["dream"])
        with patch.object(self.event_delivery, "acknowledge", side_effect=ValueError("interrupted")):
            first = receive_pending(self.app, self.task_context, consumer="dream", observed_at=self.clock.now())
        self.assertEqual(first, {"received": 1, "acknowledged": 0, "rejected": 1, "next_after": 0})
        self.assertEqual(len(self.snapshot_life.pending_events("dream")), 1)
        second = receive_pending(self.app, self.task_context, consumer="dream", observed_at=self.clock.now())
        self.assertEqual(second, {"received": 0, "acknowledged": 1, "rejected": 0, "next_after": 0})
        self.assertEqual(self.snapshot_life.pending_events("dream"), ())
        self.assertTrue(verify_received(self.runtime, self.task_context, event, consumer="dream"))

    async def test_rejected_event_does_not_block_later_events(self):
        _routine, completed = await self.setup_life_snapshot()
        owner = RoleOwnerRef(self.task_context.scope.installation_id, self.task_context.scope.bot_id,
                             self.task_context.role_binding.role.subject_id)
        missing = "missing-operation"
        with self.snapshot_life.transaction():
            self.snapshot_life.record_event(event_id="life-activity-" + hashlib.sha256(missing.encode()).hexdigest(),
                owner_key=digest(owner), context_key=self.task_context.key, kind="activity.completed",
                payload={"activity_id": completed.activity_id, "revision": completed.revision, "operation_id": missing},
                occurred_at=completed.updated_at, consumers=["dream"])
        first = receive_pending(self.app, self.task_context, consumer="dream", observed_at=self.clock.now(), limit=1)
        self.assertEqual((first["rejected"], first["next_after"]), (1, 1))
        event = self.publish_completion(completed, "joint-snapshot-wake", ["dream"])
        second = receive_pending(self.app, self.task_context, consumer="dream", observed_at=self.clock.now(),
                                 limit=1, after_sequence=first["next_after"])
        self.assertEqual((second["received"], second["acknowledged"]), (1, 1))
        self.assertTrue(verify_received(self.runtime, self.task_context, event, consumer="dream"))

    async def test_provider_failure_does_not_stop_the_host(self):
        _routine, _completed = await self.setup_life_snapshot()
        with patch.object(self.snapshot_life, "pending_events", side_effect=RuntimeError("provider stopped")):
            self.app.life_runtime.advance()
        self.assertEqual(self.app.state, "running")
        self.assertEqual(self.sent, [])

    async def test_sleep_event_runs_one_real_host_dream(self):
        _routine, completed = await self.setup_life_snapshot()
        event = self.publish_completion(completed, "joint-snapshot-wake", ["dream"])
        key = inbox_key(self.task_context, "dream", event["event_id"])
        self.app.life_runtime.advance()
        def settled():
            row = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?", (key,))
            return row is not None and json.loads(row["value"])["state"] == "completed"
        await self.until(settled)
        self.assertEqual(self.snapshot_life.pending_events("dream"), ())
        self.assertEqual(self.runtime.read_one("SELECT COUNT(*) AS n FROM role_journals WHERE kind='dream'", ())["n"], 1)
        with self.snapshot_life.read_snapshot(), self.runtime.read_snapshot() as host:
            self.assertEqual(_events_confirmed(host, self.snapshot_life._connection), 1)
        calls = len(self.called)
        for _ in range(3):
            self.app.life_runtime.advance()
            await asyncio.sleep(0)
        self.assertEqual(len(self.called), calls)
        self.assertEqual(self.sent, [])

    async def test_disabled_life_defers_dream_until_reenabled(self):
        _routine, completed = await self.setup_life_snapshot()
        await self.configure(enabled=False)
        event = self.publish_completion(completed, "joint-snapshot-wake", ["dream"])
        self.app.life_runtime.advance()
        await asyncio.sleep(0)
        self.assertEqual(len(self.snapshot_life.pending_events("dream")), 1)
        self.assertEqual(self.called, [])
        await self.configure(enabled=True)
        self.app.life_runtime.advance()
        key = inbox_key(self.task_context, "dream", event["event_id"])
        await self.until(lambda: (row := self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?", (key,)))
                         is not None and json.loads(row["value"])["state"] == "completed")
        self.assertEqual(self.runtime.read_one("SELECT COUNT(*) AS n FROM role_journals WHERE kind='dream'", ())["n"], 1)

    async def test_accepted_sleep_event_survives_host_restart(self):
        _routine, completed = await self.setup_life_snapshot()
        event = self.publish_completion(completed, "joint-snapshot-wake", ["dream"])
        receive_pending(self.app, self.task_context, consumer="dream", observed_at=self.clock.now())
        self.assertEqual(self.snapshot_life.pending_events("dream"), ())
        with self.snapshot_life.read_snapshot(), self.runtime.read_snapshot() as host:
            with self.assertRaisesRegex(ValueError, "inbox is not fully settled"):
                _events_confirmed(host, self.snapshot_life._connection)
        await self.app.close()
        self.runtime.close()
        self.runtime = self.open_runtime()
        self.host_instance = self.host(runtime=self.runtime)
        self.app = self.host_instance.application
        lease = self.snapshot_registry.lease()
        self.app.role_life.bind_reader(LifeLedgerReadProxy(self.runtime, lease),
                                      events_provider=LifeEventProviderProxy(self.runtime, lease))
        await self.start(self.host_instance, task_generation=1, model_generation=1)
        routine = self.app.life_runtime.bind(self.task_context, "原人格")
        routine.refresh()
        self.app.life_runtime.advance()
        key = inbox_key(self.task_context, "dream", event["event_id"])
        await self.until(lambda: json.loads(self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?", (key,))["value"])["state"] == "completed")
        self.assertEqual(self.runtime.read_one("SELECT COUNT(*) AS n FROM role_journals WHERE kind='dream'", ())["n"], 1)
        self.assertEqual(self.sent, [])

    async def test_interrupted_event_dream_is_not_retried_after_host_restart(self):
        _routine, completed = await self.setup_life_snapshot()
        event = self.publish_completion(completed, "joint-snapshot-wake", ["dream"])
        entered = asyncio.Event()
        async def model(_envelope):
            entered.set()
            await asyncio.Event().wait()
        self.model_behavior = model
        self.app.life_runtime.advance()
        await asyncio.wait_for(entered.wait(), 10)
        calls = len(self.called)
        self.assertEqual((await self.app.close()).state, "stopped")
        self.runtime.close()
        self.runtime = self.open_runtime()
        self.host_instance = self.host(runtime=self.runtime)
        self.app = self.host_instance.application
        lease = self.snapshot_registry.lease()
        self.app.role_life.bind_reader(LifeLedgerReadProxy(self.runtime, lease),
                                      events_provider=LifeEventProviderProxy(self.runtime, lease))
        await self.start(self.host_instance, task_generation=1, model_generation=1)
        routine = self.app.life_runtime.bind(self.task_context, "原人格")
        routine.refresh()
        self.app.life_runtime.advance()
        key = inbox_key(self.task_context, "dream", event["event_id"])
        await self.until(lambda: json.loads(self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?", (key,))["value"])["state"] == "unknown")
        self.assertEqual(len(self.called), calls)
        self.assertEqual(self.runtime.read_one("SELECT COUNT(*) AS n FROM role_journals WHERE kind='dream'", ())["n"], 0)
        self.assertEqual(self.sent, [])

    async def test_persona_rebind_cancels_original_event_worker(self):
        _routine, completed = await self.setup_life_snapshot()
        self.publish_completion(completed, "joint-snapshot-wake", ["dream"])
        entered = asyncio.Event()
        async def model(_envelope):
            entered.set()
            await asyncio.Event().wait()
        self.model_behavior = model
        self.app.life_runtime.advance()
        await asyncio.wait_for(entered.wait(), 10)
        calls = len(self.called)
        await self.app.life_runtime.rebind(self.task_context, "更新后的人格")
        await self.until(lambda: not self.app.life_runtime.events.active_handles)
        self.app.life_runtime.advance()
        await asyncio.sleep(0.05)
        self.assertEqual(len(self.called), calls)
        self.assertEqual(self.runtime.read_one("SELECT COUNT(*) AS n FROM role_journals WHERE kind='dream'", ())["n"], 0)

    async def test_revoked_reading_is_not_enqueued_for_another_consumer(self):
        routine, _completed = await self.setup_life_snapshot(reading=True)
        reading = next(item for item in routine.activities() if item.work.kind == "reading")
        evidence = self.app.role_life.activity_revision_evidence(reading.activity_id, reading.revision, owner=routine.owner)
        event = self.publish_completion(reading, evidence["operation_id"], ["interest", "journal"])
        self.assertTrue(accept(self.app, self.task_context, event, consumer="interest", observed_at=self.clock.now()))
        self.app.role_journal.sources.revoke(reading.reading.source, expected_governance_revision=1,
                                             observed_at=self.clock.now()).unwrap()
        with self.assertRaises(TaskError) as revoked:
            accept(self.app, self.task_context, event, consumer="journal", observed_at=self.clock.now())
        self.assertEqual(revoked.exception.code, "source_revoked")
        self.assertFalse(verify_received(self.runtime, self.task_context, event, consumer="journal"))

    async def test_actual_dream_commit_from_joint_activity_snapshot(self):
        routine, completed = await self.setup_life_snapshot()
        result = await routine.dream(completed)
        self.assertTrue(result.is_success, result)
        self.assertEqual(result.unwrap().state, "written", result)
        count = len(self.called)
        repeated = await routine.dream(completed)
        self.assertTrue(repeated.is_success, repeated)
        self.assertEqual(repeated.unwrap().state, "written")
        self.assertTrue(repeated.unwrap().replayed)
        self.assertEqual(len(self.called), count)
        self.assertEqual(self.runtime.read_one("SELECT COUNT(*) AS n FROM role_journals WHERE kind='dream'", ())["n"], 1)
        self.assertEqual(self.sent, [])

    async def test_source_revocation_during_model_work_prevents_late_dream(self):
        routine, completed = await self.setup_life_snapshot()
        event = routine.event("sleep-completed-" + completed.activity_id,
            "生活执行端确认本段睡眠已结束，没有执行其他醒后动作。", at=completed.updated_at)
        entered, release = asyncio.Event(), asyncio.Event()
        async def model(envelope):
            entered.set()
            await release.wait()
            return await self.model(envelope)
        self.model_behavior = model
        job = asyncio.create_task(routine.dream(completed))
        try:
            await asyncio.wait_for(entered.wait(), 10)
            self.app.role_journal.sources.revoke(event.source, expected_governance_revision=1,
                                                 observed_at=self.clock.now()).unwrap()
        finally:
            release.set()
        result = await job
        self.assertTrue(not result.is_success or result.unwrap().state != "written", result)
        self.assertEqual(self.runtime.read_one("SELECT COUNT(*) AS n FROM role_journals WHERE kind='dream'", ())["n"], 0)
        self.assertEqual(self.sent, [])

    @pytest.mark.timing
    async def test_provider_unload_fences_dream_but_keeps_model_usage(self):
        routine, completed = await self.setup_life_snapshot()
        # The wake notifies the background driver, which can otherwise launch
        # the same dream and make `job` return before that model call settles.
        routine._dreams.add(completed.activity_id)
        entered, release = asyncio.Event(), asyncio.Event()
        async def model(envelope):
            entered.set()
            await release.wait()
            return await self.model(envelope)
        self.model_behavior = model
        job = asyncio.create_task(routine.dream(completed))
        try:
            await asyncio.wait_for(entered.wait(), 10)
            def unrelated_life_write():
                with self.snapshot_life.transaction() as db:
                    db.execute("INSERT INTO life_metadata VALUES ('test-model-wait', 'available')")
            await asyncio.wait_for(asyncio.to_thread(unrelated_life_write), 5)
            self.assertTrue(self.snapshot_registry.unregister(self.snapshot_token))
        finally:
            release.set()
        result = await job
        self.assertTrue(not result.is_success or result.unwrap().state != "written", result)
        self.assertEqual(self.runtime.read_one("SELECT COUNT(*) AS n FROM role_journals WHERE kind='dream'", ())["n"], 0)
        calls = self.runtime.read_all("SELECT actual_tokens, state FROM model_calls", ())
        self.assertTrue(any(row["actual_tokens"] == 15 for row in calls), tuple(dict(row) for row in calls))
        self.assertEqual(self.runtime.read_one("SELECT COUNT(*) AS n FROM execution_attempts WHERE slot_held=1", ())["n"], 0)
        self.assertEqual(self.sent, [])

    @pytest.mark.timing
    async def test_old_model_task_cannot_follow_a_reloaded_provider(self):
        routine, completed = await self.setup_life_snapshot()
        entered, release = asyncio.Event(), asyncio.Event()
        async def model(envelope):
            entered.set()
            await release.wait()
            return await self.model(envelope)
        self.model_behavior = model
        job = asyncio.create_task(routine.dream(completed))
        try:
            await asyncio.wait_for(entered.wait(), 10)
            self.assertTrue(self.snapshot_registry.unregister(self.snapshot_token))
            self.assertTrue(self.app.role_life.unbind_reader(self.snapshot_reader))
            replacement = self.snapshot_registry.register(plugin=self.snapshot_plugin, api_version=API_VERSION,
                                                           handlers=self.snapshot_handlers)
            self.app.role_life.bind_reader(LifeLedgerReadProxy(self.runtime, self.snapshot_registry.lease()))
        finally:
            release.set()
        result = await job
        self.assertTrue(not result.is_success or result.unwrap().state != "written", result)
        self.assertEqual(self.runtime.read_one("SELECT COUNT(*) AS n FROM role_journals WHERE kind='dream'", ())["n"], 0)
        calls = self.runtime.read_all("SELECT actual_tokens FROM model_calls", ())
        self.assertTrue(any(row["actual_tokens"] == 15 for row in calls))
        self.assertTrue(self.snapshot_registry.unregister(replacement))

    async def test_host_shutdown_cancels_inflight_joint_dream_without_saving(self):
        routine, completed = await self.setup_life_snapshot()
        entered = asyncio.Event()
        async def model(_envelope):
            entered.set()
            await asyncio.Event().wait()
        self.model_behavior = model
        job = asyncio.create_task(routine.dream(completed))
        await asyncio.wait_for(entered.wait(), 10)
        report = await self.app.close()
        self.assertEqual(report.state, "stopped")
        with self.assertRaises(asyncio.CancelledError):
            await job
        self.assertEqual(self.runtime.read_one("SELECT COUNT(*) AS n FROM role_journals WHERE kind='dream'", ())["n"], 0)
        self.assertEqual(self.runtime.read_one("SELECT COUNT(*) AS n FROM execution_attempts WHERE slot_held=1", ())["n"], 0)
        self.assertEqual(self.sent, [])

    async def test_dream_derivatives_still_use_validated_activity(self):
        routine, completed = await self.setup_life_snapshot(derive=True)
        result = await routine.dream(completed)
        self.assertTrue(result.is_success, result)
        self.assertEqual(result.unwrap().state, "written", result)
        self.assertEqual(self.runtime.read_one("SELECT COUNT(*) AS n FROM role_journal_derivatives", ())["n"], 1)

    async def test_restarted_host_replays_original_dream_without_another_model_call(self):
        routine, completed = await self.setup_life_snapshot()
        first = await routine.dream(completed)
        self.assertTrue(first.is_success, first)
        self.assertEqual(first.unwrap().state, "written", first)
        calls = len(self.called)
        await self.app.close()
        self.runtime.close()
        self.runtime = self.open_runtime()
        self.host_instance = self.host(runtime=self.runtime)
        self.app = self.host_instance.application
        self.app.role_life.bind_reader(LifeLedgerReadProxy(self.runtime, self.snapshot_registry.lease()))
        await self.start(self.host_instance, task_generation=1, model_generation=1)
        routine = self.app.life_runtime.bind(self.task_context, "原人格")
        routine.refresh()
        repeated = await routine.dream(completed)
        self.assertTrue(repeated.is_success, repeated)
        self.assertTrue(repeated.unwrap().replayed)
        self.assertEqual(repeated.unwrap().state, "written")
        self.assertEqual(len(self.called), calls)
        self.assertEqual(self.runtime.read_one("SELECT COUNT(*) AS n FROM role_journals WHERE kind='dream'", ())["n"], 1)

    async def test_diary_retains_reading_source_and_hides_after_revocation(self):
        routine, completed = await self.setup_life_snapshot(reading=True)
        reading = next(item for item in routine.activities() if item.work.kind == "reading")
        result = await routine.diary()
        self.assertTrue(result.is_success, result)
        self.assertEqual(result.unwrap().state, "written", result)
        zone = routine.settings["timezone"]
        day = completed.updated_at.astimezone(ZoneInfo(zone)).date().isoformat()
        target = self.app.journal_triggers.target(self.task_context, JournalKind.DIARY, day, zone)
        entry = self.app.role_journal.get(target.key)
        self.assertIn(reading.reading.source, entry.draft.sources)
        self.app.role_journal.sources.revoke(reading.reading.source, expected_governance_revision=1,
                                             observed_at=self.clock.now()).unwrap()
        with self.assertRaises(TaskError) as revoked:
            self.app.role_journal.get(target.key)
        self.assertEqual(revoked.exception.code, "source_revoked")
        self.assertEqual(self.sent, [])

if __name__ == "__main__":
    unittest.main()
