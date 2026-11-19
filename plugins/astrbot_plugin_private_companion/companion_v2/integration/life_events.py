"""Deduplicate life events together with their local host-side effects."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import inspect
import json

from companion_v2.execution.models import TaskError, digest, require
from companion_v2.foundation.refs import OperationRef, RoleOwnerRef, VersionRef
from companion_v2.storage.models import OperationState
from companion_v2.storage.repositories import OperationRepository


def _validate_event(context, event, consumer):
    fields = {"sequence", "event_id", "owner_key", "context_key", "kind", "payload",
              "content_hash", "occurred_at", "consumers"}
    try:
        if type(event) is not dict or set(event) != fields or type(event["payload"]) is not dict:
            raise ValueError("invalid event fields")
        if type(event["sequence"]) is not int or event["sequence"] < 1:
            raise ValueError("invalid event sequence")
        for value in (event["event_id"], event["owner_key"], event["context_key"], event["kind"], consumer):
            if type(value) is not str or not value.strip() or len(value) > 256:
                raise ValueError("invalid event identity")
        consumers = event["consumers"]
        if (type(consumers) is not list or not consumers or any(type(x) is not str or not x.strip()
                or len(x) > 256 for x in consumers) or consumers != sorted(set(consumers))
                or consumer not in consumers):
            raise ValueError("invalid event consumers")
        timestamp = datetime.fromisoformat(event["occurred_at"])
        if (timestamp.utcoffset() is None
                or timestamp.astimezone(timezone.utc).isoformat() != event["occurred_at"]):
            raise ValueError("invalid event timestamp")
        owner = RoleOwnerRef(context.scope.installation_id, context.scope.bot_id,
                             context.role_binding.role.subject_id)
        if event["owner_key"] != digest(owner) or event["context_key"] != context.key:
            raise ValueError("event does not belong to the original role and context")
        payload = json.dumps(event["payload"], ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False)
        identity = json.dumps([event["event_id"], event["owner_key"], event["context_key"],
                               event["kind"], payload, event["occurred_at"], consumers],
                              ensure_ascii=False, separators=(",", ":"))
        expected = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        if type(event["content_hash"]) is not str or not hmac.compare_digest(expected, event["content_hash"]):
            raise ValueError("event content changed")
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError) as error:
        raise TaskError("life_event_invalid", "life event identity or content is invalid") from error


def consume_once(runtime, *, context, event, consumer, observed_at, authorize, apply):
    """Run one authorized synchronous local effect and its receipt in one transaction.

    `authorize` must return the host's authorization Outcome. `apply` may only
    write through the supplied host connection; it must not send or perform
    external side effects. The caller acknowledges the provider only after this
    function returns successfully.
    """
    _validate_event(context, event, consumer)
    if not callable(authorize) or not callable(apply):
        raise ValueError("life event requires host authorization and effect callbacks")
    operation_id = "life-event-" + digest([context.key, consumer, event["event_id"]])
    operation = OperationRef(operation_id, "life.event", context.scope,
                             VersionRef(operation_id, 1), operation_id)
    operations = OperationRepository(runtime)
    with runtime.transaction() as connection:
        authorization = authorize(context, observed_at)
        if inspect.isawaitable(authorization):
            if inspect.iscoroutine(authorization):
                authorization.close()
            raise ValueError("life event authorization must be synchronous")
        if require(authorization) is not True:
            raise TaskError("life_event_denied", "life event authorization was denied")
        receipt = require(operations.reserve(operation, parameter_hash=event["content_hash"], observed_at=observed_at))
        if receipt.replayed:
            if receipt.state is not OperationState.COMPLETED:
                raise TaskError("life_event_unconfirmed", "prior event processing is not confirmed")
            return False
        result = apply(connection, event)
        if inspect.isawaitable(result):
            if inspect.iscoroutine(result):
                result.close()
            raise ValueError("life event effects must finish synchronously inside the transaction")
        if result is not None:
            raise ValueError("life event effect must return None")
        require(operations.transition(operation_id, expected_state=OperationState.ACCEPTED,
                                      next_state=OperationState.COMPLETED, observed_at=observed_at))
    return True
