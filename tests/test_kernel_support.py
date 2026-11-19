"""Shared A1 fixtures: temporary SQLite and explicit trusted-adapter test doubles."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/astrbot_plugin_good_life"))
from good_life.activities.local_order import LocalOrder, LocalTimedUnits, SPEC, TEMPLATE
from good_life.contracts import models as m
from good_life.kernel import ActivityKernel, ComponentRegistry, GateSnapshot, KernelConfig
from good_life.kernel.codec import freeze
from good_life.storage import LifeStorage

NOW = datetime(2026, 11, 16, 8, 0, tzinfo=timezone.utc)


class Access:
    def __init__(self):
        self.scope = m.ExecutionScope(m.OwnerKey("verified-owner"), m.ContextKey("private-context"), "authority-1", m.RegistrationGeneration(1))
        self.inventory = (self.scope,)
        self.allowed, self.busy, self.free_s = True, False, 3600
        self.capabilities = ("local_order",)
        self.resources = {}
        self.snapshot_calls = 0
        self.revision = 1
        self.observed_age_s = 0

    def scopes(self):
        return self.inventory

    def authorize(self, scope, action):
        return self.allowed and scope in self.inventory

    def snapshot(self, scope, now):
        self.snapshot_calls += 1
        return GateSnapshot(m.StateSnapshot(scope.owner_key, now - timedelta(seconds=self.observed_age_s),
            m.Revision(self.revision), freeze(self.resources)),
            None if self.free_s is None else now + timedelta(seconds=self.free_s), self.busy, self.capabilities)

    def audience(self, scope):
        return m.AudienceBound("permission-private", (scope.context_key,))


class SimulationClock:
    """Deterministic virtual monotonic time for explicitly simulated tests."""
    def stamp(self, now):
        return (now - NOW).total_seconds()


class CountingExecutor(LocalTimedUnits):
    def __init__(self):
        self.prepares = self.starts = self.advances = self.resumes = 0

    def prepare(self, template, now):
        self.prepares += 1
        return super().prepare(template, now)

    def start(self, instance, now):
        self.starts += 1
        return super().start(instance, now)

    def advance(self, instance, now, audience, *, elapsed_delta_s):
        self.advances += 1
        return super().advance(instance, now, audience, elapsed_delta_s=elapsed_delta_s)

    def resume(self, instance, now):
        self.resumes += 1
        return super().resume(instance, now)


class KernelCase(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix=".kernel-test-", dir=ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.storage = LifeStorage.open(self.temporary.name)
        self.addCleanup(lambda: self.storage.close())
        self.access = Access()
        self.registry = ComponentRegistry()
        self.domain, self.executor = LocalOrder(), CountingExecutor()
        self.registry.register_module(SPEC, self.domain)
        self.registry.register_executor(self.executor)
        self.template = self.registry.register_template(TEMPLATE)
        self.kernel = ActivityKernel(self.storage, self.registry, config=KernelConfig(enabled=True), access=self.access, clock=SimulationClock())
        self.scope = self.access.scope

    def reopen(self):
        self.kernel.close()
        self.storage.close()
        self.storage = LifeStorage.open(self.temporary.name)
        self.kernel = ActivityKernel(self.storage, self.registry, config=KernelConfig(enabled=True), access=self.access, clock=SimulationClock())
        return self.kernel

    def propose(self, opportunity="opportunity-1", *, now=NOW, template=None, scope=None):
        return self.kernel.propose(scope or self.scope, m.TemplateRef((template or self.template).id, m.TemplateVersion(1)),
            opportunity_key=opportunity, reason="tidy_requested", now=now, expires_at=now + timedelta(hours=1))

    def prepared(self, opportunity="opportunity-1", *, now=NOW, scope=None):
        scope = scope or self.scope
        candidate = self.propose(opportunity, now=now, scope=scope)
        result = self.kernel.prepare_candidate(scope, candidate.candidate_id, command_id="prepare-" + opportunity, now=now)
        self.assertTrue(result.gate.allowed)
        self.assertIsNotNone(result.instance)
        return result.instance

    def running(self, opportunity="opportunity-1", *, now=NOW, scope=None):
        scope = scope or self.scope
        instance = self.prepared(opportunity, now=now, scope=scope)
        return self.kernel.start(scope, instance.instance_id, expected_revision=instance.revision,
                                 command_id="start-" + opportunity, now=now)

    def count(self, table):
        with self.storage._lock:
            return self.storage._connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]

    def units(self):
        with self.storage._lock:
            row = self.storage._connection.execute("SELECT completed_units FROM life_local_order_units WHERE owner_key=?",
                                                   (self.scope.owner_key,)).fetchone()
            return row[0] if row else 0
