"""Narrow, authenticated management calls for the paired life plugin."""

import json

from astrbot.api.web import request
from companion_v2.configuration.life_provider import LIFE_FIELDS
from companion_v2.execution.models import TaskError, digest
from companion_v2.foundation.outcome import Outcome

from .academics_routes import ARTIFACT_READS, AcademicsRoutes
from .life_api import LifeWebAPI
from .life_page_reads import LifePageReads, life_page_data
from .multi_api import TargetControllerView
from .page_api import PageWebAPI
from .state_api import StateWebAPI
from .web_api import outcome_response


LIFE_COMMANDS = frozenset({"configure", "add_text", "add_epub", "sleep", "read", "wake", "cancel", "withdraw", "remove"})
STATE_COMMANDS = frozenset({"configure", "configure_role", "configure_window", "state.write", "cycle.align", "maintain",
    "calendar.create", "calendar.revise", "calendar.presence", "calendar.accept", "calendar.cancel", "calendar.exception", "calendar.remind"})


async def dispatch(controller, registry, *, plugin, registration_id, operation, target_id=None, role_token=None, artifact_id=None):
    try:
        username = request.username
        if (request.plugin_name != "astrbot_plugin_good_life" or type(username) is not str
                or not username.strip() or username.startswith("api_key:")):
            raise TaskError("dashboard_identity_required", "sign in to the AstrBot Dashboard")
        if not registry.owns_registration(registration_id, plugin):
            raise TaskError("life_provider_unavailable", "life provider is not active")
        if operation == "targets":
            result = await PageWebAPI(controller).targets()
            if result.get("status") != "ok":
                return result
            data = result["data"]
            targets = []
            for target in data["targets"]:
                if target.get("scope_kind") != "private":
                    continue
                roles = [{"token": digest(role["role_id"]), "name": role.get("role_name") or "当前角色"}
                         for role in target.get("roles", ()) if role.get("life_api") and role.get("state_api")]
                if roles:
                    targets.append({"id": target["target_id"], "name": target.get("display_name") or "私聊窗口",
                                    "roles": roles})
            return outcome_response(Outcome.success({"installation_id": data["installation_id"],
                "operator_id": data["operator_id"], "targets": targets}))
        if (type(target_id) is not str or not target_id or type(role_token) is not str or len(role_token) != 64
                or operation not in ("life.read", "life.command", "state.read", "state.command",
                                     "calendar.occurrences", "life.activities", "life.wallet", "life.management", "academics.term",
                                     "academics.timetable", "academics.occurrences", "academics.syllabus",
                                     "academics.assignments", "academics.grades", "academics.study_progress", "academics.study_artifacts",
                                     "academics.study_artifact", "academics.command", "academics.places")):
            raise TaskError("life_dashboard_invalid", "invalid life management route")
        inventory = controller.web_api.target_inventory(refresh_routes=False)
        target = next((item for item in inventory if item.get("target_id") == target_id and item.get("scope_kind") == "private"), None)
        role = next((item for item in target.get("roles", ()) if digest(item["role_id"]) == role_token
                     and item.get("life_api") and item.get("state_api")), None) if target else None
        if role is None:
            raise TaskError("life_dashboard_invalid", "the selected role or window is unavailable")
        source = getattr(controller, "role_source_target_id", lambda _role, fallback: fallback)(role["role_id"], target_id)
        view = TargetControllerView(controller, source, role["role_id"])
        if operation.startswith("academics."):
            runtime = plugin._runtime
            kind = operation.split(".")[1]
            if kind in ARTIFACT_READS:
                api = AcademicsRoutes(view, getattr(runtime, "academics", None),
                                      artifacts=getattr(runtime, "study_artifact_reader", None))
                return await api.read(kind, artifact_id=artifact_id, ports=getattr(runtime, "study_ports", None))
            api = AcademicsRoutes(view, getattr(runtime, "academics", None))
            if kind == "places":
                return await api.places(getattr(runtime, "read_academic_places", None))
            return await (api.command() if kind == "command" else api.read(kind))
        if operation.endswith(".command"):
            raw = await request.body()
            if len(raw) > 28 * 1024 * 1024:
                raise ValueError("command too large")
            body = json.loads(raw.decode("utf-8"))
            action = body.get("action") if type(body) is dict else None
            allowed = LIFE_COMMANDS if operation.startswith("life.") else STATE_COMMANDS
            if action not in allowed:
                raise ValueError("command is outside the life page")
            if operation == "life.command" and action == "configure":
                changes = body.get("changes")
                if type(changes) is not dict or not set(changes) <= LIFE_FIELDS:
                    raise ValueError("configuration is outside the life page")
        if operation == "calendar.occurrences":
            return await LifePageReads(view).calendar_occurrences()
        if operation == "life.activities":
            return await LifePageReads(view).activity_history()
        if operation == "life.management":
            return await LifePageReads(view).management(plugin._runtime.read_management_page)
        if operation == "life.wallet":
            return await LifePageReads(view).wallet(getattr(plugin._runtime, "read_wallet_page", None))
        api = LifeWebAPI(view) if operation.startswith("life.") else StateWebAPI(view)
        if operation == "life.read":
            result = await api.status(notes_only=True,
                overview_reader=getattr(plugin._runtime, "read_overview_activities", None))
        else:
            result = await (api.command() if operation.endswith(".command") else api.status())
        if operation == "life.read" and result.get("status") == "ok":
            result = {**result, "data": life_page_data(result.get("data", {}))}
        return result
    except TaskError as error:
        return outcome_response(Outcome.failed(error.code, str(error)))
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError):
        return outcome_response(Outcome.failed("life_dashboard_invalid", "invalid life management request"))
