from dataclasses import replace
from datetime import timedelta
import json
from tempfile import TemporaryDirectory
import unittest

from test_kernel_support import Access, KernelCase, NOW, ROOT, SimulationClock, m
from good_life.activities.local_order import register_local_order
from good_life.event_delivery import LifeEventDelivery
from good_life.kernel import ActivityKernel, ComponentRegistry, KernelConfig, KernelError
from good_life.kernel.codec import freeze
from good_life.kernel.repository import KernelRepository
from good_life.runtime import LifeRuntime
from good_life.storage import LifeStorage


class KernelMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix=".kernel-test-", dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.storage = LifeStorage.open(self.temp.name)
        self.addCleanup(self.storage.close)
        self.registry = ComponentRegistry()
        register_local_order(self.registry)
        self.access = Access()
        with self.storage.transaction() as db:
            db.execute("CREATE TABLE old_records (id TEXT PRIMARY KEY, record_json TEXT NOT NULL)")
            db.execute("INSERT INTO old_records VALUES ('old',?)", ('{"state":"planned","value":7}',))
            db.execute("INSERT INTO life_metadata VALUES ('life.activity.execution_owner.v1','good_life')")
            self.storage.record_event(event_id="old-stable-event", owner_key="old-owner", context_key="old-context",
                                      kind="old.kind", payload={"kept": True}, occurred_at=NOW, consumers=("old-consumer",))

    def snapshot(self):
        db = self.storage._connection
        with self.storage._lock:
            schemas = tuple(tuple(row) for row in db.execute("SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name"))
            rows = {table: tuple(tuple(row) for row in db.execute("SELECT * FROM " + table))
                    for table in ("old_records", "life_events", "life_event_deliveries", "life_metadata")}
        return schemas, rows

    def test_disabled_kernel_does_not_migrate_mutate_or_schedule(self):
        before = self.snapshot()
        kernel = ActivityKernel(self.storage, self.registry)
        self.assertFalse(kernel.config.enabled)
        self.assertEqual(kernel.tick(now=NOW), {"advanced": 0, "started": 0, "failures": ()})
        with self.assertRaises(KernelError):
            kernel.propose(self.access.scope, m.TemplateRef("tidy_desk", m.TemplateVersion(1)), opportunity_key="one",
                           reason="tidy_requested", now=NOW, expires_at=NOW + timedelta(hours=1))
        self.assertEqual(self.snapshot(), before)

    def test_migration_adds_only_new_tables_indexes_metadata_no_old_rewrite(self):
        before_schema, before_rows = self.snapshot()
        ActivityKernel(self.storage, self.registry, config=KernelConfig(enabled=True), access=self.access)
        after_schema, after_rows = self.snapshot()
        self.assertTrue(set(before_schema) <= set(after_schema))
        for table in ("old_records", "life_events", "life_event_deliveries"):
            self.assertEqual(after_rows[table], before_rows[table])
        self.assertTrue(set(before_rows["life_metadata"]) <= set(after_rows["life_metadata"]))
        extra = set(after_rows["life_metadata"]) - set(before_rows["life_metadata"])
        self.assertEqual(extra, {("kernel_schema_version", "2"), ("local_order_schema_version", "1")})
        snapshot = self.snapshot()
        ActivityKernel(self.storage, self.registry, config=KernelConfig(enabled=True), access=self.access)
        self.assertEqual(self.snapshot(), snapshot)
        self.storage.close()
        reopened = LifeStorage.open(self.temp.name)
        try:
            self.assertEqual(reopened._connection.execute("SELECT value FROM life_metadata WHERE key='schema_version'").fetchone()[0], "good-life.storage.v1")
        finally:
            reopened.close()

    def test_unrecognized_or_unclaimed_schema_fails_without_partial_migration(self):
        with self.storage.transaction() as db:
            db.execute("CREATE TABLE life_kernel_unclaimed (x INTEGER)")
        before = self.snapshot()
        with self.assertRaises(KernelError):
            KernelRepository(self.storage).install()
        self.assertEqual(self.snapshot(), before)

    def test_future_schema_is_not_downgraded(self):
        with self.storage.transaction() as db:
            db.execute("INSERT INTO life_metadata VALUES ('kernel_schema_version','99')")
        before = self.snapshot()
        with self.assertRaises(KernelError):
            KernelRepository(self.storage).install()
        self.assertEqual(self.snapshot(), before)

    def test_enabled_requires_explicit_trusted_adapter_before_any_new_schema(self):
        before = self.snapshot()
        with self.assertRaises(KernelError):
            ActivityKernel(self.storage, self.registry, config=KernelConfig(enabled=True))
        self.assertEqual(self.snapshot(), before)


class KernelEventTests(KernelCase):
    def test_audit_ack_requires_receipt_and_reuses_existing_delivery_protocol(self):
        instance = self.running()
        self.kernel.advance(self.scope, instance.instance_id, expected_revision=instance.revision,
                            command_id="done", now=NOW + timedelta(seconds=180))
        event, = self.storage.pending_events("kernel.audit.v1")
        delivery = LifeEventDelivery(self.storage)
        with self.assertRaises(ValueError):
            delivery.acknowledge(event_id=event["event_id"], consumer="kernel.audit.v1", acknowledged_at=NOW,
                                 verify_host_receipt=lambda *_: False)
        self.assertEqual(len(self.storage.pending_events("kernel.audit.v1")), 1)
        receipt = dict(event)
        verify = lambda found, consumer: found == receipt and consumer == "kernel.audit.v1"
        for _ in range(2):
            self.assertTrue(delivery.acknowledge(event_id=event["event_id"], consumer="kernel.audit.v1",
                                               acknowledged_at=NOW, verify_host_receipt=verify))
        self.assertEqual(self.storage.pending_events("kernel.audit.v1"), ())
        self.reopen()
        self.assertEqual(self.storage.pending_events("kernel.audit.v1"), ())
        self.assertIsNotNone(self.kernel.repo.envelope(event["event_id"]))

    def test_existing_event_id_collision_rejects_cross_owner_type_without_overwrite(self):
        with self.storage.transaction():
            self.storage.record_event(event_id="global-identity", owner_key="a", context_key="c", kind="one",
                                      payload={}, occurred_at=NOW, consumers=("kernel.audit.v1",))
        for owner, kind in (("b", "one"), ("a", "two")):
            with self.assertRaises(ValueError), self.storage.transaction():
                self.storage.record_event(event_id="global-identity", owner_key=owner, context_key="c", kind=kind,
                                          payload={}, occurred_at=NOW, consumers=("kernel.audit.v1",))
        self.assertEqual(self.count("life_events"), 1)
        self.assertEqual(self.storage.pending_events("kernel.audit.v1")[0]["owner_key"], "a")

    def test_event_ids_are_global_across_roles_and_effect_ids_are_owner_scoped(self):
        first = self.running()
        self.kernel.advance(self.scope, first.instance_id, expected_revision=first.revision, command_id="complete", now=NOW + timedelta(seconds=180))
        other = replace(self.scope, owner_key=m.OwnerKey("second-owner"), context_key=m.ContextKey("second-context"))
        self.access.inventory += (other,)
        second = self.running("other", scope=other)
        self.kernel.advance(other, second.instance_id, expected_revision=second.revision, command_id="complete", now=NOW + timedelta(seconds=180))
        events = self.storage.pending_events("kernel.audit.v1")
        self.assertEqual(len(events), 2)
        self.assertEqual(len({event["event_id"] for event in events}), 2)
        self.assertEqual({event["owner_key"] for event in events}, {self.scope.owner_key, other.owner_key})
        self.assertEqual(self.count("life_kernel_effects"), 6)

    def test_same_effect_id_different_payload_rolls_back(self):
        instance = self.running()
        progressed = self.kernel.advance(self.scope, instance.instance_id, expected_revision=instance.revision,
                                         command_id="one", now=NOW + timedelta(seconds=60))
        effect = m.Effect(instance.instance_id + ":unit:1", instance.instance_id, "local_order", "local_order.unit_completed",
                          freeze({"unit": 2}), NOW + timedelta(seconds=60), ())
        with self.assertRaises(KernelError), self.storage.transaction() as db:
            self.kernel.repo.commit_effect(db, progressed, effect, self.domain)
        self.assertEqual((self.units(), self.count("life_kernel_effects")), (1, 1))


class KernelRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_runtime_is_unchanged_and_enabled_embedding_has_explicit_step(self):
        with TemporaryDirectory(prefix=".kernel-test-", dir=ROOT) as directory:
            storage = LifeStorage.open(directory)
            try:
                default = LifeRuntime(storage, object())
                self.assertIsNone(default.kernel)
                self.assertEqual(default.advance_kernel(now=NOW)["started"], 0)
                self.assertIsNone(storage._connection.execute("SELECT value FROM life_metadata WHERE key='kernel_schema_version'").fetchone())
                handlers = set(default.handlers)
                await default.close()
                access = Access()
                runtime = LifeRuntime(storage, object(), kernel_config=KernelConfig(enabled=True), kernel_access=access)
                self.assertEqual(set(runtime.handlers), handlers)
                runtime.kernel.clock = SimulationClock()
                candidate = runtime.kernel.propose(access.scope, m.TemplateRef("tidy_desk", m.TemplateVersion(1)),
                    opportunity_key="explicit-intent", reason="tidy_requested", now=NOW, expires_at=NOW + timedelta(hours=1))
                self.assertEqual(runtime.advance_kernel(now=NOW), {"advanced": 0, "started": 1, "failures": ()})
                result = runtime.advance_kernel(now=NOW + timedelta(seconds=180))
                self.assertEqual((result["advanced"], result["failures"]), (1, ()))
                instance, = tuple(runtime.kernel.repo.reader.all("SELECT record_json FROM life_kernel_instances", ()))
                self.assertEqual(json.loads(instance[0])["status"], "completed")
                await runtime.close()
                self.assertEqual(runtime.advance_kernel(now=NOW)["started"], 0)
                self.assertTrue(runtime.kernel.closed)
            finally:
                storage.close()


if __name__ == "__main__":
    unittest.main()
