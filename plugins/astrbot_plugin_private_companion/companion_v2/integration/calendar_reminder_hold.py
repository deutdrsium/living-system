"""Fence a calendar reminder while a cross-plugin change is unresolved.

Call hold/release inside the host's own transaction after verifying the
original conversation and reminder. A hold never starts or retries delivery.
"""

from companion_v2.execution.models import TaskError


PREFIX = "life.calendar.reminder.pending.v1:"
ERROR = "life_calendar_pending"


def _key(reminder_id):
    if (type(reminder_id) is not str or not reminder_id
            or reminder_id != reminder_id.strip() or len(reminder_id) > 512):
        raise ValueError("invalid calendar reminder identity")
    return PREFIX + reminder_id


def held(runtime, reminder_id):
    return runtime.read_one("SELECT 1 FROM runtime_metadata WHERE key=?", (_key(reminder_id),)) is not None


def held_task(runtime, task_id):
    return runtime.read_one("""SELECT 1 FROM background_deliveries d
        JOIN agenda_occurrences ao ON ao.occurrence_id=json_extract(d.request_json, '$.logical_key')
        JOIN runtime_metadata m ON m.key=? || ao.reminder_id
        WHERE d.owner='agenda.reminder' AND d.task_id=? AND d.state='pending' LIMIT 1""",
        (PREFIX, task_id)) is not None


def hold(connection, reminder_id, decision_id):
    key = _key(reminder_id)
    if not connection.in_transaction or type(decision_id) is not str or not decision_id.startswith("life-decision-"):
        raise ValueError("a host transaction and life decision are required")
    existing = connection.execute("SELECT value FROM runtime_metadata WHERE key=?", (key,)).fetchone()
    if existing is not None:
        if existing[0] != decision_id:
            raise TaskError("life_calendar_conflict", "another calendar change already holds this reminder")
        return False
    if connection.execute("SELECT 1 FROM agenda_reminders WHERE reminder_id=?", (reminder_id,)).fetchone() is None:
        raise TaskError("calendar_reminder_changed", "original reminder is unavailable")
    active = connection.execute("""SELECT 1 FROM agenda_occurrences o
        JOIN agenda_dispatches a ON a.occurrence_id=o.occurrence_id
        JOIN background_deliveries d ON d.delivery_id=a.delivery_id
        LEFT JOIN execution_tasks t ON t.task_id=d.task_id
        WHERE o.reminder_id=? AND (t.task_id IS NULL OR t.state IN ('admitted','running','cancelling')
            OR d.state IN ('dispatched','accepted','unknown')) LIMIT 1""",
        (reminder_id,)).fetchone()
    if active is not None:
        raise TaskError("life_calendar_reminder_in_flight", "the original reminder is still in flight")
    connection.execute("INSERT INTO runtime_metadata(key, value) VALUES (?, ?)", (key, decision_id))
    return True


def release(connection, reminder_id, decision_id):
    key = _key(reminder_id)
    if not connection.in_transaction:
        raise ValueError("a host transaction is required")
    existing = connection.execute("SELECT value FROM runtime_metadata WHERE key=?", (key,)).fetchone()
    if existing is None:
        return False
    if existing[0] != decision_id:
        raise TaskError("life_calendar_conflict", "calendar reminder hold belongs to another decision")
    connection.execute("DELETE FROM runtime_metadata WHERE key=?", (key,))
    return True
