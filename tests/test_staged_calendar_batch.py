import copy
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

import test_staged_calendar_transition as fixture_module
from companion_v2.foundation.outcome import Outcome
from companion_v2.integration import life_commands
from good_life.calendar_records import CalendarTransitionPending, StagedCalendarRecords


class StagedCalendarBatchTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.StagedCalendarTransitionTests("test_change_and_reminder_recover_without_exposing_unconfirmed_state")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_batch_is_atomic_fenced_and_recoverable(self):
        fixture = self.fixture
        first = copy.deepcopy(fixture.request)
        second = copy.deepcopy(first)
        second.update(command_id="second-command", expected_revision=0)
        second["payload"].update(record_id="second", operation_id="calendar:second", previous_hash=None, action="create")
        batch = {"command_id": "planning-batch", "owner_key": first["owner_key"], "context_key": first["context_key"],
            "kind": "calendar.batch", "expected_revision": 0,
            "payload": {"commands": [first, second], "storage_keys": ["calendar-owner"]}}
        invalid = copy.deepcopy(batch)
        invalid["payload"]["commands"][1]["payload"]["record_id"] = "event"
        with self.assertRaises(ValueError):
            fixture.changes.dispatch("batch.prepare", invalid, prepared_at=fixture_module.NOW)
        self.assertEqual(fixture.commands.pending(), ())
        fixture.changes.dispatch("batch.prepare", batch, prepared_at=fixture_module.NOW)
        prepared = fixture.changes.dispatch("batch.status", batch)
        self.assertEqual(len(prepared["proposal"]["proposals"]), 2)
        records = StagedCalendarRecords(fixture.life)
        self.assertEqual(records.dispatch("get", owner_key="calendar-owner", identifier="event")["revision"], 1)
        self.assertIsNone(records.dispatch("get", owner_key="calendar-owner", identifier="second"))
        decision = life_commands.decide(fixture.runtime, context=fixture.context, request=batch, outcome="commit",
            observed_at=fixture_module.NOW, verify_prepared=fixture.commands.verify_prepared,
            authorize=lambda *_: Outcome.success(True), apply=lambda *_: None)
        result = fixture.changes.dispatch("batch.resolve", decision,
            verify_host_decision=lambda value: life_commands.verify_decision(fixture.runtime, value))
        self.assertEqual(result["result"], prepared["proposal"])
        self.assertEqual(fixture.changes.dispatch("batch.resolve", decision,
            verify_host_decision=lambda value: life_commands.verify_decision(fixture.runtime, value)), result)
        with self.assertRaises(CalendarTransitionPending):
            records.dispatch("get", owner_key="calendar-owner", identifier="event")
        self.assertIsNone(records.dispatch("get", owner_key="foreign", identifier="second"))
        pending = fixture.commands.pending()[0]
        self.assertEqual(fixture.commands.recovery_request(command_id=pending["command_id"], owner_key=pending["owner_key"],
            context_key=pending["context_key"], request_hash=pending["request_hash"]), batch)
        verify = lambda value: fixture.changes.dispatch("batch.verify_commit", value)
        self.assertTrue(life_commands.complete(fixture.runtime, decision, observed_at=fixture_module.NOW, verify_life_commit=verify))
        self.assertTrue(fixture.changes.dispatch("batch.acknowledge", "planning-batch",
            verify_host_completion=lambda evidence: life_commands.verify_completion(fixture.runtime, evidence)))
        self.assertEqual(records.dispatch("get", owner_key="calendar-owner", identifier="event")["revision"], 2)
        self.assertEqual(records.dispatch("get", owner_key="calendar-owner", identifier="second")["revision"], 1)


if __name__ == "__main__":
    unittest.main()
