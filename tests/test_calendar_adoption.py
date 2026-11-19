from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life"), str(ROOT / "tools")]
from audit_calendar_ownership import adoption_plan
from companion_v2.storage.versioned_records import VersionedRecords
from companion_v2.execution.models import TaskError
from companion_v2.integration.calendar_adoption_guard import CERTIFICATE_KEY, require_verified
from good_life.calendar_adoption import _json, _plan, adopt_staged_calendar
from good_life.calendar_import import stage_calendar
from good_life.calendar_records import StagedCalendarRecords
from good_life.prepared_commands import PreparedCommands
from good_life.staged_calendar_commands import StagedCalendarCommands
from good_life.storage import LifeStorage
from rehearse_calendar_migration import rehearse
from verify_calendar_adoption import verify
from tests.test_management_access import NOW
from tests.test_role_state import StateFixture, calendar_data


class CalendarAdoptionTests(StateFixture, unittest.IsolatedAsyncioTestCase):
    def old_record(self, context, identifier, key):
        return VersionedRecords(self.runtime, "calendar").change(context, identifier, operation_key=key,
            expected_revision=0, parameters=[key], at=NOW, transform=lambda _current: {
                **calendar_data(), "state": "accepted", "sources": [], "exceptions": {}, "details": {},
                "accepted_by": [], "changed_by": context.role_binding.role, "reminder_id": None})

    def staged(self, context, app):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name) / "life"
        version = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key='schema_version'", ())["value"]
        self.staged_directory, self.source_version = directory, version
        plan = adoption_plan(self.runtime.database_path, expected_version=version)
        stage_calendar(self.runtime.database_path, expected_version=version, destination=directory)
        life = LifeStorage.open(directory)
        self.addCleanup(life.close)
        return life, plan

    async def test_adoption_keeps_versions_and_original_operation_receipts(self):
        _host, app, context, _manager = self.setup_state()
        first = self.old_record(context, "legacy", "legacy-first")
        records = VersionedRecords(self.runtime, "calendar")
        second = records.change(context, "legacy", operation_key="legacy-second", expected_revision=1,
            parameters=["second"], at=NOW, transform=lambda current: {**current, "detail": "new detail"})
        source_owner, target_owner = app.calendar.records.conversation_key(context), app.calendar.records.role_key(context)
        source_rows = tuple(tuple(row) for row in self.runtime.read_all(
            "SELECT record_json FROM calendar_history WHERE owner_key=? AND record_id=? ORDER BY revision",
            (source_owner, "legacy")))
        life, plan = self.staged(context, app)
        self.assertEqual(adopt_staged_calendar(life, plan), {"state": "adopted", "adopted": 1})
        staged = StagedCalendarRecords(life)
        self.assertIsNone(staged.get(owner_key=source_owner, identifier="legacy"))
        self.assertEqual(staged.get(owner_key=target_owner, identifier="legacy")["revision"], 2)
        self.assertEqual(staged.version(owner_key=target_owner, identifier="legacy", revision=1)["detail"], "读自己选的书")
        self.assertEqual(staged.version(owner_key=target_owner, identifier="legacy", revision=2)["detail"], "new detail")
        self.assertEqual(staged.receipt(owner_key=target_owner, operation_id=first["operation_id"])["revision"], 1)
        self.assertEqual(staged.receipt(owner_key=target_owner, operation_id=second["operation_id"])["revision"], 2)
        self.assertEqual(source_rows, tuple(tuple(row) for row in self.runtime.read_all(
            "SELECT record_json FROM calendar_history WHERE owner_key=? AND record_id=? ORDER BY revision",
            (source_owner, "legacy"))))
        self.assertEqual(adopt_staged_calendar(life, plan), {"state": "unchanged", "adopted": 1})
        self.assertEqual(stage_calendar(self.runtime.database_path, expected_version=self.source_version,
                                        destination=self.staged_directory)["state"], "unchanged")
        changed = deepcopy(plan)
        changed["identity_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "plan changed"):
            adopt_staged_calendar(life, changed)
        with self.runtime.transaction() as db:
            db.execute("UPDATE conversation_routes SET record_json='{}'")
        with self.assertRaisesRegex(ValueError, "source or identity changed"):
            stage_calendar(self.runtime.database_path, expected_version=self.source_version, destination=self.staged_directory)

    async def test_failure_mid_adoption_rolls_back_every_record_and_can_retry(self):
        _host, app, context, _manager = self.setup_state()
        self.old_record(context, "first", "first")
        self.old_record(context, "second", "second")
        life, plan = self.staged(context, app)
        with life.transaction() as db:
            db.execute("""CREATE TRIGGER fail_second_adoption BEFORE INSERT ON staged_calendar_history
                WHEN NEW.record_id='second' BEGIN SELECT RAISE(ABORT, 'simulated interruption'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            adopt_staged_calendar(life, plan)
        records = StagedCalendarRecords(life)
        source_owner, target_owner = app.calendar.records.conversation_key(context), app.calendar.records.role_key(context)
        for identifier in ("first", "second"):
            self.assertIsNotNone(records.get(owner_key=source_owner, identifier=identifier))
            self.assertIsNone(records.get(owner_key=target_owner, identifier=identifier))
        with life.transaction() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='staged_calendar_adoption'").fetchone())
            db.execute("DROP TRIGGER fail_second_adoption")
        self.assertEqual(adopt_staged_calendar(life, plan), {"state": "adopted", "adopted": 2})

    async def test_changed_source_or_result_is_rejected(self):
        _host, app, context, _manager = self.setup_state()
        self.old_record(context, "legacy", "legacy")
        life, plan = self.staged(context, app)
        changed = deepcopy(plan)
        changed["moves"][0]["head_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "source changed"):
            adopt_staged_calendar(life, changed)
        self.assertEqual(adopt_staged_calendar(life, plan)["state"], "adopted")
        with life.transaction() as db:
            db.execute("UPDATE staged_calendar_records SET record_json='{}'")
        with self.assertRaisesRegex(ValueError, "changed after staging"):
            adopt_staged_calendar(life, plan)

    async def test_whole_rehearsal_can_be_repeated_without_exposing_private_ids(self):
        _host, _app, context, _manager = self.setup_state()
        self.old_record(context, "private-calendar-id", "private-operation-id")
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name) / "life"
        version = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key='schema_version'", ())["value"]
        dry = rehearse(self.runtime.database_path, expected_version=version, adopt_legacy=True)
        self.assertEqual(dry["state"], "dry_run")
        self.assertFalse(directory.exists())
        first = rehearse(self.runtime.database_path, expected_version=version, destination=directory, adopt_legacy=True)
        self.assertEqual(first["adoption"], {"state": "adopted", "adopted": 1})
        second = rehearse(self.runtime.database_path, expected_version=version, destination=directory, adopt_legacy=True)
        self.assertEqual(second["state"], "unchanged")
        self.assertEqual(second["adoption"], {"state": "unchanged", "adopted": 1})
        self.assertNotIn("private-calendar-id", str(first))
        self.assertNotIn("private-operation-id", str(first))

    async def test_certificate_requires_exact_audited_ownership_and_imported_rows(self):
        _host, app, context, _manager = self.setup_state()
        self.old_record(context, "private-calendar-id", "private-operation-id")
        life, plan = self.staged(context, app)
        adopt_staged_calendar(life, plan)
        expected = StagedCalendarCommands(life).dispatch("adoption_certificate")
        result = verify(self.runtime.database_path, self.staged_directory / "good-life.sqlite3",
                        expected_version=self.source_version)
        self.assertEqual(result, {"state": "verified", "certificate": expected})
        self.assertNotIn("private-calendar-id", str(result))
        self.assertNotIn("private-operation-id", str(result))
        provider = StagedCalendarCommands(life)
        with self.assertRaises(TaskError):
            require_verified(self.runtime, provider)
        with self.runtime.transaction() as db:
            db.execute("INSERT INTO runtime_metadata VALUES (?, ?)", (CERTIFICATE_KEY, json.dumps(expected)))
        self.assertTrue(require_verified(self.runtime, provider))
        with self.runtime.transaction():
            with self.assertRaises(TaskError) as nested:
                require_verified(self.runtime, provider)
            self.assertEqual(nested.exception.code, "life_calendar_transaction_active")
        altered_certificate = {**expected, "plan_sha256": "0" * 64}
        with self.runtime.transaction() as db:
            db.execute("UPDATE runtime_metadata SET value=? WHERE key=?", (json.dumps(altered_certificate), CERTIFICATE_KEY))
        with self.assertRaises(TaskError) as mismatch:
            require_verified(self.runtime, provider)
        self.assertEqual(mismatch.exception.code, "life_calendar_adoption_unverified")
        with self.runtime.transaction() as db:
            db.execute("UPDATE runtime_metadata SET value=? WHERE key=?", (json.dumps(expected), CERTIFICATE_KEY))
        self.assertEqual(verify(self.runtime.database_path, self.staged_directory / "good-life.sqlite3",
                                expected_version=self.source_version), result)
        with life.transaction() as db:
            original = tuple(db.execute("SELECT request_hash, plan_json FROM staged_calendar_adoption").fetchone())
            altered = json.loads(original[1])
            altered["moves"][0]["to_owner"] = "different-owner"
            db.execute("UPDATE staged_calendar_adoption SET request_hash=?, plan_json=?", (_plan(altered), _json(altered)))
        with self.assertRaisesRegex(ValueError, "verified host ownership"):
            verify(self.runtime.database_path, self.staged_directory / "good-life.sqlite3", expected_version=self.source_version)
        with life.transaction() as db:
            db.execute("UPDATE staged_calendar_adoption SET request_hash=?, plan_json=?", original)
        commands = PreparedCommands(life)
        commands.install()
        commands.prepare(command_id="pending", owner_key="owner", context_key="context", kind="calendar.change",
            expected_revision=0, payload={}, prepared_at=NOW, validate=lambda *_: True)
        with self.assertRaisesRegex(ValueError, "not yet resolved"):
            verify(self.runtime.database_path, self.staged_directory / "good-life.sqlite3", expected_version=self.source_version)
        with life.transaction() as db:
            db.execute("UPDATE life_prepared_commands SET state='aborted' WHERE command_id='pending'")
        with life.transaction() as db:
            db.execute("UPDATE staged_calendar_records SET record_json='{}'")
        with self.assertRaisesRegex(ValueError, "changed after staging"):
            verify(self.runtime.database_path, self.staged_directory / "good-life.sqlite3", expected_version=self.source_version)


if __name__ == "__main__":
    unittest.main()
