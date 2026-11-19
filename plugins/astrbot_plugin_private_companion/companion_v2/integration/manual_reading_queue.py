"""Queue an authorized dashboard reading request for the life executor."""

import json

from companion_v2.execution.models import TaskError, digest, encode, require
from companion_v2.foundation.refs import OperationRef, VersionRef
from companion_v2.role_work.codec import activity_from_json
from companion_v2.storage.models import CommitState, OperationState
from companion_v2.storage.repositories import OperationRepository

from .reading_handoff import ReadingStartHandoff


PREFIX = "life.reading.manual.v1:"
RESULT_PREFIX = "life.reading.manual.result.v1:"


def enqueue(manager, operator, data, operation_id):
    app, context = manager.app, manager.context
    handoff = ReadingStartHandoff(app, manager.life, app.role_life.transition_provider())
    manager.life.refresh()
    with app.role_life.consistent_snapshot():
        access = manager.authorize(operator)
        if data.get("view_token") != manager.token(operator, access, data["activity_id"], data["expected_revision"]):
            raise TaskError("life_view_stale", "refresh before applying this change")
        existing = OperationRepository(app.runtime).get(operation_id)
        if not existing.is_success:
            activity = manager._activity(data["activity_id"])
            if activity.revision != data["expected_revision"]:
                raise TaskError("life_revision_conflict", "reading activity changed")
            handoff._expected_start(activity, operation_id, app.now())
        with app.runtime.transaction() as db:
            manager.authorize(operator)
            operations = OperationRepository(app.runtime)
            receipt = require(operations.reserve(OperationRef(operation_id, "role.life.management", context.scope,
                VersionRef(operation_id, 1), operation_id), parameter_hash=digest(data), observed_at=app.now()))
            if receipt.replayed:
                if receipt.state is OperationState.ACCEPTED and db.execute(
                        "SELECT 1 FROM runtime_metadata WHERE key=?", (PREFIX + operation_id,)).fetchone() is None:
                    raise TaskError("life_reading_unresolved", "the original manual reading request is unavailable")
                if receipt.state not in (OperationState.ACCEPTED, OperationState.COMPLETED):
                    raise TaskError("life_reading_unresolved", "the original manual reading request cannot be confirmed")
            else:
                intent = {"operation_id": operation_id, "context_key": context.key, "owner_key": digest(manager.owner),
                          "activity_id": activity.activity_id, "revision": activity.revision, "parameter_hash": digest(data)}
                db.execute("INSERT INTO runtime_metadata VALUES (?,?)", (PREFIX + operation_id, encode(intent)))
    app.notify()
    result = {"state": "scheduled" if receipt.state is OperationState.ACCEPTED else "completed"}
    if receipt.state is OperationState.COMPLETED:
        saved = app.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?", (RESULT_PREFIX + operation_id,))
        if saved:
            result = json.loads(saved["value"])
    return {"operation_id": operation_id, "activity_id": data["activity_id"], **result, "replayed": receipt.replayed}


def pending(app):
    rows = app.runtime.read_all("SELECT value FROM runtime_metadata WHERE key LIKE ? ORDER BY key LIMIT 100", (PREFIX + "%",))
    result = []
    for row in rows:
        try:
            intent = json.loads(row["value"])
            result.append({"operation_id": intent["operation_id"], "context_key": intent["context_key"],
                           "owner_key": intent["owner_key"], "activity_id": intent["activity_id"]})
        except (ValueError, TypeError, KeyError):
            continue
    return result


async def start(app, routine, operation_id, *, handoff=None):
    row = app.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?", (PREFIX + operation_id,))
    if row is None:
        raise TaskError("life_reading_unresolved", "manual reading request is unavailable")
    intent = json.loads(row["value"])
    fields = {"operation_id", "context_key", "owner_key", "activity_id", "revision", "parameter_hash"}
    if (type(intent) is not dict or set(intent) != fields or intent["operation_id"] != operation_id
            or intent["context_key"] != routine.context.key or intent["owner_key"] != digest(routine.owner)):
        raise TaskError("life_reading_unresolved", "manual reading request authority changed")
    operations = OperationRepository(app.runtime)
    receipt = require(operations.get(operation_id))
    if (receipt.operation.owner != "role.life.management" or receipt.operation.scope != routine.context.scope
            or receipt.parameter_hash != intent["parameter_hash"] or receipt.commit_state is not CommitState.COMMITTED
            or receipt.state not in (OperationState.ACCEPTED, OperationState.COMPLETED)):
        raise TaskError("life_reading_unresolved", "manual reading authorization is unavailable")
    start_id = "read-start-" + digest([routine.context.key, operation_id])
    saved = app.role_life.activity_receipt(start_id, owner=routine.owner)
    if saved is None:
        try:
            current = routine.activity(intent["activity_id"])
        except TaskError as error:
            if error.code != "life_activity_unavailable":
                raise
            current = None
        if current is None or current.revision != intent["revision"] or current.state.value not in ("planned", "paused"):
            with app.runtime.transaction() as db:
                receipt = require(operations.get(operation_id))
                if receipt.state is OperationState.ACCEPTED:
                    require(operations.transition(operation_id, expected_state=OperationState.ACCEPTED,
                                                  next_state=OperationState.COMPLETED, observed_at=app.now()))
                db.execute("INSERT OR REPLACE INTO runtime_metadata VALUES (?,?)",
                           (RESULT_PREFIX + operation_id, encode({"state": "rejected", "code": "life_revision_conflict"})))
                db.execute("DELETE FROM runtime_metadata WHERE key=? AND value=?", (PREFIX + operation_id, row["value"]))
            return {"state": "rejected", "active": False}
        starter = handoff if handoff is not None else ReadingStartHandoff(app, routine, app.role_life.transition_provider())
        activity = await starter.execute(
            intent["activity_id"], expected_revision=intent["revision"], request_id=operation_id)
    else:
        activity = activity_from_json(saved)
        start_receipt = require(operations.get(start_id))
        if (activity.activity_id != intent["activity_id"] or activity.revision != intent["revision"] + 1
                or activity.work.request_id != operation_id or start_receipt.operation.owner != "role.life"
                or start_receipt.operation.scope != routine.context.scope or start_receipt.state is not OperationState.COMPLETED
                or start_receipt.commit_state is not CommitState.COMMITTED):
            raise TaskError("life_reading_unresolved", "manual reading start receipt changed")
    with app.runtime.transaction() as db:
        receipt = require(operations.get(operation_id))
        if receipt.state is OperationState.ACCEPTED:
            require(operations.transition(operation_id, expected_state=OperationState.ACCEPTED,
                                          next_state=OperationState.COMPLETED, observed_at=app.now()))
        elif receipt.state is not OperationState.COMPLETED:
            raise TaskError("life_reading_unresolved", "manual reading request cannot be confirmed")
        db.execute("DELETE FROM runtime_metadata WHERE key=? AND value=?", (PREFIX + operation_id, row["value"]))
    current = routine.activity(activity.activity_id)
    return {"activity_id": activity.activity_id, "revision": activity.revision,
            "active": current == activity and current.state.value == "active"}
