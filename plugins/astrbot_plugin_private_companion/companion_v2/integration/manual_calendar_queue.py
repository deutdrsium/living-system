"""Queue an authenticated manual planning request for the life scheduler."""

import asyncio
import json

from companion_v2.execution.models import TaskError, digest, encode, require
from companion_v2.foundation.refs import OperationRef, VersionRef
from companion_v2.role_work.state.ownership import role_owner
from companion_v2.storage.models import CommitState, OperationState
from companion_v2.storage.repositories import OperationRepository


PREFIX = "life.calendar.manual.v1:"
RESULT_PREFIX = "life.calendar.manual.result.v1:"


def enqueue(app, context, *, mode, slot, refresh, expected_input_epoch=None):
    if (app.calendar._life_calendar_owner() != "good_life" or mode not in ("daily", "horizon", "intervention")
            or type(slot) is not str or not slot or len(slot) > 256 or type(refresh) is not bool
            or (mode == "intervention" and (type(expected_input_epoch) is not int or expected_input_epoch < 0))
            or (mode != "intervention" and expected_input_epoch is not None)):
        raise TaskError("life_calendar_command_invalid", "manual planning request is invalid")
    app.calendar.prepare_life_snapshot_read()
    app.calendar.access.check(context, "calendar.write")
    if mode == "intervention":
        app.calendar_planning._check_intervention(context, app.now())
        if app.state_maintenance._input_epochs.get(context.key, 0) != expected_input_epoch:
            raise TaskError("planning_inputs_changed", "new conversation input superseded this intervention")
    generation = app.state_maintenance._intervention_generation if mode == "intervention" else None
    identifier = "life-calendar-manual-" + digest([context.key, slot])
    parameters = digest([context.key, mode, slot, refresh, expected_input_epoch, generation])
    operation = OperationRef(identifier, "calendar.manual_request", context.scope, VersionRef(identifier, 1), identifier)
    with app.runtime.transaction() as db:
        receipt = require(OperationRepository(app.runtime).reserve(operation, parameter_hash=parameters, observed_at=app.now()))
        if receipt.replayed:
            if receipt.state is OperationState.ACCEPTED and db.execute(
                    "SELECT 1 FROM runtime_metadata WHERE key=?", (PREFIX + identifier,)).fetchone() is None:
                raise TaskError("life_calendar_unconfirmed", "manual planning request is unavailable")
            if receipt.state not in (OperationState.ACCEPTED, OperationState.COMPLETED):
                raise TaskError("life_calendar_unconfirmed", "manual planning request is unresolved")
        else:
            intent = {"operation_id": identifier, "context_key": context.key, "owner_key": digest(role_owner(context)),
                      "mode": mode, "slot": slot, "refresh": refresh, "expected_input_epoch": expected_input_epoch,
                      "generation": generation, "parameter_hash": parameters, "state": "queued"}
            db.execute("INSERT INTO runtime_metadata VALUES (?,?)", (PREFIX + identifier, encode(intent)))
    app.notify()
    return identifier


def pending(app):
    rows = app.runtime.read_all("SELECT value FROM runtime_metadata WHERE key LIKE ? ORDER BY key LIMIT 100", (PREFIX + "%",))
    result = []
    for row in rows:
        try:
            item = json.loads(row["value"])
            result.append({"operation_id": item["operation_id"], "context_key": item["context_key"], "owner_key": item["owner_key"]})
        except (ValueError, TypeError, KeyError):
            continue
    return result


def result(app, operation_id):
    row = app.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?", (RESULT_PREFIX + operation_id,))
    return json.loads(row["value"]) if row else None


async def wait(app, operation_id, *, timeout=480):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        report = result(app, operation_id)
        if report is not None:
            return report
        await asyncio.sleep(0.25)
    return {"state": "scheduled"}


async def run(app, context, instruction, operation_id):
    row = app.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?", (PREFIX + operation_id,))
    if row is None:
        return result(app, operation_id) or {"state": "unknown"}
    intent = json.loads(row["value"])
    fields = {"operation_id", "context_key", "owner_key", "mode", "slot", "refresh", "expected_input_epoch", "generation", "parameter_hash", "state"}
    expected = digest([context.key, intent.get("mode"), intent.get("slot"), intent.get("refresh"),
                       intent.get("expected_input_epoch"), intent.get("generation")])
    receipt = require(OperationRepository(app.runtime).get(operation_id))
    if (type(intent) is not dict or set(intent) != fields or intent["operation_id"] != operation_id
            or intent["context_key"] != context.key or intent["owner_key"] != digest(role_owner(context))
            or intent["parameter_hash"] != expected or intent["state"] not in ("queued", "running")
            or receipt.operation.owner != "calendar.manual_request" or receipt.operation.scope != context.scope
            or receipt.parameter_hash != expected or receipt.commit_state is not CommitState.COMMITTED
            or receipt.state is not OperationState.ACCEPTED):
        raise TaskError("life_calendar_unconfirmed", "manual planning request authority changed")
    planner = app.calendar_planning
    task_id = planner.identifier(context, intent["slot"])
    prior = OperationRepository(app.runtime).get(task_id)
    if intent["state"] == "running" and prior.is_success:
        report = {"state": "already_attempted" if prior.value.state is OperationState.COMPLETED else "unknown",
                  "task_id": task_id}
    elif intent["mode"] == "intervention" and intent["generation"] != app.state_maintenance._intervention_generation:
        report = {"state": "failed", "code": "planning_inputs_changed"}
    else:
        with app.runtime.transaction() as db:
            changed = db.execute("UPDATE runtime_metadata SET value=? WHERE key=? AND value=?",
                (encode({**intent, "state": "running"}), PREFIX + operation_id, row["value"]))
            if changed.rowcount != 1:
                raise TaskError("life_calendar_unconfirmed", "manual planning request changed")
        try:
            report = await planner.run(context, instruction, mode=intent["mode"], slot=intent["slot"],
                manual=True, refresh=intent["refresh"], intervention=intent["mode"] == "intervention",
                expected_input_epoch=intent["expected_input_epoch"], life=True)
        except TaskError as error:
            report = {"state": "failed", "code": error.code}
        if report.get("state") != "succeeded" and report.get("task_id"):
            command_id = "life-calendar-batch-" + digest([context.key, report["task_id"]])
            decision_id = "life-decision-" + digest([context.key, command_id])
            decision_row = app.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?",
                                                ("life.command.decision.v1:" + decision_id,))
            if decision_row is not None:
                decision = json.loads(decision_row["value"])
                if decision.get("decision", {}).get("decision") == "commit" and decision.get("state") == "decided":
                    report = {"state": "unknown", "code": "life_calendar_unconfirmed", "task_id": report["task_id"]}
    with app.runtime.transaction() as db:
        current = require(OperationRepository(app.runtime).get(operation_id))
        if current.state is OperationState.ACCEPTED:
            require(OperationRepository(app.runtime).transition(operation_id, expected_state=OperationState.ACCEPTED,
                next_state=OperationState.COMPLETED, observed_at=app.now()))
        elif current.state is not OperationState.COMPLETED:
            raise TaskError("life_calendar_unconfirmed", "manual planning request cannot be confirmed")
        db.execute("INSERT OR REPLACE INTO runtime_metadata VALUES (?,?)", (RESULT_PREFIX + operation_id, encode(report)))
        db.execute("DELETE FROM runtime_metadata WHERE key=?", (PREFIX + operation_id,))
    app.notify()
    return report
