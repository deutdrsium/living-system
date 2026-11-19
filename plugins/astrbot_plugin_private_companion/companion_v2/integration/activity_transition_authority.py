"""Recheck host identity and evidence for a prepared life activity transition."""

from datetime import datetime
import hashlib
import inspect
import json

from companion_v2.execution.models import TaskError, digest, encode, require
from companion_v2.foundation.outcome import Outcome
from companion_v2.foundation.refs import RoleOwnerRef, ScopeKind, normalize_utc
from companion_v2.role_work.codec import activity_from_json
from companion_v2.role_work.models import RoleActivityState, advance_activity

from . import life_commands


def authorize(tasks, sources, context, request, proposal, *, expected_operation_id, expected_consumers,
              observed_at, verify_completion=None, require_life_authority=True, allow_prior_session_cancel=False):
    """Check exact versions and sources; completion needs a host-owned proof callback."""
    try:
        life_commands._request(context, request)
        payload = request["payload"]
        if (request["kind"] != "activity.transition" or type(proposal) is not dict
                or set(proposal) != {"previous_json", "result_json"}
                or type(expected_operation_id) is not str or payload["operation_id"] != expected_operation_id
                or not isinstance(expected_consumers, (tuple, list))
                or payload["event_consumers"] != list(expected_consumers)
                or context.scope.kind is not ScopeKind.PRIVATE or not context.scope.is_resolved):
            raise ValueError("activity transition request is invalid")
        previous_raw, result_raw = proposal["previous_json"], proposal["result_json"]
        if (type(previous_raw) is not str or type(result_raw) is not str
                or hashlib.sha256(previous_raw.encode("utf-8")).hexdigest() != payload["previous_hash"]):
            raise ValueError("activity transition original changed")
        previous, result = activity_from_json(previous_raw), activity_from_json(result_raw)
        owner = RoleOwnerRef(context.scope.installation_id, context.scope.bot_id, context.role_binding.role.subject_id)
        prior_session_cancel = (allow_prior_session_cancel and previous.work is not None
            and previous.work.kind == "reading" and previous.work.context_key != context.key
            and payload["state"] == "cancelled" and payload["work"] is None and payload["reading"] is None
            and payload["sleep"] is None and not payload["event_consumers"] and "reading_control" not in payload
            and "management" in payload)
        if (previous.owner != owner or previous.scope != context.scope or previous.activity_id != payload["activity_id"]
                or previous.revision != request["expected_revision"] or previous.work is None
                or previous.work.context_key != context.key and not prior_session_cancel or result.scope != context.scope
                or result.owner != owner or result.activity_id != previous.activity_id
                or result.work is None or result.work.kind != previous.work.kind
                or result.work.context_key != previous.work.context_key):
            raise ValueError("activity transition crosses its original authority")
        requested_at = normalize_utc(datetime.fromisoformat(payload["observed_at"]), field="activity observation")
        now = normalize_utc(observed_at, field="host observation")
        if requested_at > now:
            raise ValueError("activity transition cannot be in the future")
        expected = advance_activity(previous, expected_revision=request["expected_revision"],
            state=RoleActivityState(payload["state"]), observed_at=requested_at, checkpoint=payload["checkpoint"],
            reading=result.reading, work=result.work if payload["work"] is not None else None,
            sleep=result.sleep if payload["sleep"] is not None else None)
        changed_fields_match = all(payload[name] == json.loads(encode(getattr(result, name)))
                                   for name in ("reading", "work", "sleep")
                                   if name == "reading" or payload[name] is not None)
        if (expected != result or not changed_fields_match
                or (payload["event_consumers"] and result.state is not RoleActivityState.COMPLETED)):
            raise ValueError("activity transition differs from the original rules")
    except TaskError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise TaskError("life_activity_transition_invalid", "activity identity or proposed revision is invalid") from error

    if require_life_authority:
        require(tasks.identity.authorize(context.actor, scope=context.scope, capability="role.life.run", observed_at=now))
        require(tasks.authorize_context(context, action="role.life.run", observed_at=now))
    if previous.work.kind == "reading":
        if (result.work.source != previous.work.source or result.work.content_hash != previous.work.content_hash
                or (result.state is RoleActivityState.COMPLETED
                    and (result.reading is None or payload["work"] is not None or payload["sleep"] is not None))):
            raise TaskError("life_reading_source_changed", "reading must retain its original source and completion receipt")
    if previous.work.kind == "reading" and (result.state is RoleActivityState.ACTIVE or result.reading is not None):
        require(tasks.identity.authorize(context.actor, scope=context.scope, capability="role.reading.record", observed_at=now))
        require(tasks.authorize_context(context, action="role.reading.record", observed_at=now))
        source = require(sources.read(previous.work.source, resolved=context.actor, capability="role.reading.record",
                                      observed_at=now, authorizer=tasks.identity))
        work = result.work
        if (type(source.payload) is not str or source.content_hash != previous.work.content_hash
                or digest(source.payload) != previous.work.content_hash or work is None or work.kind != "reading"
                or not set(context.audience.recipients).issubset(source.audience.recipients)
                or work.source != previous.work.source or work.content_hash != previous.work.content_hash
                or work.start_offset < previous.work.start_offset or work.end_offset > len(source.payload)
                or source.payload[previous.work.start_offset:work.start_offset].strip()):
            raise TaskError("life_reading_source_changed", "reading must retain its authorized original span")
        if result.reading is not None:
            reading = result.reading
            if (reading.source != previous.work.source or reading.content_hash != previous.work.content_hash
                    or reading.text_length != len(source.payload) or reading.start_offset != previous.work.start_offset
                    or reading.end_offset != previous.work.end_offset
                    or not source.payload[reading.start_offset:reading.end_offset].strip()):
                raise TaskError("life_reading_source_changed", "reading receipt must match the original consumed span")
    if result.state is RoleActivityState.COMPLETED:
        if not callable(verify_completion):
            raise TaskError("life_activity_evidence_required", "completion needs its original host execution evidence")
        evidence = verify_completion(context, previous, result, request, now)
        if inspect.isawaitable(evidence):
            if inspect.iscoroutine(evidence):
                evidence.close()
            raise TaskError("life_activity_evidence_required", "completion evidence must be checked synchronously")
        if require(evidence) is not True:
            raise TaskError("life_activity_evidence_required", "completion evidence is unavailable")
    return Outcome.success(True)
