"""Bind life activity and calendar providers to the current host session."""

from threading import RLock

from companion_v2.integration.life_ledger_proxy import LifeLedgerReadProxy
from companion_v2.integration.life_activity_control_proxy import LifeActivityCreationProxy, LifeActivityDeletionProxy, LifeActivityTransitionProxy
from companion_v2.integration.life_event_provider_proxy import LifeEventProviderProxy
from companion_v2.integration.life_state_records import LifeStateRecords
from companion_v2.integration.exposure_session_proxy import ExposureSessionReadProxy
from companion_v2.integration.state_write_handoff import LifeStateCommandsProxy
from companion_v2.execution.models import TaskError
from companion_v2.integration.calendar_adoption_guard import require_verified
from companion_v2.integration.calendar_command_bridge import CalendarCommandBridge
from companion_v2.integration.calendar_records_proxy import LifeCalendarRecords
from companion_v2.integration.life_calendar_provider_proxy import LifeCalendarCommandsProxy, LifeCalendarStorageProxy

from .life_provider_registry import REGISTRY_ATTRIBUTE


_creation_lock = RLock()


def binding_for(controller):
    with _creation_lock:
        binding = getattr(controller, "_life_ledger_binding", None)
        if binding is None:
            binding = LifeLedgerBinding(controller)
            controller._life_ledger_binding = binding
        return binding


def refresh_session_binding(controller):
    binding = binding_for(controller)
    with binding.lock:
        registry = getattr(controller.context, REGISTRY_ATTRIBUTE, None)
        binding.refresh(registry if getattr(registry, "owner", None) is controller else None)


class LifeLedgerBinding:
    def __init__(self, controller):
        self.controller = controller
        self.lock = RLock()
        self._bound = None
        self._calendar = None
        self._state = None
        self._exposure = None
        self._configuration = None

    def invalidate(self):
        # Do not wait for a callback while holding the lifecycle lock: a
        # callback may itself stop its plugin. Registry draining prevents a
        # replacement from being registered until such callbacks finish.
        if self._bound is not None:
            self._bound[2].revoke()
        if self._calendar is not None:
            self._calendar[2].revoke()
        if self._state is not None:
            self._state[1].revoke()
        if self._exposure is not None:
            self._exposure[1].revoke()
        if self._configuration is not None:
            store, lease, _session = self._configuration
            lease.revoke()
            if getattr(store, "_life_configuration_lease", None) is lease:
                store._life_configuration_lease = None
            self._configuration = None

    def _refresh_configuration(self, session, application, registry):
        runtime = getattr(application, "runtime", None)
        store = getattr(runtime, "configuration_store", None)
        lease = registry.lease() if registry is not None else None
        if self._configuration is not None:
            old_store, old_lease, old_session = self._configuration
            if (store is old_store and session is old_session and lease is not None
                    and lease._entry is old_lease._entry and not old_lease.revoked):
                return
            old_lease.revoke()
            if getattr(old_store, "_life_configuration_lease", None) is old_lease:
                old_store._life_configuration_lease = None
            self._configuration = None
        if store is not None and lease is not None:
            store._life_configuration_lease = lease
            self._configuration = (store, lease, session)

    def _refresh_exposure(self, session, application, registry):
        social, life = getattr(application, "role_social", None), getattr(application, "role_life", None)
        lease = registry.lease() if registry is not None else None
        if self._exposure is not None:
            old_social, old_lease, old_session = self._exposure
            if (social is old_social and session is old_session and lease is not None
                    and lease._entry is old_lease._entry and not old_lease.revoked):
                return
            old_lease.revoke()
            self._exposure = None
        if social is not None and life is not None and lease is not None:
            reader = ExposureSessionReadProxy(application.runtime, lease, life=life, visible=social._visible_contact,
                                               writable=social._active_contact)
            social.exposure.session_reader = reader
            life.browsing_reader = reader
            self._exposure = (social, lease, session)

    def _refresh_state(self, session, application, registry):
        state = getattr(application, "role_state", None)
        lease = registry.lease() if registry is not None else None
        if not callable(getattr(state, "bind_life_reader", None)):
            state = None
        if self._state is not None:
            old_state, old_lease, old_session = self._state
            if (state is old_state and session is old_session and lease is not None
                    and lease._entry is old_lease._entry and not old_lease.revoked):
                return
            old_lease.revoke()
            self._state = None
        if state is not None and lease is not None:
            state.bind_life_reader(LifeStateRecords(state, lease), LifeStateCommandsProxy(lease))
            self._state = (state, lease, session)

    def _refresh_calendar(self, session, application, registry):
        calendar = getattr(application, "calendar", None)
        lease = registry.lease() if registry is not None else None
        if not callable(getattr(calendar, "set_life_bridge_resolver", None)):
            calendar = None
        if self._calendar is not None:
            old_calendar, _old_resolver, old_lease, old_session = self._calendar
            if (calendar is old_calendar and session is old_session and lease is not None and lease._entry is old_lease._entry
                    and not old_lease.revoked):
                return
            old_lease.revoke()
            self._calendar = None
        if calendar is None or lease is None:
            return

        cached = []

        def resolve():
            if getattr(self.controller, "session", None) is not session or not lease.is_active():
                raise TaskError("life_provider_unavailable", "the calendar life provider is unavailable")
            if not cached:
                storage = LifeCalendarStorageProxy(lease)
                commands = LifeCalendarCommandsProxy(lease)
                require_verified(application.runtime, commands)
                if getattr(self.controller, "session", None) is not session or not lease.is_active():
                    raise TaskError("life_provider_unavailable", "the calendar life provider is unavailable")
                # The verified adoption moves every eligible old role plan;
                # old reminders that cannot move remain conversation-owned.
                records = LifeCalendarRecords(storage, legacy_owners=lambda _context: ())
                cached.append((CalendarCommandBridge(calendar, storage, commands, legacy_adopted=True), records))
            return cached[0]

        calendar.set_life_bridge_resolver(resolve)
        self._calendar = (calendar, resolve, lease, session)

    def refresh(self, registry):
        session = getattr(self.controller, "session", None)
        application = getattr(session, "application", None)
        router = getattr(application, "role_life", None)
        self._refresh_configuration(session, application, registry)
        self._refresh_calendar(session, application, registry)
        self._refresh_state(session, application, registry)
        self._refresh_exposure(session, application, registry)
        lease = registry.lease() if registry is not None else None
        if lease is None:
            self.invalidate()
            return
        if not callable(getattr(router, "bind_reader", None)):
            router = None
        if self._bound is not None:
            old_router, old_reader, old_lease = self._bound
            if (router is old_router and lease is not None and lease._entry is old_lease._entry
                    and not old_lease.revoked):
                return
            old_lease.revoke()
            if router is old_router:
                old_router.unbind_reader(old_reader)
            self._bound = None
        if router is not None and lease is not None:
            reader = LifeLedgerReadProxy(application.runtime, lease)
            router.bind_reader(reader, deletion_provider=LifeActivityDeletionProxy(lease),
                               creation_provider=LifeActivityCreationProxy(lease),
                               transition_provider=LifeActivityTransitionProxy(lease),
                               events_provider=LifeEventProviderProxy(application.runtime, lease))
            self._bound = (router, reader, lease)
