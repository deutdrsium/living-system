import asyncio
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "astrbot_plugin_private_companion"))
from companion_v2.delivery.service import DeliveryRequest
from companion_v2.agenda.background import AgendaDispatchPolicy
from companion_v2.execution.models import ClaimResult, TaskError
from companion_v2.foundation.outcome import Outcome
from companion_v2.integration import calendar_reminder_hold as holds
from tests.test_agenda_background import AgendaBackgroundFixture
from tests.test_management_access import NOW


class CalendarReminderHoldTests(AgendaBackgroundFixture, unittest.IsolatedAsyncioTestCase):
    async def test_due_reminder_waits_and_other_reminders_continue(self):
        host = self.host(agenda_policy=AgendaDispatchPolicy(scan_size=1))
        app = host.application
        identifier = "legacy-reminder-held"
        held_occurrence, _ = self.reminder(app, identifier)
        second_identifier = "calendar-reminder-held-two"
        second_occurrence, _ = self.reminder(app, second_identifier)
        other_occurrence, _ = self.reminder(app, "ordinary-reminder")
        with self.runtime.transaction() as connection:
            self.assertTrue(holds.hold(connection, identifier, "life-decision-one"))
            self.assertFalse(holds.hold(connection, identifier, "life-decision-one"))
            holds.hold(connection, second_identifier, "life-decision-second")
        reminder = app.agenda.repository.reminder(identifier)
        context = app.agenda_background._context(reminder)
        with self.assertRaises(TaskError) as blocked:
            app.calendar.validate_reminder(context, identifier, NOW)
        self.assertEqual(blocked.exception.code, holds.ERROR)
        with patch.object(app.calendar, "validate_reminder", return_value=None):
            await self.start(host)
            await self.delivered(app, other_occurrence)
            self.assertFalse(app.agenda_background.advance().unwrap())
            self.assertIsNone(self.dispatch(held_occurrence))
            self.assertIsNone(self.dispatch(second_occurrence))
            self.assertEqual(len(self.sent), 1)
            with self.runtime.transaction() as connection:
                with self.assertRaises(TaskError):
                    holds.release(connection, identifier, "life-decision-other")
                self.assertTrue(holds.release(connection, identifier, "life-decision-one"))
            app.notify()
            await self.delivered(app, held_occurrence)
        self.assertEqual(len(self.sent), 2)

    async def test_queued_reminder_is_deferred_without_a_failed_attempt(self):
        host = self.host()
        app = host.application
        identifier = "calendar-reminder-queued"

        def validate(_context, reminder_id, _at):
            if holds.held(self.runtime, reminder_id):
                raise TaskError(holds.ERROR, "calendar reminder awaits a decision")

        with patch.object(app.calendar, "validate_reminder", side_effect=validate), \
             patch.object(app.background, "advance", return_value=False):
            await self.start(host)
            occurrence, _ = self.reminder(app, identifier)
            app.agenda_background.advance().unwrap()
            delivery = self.delivery(occurrence)
            self.assertIsNotNone(delivery)
            request = DeliveryRequest.restore(delivery["request_json"])
            with patch.object(app.tasks, "claim_task", return_value=Outcome.success(ClaimResult(None, "task_lane_full"))):
                waiter = asyncio.create_task(app.background.runner.run(request.context, delivery["task_id"]))
                await self.until(lambda: delivery["task_id"] in app.background.waiting._pending)
                with self.runtime.transaction() as connection:
                    holds.hold(connection, identifier, "life-decision-two")
                waiter.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await waiter
            self.assertEqual(app.tasks.read(request.context, delivery["task_id"]).unwrap().state, "created")
            claim = app.tasks.claim_task(request.context, delivery["task_id"]).unwrap()
            self.assertEqual((claim.claim, claim.refused), (None, holds.ERROR))
            result = (await app.background.runner.run(request.context, delivery["task_id"])).unwrap()
            self.assertEqual((result.task.state, result.refused), ("created", holds.ERROR))
            self.assertEqual(result.task.attempt_no, 0)
            self.assertEqual(app.outbound.pending(limit=1), ())
            self.assertIsNone(app.outbound.next_wake())
            with self.runtime.transaction() as connection:
                holds.release(connection, identifier, "life-decision-two")
                connection.execute("UPDATE background_deliveries SET state='unknown' WHERE delivery_id=?", (delivery["delivery_id"],))
                with self.assertRaises(TaskError) as unknown:
                    holds.hold(connection, identifier, "life-decision-unknown")
                self.assertEqual(unknown.exception.code, "life_calendar_reminder_in_flight")
                connection.execute("UPDATE background_deliveries SET state='pending' WHERE delivery_id=?", (delivery["delivery_id"],))
            claimed = app.tasks.claim_task(request.context, delivery["task_id"]).unwrap().claim
            self.assertIsNotNone(claimed)
            with self.runtime.transaction() as connection:
                with self.assertRaises(TaskError) as in_flight:
                    holds.hold(connection, identifier, "life-decision-three")
            self.assertEqual(in_flight.exception.code, "life_calendar_reminder_in_flight")
            app.tasks.abandon(claimed, reason="runner_cancelled").unwrap()
        self.assertEqual(self.sent, [])

    async def test_queued_hold_survives_host_restart(self):
        host = self.host()
        app = host.application
        identifier = "calendar-reminder-restart"
        with patch.object(app.calendar, "validate_reminder", return_value=None), \
             patch.object(app.background, "advance", return_value=False):
            await self.start(host)
            occurrence, _ = self.reminder(app, identifier)
            app.agenda_background.advance().unwrap()
            original = self.delivery(occurrence)["request_json"]
            with self.runtime.transaction() as connection:
                holds.hold(connection, identifier, "life-decision-restart")
            await app.close()
        next_host = self.host(runtime=self.open_runtime())
        next_app = next_host.application
        with patch.object(next_app.calendar, "validate_reminder", return_value=None):
            await self.start(next_host, task_generation=1, model_generation=1)
            self.assertEqual(self.delivery(occurrence)["state"], "pending")
            self.assertEqual(next_app.outbound.pending(limit=1), ())
            self.assertEqual(self.sent, [])
            with next_app.runtime.transaction() as connection:
                holds.release(connection, identifier, "life-decision-restart")
            next_app.notify()
            row = await self.delivered(next_app, occurrence)
            self.assertEqual(row["request_json"], original)
        self.assertEqual(len(self.sent), 1)


if __name__ == "__main__":
    unittest.main()
