"""Inspect historical calendar ownership without changing either database.

The output is an inventory, not authorization to switch execution. The host
must repeat it against a quiesced consistent backup before any adoption.
"""

from datetime import datetime
import hashlib
import json

from companion_v2.execution.models import digest
from companion_v2.foundation.refs import RoleOwnerRef
from companion_v2.identity_policy.personas import PersonaDirectory
from companion_v2.storage.codec import scope_from_row


def audit_calendar_ownership(runtime, identity):
    """Identify role plans that can be adopted, preserving ambiguous cases.

    ``moves`` contains private identifiers and must not be logged or committed.
    No rows or ownership markers are changed by this inspection.
    """
    with runtime.read_snapshot():
        rows = runtime.read_all("""SELECT r.owner_key, r.record_id, r.revision, r.updated_at, r.record_json,
                o.installation_id, o.platform, o.scope_kind, o.native_scope_id, o.bot_id,
                o.account_id, o.conversation_id, o.session_id, o.owner AS operation_owner,
                o.state AS operation_state, o.commit_state AS operation_commit_state
            FROM calendar_records r
            LEFT JOIN calendar_history h ON h.owner_key=r.owner_key
                AND h.record_id=r.record_id AND h.revision=r.revision
            LEFT JOIN operations o ON o.operation_id=h.operation_id
            ORDER BY r.owner_key, r.record_id""", ())
        keys = {(row["owner_key"], row["record_id"]) for row in rows}
        if len(keys) != len(rows):
            raise ValueError("calendar heads are not unique")
        personas = PersonaDirectory(runtime)
        summary = {"records": len(rows), "other": 0, "shared": 0, "adoptable": 0, "pinned": 0}
        moves, targets = [], set()
        for row in rows:
            try:
                record = json.loads(row["record_json"])
                if (type(record) is not dict or record.get("id") != row["record_id"]
                        or record.get("revision") != row["revision"]
                        or record.get("updated_at") != row["updated_at"]):
                    raise ValueError("calendar head is inconsistent")
                if record.get("kind") not in ("role_plan", "user_report", "commitment"):
                    raise ValueError("calendar kind is unrecognized")
                if record["kind"] != "role_plan":
                    summary["other"] += 1
                    continue
                if (row["operation_owner"] != "calendar" or row["operation_state"] != "completed"
                        or row["operation_commit_state"] != "committed"):
                    raise ValueError("calendar operation is not confirmed")
                at = datetime.fromisoformat(record["updated_at"])
                if at.utcoffset() is None:
                    raise ValueError("calendar time is not timezone-aware")
                scope = scope_from_row(row)
                resolved = identity.resolve_role(scope, observed_at=at)
                if not resolved.is_success:
                    raise ValueError("calendar role cannot be resolved")
                role = resolved.unwrap().role
                owner = RoleOwnerRef(scope.installation_id, scope.bot_id, role.subject_id)
                role_key = digest(["calendar-role.v1", owner])
                if row["owner_key"] == role_key:
                    summary["shared"] += 1
                    continue
                route = personas.route(scope)
                if (route is None or route.user is None
                        or row["owner_key"] != digest([scope, role, route.user])):
                    raise ValueError("legacy calendar owner cannot be verified")
                if record.get("reminder_id") and not record.get("reminder_context_key"):
                    summary["pinned"] += 1
                    continue
                target = (role_key, row["record_id"])
                if target in keys or target in targets:
                    raise ValueError("legacy calendar adoption has an ownership collision")
                targets.add(target)
                moves.append({"from_owner": row["owner_key"], "to_owner": role_key,
                              "record_id": row["record_id"], "revision": row["revision"],
                              "head_sha256": hashlib.sha256(row["record_json"].encode("utf-8")).hexdigest()})
                summary["adoptable"] += 1
            except (AttributeError, KeyError, TypeError, ValueError) as error:
                raise ValueError("calendar ownership inspection failed: " + str(error)) from error
        return {"summary": summary, "moves": tuple(moves)}
