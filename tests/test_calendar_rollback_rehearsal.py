from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life"), str(ROOT / "tools")]
from audit_calendar_ownership import adoption_plan
from companion_v2.integration.calendar_command_bridge import CalendarCommandBridge
from companion_v2.storage.versioned_records import VersionedRecords
from good_life.calendar_adoption import adopt_staged_calendar
from good_life.calendar_import import stage_calendar
from good_life.calendar_records import StagedCalendarRecords
from good_life.prepared_commands import PreparedCommands
from good_life.staged_calendar_commands import StagedCalendarCommands
from good_life.storage import LifeStorage
from rehearse_calendar_rollback import rehearse
from tests.test_management_access import NOW
from tests.test_role_state import StateFixture, calendar_data


class CalendarRollbackTests(StateFixture, unittest.IsolatedAsyncioTestCase):
    async def setup_life(self, *, legacy=False):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        host, app, context, manager = self.setup_state()
        self.configure(manager, enabled=True, calendar_enabled=True)
        await self.start(host)
        data = calendar_data()
        original = app.calendar.command(context, identifier="event", action="create", expected_revision=0,
                                        operation_key="original", data=data)
        if legacy:
            VersionedRecords(self.runtime, "calendar").change(context, "legacy", operation_key="legacy",
                expected_revision=0, parameters=["legacy"], at=NOW, transform=lambda _current: {
                    **data, "state": "accepted", "sources": [], "exceptions": {}, "details": {},
                    "accepted_by": [], "changed_by": context.role_binding.role, "reminder_id": None})
        version = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key='schema_version'", ())["value"]
        plan = adoption_plan(self.runtime.database_path, expected_version=version)
        stage_calendar(self.runtime.database_path, expected_version=version, destination=directory / "life")
        life = LifeStorage.open(directory / "life")
        self.addCleanup(life.close)
        adopt_staged_calendar(life, plan)
        PreparedCommands(life).install()
        records, commands = StagedCalendarRecords(life), StagedCalendarCommands(life)
        bridge = CalendarCommandBridge(app.calendar, records, commands, legacy_adopted=True)
        return app, context, data, original, life, bridge, directory, version

    async def test_new_life_versions_and_receipts_return_to_an_isolated_host_copy(self):
        app, context, data, original, _life, bridge, directory, version = await self.setup_life()
        changed = bridge.command(context, identifier="event", action="revise", expected_revision=1,
                                 operation_key="changed", data={**data, "title": "new plan"})
        added = bridge.command(context, identifier="new", action="create", expected_revision=0,
                               operation_key="added", data=data)
        life_path, output = directory / "life" / "good-life.sqlite3", directory / "restored.sqlite3"
        expected = {"records": 2, "revisions": 3, "receipts": 3}
        self.assertEqual(rehearse(self.runtime.database_path, life_path, expected_version=version),
                         {"state": "dry_run", **expected})
        self.assertFalse(output.exists())
        self.assertEqual(rehearse(self.runtime.database_path, life_path, expected_version=version, destination=output),
                         {"state": "rehearsed", **expected})
        self.assertEqual(app.calendar.read(context, "event")["revision"], 1)
        with closing(sqlite3.connect(output)) as db:
            records = {row[0]: json.loads(row[1]) for row in db.execute("SELECT record_id, record_json FROM calendar_records")}
            self.assertEqual(records["event"]["revision"], 2)
            self.assertEqual(records["event"]["title"], "new plan")
            self.assertEqual(records["new"]["revision"], 1)
            receipts = {row[0] for row in db.execute("SELECT operation_id FROM calendar_receipts")}
            self.assertEqual(receipts, {original["operation_id"], changed["operation_id"], added["operation_id"]})
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
        with self.assertRaises(FileExistsError):
            rehearse(self.runtime.database_path, life_path, expected_version=version, destination=output)

    async def test_unresolved_command_blocks_the_rollback_copy(self):
        _app, context, data, _original, _life, bridge, directory, version = await self.setup_life()
        with patch.object(bridge.handoff, "apply", side_effect=RuntimeError("interrupted")):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                bridge.command(context, identifier="event", action="revise", expected_revision=1,
                               operation_key="pending", data=data)
        output = directory / "restored.sqlite3"
        with self.assertRaisesRegex(ValueError, "not yet resolved"):
            rehearse(self.runtime.database_path, directory / "life" / "good-life.sqlite3",
                     expected_version=version, destination=output)
        self.assertFalse(output.exists())

    async def test_missing_history_or_unconfirmed_host_audit_stops_before_writing(self):
        _app, context, data, _original, life, bridge, directory, version = await self.setup_life()
        changed = bridge.command(context, identifier="event", action="revise", expected_revision=1,
                                 operation_key="changed", data=data)
        life_path, output = directory / "life" / "good-life.sqlite3", directory / "restored.sqlite3"
        with self.runtime.transaction() as db:
            db.execute("UPDATE operations SET state='accepted' WHERE operation_id=?", (changed["operation_id"],))
        with self.assertRaisesRegex(ValueError, "not confirmed"):
            rehearse(self.runtime.database_path, life_path, expected_version=version, destination=output)
        self.assertFalse(output.exists())
        with self.runtime.transaction() as db:
            db.execute("UPDATE operations SET state='completed', commit_state='unknown' WHERE operation_id=?", (changed["operation_id"],))
        with self.assertRaisesRegex(ValueError, "not confirmed"):
            rehearse(self.runtime.database_path, life_path, expected_version=version, destination=output)
        self.assertFalse(output.exists())
        with self.runtime.transaction() as db:
            db.execute("UPDATE operations SET commit_state='committed' WHERE operation_id=?", (changed["operation_id"],))
        with life.transaction() as db:
            db.execute("DELETE FROM staged_calendar_history WHERE revision=1")
        with self.assertRaisesRegex(ValueError, "omitted original history"):
            rehearse(self.runtime.database_path, life_path, expected_version=version, destination=output)
        self.assertFalse(output.exists())

    async def test_rollback_retains_adopted_legacy_plan_and_its_new_revision(self):
        app, context, data, _original, _life, bridge, directory, version = await self.setup_life(legacy=True)
        bridge.command(context, identifier="legacy", action="revise", expected_revision=1,
                       operation_key="change-legacy", data={**data, "title": "adopted update"})
        output = directory / "restored.sqlite3"
        rehearse(self.runtime.database_path, directory / "life" / "good-life.sqlite3",
                 expected_version=version, destination=output)
        with closing(sqlite3.connect(output)) as db:
            owner, raw = db.execute("SELECT owner_key, record_json FROM calendar_records WHERE record_id='legacy'").fetchone()
            self.assertEqual(owner, app.calendar.records.role_key(context))
            self.assertEqual(json.loads(raw)["title"], "adopted update")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM calendar_history WHERE owner_key=? AND record_id='legacy'",
                                        (owner,)).fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM calendar_receipts WHERE owner_key=? AND record_id='legacy'",
                                        (owner,)).fetchone()[0], 2)

    async def test_failed_copy_is_removed_without_touching_host_calendar(self):
        app, context, data, _original, _life, bridge, directory, version = await self.setup_life()
        bridge.command(context, identifier="event", action="revise", expected_revision=1,
                       operation_key="changed", data=data)
        with self.runtime.transaction() as db:
            db.execute("""CREATE TRIGGER fail_restore BEFORE INSERT ON calendar_history
                WHEN NEW.revision=2 BEGIN SELECT RAISE(ABORT, 'simulated failure'); END""")
        output = directory / "restored.sqlite3"
        with self.assertRaises(sqlite3.IntegrityError):
            rehearse(self.runtime.database_path, directory / "life" / "good-life.sqlite3",
                     expected_version=version, destination=output)
        self.assertFalse(output.exists())
        self.assertEqual(app.calendar.read(context, "event")["revision"], 1)


if __name__ == "__main__":
    unittest.main()
