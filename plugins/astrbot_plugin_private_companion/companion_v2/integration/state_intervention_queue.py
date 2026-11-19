"""Queue an authorized state intervention for the life scheduler."""

import json

from companion_v2.execution.models import TaskError, digest, encode, require
from companion_v2.foundation.refs import OperationRef, VersionRef
from companion_v2.role_work.state.ownership import role_owner
from companion_v2.storage.models import CommitState, OperationState
from companion_v2.storage.repositories import OperationRepository


PREFIX = "life.state.intervention.v1:"
RESULT_PREFIX = "life.state.intervention.result.v1:"


def enqueue(app, context, request_id, expected_input_epoch):
    if (app.role_state.state_owner() != "good_life" or type(request_id) is not str or not request_id
            or len(request_id) > 256 or type(expected_input_epoch) is not int or expected_input_epoch < 0):
        raise TaskError("life_state_command_invalid", "state intervention request is invalid")
    app.state_maintenance._check_intervention(context, app.now())
    if app.state_maintenance._input_epochs.get(context.key, 0) != expected_input_epoch:
        raise TaskError("state_inputs_changed", "new conversation input superseded this intervention")
    generation = app.state_maintenance._intervention_generation
    identifier = "life-state-intervention-" + digest([context.key, request_id])
    parameters = digest([context.key, request_id, expected_input_epoch, generation])
    operation = OperationRef(identifier, "state.intervention_request", context.scope, VersionRef(identifier, 1), identifier)
    with app.runtime.transaction() as db:
        receipt = require(OperationRepository(app.runtime).reserve(operation, parameter_hash=parameters, observed_at=app.now()))
        if receipt.replayed:
            if receipt.state is OperationState.ACCEPTED and db.execute(
                    "SELECT 1 FROM runtime_metadata WHERE key=?", (PREFIX + identifier,)).fetchone() is None:
                raise TaskError("life_state_unconfirmed", "state intervention request is unavailable")
            if receipt.state not in (OperationState.ACCEPTED, OperationState.COMPLETED):
                raise TaskError("life_state_unconfirmed", "state intervention request is unresolved")
        else:
            intent = {"operation_id": identifier, "context_key": context.key, "owner_key": digest(role_owner(context)),
                      "request_id": request_id, "expected_input_epoch": expected_input_epoch,
                      "generation": generation, "parameter_hash": parameters}
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


async def run(app, context, instruction, operation_id):
    row = app.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?", (PREFIX + operation_id,))
    if row is None:
        return {"state": "already_handled"}
    intent = json.loads(row["value"])
    fields = {"operation_id", "context_key", "owner_key", "request_id", "expected_input_epoch", "generation", "parameter_hash"}
    expected = digest([context.key, intent.get("request_id"), intent.get("expected_input_epoch"), intent.get("generation")])
    receipt = require(OperationRepository(app.runtime).get(operation_id))
    if (type(intent) is not dict or set(intent) != fields or intent["operation_id"] != operation_id
            or intent["context_key"] != context.key or intent["owner_key"] != digest(role_owner(context))
            or intent["parameter_hash"] != expected or receipt.operation.owner != "state.intervention_request"
            or receipt.operation.scope != context.scope or receipt.parameter_hash != expected
            or receipt.commit_state is not CommitState.COMMITTED or receipt.state is not OperationState.ACCEPTED):
        raise TaskError("life_state_unconfirmed", "state intervention authority changed")
    try:
        if intent["generation"] != app.state_maintenance._intervention_generation:
            report = {"state": "failed", "code": "state_inputs_changed"}
        else:
            report = await app.state_maintenance.run_life(context, instruction, intent["request_id"], intervention=True,
                                                          expected_input_epoch=intent["expected_input_epoch"])
    except TaskError as error:
        if error.code in ("life_provider_unavailable", "life_state_write_unavailable"):
            raise
        report = {"state": "failed", "code": error.code}
    with app.runtime.transaction() as db:
        current = require(OperationRepository(app.runtime).get(operation_id))
        if current.state is OperationState.ACCEPTED:
            require(OperationRepository(app.runtime).transition(operation_id, expected_state=OperationState.ACCEPTED,
                next_state=OperationState.COMPLETED, observed_at=app.now()))
        elif current.state is not OperationState.COMPLETED:
            raise TaskError("life_state_unconfirmed", "state intervention cannot be confirmed")
        db.execute("INSERT OR REPLACE INTO runtime_metadata VALUES (?,?)", (RESULT_PREFIX + operation_id, encode(report)))
        db.execute("DELETE FROM runtime_metadata WHERE key=? AND value=?", (PREFIX + operation_id, row["value"]))
    app.notify()
    return report
