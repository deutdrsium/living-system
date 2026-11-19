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
from companion_v2.execution.models import TaskError, stamp
from companion_v2.integration.life_ledger_proxy import LifeLedgerReadProxy
from companion_v2.storage.schema import SCHEMA_VERSION
from good_life.activity_import import stage_activities
from good_life.activity_records import StagedActivityRecords
from good_life.storage import LifeStorage
from tests.test_role_timeline import TimelineFixture


class LifeProxyTimelineIntegrationTests(TimelineFixture, unittest.IsolatedAsyncioTestCase):
    def test_historical_detail_and_revoked_source_go_through_provider(self):
        activity = self.reading()
        reference = {"kind": "activity", "identifier": activity.activity_id,
                     "revision": activity.revision, "occurred_at": stamp(activity.updated_at)}
        expected = self.timeline._project(reference, self.context, self.clock.now())
        expected_page = self.query(kinds=["activity"], limit=20)
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
        registry = LifeProviderRegistry(SimpleNamespace(get_all_stars=lambda: [star]), owner=object(),
                                        host_active=lambda: True)
        self.addCleanup(registry.close)
        calls = []
        def read(operation, **request):
            calls.append(operation)
            return records.dispatch(operation, **request)
        handlers = {name: (lambda *_args, **_kwargs: None) for name in HANDLERS}
        handlers["activity_read"] = read
        registry.register(plugin=plugin, api_version=API_VERSION, handlers=handlers)
        self.reader.life = LifeLedgerReadProxy(self.runtime, registry)
        read_one, read_all = self.runtime.read_one, self.runtime.read_all
        def guard(original):
            def no_old_activity_tables(statement, parameters):
                if "role_activity_revisions" in statement:
                    raise AssertionError("timeline read the archived host activity table")
                return original(statement, parameters)
            return no_old_activity_tables
        with patch.object(self.runtime, "read_one", side_effect=guard(read_one)), \
             patch.object(self.runtime, "read_all", side_effect=guard(read_all)):
            self.assertEqual(self.timeline._project(reference, self.context, self.clock.now()), expected)
            self.assertEqual(self.query(kinds=["activity"], limit=20), expected_page)
            self.assertIn("activity_revision_coverage", calls)
            self.assertIn("timeline_activity_ceiling", calls)
            self.assertIn("timeline_activity_candidates", calls)
            self.messages.sources.revoke(activity.reading.source, expected_governance_revision=1,
                                         observed_at=self.clock.now()).unwrap()
            with self.assertRaises(TaskError):
                self.timeline._project(reference, self.context, self.clock.now())


if __name__ == "__main__":
    unittest.main()
