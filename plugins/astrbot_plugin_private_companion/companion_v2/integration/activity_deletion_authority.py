"""Recheck a proposed reading deletion against the host management policy."""

import hashlib
from datetime import datetime

from companion_v2.execution.models import TaskError, digest, encode
from companion_v2.foundation.outcome import Outcome
from companion_v2.foundation.refs import RoleOwnerRef
from companion_v2.role_work.codec import activity_from_json
from companion_v2.role_work.models import RoleActivityState, advance_activity

from . import life_commands


def authorize(manager, operator, data, request, proposal, *, observed_at=None):
    """Check the original operator, token, activity and management operation.

    Source revocation and any worker cancellation are separate required
    effects; this check alone must not be used to start a production deletion.
    """
    try:
        context = manager.context
        life_commands._request(context, request)
        if type(data) is not dict or data.get("action") not in ("remove", "withdraw"):
            raise ValueError("not a reading removal")
        withdraw = data["action"] == "withdraw"
        key, identifier, revision = data["operation_key"], data["activity_id"], data["expected_revision"]
        if (type(key) is not str or not key.strip() or len(key) > 128
                or type(identifier) is not str or not identifier or type(revision) is not int or revision < 1):
            raise ValueError("invalid management identity")
        access = manager.authorize(operator)
        if data.get("view_token") != manager.token(operator, access, identifier, revision):
            raise TaskError("life_view_stale", "refresh before applying this change")
        payload = request["payload"]
        if (request["kind"] != "activity.delete" or request["expected_revision"] != revision
                or type(proposal) is not dict or set(proposal) != {"previous_json", "rows_hash", "related_json"}
                or type(proposal["previous_json"]) is not str
                or payload["activity_id"] != identifier or payload["rows_hash"] != proposal["rows_hash"]
                or payload["previous_hash"] != hashlib.sha256(proposal["previous_json"].encode("utf-8")).hexdigest()
                or payload["management_operation_id"] != manager.operation_id("life-command-", operator, key)
                or payload["management_parameter_hash"] != digest(data)
                or (payload.get("mode") == "withdraw") != withdraw):
            raise ValueError("deletion does not match the management request")
        activity = activity_from_json(proposal["previous_json"])
        owner = RoleOwnerRef(context.scope.installation_id, context.scope.bot_id, context.role_binding.role.subject_id)
        if (activity.owner != owner or activity.scope != context.scope or activity.activity_id != identifier
                or activity.revision != revision or activity.work is None or activity.work.kind != "reading"
                or digest(activity.work.source) != payload["source_hash"]
                or (not withdraw and activity.state not in (RoleActivityState.COMPLETED, RoleActivityState.CANCELLED))
                or (withdraw and activity.work.source is None)):
            raise ValueError("activity is outside the removable reading scope")
        now = observed_at or manager.app.now()
        at = datetime.fromisoformat(payload["observed_at"]) if "observed_at" in payload else now
        if at.utcoffset() is None or now.utcoffset() is None or at > now:
            raise ValueError("reading cancellation observation is invalid")
        if type(proposal["related_json"]) is not list or type(payload.get("related", [])) is not list:
            raise ValueError("related reading list is invalid")
        related = []
        for raw in proposal["related_json"]:
            item = activity_from_json(raw)
            if (item.owner != owner or item.scope != context.scope or (not withdraw and item.activity_id == identifier)
                    or item.work is None or item.work.kind != "reading" or item.work.source != activity.work.source
                    or item.state not in (RoleActivityState.PLANNED, RoleActivityState.ACTIVE, RoleActivityState.PAUSED)):
                raise ValueError("related reading is not safely cancellable")
            cancelled = advance_activity(item, expected_revision=item.revision,
                state=RoleActivityState.CANCELLED, observed_at=at)
            related.append({"activity_id": item.activity_id, "revision": item.revision,
                "previous_hash": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                "operation_id": payload["management_operation_id"] + (
                    ":cancel" if withdraw and item.activity_id == identifier else ":" + digest(item.activity_id)),
                "result_hash": hashlib.sha256(encode(cancelled).encode("utf-8")).hexdigest()})
        if (related != payload.get("related", [])
                or [item["activity_id"] for item in related] != sorted({item["activity_id"] for item in related})):
            raise ValueError("related reading cancellation evidence changed")
    except TaskError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise TaskError("life_activity_deletion_invalid", "reading deletion identity or evidence is invalid") from error
    return Outcome.success(True)
