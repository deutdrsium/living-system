"""Read-only kernel access for one registered life provider and host session."""

from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta
from itertools import count
import json
from sqlite3 import Error as SQLiteError
from types import MappingProxyType

from companion_v2.agenda.calendar import CalendarService
from companion_v2.configuration.life_provider import transferred
from companion_v2.conversation.native_turns import context_rows
from companion_v2.execution.models import TaskContext, TaskError, digest, require
from companion_v2.foundation.refs import ScopeKind, SubjectKind, normalize_utc
from companion_v2.integration.calendar_adoption_guard import CERTIFICATE_KEY, HASHES, SCHEMA
from companion_v2.role_work.state.ownership import role_owner
from companion_v2.storage.runtime import StorageOpenError


_TYPES = ("ExecutionScope", "AudienceBound", "GateSnapshot", "StateSnapshot")
_ACTIONS = frozenset({
    "kernel.propose", "kernel.prepare", "kernel.start", "kernel.advance", "kernel.cancel",
    "kernel.pause", "kernel.mark_unknown", "kernel.recover", "kernel.recover_pending",
    "kernel.commit_result", "kernel.abandon_result", "kernel.snapshot",
})
_RUN = "role.life.run"
_LIFE_CALENDAR = "host.calendar.life_owned"
_AGENDA_HORIZON = timedelta(days=1)
_GENERATIONS = count(1)
_READ_ERRORS = (StorageOpenError, SQLiteError, KeyError, TypeError, ValueError, OverflowError)
_EVIDENCE_ERRORS = (*_READ_ERRORS, AttributeError)


def access_for(controller, registry, *, plugin, registration_id, version):
    if type(version) is not int or version != 1:
        raise TaskError("life_kernel_version_unsupported", "unsupported life kernel access version")
    if not registry.owns_registration(registration_id, plugin):
        raise TaskError("life_provider_unavailable", "the registered life provider is unavailable")
    access = _KernelAccess(controller, registry, plugin, registration_id)
    with access._host_read():
        pass  # Check the pinned running application before consulting the plugin.
    factory = getattr(getattr(plugin, "_runtime", None), "kernel_access_types", None)
    types = factory() if callable(factory) else None
    if not isinstance(types, Mapping) or any(not isinstance(types.get(name), type) for name in _TYPES):
        raise TaskError("life_kernel_types_invalid", "life kernel access constructors are unavailable")
    access._types = {name: types[name] for name in _TYPES}
    access._check_lease()
    return access


class _KernelAccess:
    def __init__(self, controller, registry, plugin, registration_id):
        self._controller, self._registry = controller, registry
        self._plugin, self._registration_id = plugin, registration_id
        self._session = getattr(controller, "session", None)
        self._app = getattr(self._session, "application", None)
        self._task_generation = getattr(getattr(self._app, "tasks", None), "generation", None)
        self._generation = next(_GENERATIONS)

    def _check_lease(self):
        if (self._app is None or getattr(self._controller, "_terminated", False)
                or getattr(self._controller, "session", None) is not self._session
                or self._session.application is not self._app
                or self._app.tasks.generation != self._task_generation
                or not self._registry.owns_registration(self._registration_id, self._plugin)):
            raise TaskError("life_provider_unavailable", "the original life kernel lease is unavailable")

    @contextmanager
    def _host_read(self):
        self._check_lease()
        app = self._app
        # Life may already hold its writer. Suppress ambient coordinated reads,
        # which can otherwise dispatch back to the provider from host services.
        with app.runtime.without_coordinated_snapshot():
            with app.runtime.read_only_snapshot():
                app.require_running()
                if app.role_life.activity_owner() != "good_life":
                    raise TaskError("life_activity_owner_invalid", "life execution has not transferred")
                yield app
            app.require_running()
        self._check_lease()

    def _scope_for(self, app, context_key, at):
        routine = app.life_runtime.routines.get(context_key)
        context = routine.context if routine is not None else None
        if (context is None or context.key != context_key or not routine.instruction
                or routine._closed or routine._retired or context.scope.kind is not ScopeKind.PRIVATE
                or context.actor.subject.kind is not SubjectKind.HUMAN
                or context.audience.recipients != (context.actor.subject,)):
            raise TaskError("life_context_changed", "the original private life context is unavailable")
        # Match the role-life routine's explicit grant and exact current binding
        # checks. Refreshing the routine can dispatch life configuration reads.
        require(app.tasks.identity.authorize(context.actor, scope=context.scope, capability=_RUN, observed_at=at))
        require(app.tasks.authorize_context(context, action=_RUN, observed_at=at))
        # The role envelope is the same one AcademicsRoutes gets from management.
        scope = self._types["ExecutionScope"](owner_key=digest(role_owner(context)), context_key=context_key,
            authority_ref=digest(["life-kernel.v1", self._registration_id, self._generation, context_key]),
            registration_generation=self._generation)
        return scope, context

    def _checked_scope(self, app, scope):
        if (type(scope) is not self._types["ExecutionScope"]
                or type(scope.registration_generation) is not int
                or scope.registration_generation != self._generation
                or type(scope.context_key) is not str or len(scope.context_key) != 64):
            raise TaskError("life_context_changed", "scope is outside the original life kernel lease")
        current, context = self._scope_for(app, scope.context_key, app.now())
        if scope != current:
            raise TaskError("life_context_changed", "scope is outside the original life kernel lease")
        return context

    def is_current(self):
        """Return False for revocation and None if the read cannot verify it."""
        try:
            with self._host_read():
                pass
        except TaskError as error:
            if error.code in {"life_provider_unavailable", "life_activity_owner_invalid",
                              "host_not_running", "supervisor_fenced", "supervisor_limits_changed"}:
                return False
            return None
        except _READ_ERRORS:
            return None
        return True

    def scopes(self):
        try:
            with self._host_read() as app:
                scopes = []
                at = app.now()
                for key in sorted(tuple(app.life_runtime.routines)):
                    try:
                        scope, _ = self._scope_for(app, key, at)
                    except _READ_ERRORS:
                        continue
                    scopes.append(scope)
                return tuple(scopes)
        except _READ_ERRORS:
            return ()

    def authorize(self, scope, action):
        if type(action) is not str or action not in _ACTIONS:
            return False
        try:
            with self._host_read() as app:
                self._checked_scope(app, scope)
        except _READ_ERRORS:
            return False
        return True

    def audience(self, scope):
        with self._host_read() as app:
            self._checked_scope(app, scope)
            return self._types["AudienceBound"](
                permission_ref="context:" + digest([scope.owner_key, scope.context_key]),
                context_keys=(scope.context_key,),
            )

    @contextmanager
    def _adopted_calendar(self, scope):
        with self._host_read() as app:
            context = self._checked_scope(app, scope)
            if app.calendar._life_calendar_owner() != "good_life":
                raise TaskError("life_calendar_owner_invalid", "calendar execution has not transferred")
            require(app.tasks.check_context_access(context, action="conversation.reply", observed_at=app.now()))
            row = app.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?", (CERTIFICATE_KEY,))
            certificate = json.loads(row["value"]) if row is not None else None
            if (type(certificate) is not dict or set(certificate) != {"schema", "source_version", *HASHES}
                    or certificate["schema"] != SCHEMA or type(certificate["source_version"]) is not str
                    or not 0 < len(certificate["source_version"]) <= 256
                    or any(type(certificate[name]) is not str or len(certificate[name]) != 64
                           or any(c not in "0123456789abcdef" for c in certificate[name]) for name in HASHES)):
                raise TaskError("life_calendar_adoption_unverified", "host calendar adoption receipt is unavailable")
            yield app.calendar._legacy_records, context, certificate

    def calendar_keys(self, scope):
        """Resolve the adopted role and conversation storage keys for this scope."""
        try:
            with self._adopted_calendar(scope) as (records, context, _certificate):
                return records.role_key(context), records.conversation_key(context)
        except _READ_ERRORS:
            return None

    def calendar_adoption_certificate(self, scope):
        """Return the host receipt for comparison with Life's locally stored receipt."""
        try:
            with self._adopted_calendar(scope) as (_records, _context, certificate):
                return certificate
        except _READ_ERRORS:
            return None

    @staticmethod
    def _foreground(app, context):
        if app.state_maintenance.foreground_busy(context):
            return True
        owner = role_owner(context)
        # The existing foreground check covers the current native route and
        # role-wide V2 flows. Also include native turns in other role windows.
        for row in context_rows(app.runtime, open_only=True):
            if role_owner(TaskContext.restore(json.loads(row["context_json"]))) == owner:
                return True
        return any(
            role_owner(routine.context) == owner and any(not worker.done() for worker in tuple(routine._workers.values()))
            for routine in tuple(app.life_runtime.routines.values()) if routine.context is not None
        )

    @staticmethod
    def _agenda(app, context, now):
        if app.calendar._life_calendar_owner() == "good_life":
            # The local rows are cutover copies. Life must prove its calendar
            # coverage and check its confirmation barriers itself.
            return None, (), (_LIFE_CALENDAR,)
        store = app.configuration.store
        with store.without_coordinated_snapshot(), store.read_only_snapshot():
            if transferred(store):
                # Calendar access may read state settings. Those settings no
                # longer have an authoritative host-only path after transfer.
                return None, (), ()
            # A fresh legacy read avoids the shared legacy role-plan cache. The
            # host runtime snapshot above pins the calendar's legacy owner.
            calendar = CalendarService(app.tasks, app.calendar.agenda, app.role_state)
            end = now + _AGENDA_HORIZON
            projection = calendar.occurrences(context, start=now, end=end, kinds=("role_plan", "commitment"))
        slots = tuple(sorted(
            (item["id"], item["revision"], item["sequence"], datetime.fromisoformat(item["start"]),
             datetime.fromisoformat(item["end"]))
            for item in projection["items"] if item["state"] == "accepted"
        ))
        if projection["partial"]:
            return None, slots, ()
        return min((max(now, slot[3]) for slot in slots), default=end), slots, ()

    def snapshot(self, scope, now):
        now = normalize_utc(now, field="life kernel observation")
        with self._host_read() as app:
            context = self._checked_scope(app, scope)
            try:
                busy = self._foreground(app, context)
            except _EVIDENCE_ERRORS:
                busy = None
            try:
                free_until, slots, capabilities = self._agenda(app, context, now)
            except _EVIDENCE_ERRORS:
                free_until, slots, capabilities = None, (), ()
            if any(start <= now < end for _, _, _, start, end in slots):
                busy = True
            # The life kernel coordinates the composed revision. Supply a
            # deterministic host fact version rather than incrementing on reads.
            facts = (scope.context_key, busy, slots, free_until is not None, capabilities)
            revision = int(digest(facts)[:12], 16) + 1
            state = self._types["StateSnapshot"](owner_key=scope.owner_key, observed_at=now,
                revision=revision, values=MappingProxyType({}))
            return self._types["GateSnapshot"](state=state, free_until=free_until, busy=busy,
                capabilities=capabilities)
