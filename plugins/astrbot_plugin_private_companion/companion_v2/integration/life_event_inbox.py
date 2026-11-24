"""Durably accept life completions and private role notices."""

from datetime import datetime
import hashlib
import json

from companion_v2.execution.models import TaskError, digest, require
from companion_v2.foundation.outcome import Outcome
from companion_v2.foundation.refs import RoleOwnerRef
from companion_v2.role_work.codec import activity_from_json
from companion_v2.role_work.models import RoleActivityState
from companion_v2.storage.models import CommitState, OperationState
from companion_v2.storage.repositories import OperationRepository

from . import life_events


PREFIX = "life.event.inbox.v1:"
VERSION_KEY = "life.event.inbox.version"
ACADEMIC_NOTICES = "academic_notices"
ACADEMIC_NOTICE_PREFIX = PREFIX + "academics:"
MAX_ACADEMIC_NOTICES = 12
CONSUMERS = frozenset({"journal", "dream", "interest", "wardrobe", ACADEMIC_NOTICES})


def inbox_key(context, consumer, event_id):
    prefix = ACADEMIC_NOTICE_PREFIX if consumer == ACADEMIC_NOTICES else PREFIX
    return prefix + digest([context.key, consumer, event_id])


def _operation_id(context, consumer, event_id):
    return "life-event-" + digest([context.key, consumer, event_id])


def _validate_completion(app, context, event, consumer, observed_at):
    life_events._validate_event(context, event, consumer)
    if consumer not in CONSUMERS or event["kind"] != "activity.completed":
        raise TaskError("life_event_invalid", "unsupported life event or consumer")
    payload = event["payload"]
    if (set(payload) != {"activity_id", "revision", "operation_id"}
            or type(payload["activity_id"]) is not str or not payload["activity_id"]
            or type(payload["operation_id"]) is not str or not payload["operation_id"]
            or type(payload["revision"]) is not int or payload["revision"] < 1):
        raise TaskError("life_event_invalid", "invalid completion reference")
    expected_id = "life-activity-" + hashlib.sha256(payload["operation_id"].encode("utf-8")).hexdigest()
    if event["event_id"] != expected_id:
        raise TaskError("life_event_invalid", "completion event does not match its operation")
    owner = RoleOwnerRef(context.scope.installation_id, context.scope.bot_id,
                         context.role_binding.role.subject_id)
    activity = app.role_life.activity_revision(payload["activity_id"], payload["revision"], owner=owner)
    current = app.role_life.activity(payload["activity_id"], owner=owner)
    receipt = app.role_life.activity_receipt(payload["operation_id"], owner=owner)
    evidence = app.role_life.activity_revision_evidence(payload["activity_id"], payload["revision"], owner=owner)
    operation = require(OperationRepository(app.runtime).get(payload["operation_id"]))
    if (activity != current or activity.state is not RoleActivityState.COMPLETED
            or activity.scope != context.scope or activity.work is None or activity.work.context_key != context.key
            or receipt is None or activity_from_json(receipt) != activity
            or evidence != {"provenance": "recorded", "operation_id": payload["operation_id"]}
            or operation.operation.owner != "role.life" or operation.operation.scope != context.scope
            or operation.state is not OperationState.COMPLETED or operation.commit_state is not CommitState.COMMITTED
            or datetime.fromisoformat(event["occurred_at"]) != activity.updated_at
            or activity.updated_at > observed_at):
        raise TaskError("life_event_unconfirmed", "completion has no matching committed host and life evidence")
    if ((consumer in ("dream", "wardrobe") and activity.work.kind != "sleep")
            or (consumer == "wardrobe" and (activity.sleep is None or activity.sleep.kind != "night"))
            or (consumer == "interest" and activity.work.kind not in ("reading", "social"))):
        raise TaskError("life_event_invalid", "completion does not match its consumer")
    return activity, operation


def accept(app, context, event, *, consumer, observed_at):
    """Queue one event atomically with its host deduplication receipt."""
    if app.role_life.activity_owner() != "good_life":
        raise TaskError("life_event_unavailable", "life events require transferred activity ownership")
    with app.role_life.consistent_snapshot():
        life_events._validate_event(context, event, consumer)
        provider = app.role_life.events_provider()
        if provider.dispatch("get", event_id=event["event_id"], consumer=consumer) != event:
            raise TaskError("life_event_unconfirmed", "completion event is not present in the life provider")
        if consumer == ACADEMIC_NOTICES:
            notice = event["payload"].get("notice")
            if (event["kind"] != "academics.grade.released" or type(notice) is not str
                    or not 1 <= len(notice) <= 600 or datetime.fromisoformat(event["occurred_at"]) > observed_at):
                raise TaskError("life_event_invalid", "invalid academic notice")
        else:
            activity, original_operation = _validate_completion(app, context, event, consumer, observed_at)

        def authorize(value, at):
            result = app.tasks.authorize_context(value, action="role.life.run", observed_at=at)
            if not result.is_success:
                return result
            if consumer == ACADEMIC_NOTICES:
                return Outcome.success(True)
            current = OperationRepository(app.runtime).get(event["payload"]["operation_id"])
            if (not current.is_success or current.value.operation != original_operation.operation
                    or current.value.parameter_hash != original_operation.parameter_hash
                    or current.value.state is not OperationState.COMPLETED
                    or current.value.commit_state is not CommitState.COMMITTED):
                return Outcome.failed("life_event_unconfirmed", "the host completion receipt changed")
            if activity.reading is not None:
                source = app.role_journal.sources.validate_reference_for_audience(
                    activity.reading.source, recipient=value.actor.subject, observed_at=at,
                    expected_content_hash=activity.reading.content_hash)
                if not source.is_success:
                    return source
            return Outcome.success(True)

        def enqueue(connection, item):
            version = connection.execute("SELECT value FROM runtime_metadata WHERE key=?", (VERSION_KEY,)).fetchone()
            if version is not None and version["value"] != "1":
                raise TaskError("life_event_inbox_invalid", "life event inbox version is incompatible")
            connection.execute("INSERT OR IGNORE INTO runtime_metadata(key, value) VALUES (?, '1')", (VERSION_KEY,))
            key = inbox_key(context, consumer, item["event_id"])
            # These consumers read their originals on demand; they need no worker.
            state = "completed" if consumer in ("journal", ACADEMIC_NOTICES) else "pending"
            document = json.dumps({"schema": "companion.life-event-inbox.v1", "state": state,
                                   "consumer": consumer, "event": item},
                                  ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            connection.execute("INSERT INTO runtime_metadata(key, value) VALUES (?, ?)", (key, document))

        return life_events.consume_once(app.runtime, context=context, event=event, consumer=consumer,
                                        observed_at=observed_at, authorize=authorize, apply=enqueue)


def academic_notice_ids(runtime, context, at):
    rows = runtime.read_all("""SELECT json_extract(value, '$.event.event_id') AS event_id
        FROM runtime_metadata WHERE key GLOB ? AND json_valid(value)
        AND json_extract(value, '$.event.context_key')=?
        AND julianday(json_extract(value, '$.event.occurred_at')) <= julianday(?)
        ORDER BY json_extract(value, '$.event.sequence') DESC LIMIT ?""",
        (ACADEMIC_NOTICE_PREFIX + "*", context.key, at.isoformat(), MAX_ACADEMIC_NOTICES))
    return [row["event_id"] for row in rows]


def academic_notice(runtime, context, event_id, at):
    row = runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?",
                           (inbox_key(context, ACADEMIC_NOTICES, event_id),))
    if row is not None:
        event = json.loads(row["value"])["event"]
        life_events._validate_event(context, event, ACADEMIC_NOTICES)
        if datetime.fromisoformat(event["occurred_at"]) <= at:
            return event["payload"]["notice"]
    return None


def verify_received(runtime, context, event, *, consumer):
    """Confirm the durable inbox and matching deduplication receipt before ack."""
    life_events._validate_event(context, event, consumer)
    operation = OperationRepository(runtime).get(_operation_id(context, consumer, event["event_id"]))
    if (not operation.is_success or operation.value.state is not OperationState.COMPLETED
            or operation.value.commit_state is not CommitState.COMMITTED
            or operation.value.operation.owner != "life.event" or operation.value.operation.scope != context.scope
            or operation.value.parameter_hash != event["content_hash"]):
        return False
    row = runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?",
                           (inbox_key(context, consumer, event["event_id"]),))
    if row is None:
        return False
    try:
        item = json.loads(row["value"])
        return (type(item) is dict and item.get("schema") == "companion.life-event-inbox.v1"
                and item.get("consumer") == consumer and item.get("event") == event
                and item.get("state") in ("pending", "completed", "failed", "unknown"))
    except (ValueError, TypeError):
        return False


def receive_pending(app, context, *, consumer, observed_at, limit=50, after_sequence=0):
    """Copy pending provider events to the host inbox, then acknowledge them."""
    if (consumer not in CONSUMERS or type(limit) is not int or not 1 <= limit <= 100
            or type(after_sequence) is not int or after_sequence < 0):
        raise ValueError("invalid life event consumer or page size")
    provider = app.role_life.events_provider()
    events = provider.dispatch("pending", consumer=consumer, context_key=context.key, limit=limit,
                               after_sequence=after_sequence)
    if not isinstance(events, (tuple, list)) or len(events) > limit:
        raise TaskError("life_provider_data_invalid", "life provider returned an invalid event page")
    cursor = after_sequence
    for event in events:
        if type(event) is not dict or type(event.get("sequence")) is not int or event["sequence"] <= cursor:
            raise TaskError("life_provider_data_invalid", "life provider returned unordered events")
        cursor = event["sequence"]
    received = acknowledged = rejected = 0
    for event in events:
        try:
            if accept(app, context, event, consumer=consumer, observed_at=observed_at):
                received += 1
            confirmed = provider.dispatch("acknowledge", event_id=event["event_id"], consumer=consumer,
                acknowledged_at=observed_at, verify_host_receipt=lambda item, recipient:
                    recipient == consumer and verify_received(app.runtime, context, item, consumer=consumer))
            if confirmed is not True:
                raise TaskError("life_event_unconfirmed", "life provider did not acknowledge the event")
            acknowledged += 1
        except (TaskError, ValueError, KeyError, TypeError):
            rejected += 1
    return {"received": received, "acknowledged": acknowledged, "rejected": rejected,
            "next_after": cursor if len(events) == limit else 0}
