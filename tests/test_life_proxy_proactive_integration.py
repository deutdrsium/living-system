from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life")]

from astrbot_host.life_provider_registry import API_VERSION, HANDLERS, PROVIDER_ID, LifeProviderRegistry
from companion_v2.integration.life_ledger_proxy import LifeLedgerReadProxy
from companion_v2.storage.schema import SCHEMA_VERSION
from good_life.activity_import import stage_activities
from good_life.activity_records import StagedActivityRecords
from good_life.storage import LifeStorage
from tests.contact_schedule_support import ContactScheduleFixture
import tests.test_proactive_activity_events as proactive_fixtures


class LifeProxyProactiveIntegrationTests(ContactScheduleFixture, unittest.IsolatedAsyncioTestCase):
    setup_contacts = proactive_fixtures.ActivityContactTests.setup_contacts
    reading = proactive_fixtures.ActivityContactTests.reading

    async def test_activity_contact_reads_actual_reading_evidence_through_provider(self):
        await self.setup_contacts()
        self.reading()
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        backup = root / "source.sqlite3"
        with closing(sqlite3.connect(self.app.runtime.database_path)) as source:
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
        calls = []
        def read(operation, **request):
            calls.append(operation)
            return records.dispatch(operation, **request)
        handlers = {name: (lambda *_args, **_kwargs: None) for name in HANDLERS}
        handlers["activity_read"] = read
        registry.register(plugin=plugin, api_version=API_VERSION, handlers=handlers)
        self.app.contact_schedule.events.life = LifeLedgerReadProxy(self.app.runtime, registry)
        self.advance(self.app, 60)
        await self.automatic_delivery(self.app)
        evidence = [m.payload for m in self.contact_calls()[0].original.context.materials if m.owner == "proactive.activity"]
        self.assertEqual(len(evidence), 1)
        self.assertIn("真正读过的这一段", evidence[0])
        self.assertNotIn("未读开头", evidence[0])
        self.assertNotIn("未读结尾", evidence[0])
        self.assertIn("activity_revisions", calls)
        self.assertIn("activity_receipt", calls)
        self.assertEqual(json.loads(self.contacts(self.app)[0]["record_json"])["purpose"], "creation")


if __name__ == "__main__":
    unittest.main()
