"""Authenticated AstrBot page endpoints for the life plugin."""

from astrbot.api.web import request


PREFIX = "/astrbot_plugin_good_life"


def _failure(code, reason):
    return {"status": "error", "message": code, "reason": reason, "data": {"code": code}}


class LifePageAPI:
    def __init__(self, plugin):
        self.plugin = plugin
        self.routes = []

    def register(self):
        register = getattr(self.plugin.context, "register_web_api", None)
        if not callable(register):
            raise ValueError("AstrBot plugin Web API is unavailable")
        entries = [(PREFIX + "/status", self.status, ["GET"], "Read life plugin connection status"),
                   (PREFIX + "/targets", self.targets, ["GET"], "Read authorized life targets"),
                   (PREFIX + "/targets/<target>/<role>/life", self.life, ["GET"], "Read life records"),
                   (PREFIX + "/targets/<target>/<role>/life/command", self.life_command, ["POST"], "Manage life records"),
                   (PREFIX + "/targets/<target>/<role>/calendar/occurrences", self.calendar_occurrences, ["GET"], "Read calendar instances by date range"),
                   (PREFIX + "/targets/<target>/<role>/life/activities", self.activity_history, ["GET"], "Read life activity revisions by date"),
                   (PREFIX + "/targets/<target>/<role>/academics/term", self.academics_term, ["GET"], "Read manual academic terms"),
                   (PREFIX + "/targets/<target>/<role>/academics/timetable", self.academics_timetable, ["GET"], "Read manual timetable entries"),
                   (PREFIX + "/targets/<target>/<role>/academics/occurrences", self.academics_occurrences, ["GET"], "Read academic class instances"),
                   (PREFIX + "/targets/<target>/<role>/academics/syllabus", self.academics_syllabus, ["GET"], "Read manual syllabus units"),
                   (PREFIX + "/targets/<target>/<role>/academics/assignments", self.academics_assignments, ["GET"], "Read manual assignments"),
                   (PREFIX + "/targets/<target>/<role>/academics/grades", self.academics_grades, ["GET"], "Read released term grades"),
                   (PREFIX + "/targets/<target>/<role>/academics/study_progress", self.academics_study_progress, ["GET"], "Read study chunk progress"),
                   (PREFIX + "/targets/<target>/<role>/academics/command", self.academics_command, ["POST"], "Manage manual terms and timetables"),
                   (PREFIX + "/targets/<target>/<role>/academics/places", self.academics_places, ["GET"], "Read class place options"),
                   (PREFIX + "/targets/<target>/<role>/life/management", self.management, ["GET"], "Read class, trip and wallet status"),
                   (PREFIX + "/targets/<target>/<role>/life/wallet", self.wallet, ["GET"], "Read wallet balance and transactions"),
                   (PREFIX + "/targets/<target>/<role>/state", self.state, ["GET"], "Read state and calendar"),
                   (PREFIX + "/targets/<target>/<role>/state/command", self.state_command, ["POST"], "Manage state and calendar")]
        runtime = getattr(self.plugin, "_runtime", None)
        if getattr(self.plugin, "_study_enabled", False) or getattr(runtime, "has_study_history", False):
            base = PREFIX + "/targets/<target>/<role>/academics/study_artifacts"
            entries.extend((
                (base, self.academics_study_artifacts, ["GET"], "List study artifacts"),
                (base + "/<artifact_id>", self.academics_study_artifact, ["GET"], "Read a study artifact"),
            ))
        for route, handler, methods, description in entries:
            register(route, handler, methods, description)
            self.routes.append((route, handler))

    def unregister(self):
        registered = getattr(self.plugin.context, "registered_web_apis", None)
        if isinstance(registered, list):
            owned = {(route, id(handler)) for route, handler in self.routes}
            registered[:] = [item for item in registered if not (isinstance(item, tuple) and len(item) >= 2
                            and (item[0], id(item[1])) in owned)]
        self.routes.clear()

    @staticmethod
    def _authenticated():
        username = request.username
        return (request.plugin_name == "astrbot_plugin_good_life" and type(username) is str
                and bool(username.strip()) and not username.startswith("api_key:"))

    async def status(self):
        if not self._authenticated():
            return _failure("dashboard_identity_required", "sign in to the AstrBot Dashboard")
        return {"status": "ok", "message": "", "data": self.plugin.companion_v2_status()}

    async def _dispatch(self, operation, target=None, role=None, **route_params):
        if not self._authenticated():
            return _failure("dashboard_identity_required", "sign in to the AstrBot Dashboard")
        runtime = self.plugin._runtime
        api = getattr(runtime, "api", None)
        handler = getattr(api, "life_dashboard", None)
        if not runtime or not runtime.ready or not callable(handler):
            return _failure("life_provider_unavailable", "the companion connection is unavailable")
        return await handler(plugin=self.plugin, registration_id=runtime.token, operation=operation,
                             target_id=target, role_token=role, **route_params)

    async def targets(self):
        return await self._dispatch("targets")

    async def life(self, target, role):
        return await self._dispatch("life.read", target, role)

    async def life_command(self, target, role):
        return await self._dispatch("life.command", target, role)

    async def calendar_occurrences(self, target, role):
        return await self._dispatch("calendar.occurrences", target, role)

    async def activity_history(self, target, role):
        return await self._dispatch("life.activities", target, role)

    async def academics_term(self, target, role):
        return await self._dispatch("academics.term", target, role)

    async def academics_timetable(self, target, role):
        return await self._dispatch("academics.timetable", target, role)

    async def academics_occurrences(self, target, role):
        return await self._dispatch("academics.occurrences", target, role)

    async def academics_syllabus(self, target, role):
        return await self._dispatch("academics.syllabus", target, role)

    async def academics_assignments(self, target, role):
        return await self._dispatch("academics.assignments", target, role)

    async def academics_grades(self, target, role):
        return await self._dispatch("academics.grades", target, role)

    async def academics_study_progress(self, target, role):
        return await self._dispatch("academics.study_progress", target, role)

    async def academics_study_artifacts(self, target, role):
        return await self._dispatch("academics.study_artifacts", target, role)

    async def academics_study_artifact(self, target, role, artifact_id):
        return await self._dispatch("academics.study_artifact", target, role, artifact_id=artifact_id)

    async def academics_command(self, target, role):
        return await self._dispatch("academics.command", target, role)

    async def academics_places(self, target, role):
        return await self._dispatch("academics.places", target, role)

    async def management(self, target, role):
        return await self._dispatch("life.management", target, role)

    async def wallet(self, target, role):
        return await self._dispatch("life.wallet", target, role)

    async def state(self, target, role):
        return await self._dispatch("state.read", target, role)

    async def state_command(self, target, role):
        return await self._dispatch("state.command", target, role)
