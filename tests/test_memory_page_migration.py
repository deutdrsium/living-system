import asyncio
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life")]

from astrbot_host.memory_page_api import MemoryPageAPI, MemoryPageUnavailable
from good_life.calendar_records import StagedCalendarRecords


class MemoryPageMigrationTests(unittest.TestCase):
    def test_current_life_records_are_used_and_unconfirmed_changes_are_hidden(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        self.addCleanup(db.close)
        db.execute("CREATE TABLE staged_calendar_records (record_json TEXT)")
        db.execute("CREATE TABLE staged_role_state_records (record_json TEXT, updated_at TEXT)")
        db.execute("CREATE TABLE life_prepared_commands (state TEXT, kind TEXT)")
        plan = {"kind": "role_plan", "state": "accepted", "title": "画画", "detail": "",
                "schedule": {"anchor": "2026-10-28T02:30:00+00:00", "timezone_name": "Asia/Shanghai"}}
        db.execute("INSERT INTO staged_calendar_records VALUES (?)", (json.dumps(plan, ensure_ascii=False),))
        db.execute("INSERT INTO staged_role_state_records VALUES (?, ?)",
                   (json.dumps({"values": {"energy": 77}}), "2026-10-28"))
        provider = StagedCalendarRecords(db)

        class HostRuntime:
            def read_one(self, statement, parameters):
                return {"value": "good_life"}

            def read_all(self, statement, parameters):
                if "role_journals" not in statement:
                    raise AssertionError("sealed life records must not be read")
                return []

        class Registry:
            def lease(self):
                return self

            def dispatch(self, handler, operation):
                assert handler == "calendar_read"
                return provider.dispatch(operation)

        controller = SimpleNamespace(runtime=HostRuntime(), context=SimpleNamespace())
        registry = Registry()
        registry.owner = controller
        controller.context._private_companion_life_provider = registry
        adapter = MemoryPageAPI(controller)
        snapshot = asyncio.run(adapter.export_snapshot(selected_date="2026-10-28"))
        self.assertEqual(snapshot["day"]["plan"]["items"][0]["activity"], "画画")
        self.assertEqual(snapshot["day"]["daily_state"]["energy"], 77)
        db.execute("INSERT INTO life_prepared_commands VALUES ('committed', 'state.change')")
        with self.assertRaises(MemoryPageUnavailable):
            asyncio.run(adapter.export_snapshot(selected_date="2026-10-28"))


if __name__ == "__main__":
    unittest.main()
