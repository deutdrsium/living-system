from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life")]
from companion_v2.execution.models import TaskError, digest, encode, require, stamp
from companion_v2.foundation.outcome import Outcome
from companion_v2.foundation.refs import OperationRef, RoleOwnerRef, ScopeKind, ScopeRef, SubjectKind, SubjectRef, VersionRef
from companion_v2.integration import life_commands
from companion_v2.storage.models import OperationState
from companion_v2.storage.repositories import OperationRepository
from companion_v2.storage.runtime import StorageRuntime
from good_life.calendar_import import DDL
from good_life.calendar_records import CalendarTransitionPending, StagedCalendarRecords
from good_life.prepared_commands import PreparedCommands
from good_life.staged_calendar_commands import StagedCalendarCommands
from good_life.storage import LifeStorage


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class StagedCalendarTransitionTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.life = LifeStorage.open(self.root / "life")
        self.addCleanup(self.life.close)
        with self.life.transaction() as db:
            for statement in DDL:
                db.execute(statement)
        self.commands = PreparedCommands(self.life)
        self.commands.install()
        self.changes = StagedCalendarCommands(self.life)
        self.runtime = StorageRuntime.open(data_root=self.root / "host", installation_id="installation")
        self.addCleanup(self.runtime.close)
        scope = ScopeRef("installation", "platform", ScopeKind.PRIVATE, "native", "bot", "account", "conversation", "session")
        role = SubjectRef("installation", SubjectKind.BOT, "role")
        self.context = SimpleNamespace(scope=scope, role_binding=SimpleNamespace(role=role), key="context")
        self.owner = RoleOwnerRef("installation", "bot", "role")
        self.data = {"kind": "role_plan", "title": "午后散步", "detail": "在附近走走",
                     "schedule": {"anchor": "2026-01-01T00:00:00.000000+00:00", "timezone_name": "Asia/Shanghai",
                                  "frequency": "daily", "interval": 1, "count": 3, "fold": 0, "gap_policy": "reject"},
                     "duration_minutes": 30, "presence": "散步"}
        self.current = {**self.data, "id": "event", "revision": 1, "updated_at": stamp(NOW),
                        "state": "accepted", "sources": [], "exceptions": {}, "accepted_by": [],
                        "changed_by": json.loads(encode(role)), "reminder_id": "reminder", "reminder_context_key": "context"}
        raw = encode(self.current)
        with self.life.transaction() as db:
            db.execute("INSERT INTO staged_calendar_records VALUES (?, ?, ?, ?, ?)",
                       ("calendar-owner", "event", 1, stamp(NOW), raw))
            db.execute("INSERT INTO staged_calendar_history VALUES (?, ?, ?, ?, ?)",
                       ("calendar-owner", "event", 1, "old-operation", raw))
        with self.runtime.transaction() as db:
            db.execute("INSERT INTO runtime_metadata VALUES ('test-reminder-state', 'scheduled')")
        self.request = {"command_id": "calendar-command", "owner_key": digest(self.owner), "context_key": self.context.key,
            "kind": "calendar.change", "expected_revision": 1,
            "payload": {"storage_key": "calendar-owner", "lookup_owner_keys": ["calendar-owner", "conversation-owner"],
                        "record_id": "event", "operation_id": "calendar:original-operation",
                        "previous_hash": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                        "action": "revise", "data": {**self.data, "title": "傍晚散步"}, "sources": [],
                        "actor": json.loads(encode(role)), "operator_authenticated": False, "planning": None,
                        "observed_at": stamp(NOW + timedelta(seconds=1))}}
        self.original = OperationRef("calendar:original-operation", "calendar", scope,
                                     VersionRef("calendar:original-operation", 1), "calendar:original-operation")

    def reopen(self):
        self.life.close()
        self.runtime.close()
        self.life = LifeStorage.open(self.root / "life")
        self.addCleanup(self.life.close)
        self.commands = PreparedCommands(self.life)
        self.commands.install()
        self.changes = StagedCalendarCommands(self.life)
        self.runtime = StorageRuntime.open(data_root=self.root / "host", installation_id="installation")
        self.addCleanup(self.runtime.close)

    def reserve_and_fence(self, db, _request):
        receipt = require(OperationRepository(self.runtime).reserve(self.original, parameter_hash="calendar-parameters", observed_at=NOW))
        if receipt.replayed:
            raise ValueError("original calendar operation was already reserved")
        db.execute("INSERT INTO runtime_metadata VALUES ('test-reminder-pending', 'reminder')")

    def finalize(self, db, _decision):
        db.execute("UPDATE runtime_metadata SET value='cancelled' WHERE key='test-reminder-state'")
        db.execute("DELETE FROM runtime_metadata WHERE key='test-reminder-pending'")
        require(OperationRepository(self.runtime).transition(self.original.operation_id, expected_state=OperationState.ACCEPTED,
            next_state=OperationState.COMPLETED, observed_at=NOW))

    def reminder_may_deliver(self):
        pending = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key='test-reminder-pending'", ())
        state = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key='test-reminder-state'", ())
        return pending is None and state["value"] == "scheduled"

    def completion(self, evidence):
        receipt = OperationRepository(self.runtime).get(self.original.operation_id)
        return (life_commands.verify_completion(self.runtime, evidence) and receipt.is_success
                and receipt.value.state is OperationState.COMPLETED and receipt.value.operation == self.original
                and receipt.value.parameter_hash == "calendar-parameters" and not self.reminder_may_deliver())

    def test_change_and_reminder_recover_without_exposing_unconfirmed_state(self):
        self.assertTrue(self.reminder_may_deliver())
        self.changes.prepare(self.request, prepared_at=NOW)
        proposal = self.changes.proposal(self.request)
        prepared_status = self.changes.status(self.request)
        self.assertEqual((prepared_status["state"], prepared_status["proposal"]), ("prepared", proposal))
        altered = json.loads(json.dumps(self.request))
        altered["payload"]["record_id"] = "other"
        with self.assertRaises(ValueError):
            self.changes.status(altered)
        self.assertEqual(proposal["cancel_reminder"], self.current)
        self.assertEqual(proposal["record"]["title"], "傍晚散步")
        decision = life_commands.decide(self.runtime, context=self.context, request=self.request, outcome="commit",
            observed_at=NOW, verify_prepared=self.commands.verify_prepared,
            authorize=lambda *_: Outcome.success(True), apply=self.reserve_and_fence)
        self.assertFalse(self.reminder_may_deliver())
        self.reopen()
        pending = self.commands.pending()[0]
        self.assertEqual(self.changes.recover(command_id=pending["command_id"], owner_key=pending["owner_key"],
            context_key=pending["context_key"], request_hash=pending["request_hash"]), self.request)
        decision = life_commands.lookup(self.runtime, context_key=pending["context_key"],
            command_id=pending["command_id"], request_hash=pending["request_hash"])
        result = self.changes.resolve(decision, verify_host_decision=lambda value: life_commands.verify_decision(self.runtime, value))
        self.assertEqual(result["result"], proposal)
        self.assertEqual(self.changes.status(self.request)["state"], "committed")
        records = StagedCalendarRecords(self.life)
        with self.assertRaises(CalendarTransitionPending):
            records.dispatch("get", owner_key="calendar-owner", identifier="event")
        with self.assertRaises(CalendarTransitionPending):
            records.dispatch("by_reminder", owner_keys=("calendar-owner",), reminder_id="reminder")
        self.assertIsNone(records.dispatch("get", owner_key="another-owner", identifier="event"))
        self.assertTrue(self.changes.verify_commit(decision))
        self.assertFalse(self.reminder_may_deliver())
        self.reopen()
        self.assertTrue(life_commands.complete(self.runtime, decision, observed_at=NOW,
                        verify_life_commit=self.changes.verify_commit, finalize=self.finalize))
        with self.assertRaises(CalendarTransitionPending):
            StagedCalendarRecords(self.life).dispatch("get", owner_key="calendar-owner", identifier="event")
        self.assertTrue(self.changes.acknowledge("calendar-command", verify_host_completion=self.completion))
        self.assertFalse(self.changes.acknowledge("calendar-command", verify_host_completion=self.completion))
        self.assertEqual(self.changes.status(self.request)["state"], "acknowledged")
        record = StagedCalendarRecords(self.life).dispatch("get", owner_key="calendar-owner", identifier="event")
        self.assertEqual((record["revision"], record["title"], record["reminder_id"]), (2, "傍晚散步", None))
        self.assertFalse(self.reminder_may_deliver())

    def test_changed_calendar_blocks_host_finalization(self):
        self.changes.prepare(self.request, prepared_at=NOW)
        decision = life_commands.decide(self.runtime, context=self.context, request=self.request, outcome="commit",
            observed_at=NOW, verify_prepared=self.commands.verify_prepared,
            authorize=lambda *_: Outcome.success(True), apply=self.reserve_and_fence)
        self.commands.resolve(decision, verify_host_decision=lambda value: life_commands.verify_decision(self.runtime, value),
                              apply=self.changes.apply)
        with self.life.transaction() as db:
            db.execute("UPDATE staged_calendar_records SET record_json='{}' WHERE record_id='event'")
        self.assertFalse(self.changes.verify_commit(decision))
        with self.assertRaises(TaskError):
            life_commands.complete(self.runtime, decision, observed_at=NOW,
                                   verify_life_commit=self.changes.verify_commit, finalize=self.finalize)
        self.assertFalse(self.reminder_may_deliver())

    def test_create_checks_all_original_owner_keys_and_replays_once(self):
        self.request["expected_revision"] = 0
        self.request["payload"].update(record_id="new-event", action="create", previous_hash=None)
        other = {**self.current, "id": "new-event"}
        with self.life.transaction() as db:
            db.execute("INSERT INTO staged_calendar_records VALUES (?, ?, ?, ?, ?)",
                       ("conversation-owner", "new-event", 1, stamp(NOW), encode(other)))
        with self.assertRaises(ValueError):
            self.changes.prepare(self.request, prepared_at=NOW)
        with self.life.transaction() as db:
            db.execute("DELETE FROM staged_calendar_records WHERE owner_key='conversation-owner' AND record_id='new-event'")
        self.changes.prepare(self.request, prepared_at=NOW)
        self.assertIsNone(self.changes.proposal(self.request)["cancel_reminder"])

        def reserve(_db, _request):
            require(OperationRepository(self.runtime).reserve(self.original,
                    parameter_hash="calendar-parameters", observed_at=NOW))

        def complete_original(_db, _decision):
            require(OperationRepository(self.runtime).transition(self.original.operation_id,
                    expected_state=OperationState.ACCEPTED, next_state=OperationState.COMPLETED, observed_at=NOW))

        decision = life_commands.decide(self.runtime, context=self.context, request=self.request, outcome="commit",
            observed_at=NOW, verify_prepared=self.commands.verify_prepared,
            authorize=lambda *_: Outcome.success(True), apply=reserve)
        first = self.commands.resolve(decision, verify_host_decision=lambda value: life_commands.verify_decision(self.runtime, value),
                                      apply=self.changes.apply)
        again = self.commands.resolve(decision, verify_host_decision=lambda value: life_commands.verify_decision(self.runtime, value),
                                      apply=self.changes.apply)
        self.assertEqual(first, again)
        self.assertTrue(life_commands.complete(self.runtime, decision, observed_at=NOW,
                        verify_life_commit=self.changes.verify_commit, finalize=complete_original))
        self.changes.acknowledge("calendar-command", verify_host_completion=lambda evidence:
                                 life_commands.verify_completion(self.runtime, evidence))
        saved = StagedCalendarRecords(self.life).dispatch("get", owner_key="calendar-owner", identifier="new-event")
        self.assertEqual((saved["revision"], saved["state"]), (1, "accepted"))
        with self.life.transaction() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM staged_calendar_history WHERE record_id='new-event'").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
