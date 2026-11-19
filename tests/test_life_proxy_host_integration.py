from contextlib import closing
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life")]

from astrbot_host.life_provider_registry import API_VERSION, HANDLERS, PROVIDER_ID, LifeProviderRegistry
from companion_v2.integration.life_ledger_proxy import LifeLedgerReadProxy
from companion_v2.memory.service import MemoryService
from companion_v2.role_work.journal.service import RoleJournalService
from companion_v2.role_work.ledger import PersistentRoleLifeLedger
from companion_v2.role_work.life.context import RoleLifeContextReader
from companion_v2.storage.schema import SCHEMA_VERSION
from good_life.activity_import import stage_activities
from good_life.activity_records import StagedActivityRecords
from good_life.storage import LifeStorage
from tests.memory_support import MemoryFixture
import tests.test_memory_activity_ingestion as memory_fixtures


class LifeProxyHostIntegrationTests(MemoryFixture, unittest.IsolatedAsyncioTestCase):
    activity = memory_fixtures.ActivityMemoryTests.activity
    complete = memory_fixtures.ActivityMemoryTests.complete
    read_receipt = memory_fixtures.ActivityMemoryTests.read_receipt

    def setUp(self):
        super().setUp()
        self.service = MemoryService(self.tasks)
        self.reader = RoleLifeContextReader(PersistentRoleLifeLedger(self.runtime, now=self.clock.now),
            RoleJournalService(self.runtime, now=self.clock.now), self.tasks)
        self.producer = self.service.attach_life(self.reader)
        self.addCleanup(self.producer.close)
        self.owner = self.reader.owner(self.context)

    def test_real_memory_ingestion_reads_completed_receipt_through_provider(self):
        self.read_receipt()
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        backup = root / "source.sqlite3"
        with closing(sqlite3.connect(self.runtime.database_path)) as source:
            with closing(sqlite3.connect(backup)) as target:
                source.backup(target)
        stage_activities(backup, expected_version=SCHEMA_VERSION, destination=root / "life")
        storage = LifeStorage.open(root / "life")
        self.addCleanup(storage.close)
        records = StagedActivityRecords(storage)
        plugin = object()
        star = SimpleNamespace(root_dir_name=PROVIDER_ID, activated=True, star_cls=plugin)
        native = SimpleNamespace(get_all_stars=lambda: [star])
        registry = LifeProviderRegistry(native, owner=object(), host_active=lambda: True)
        self.addCleanup(registry.close)
        handlers = {name: (lambda *_args, **_kwargs: None) for name in HANDLERS}
        handlers["activity_read"] = records.dispatch
        registry.register(plugin=plugin, api_version=API_VERSION, handlers=handlers)
        self.reader.life = LifeLedgerReadProxy(self.runtime, registry)
        star.activated = False
        unavailable = self.producer.advance(self.context)
        self.assertEqual(unavailable.failure.code, "life_provider_unavailable")
        self.assertEqual(self.service.experiences.list(self.context).unwrap()["items"], [])
        star.activated = True
        read_one, read_all = self.runtime.read_one, self.runtime.read_all

        def guard(method):
            def check(statement, parameters):
                if any(name in statement for name in ("role_activities", "role_activity_revisions", "role_life_operations")):
                    raise AssertionError("activity read unexpectedly touched the old host tables")
                return method(statement, parameters)
            return check

        with patch.object(self.runtime, "read_one", side_effect=guard(read_one)), \
             patch.object(self.runtime, "read_all", side_effect=guard(read_all)):
            candidates = self.reader.candidates(self.context, self.clock.now())
            report = self.producer.advance(self.context).unwrap()
        self.assertTrue(candidates.variants)
        self.assertEqual(report["saved"], 1)
        self.assertEqual(len(self.service.experiences.list(self.context).unwrap()["items"]), 1)


if __name__ == "__main__":
    unittest.main()
