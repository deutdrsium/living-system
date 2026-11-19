"""Host-side decisions for a future, durable cross-plugin life command.

This module is not wired to the production executor.  Callbacks supplied by the
host must be synchronous and may perform only host-local database work.
"""

from __future__ import annotations

import hashlib
import inspect
import json

from companion_v2.execution.models import TaskError, digest, require
from companion_v2.foundation.refs import OperationRef, RoleOwnerRef, VersionRef
from companion_v2.storage.models import CommitState, OperationState
from companion_v2.storage.repositories import OperationRepository


PREFIX = "life.command.decision.v1:"


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _synchronous(value):
    if inspect.isawaitable(value):
        if inspect.iscoroutine(value):
            value.close()
        raise ValueError("life command callbacks must complete synchronously")
    return value


def _request(context, request):
    fields = {"command_id", "owner_key", "context_key", "kind", "expected_revision", "payload"}
    try:
        if type(request) is not dict or set(request) != fields or type(request["payload"]) is not dict:
            raise ValueError("invalid request fields")
        for name in ("command_id", "owner_key", "context_key", "kind"):
            value = request[name]
            if type(value) is not str or not value.strip() or value != value.strip() or len(value) > 256:
                raise ValueError("invalid request identity")
        if type(request["expected_revision"]) is not int or request["expected_revision"] < 0:
            raise ValueError("invalid request revision")
        owner = RoleOwnerRef(context.scope.installation_id, context.scope.bot_id, context.role_binding.role.subject_id)
        if request["owner_key"] != digest(owner) or request["context_key"] != context.key:
            raise ValueError("command crosses its original role or context")
        canonical = _canonical(request)
        return json.loads(canonical), hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError) as error:
        raise TaskError("life_command_invalid", "life command identity or content is invalid") from error


def _decision_key(decision_id):
    return PREFIX + decision_id


def decide(runtime, *, context, request, outcome, observed_at, verify_prepared, authorize, apply):
    """Persist one host decision and its host-local effect in a single transaction."""
    request, request_hash = _request(context, request)
    if outcome not in ("commit", "abort") or not all(callable(item) for item in (verify_prepared, authorize, apply)):
        raise ValueError("life command needs a decision and synchronous callbacks")
    identity = digest([context.key, request["command_id"]])
    decision_id, operation_id = "life-decision-" + identity, "life-command-" + identity
    decision = {"command_id": request["command_id"], "request_hash": request_hash,
                "decision": outcome, "decision_id": decision_id}
    # This is a read-only provider call and must not run under the host DB lock.
    if _synchronous(verify_prepared(dict(request))) is not True:
        # An already-persisted decision remains replayable after the life plugin
        # commits or aborts and no longer reports its preparation as pending.
        existing = runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?", (_decision_key(decision_id),))
        if existing is None or not verify_decision(runtime, decision):
            raise TaskError("life_command_unprepared", "the life command is not durably prepared")
        return decision
    operation = OperationRef(operation_id, "life.command", context.scope, VersionRef(operation_id, 1), operation_id)
    operations = OperationRepository(runtime)
    with runtime.transaction() as connection:
        existing = connection.execute("SELECT value FROM runtime_metadata WHERE key=?", (_decision_key(decision_id),)).fetchone()
        if existing is not None:
            if not verify_decision(runtime, decision):
                raise TaskError("life_command_conflict", "the original decision cannot be replaced")
            return decision
        if outcome == "commit":
            if require(_synchronous(authorize(context, observed_at))) is not True:
                raise TaskError("life_command_denied", "life command authorization was denied")
        receipt = require(operations.reserve(operation, parameter_hash=request_hash, observed_at=observed_at))
        if receipt.replayed:
            raise TaskError("life_command_unconfirmed", "the host operation has no matching durable decision")
        if outcome == "commit":
            if _synchronous(apply(connection, json.loads(_canonical(request)))) is not None:
                raise ValueError("host-local command effect must return None")
        state = "decided" if outcome == "commit" else "completed"
        if outcome == "abort":
            require(operations.transition(operation_id, expected_state=OperationState.ACCEPTED,
                                          next_state=OperationState.COMPLETED, observed_at=observed_at))
        connection.execute("INSERT INTO runtime_metadata(key, value) VALUES (?, ?)",
            (_decision_key(decision_id), _canonical({"decision": decision, "operation_id": operation_id, "state": state})))
    return decision


def _record(runtime, decision_id):
    if type(decision_id) is not str or not decision_id.startswith("life-decision-"):
        return None
    row = runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?", (_decision_key(decision_id),))
    if row is None:
        return None
    try:
        record = json.loads(row["value"])
        if (type(record) is not dict or set(record) != {"decision", "operation_id", "state"}
                or type(record["decision"]) is not dict or record["decision"].get("decision_id") != decision_id
                or record["state"] not in ("decided", "completed")):
            return None
        return record
    except (TypeError, ValueError):
        return None


def verify_decision(runtime, decision):
    if type(decision) is not dict or set(decision) != {"command_id", "request_hash", "decision", "decision_id"}:
        return False
    record = _record(runtime, decision["decision_id"])
    if record is None or record["decision"] != decision or decision["decision"] not in ("commit", "abort"):
        return False
    expected_operation = "life-command-" + decision["decision_id"][len("life-decision-"):]
    if record["operation_id"] != expected_operation or (decision["decision"] == "abort" and record["state"] != "completed"):
        return False
    receipt = OperationRepository(runtime).get(expected_operation)
    if not receipt.is_success:
        return False
    value = receipt.value
    expected_state = OperationState.COMPLETED if record["state"] == "completed" else OperationState.ACCEPTED
    return (value.state is expected_state and value.commit_state is CommitState.COMMITTED
            and value.operation.owner == "life.command" and value.operation.idempotency_key == expected_operation
            and value.parameter_hash == decision["request_hash"])


def lookup(runtime, *, context_key, command_id, request_hash):
    """Recover a decision for an exact pending request without exposing the host DB."""
    if any(type(value) is not str or not value or len(value) > 256
           for value in (context_key, command_id, request_hash)):
        return None
    decision_id = "life-decision-" + digest([context_key, command_id])
    record = _record(runtime, decision_id)
    if record is None:
        return None
    decision = record["decision"]
    if (decision.get("command_id") != command_id or decision.get("request_hash") != request_hash
            or not verify_decision(runtime, decision)):
        return None
    return dict(decision)


def complete(runtime, decision, *, observed_at, verify_life_commit, finalize=None):
    """Mark the host receipt complete only after a verified durable life commit."""
    if (not verify_decision(runtime, decision) or decision["decision"] != "commit" or not callable(verify_life_commit)
            or (finalize is not None and not callable(finalize))):
        raise TaskError("life_command_invalid", "host decision is unavailable or not a commit")
    if _synchronous(verify_life_commit(dict(decision))) is not True:
        raise TaskError("life_command_unconfirmed", "the life result is not durably committed")
    operations = OperationRepository(runtime)
    with runtime.transaction() as connection:
        row = connection.execute("SELECT value FROM runtime_metadata WHERE key=?",
                                 (_decision_key(decision["decision_id"]),)).fetchone()
        if row is None:
            raise TaskError("life_command_invalid", "host decision is unavailable")
        record = json.loads(row["value"])
        if record["decision"] != decision:
            raise TaskError("life_command_conflict", "host decision changed")
        if record["state"] == "completed":
            return False
        if record["state"] != "decided":
            raise TaskError("life_command_invalid", "host decision state is invalid")
        if finalize is not None:
            if _synchronous(finalize(connection, dict(decision))) is not None:
                raise ValueError("host-local finalization must return None")
        require(operations.transition(record["operation_id"], expected_state=OperationState.ACCEPTED,
                                      next_state=OperationState.COMPLETED, observed_at=observed_at))
        record["state"] = "completed"
        connection.execute("UPDATE runtime_metadata SET value=? WHERE key=?",
                           (_canonical(record), _decision_key(decision["decision_id"])))
    return True


def verify_completion(runtime, evidence):
    fields = {"command_id", "request_hash", "decision_id"}
    if type(evidence) is not dict or set(evidence) != fields:
        return False
    record = _record(runtime, evidence["decision_id"])
    if record is None or record["state"] != "completed" or record["decision"].get("decision") != "commit":
        return False
    if any(record["decision"].get(key) != value for key, value in evidence.items()):
        return False
    return verify_decision(runtime, record["decision"])
