"""Recheck the host identity for a prepared browsing or sleep activity."""

import json

from companion_v2.execution.models import TaskError, digest, encode, require
from companion_v2.foundation.outcome import Outcome
from companion_v2.foundation.refs import RoleOwnerRef, ScopeKind, normalize_utc
from companion_v2.role_work.codec import activity_from_json
from companion_v2.role_work.models import RoleActivityState

from . import life_commands


def authorize_identity(tasks, context, request, *, observed_at):
    """Check identity and life permission; callers must also check domain policy."""
    try:
        life_commands._request(context, request)
        payload = request["payload"]
        if (request["kind"] != "activity.create" or request["expected_revision"] != 0
                or type(payload) is not dict or set(payload) not in ({"owner", "activity"}, {"owner", "activity", "management"},
                                                                    {"owner", "activity", "schedule"})
                or context.scope.kind is not ScopeKind.PRIVATE or not context.scope.is_resolved):
            raise ValueError("activity creation request is invalid")
        owner = RoleOwnerRef(context.scope.installation_id, context.scope.bot_id, context.role_binding.role.subject_id)
        if payload["owner"] != json.loads(encode(owner)):
            raise ValueError("activity creation owner changed")
        activity = activity_from_json(encode(payload["activity"]))
        if (payload["activity"] != json.loads(encode(activity)) or activity.owner != owner
                or activity.scope != context.scope or activity.state is not RoleActivityState.ACTIVE
                or activity.revision != 1 or activity.started_at != activity.updated_at
                or activity.work is None or activity.work.kind not in ("social", "sleep")
                or activity.work.context_key != context.key or activity.reading is not None):
            raise ValueError("activity creation crosses its original authority")
        prefix = "sleep-" if activity.work.kind == "sleep" else "social-activity-"
        if activity.activity_id != prefix + digest([context.key, activity.work.request_id]):
            raise ValueError("activity creation identifier changed")
        now = normalize_utc(observed_at, field="activity observation")
        if activity.started_at > now:
            raise ValueError("activity creation cannot be in the future")
        if activity.work.kind == "sleep":
            if activity.sleep is None or activity.work.due_at <= now:
                raise ValueError("sleep needs a future wake time")
        elif activity.sleep is not None:
            raise ValueError("browsing cannot have a sleep receipt")
    except TaskError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise TaskError("life_activity_creation_invalid", "activity identity or original context is invalid") from error
    require(tasks.identity.authorize(context.actor, scope=context.scope, capability="role.life.run", observed_at=now))
    require(tasks.authorize_context(context, action="role.life.run", observed_at=now))
    return Outcome.success(True)
