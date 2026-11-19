"""Read-only gate projection around the explicit trusted kernel adapter."""
from dataclasses import replace
from datetime import timedelta

from ..contracts.models import StateQuality, StateValue
from ..kernel.codec import KernelError, freeze, require, wire
from ..kernel.repository import KernelRepository
from ..storage import StorageReader
from .service import timestamp

CLASS_WINDOW = "academics.class_window"


class AcademicsAccess:
    def __init__(self, host_access, academics, registry, campus=None, *, reserve_classes=False):
        from ..contracts.models import ReasonCode
        require(host_access is not None and all(callable(getattr(host_access, method, None))
                for method in ("scopes", "authorize", "snapshot", "audience")),
                "explicit host-verified kernel access adapter required", ReasonCode.MISSING_DEPENDENCY)
        self.host, self.academics, self.registry, self.campus = host_access, academics, registry, campus

        self.reserve_classes = reserve_classes
        self.routing_campus = campus

    def __getattr__(self, name):
        return getattr(self.host, name)

    def scopes(self):
        return self.host.scopes()

    def authorize(self, scope, action):
        return self.host.authorize(scope, action)

    def audience(self, scope):
        return self.host.audience(scope)

    def snapshot(self, scope, now):
        from ..activities.class_session import departure_matches, eligibility_key
        snapshot = self.host.snapshot(scope, now)
        require(snapshot.state.owner_key == scope.owner_key, "snapshot owner mismatch")
        values = dict(snapshot.state.values)
        with StorageReader(self.academics.storage).locked():
            db = self.academics.storage._connection
            owner = db.execute("SELECT context_key FROM life_academic_owners WHERE owner_key=?", (scope.owner_key,)).fetchone()
            sessions = self.academics._session_rows(db, scope.owner_key)
            if owner is not None and owner[0] != scope.context_key:
                # Preserve the reservation boundary without exposing the class identity.
                if self.reserve_classes:
                    snapshot, window = self._reserve(snapshot, scope, sessions.values(), now)
                    if window is not None:
                        values[CLASS_WINDOW] = replace(window, revision=1,
                            value=freeze({"free_until": window.value["free_until"], "session_id": None}))
                        snapshot = replace(snapshot, state=replace(snapshot.state, values=freeze(values)))
                return snapshot
            repo = KernelRepository(self.academics.storage)
            active = {item.instance_id: item for item in repo.active(scope.owner_key)
                      if item.context_key == scope.context_key}
            # Dynamic templates are pinned in SQLite, not reconstructed from the
            # new process's empty registry during recovery.
            keys = self.academics._template_refs.get((scope.owner_key, scope.context_key), ())
            templates = {key: self.registry.templates[key] for key in keys if key in self.registry.templates}
            unstarted_classes = {}
            for instance in active.values():
                pinned, _prepared, _audience = repo.pinned(scope.owner_key, instance.instance_id)
                templates[(pinned.id, pinned.version)] = pinned
                if pinned.executor == "class_session_local@1" and instance.started_at is None:
                    params = instance.checkpoint["params"]
                    unstarted_classes[eligibility_key(params["session"], params["decision"])] = params
            claims = {row[0]: row[1] for row in db.execute(
                "SELECT session_id,instance_id FROM life_academic_participation WHERE owner_key=?", (scope.owner_key,))}
            for template in templates.values():
                if template.executor == "study_local@1":
                    from ..activities.study import eligibility_key as study_key
                    target = template.params["target"]
                    chunk = target["chunk"]
                    assignments = self.academics._records(db, scope.owner_key, "assignment")
                    units = self.academics._records(db, scope.owner_key, "syllabus")
                    row = db.execute("SELECT record_json FROM life_study_chunks WHERE owner_key=? AND chunk_id=?", (scope.owner_key, chunk["chunk_id"])).fetchone()
                    import json
                    saved = json.loads(row[0]) if row else None
                    own_active = (saved and saved["instance_id"] in active
                        and active[saved["instance_id"]].template_id == template.id
                        and active[saved["instance_id"]].checkpoint.get("params", {}).get("target", {}).get("chunk", {}).get("chunk_id") == chunk["chunk_id"])
                    claimed_active = bool(saved and saved["active"])
                    eligible = (bool(own_active) or not claimed_active and assignments.get(chunk["assignment_id"]) == target["assignment"]
                        and units.get(target["unit"]["unit_id"]) == target["unit"] and (not saved or not saved["active"])
                        and (saved["revision"] if saved else 0) == chunk["revision"])
                    values[study_key(target, template.params)] = StateValue(int(eligible), now, max(1, saved["revision"] if saved else 1), (), StateQuality.VALID)
                    continue
                if template.executor != "class_session_local@1":
                    continue
                session = template.params["session"]
                current = sessions.get(session["session_id"])
                if current is None:
                    continue
                claim = claims.get(session["session_id"])
                decision = template.params["decision"]
                started = claim in active and active[claim].started_at is not None
                in_time = decision == "skip" or started or now < timestamp(session["end_at"])
                key = eligibility_key(session, decision)
                # All quotes for this decision share one resource. Consult the
                # pinned unstarted instance so another template cannot overwrite
                # its departure denial, including after a fresh-registry restart.
                pending_start = unstarted_classes.get(key)
                departure_valid = pending_start is None or departure_matches(pending_start, now)
                eligible = bool(current == dict(session) and not current["cancelled"] and (claim is None or claim in active) and in_time and departure_valid)
                values[key] = StateValue(int(eligible), now, session["revision"], (), StateQuality.VALID)
            capabilities = tuple(dict.fromkeys((*snapshot.capabilities, "academics")))
            if self.campus is not None:
                place, revision = self.campus.location(scope.owner_key)
                value = place
                if place and getattr(self.campus, "position", None) is not None:
                    # A v1 class may still be settling against the legacy table.
                    if self.campus.position.read(scope.owner_key).revision:
                        value = {"place_id": place, "revision": revision}
                if "geo.place_id" not in values or (place and not isinstance(value, dict)):
                    values["geo.place_id"] = StateValue(value, now, max(1, revision), (), StateQuality.VALID if place else StateQuality.UNKNOWN)
                if not getattr(self.campus, "settle_only", False) or (place and not getattr(self.campus, "position", None)):
                    capabilities = tuple(dict.fromkeys((*capabilities, "campus")))
            if self.reserve_classes:
                snapshot, window = self._reserve(snapshot, scope, sessions.values(), now)
                if window is not None:
                    values[CLASS_WINDOW] = window
        return replace(snapshot, state=replace(snapshot.state, values=freeze(values)), capabilities=capabilities)


    def _reserve(self, snapshot, scope, sessions, now):
        if snapshot.free_until is None:
            return snapshot, None
        session = min((item for item in sessions if not item["cancelled"] and timestamp(item["end_at"]) > now),
                      key=lambda item: item["start_at"], default=None)
        if session is None:
            return snapshot, None
        start = timestamp(session["start_at"])
        departure = start
        if self.routing_campus is not None and now < start <= now + timedelta(days=1):
            origin, _ = self.routing_campus.location(scope.owner_key)
            if origin:
                try:
                    departure, _ = self.routing_campus.class_departure(origin, session["place_id"], now, start,
                                                                      timedelta(minutes=5))
                except KernelError:
                    pass  # An unavailable route still leaves the classroom window reserved.
        boundary = min(snapshot.free_until, max(now, departure))
        if boundary == snapshot.free_until:
            return snapshot, None
        window = StateValue(freeze({"free_until": wire(snapshot.free_until), "session_id": session["session_id"]}),
                            now, session["revision"], (), StateQuality.VALID)
        return replace(snapshot, free_until=boundary), window

    def gate_snapshot(self, snapshot, template, *, instance=None):
        window = snapshot.state.values.get(CLASS_WINDOW)
        if window is None:
            return snapshot
        if (template.executor == "city_trip@1" and instance is not None and instance.started_at is not None
                and instance.checkpoint["cancel_requested"]):
            # Reaching the stop is required to give up the foreground.
            return replace(snapshot, free_until=timestamp(window.value["free_until"]))
        if template.executor == "class_session_local@1":
            session = template.params["session"]
            if (session["session_id"], session["revision"]) == (window.value["session_id"], window.revision):
                return replace(snapshot, free_until=timestamp(window.value["free_until"]))
        return snapshot
