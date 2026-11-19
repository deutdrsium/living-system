"""Finish authorized manual state-maintenance requests across the two stores."""

import json

from companion_v2.execution.models import TaskError, require
from companion_v2.role_work.state.ownership import role_schedule_key
from companion_v2.storage.models import OperationState
from companion_v2.storage.repositories import OperationRepository


PREFIX = "life.state.manual.v1:"


def finish(app, context, operation_id):
    if app.role_state.state_owner() != "good_life":
        raise TaskError("life_state_owner_changed", "state ownership changed")
    row = app.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?", (PREFIX + operation_id,))
    if row is None:
        raise TaskError("state_receipt_incomplete", "manual state request is unavailable")
    intent = json.loads(row["value"])
    if (type(intent) is not dict or set(intent) != {"operation_id", "context_key", "schedule_key", "due_at", "parameter_hash"}
            or intent["operation_id"] != operation_id or intent["context_key"] != context.key
            or intent["schedule_key"] != role_schedule_key(context)):
        raise TaskError("state_receipt_incomplete", "manual state request changed")
    operations = OperationRepository(app.runtime)
    operation = require(operations.get(operation_id))
    if (operation.operation.scope != context.scope or operation.parameter_hash != intent["parameter_hash"]
            or operation.state not in (OperationState.ACCEPTED, OperationState.COMPLETED)):
        raise TaskError("state_receipt_incomplete", "manual state authorization is unavailable")
    if operation.state is OperationState.ACCEPTED:
        records = app.role_state.records
        done = records.lease.dispatch("state_schedule", "force_due", request_id=operation_id,
                                      schedule_key=intent["schedule_key"], due_at=intent["due_at"])
        if not done:
            return False
    with app.runtime.transaction() as db:
        operation = require(operations.get(operation_id))
        if operation.state is OperationState.ACCEPTED:
            require(operations.transition(operation_id, expected_state=OperationState.ACCEPTED,
                next_state=OperationState.COMPLETED, observed_at=app.now()))
        elif operation.state is not OperationState.COMPLETED:
            raise TaskError("state_receipt_incomplete", "manual state request cannot be confirmed")
        db.execute("DELETE FROM runtime_metadata WHERE key=? AND value=?", (PREFIX + operation_id, row["value"]))
    return True


def recover(app):
    if app.role_state.state_owner() != "good_life":
        return 0
    recovered = 0
    rows = app.runtime.read_all("SELECT key,value FROM runtime_metadata WHERE key LIKE ? ORDER BY key LIMIT 100", (PREFIX + "%",))
    for row in rows:
        try:
            intent = json.loads(row["value"])
            binding = app.state_maintenance.bindings.get(intent["context_key"])
            if binding is not None:
                recovered += int(finish(app, binding[0], intent["operation_id"]))
        except (TaskError, ValueError, KeyError, TypeError):
            continue
    return recovered
