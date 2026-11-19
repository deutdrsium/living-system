"""Authenticated Dashboard routes for private writing and role routines."""

import asyncio
import json
import sqlite3

from astrbot.api.web import request

from companion_v2.execution.models import TaskError, digest
from companion_v2.foundation.outcome import Outcome
from companion_v2.role_work.life.management import RoleLifeManagement
from companion_v2.role_work.life.epub_import import MAX_EPUB_BASE64

from .management_identity import dashboard_operator

PREFIX = "/astrbot_plugin_private_companion/life"


class LifeWebAPI:
    def __init__(self, controller):
        self.controller = controller

    def routes(self):
        return [(PREFIX, self.status, ["GET"], "Read private journals and role routines"),
                (PREFIX + "/command", self.command, ["POST"], "Manage private journals and role routines")]

    def manager(self):
        controller = self.controller
        if controller._terminated:
            raise TaskError("staging_host_terminated", "the plugin has stopped")
        username = request.username
        if type(username) is not str or not username.strip():
            raise TaskError("dashboard_identity_required", "sign in to the AstrBot Dashboard")
        session = controller.session or controller.last_session
        if session is None or controller.runtime is None:
            raise TaskError("staging_not_initialized", "load an initialized V2 profile first")
        operator = dashboard_operator(controller.runtime, username, at=controller.now())
        return RoleLifeManagement(session.application, session.profile.target), operator

    async def status(self, *, notes_only=False, overview_reader=None):
        from .web_api import outcome_response
        try:
            query = request.query
            allowed = {"before", "after_activity", "archive_kind"}
            if notes_only:
                allowed.add("overview")
            if set(query.keys()) - allowed or any(len(query.getlist(k)) != 1 for k in query.keys()):
                raise ValueError("invalid query")
            overview = query.get("overview")
            if overview is not None and (overview != "1" or query.get("before") or query.get("after_activity")):
                raise ValueError("invalid overview query")
            async with self.controller._control_lock:
                manager, operator = self.manager()
                archive_kind = query.get("archive_kind") or None
                # The life page owns notes. Choose their pagination source before
                # the host manager applies its 50-date archive limit.
                if notes_only and archive_kind in (None, "diary"):
                    archive_kind = "reading_note"
                result = manager.status(operator, before=query.get("before") or None,
                                        after_activity=query.get("after_activity") or None,
                                        archive_kind=archive_kind)
                result["profile_id"] = getattr(self.controller.settings, "profile_id", None)
                if getattr(self.controller, "target_id", None) is not None:
                    result["target_id"] = self.controller.target_id
                if overview:
                    scope = {"owner_key": digest(manager.owner), "context_key": manager.context.key,
                             "now": manager.app.now()}
            if overview:
                from .life_overview import overview_activities
                snapshot = {"items": [], "partial": False}
                if callable(overview_reader):
                    try:
                        snapshot = await asyncio.to_thread(overview_reader, **scope)
                    except sqlite3.Error:
                        snapshot = {"items": [], "partial": True}
                result["overview_activities"] = overview_activities(snapshot)
            return outcome_response(Outcome.success(result))
        except TaskError as error:
            return outcome_response(Outcome.failed(error.code, str(error)))
        except (ValueError, TypeError):
            return outcome_response(Outcome.failed("life_query_invalid", "invalid archive query"))

    async def command(self):
        from .web_api import outcome_response
        try:
            if request.query:
                raise ValueError("commands do not accept query fields")
            raw = await request.body()
            if len(raw) > MAX_EPUB_BASE64 + 16384:
                raise ValueError("body too large")
            def unique(pairs):
                data = {}
                for key, value in pairs:
                    if key in data:
                        raise ValueError("duplicate field")
                    data[key] = value
                return data
            data = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
            common = {"action", "operation_key", "view_token", "expected_revision"}
            fields = {
                "configure": {"changes", "edit_token"}, "add_text": {"title", "text"},
                "add_epub": {"filename", "epub_base64"}, "sleep": {"minutes"},
                "read": {"activity_id"}, "wake": {"activity_id"}, "cancel": {"activity_id"}, "withdraw": {"activity_id"},
                "remove": {"activity_id"},
                "interest_pause": {"interest_id"}, "interest_resume": {"interest_id"},
                "interest_edit": {"interest_id", "name"},
                "interest_merge": {"interest_id", "target_id", "target_revision"},
                "diary": {"local_date", "retry_of"}, "dream": {"activity_id", "retry_of"},
                "edit": {"kind", "local_date", "body", "afterglow", "timezone"},
                "delete": {"kind", "local_date", "timezone"},
                "restore": {"kind", "local_date", "body", "afterglow", "recall", "timezone"},
                "derive": {"kind", "local_date", "timezone"},
            }
            if type(data) is not dict or data.get("action") not in fields or set(data) - common - fields[data["action"]]:
                raise ValueError("unknown command fields")
            if data["action"] != "add_epub" and len(raw) > 1200000:
                raise ValueError("body too large")
            async with self.controller._control_lock:
                manager, operator = self.manager()
                manager.authorize(operator)
            # Model work uses its own task lifecycle; leave stop/withdraw routes responsive.
            return outcome_response(await manager.command(operator, data))
        except TaskError as error:
            return outcome_response(Outcome.failed(error.code, str(error)))
        except (ValueError, TypeError, RecursionError):
            return outcome_response(Outcome.failed("life_command_invalid", "invalid UTF-8 life command"))
