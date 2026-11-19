from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "astrbot_plugin_good_life"))

from good_life.state_patrol import StatePatrol
from good_life.state_scheduler import StateScheduler
from good_life.storage import LifeStorage


class StatePatrolTests(unittest.TestCase):
    def test_life_scheduler_runs_the_patrol_once_for_a_bound_role(self):
        with TemporaryDirectory() as directory:
            storage = LifeStorage.open(directory)
            with storage.transaction() as db:
                db.execute("CREATE TABLE staged_state_metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                db.execute("""CREATE TABLE staged_role_state_schedules(context_key TEXT PRIMARY KEY, context_json TEXT,
                    settings_json TEXT, next_at TEXT, revision INTEGER, enabled INTEGER, result_json TEXT)""")
            owner = {"installation_id": "i", "bot_id": "b", "persona_id": "p"}
            context = {"role_binding": {"scope": {"installation_id": "i", "bot_id": "b"}, "role": {"subject_id": "p"}}}
            settings = {"enabled": True, "maintenance_minutes": 60, "daily_state_patrol_time": "04:00"}
            binding = {"context_key": "ctx", "owner": owner, "schedule_key": "schedule",
                       "context_json": json.dumps(context), "settings_json": json.dumps(
                           {"values": settings, "token": "token", "instruction": "persona"}),
                       "enabled": True, "can_anchor": True}
            calls = []
            def snapshot(context_key, owner_key):
                calls.append((context_key, owner_key))
                return {"state": "available", "revision": 3, "live_conditions": 1, "expired_conditions": 0}
            scheduler = StateScheduler(storage, {"state_bindings": lambda: (binding,),
                "state_preflight": lambda row: None, "state_run": lambda claim: None,
                "state_patrol_snapshot": snapshot})
            now = datetime(2026, 10, 28, tzinfo=timezone.utc)
            scheduler.advance(now)
            scheduler.advance(now + timedelta(seconds=61))
            self.assertEqual(len(calls), 1)
            self.assertEqual(scheduler.patrol_results["ctx"]["state"], "recorded")
            storage.close()

    def test_imported_daily_history_is_preserved_and_updated_once(self):
        with TemporaryDirectory() as directory:
            storage = LifeStorage.open(directory)
            self.addCleanup(storage.close)
            with storage.transaction() as db:
                db.execute("CREATE TABLE staged_state_metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            patrol = StatePatrol(storage)
            owner = {"installation_id": "i", "bot_id": "b", "persona_id": "p"}
            settings = {"daily_state_patrol_enabled": True, "daily_state_patrol_time": "04:00",
                        "daily_state_patrol_retention_days": 3, "planning_timezone": "Asia/Shanghai"}
            now = datetime(2026, 10, 28, tzinfo=timezone.utc)
            old = {"version": 1, "day": "2026-10-27", "reports": [
                {"day": "2026-10-20", "at": "old"}, {"day": "2026-10-27", "at": "previous"}]}
            with storage.transaction() as db:
                db.execute("INSERT INTO staged_state_metadata VALUES (?,?)", (patrol.key(owner), json.dumps(old)))
            self.assertFalse(patrol.due(owner, settings, now - timedelta(hours=5)))
            self.assertTrue(patrol.due(owner, settings, now))
            state = {"state": "available", "revision": 3, "live_conditions": 1, "expired_conditions": 2}
            result = patrol.record(owner, settings, now, state)
            self.assertEqual((result["state"], result["kept"], result["pruned"]), ("recorded", 2, 1))
            self.assertFalse(patrol.due(owner, settings, now))
            self.assertEqual(patrol.record(owner, settings, now, state)["state"], "already_recorded")
            saved = json.loads(storage._connection.execute("SELECT value FROM staged_state_metadata WHERE key=?",
                                                           (patrol.key(owner),)).fetchone()[0])
            self.assertEqual([item["day"] for item in saved["reports"]], ["2026-10-27", "2026-10-28"])
            self.assertEqual(saved["reports"][-1]["revision"], 3)
            self.assertEqual(saved["reports"][-1]["role_timezone"], "Asia/Shanghai")
            storage.close()


if __name__ == "__main__":
    unittest.main()
