"""Bind the life plugin's actual storage handlers to a companion host lifetime."""

import asyncio
from datetime import datetime, timezone
from threading import RLock
from time import monotonic

from .kernel.binding import OptionalGroupBinding, OptionalKernel
from .kernel.clock import MonotonicClock
from .activity_control import ActivityControl
from .activity_records import StagedActivityRecords
from .calendar_records import StagedCalendarRecords
from .configuration_records import StagedConfigurationRecords
from .configuration_commands import StagedConfigurationCommands
from .event_delivery import LifeEventDelivery
from .exposure_records import StagedExposureRecords
from .exposure_commands import StagedExposureCommands
from .prepared_commands import PreparedCommands
from .sleep_scheduler import DueSleepScheduler
from .state_records import StagedStateRecords
from .state_commands import StagedStateCommands
from .state_scheduler import StateSchedules
from .staged_calendar_commands import StagedCalendarCommands


class LifeRuntime:
    def __init__(self, storage, plugin, *, kernel_config=None, kernel_access=None, kernel_registry=None,
                 reading_executor_enabled=False, reading_abort_own_enabled=False,
                 state_model_config=None, state_model_access=None,
                 academics_config=None, campus=None, kernel_enabled=False, wall_clock=None, monotonic_clock=None):
        self.storage, self.plugin = storage, plugin
        self.wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self.monotonic_clock = monotonic_clock or monotonic
        self._clock = MonotonicClock(self.monotonic_clock)
        self._lifecycle = asyncio.Lock()
        self._lease_lock = RLock()
        self._lease = None
        self._optional = OptionalKernel(self, enabled=kernel_enabled is True)
        self._wallet = None
        self.reading_executor_enabled = reading_executor_enabled is True
        self.reading_abort_own_enabled = reading_abort_own_enabled is True
        self.academics = None
        self.study_ports = None
        if academics_config is not None:
            from .academics import Academics, AcademicsConfig
            if not isinstance(academics_config, AcademicsConfig):
                raise ValueError("academics_config must be AcademicsConfig")
            if academics_config.enabled:
                self.academics = Academics(config=academics_config)
                self.academics.install(storage)
        # A1 is an explicit embedding API, not a new companion.life.v1 handler.
        # Preserve the explicit local embedding used by offline callers.
        self.kernel = None
        if kernel_config is not None and not self._optional.enabled:
            from .kernel import ActivityKernel, ComponentRegistry, KernelConfig
            if not isinstance(kernel_config, KernelConfig):
                raise ValueError("kernel_config must be KernelConfig")
            if kernel_config.enabled:
                if kernel_registry is None:
                    from .activities.local_order import register_local_order
                    kernel_registry = ComponentRegistry()
                    register_local_order(kernel_registry)
                if self.academics is not None:
                    from .academics import AcademicsAccess, register_academics
                    register_academics(kernel_registry, self.academics, campus=campus)
                    kernel_access = AcademicsAccess(kernel_access, self.academics, kernel_registry, campus)
                self.kernel = ActivityKernel(storage, kernel_registry, config=kernel_config, access=kernel_access, clock=self._clock)
                if "economy" in kernel_registry.modules:
                    from .economy import Wallet, WalletActivities
                    wallet = kernel_registry.modules["economy"][1]
                    if isinstance(wallet, Wallet):
                        self._wallet = wallet
                        self.kernel = WalletActivities(self.kernel, wallet)
                if self.academics is not None:
                    from .academics import ClassActivities
                    self.kernel = ClassActivities(self.kernel, self.academics, campus)
        # A6 is an explicit shadow embedding, never attached to state handlers
        # or the existing sleep/state scheduler. All switches default to off.
        self.state_model = None
        if state_model_config is not None:
            from .state_model import ShadowConfig, ShadowState
            if not isinstance(state_model_config, ShadowConfig):
                raise ValueError("state_model_config must be ShadowConfig")
            if state_model_config.enabled:
                self.state_model = ShadowState(storage, config=state_model_config, access=state_model_access)
        PreparedCommands(storage).install()
        configuration_commands = StagedConfigurationCommands(storage)
        configuration_commands.install()
        self.handlers = {
            "calendar_read": StagedCalendarRecords(storage).dispatch,
            "configuration_read": StagedConfigurationRecords(storage).dispatch,
            "configuration_command": configuration_commands.dispatch,
            "calendar_command": StagedCalendarCommands(storage).dispatch,
            "activity_read": StagedActivityRecords(storage).dispatch,
            "activity_control": ActivityControl(storage, stop_reading=self._stop_reading).dispatch,
            "activity_history": LifeEventDelivery(storage).dispatch,
            "browsing_read": StagedExposureRecords(storage).dispatch,
            "browsing_command": StagedExposureCommands(storage).dispatch,
            "state_read": StagedStateRecords(storage).dispatch,
            "state_command": StagedStateCommands(storage).dispatch,
            "state_schedule": StateSchedules(storage).dispatch,
        }
        self.api = self.token = self.scheduler = None
        self.closed = False

    def _stop_reading(self, context_key, activity_id):
        scheduler = self.scheduler.reading_scheduler if self.scheduler is not None else None
        return scheduler.cancel_activity(context_key, activity_id) if scheduler is not None else False

    @property
    def ready(self):
        return not self.closed and self.api is not None and self.token is not None and self.scheduler is not None

    @staticmethod
    def kernel_access_types():
        from .contracts.models import AudienceBound, ExecutionScope, StateSnapshot
        from .kernel.gating import GateSnapshot
        return {cls.__name__: cls for cls in (ExecutionScope, AudienceBound, StateSnapshot, GateSnapshot)}

    def register_optional_group(self, name, bind, *, phase="post_kernel", requires=(), enabled=True, has_history=None):
        self._optional.register(name, bind, phase=phase, requires=requires, enabled=enabled, has_history=has_history)

    @property
    def optional_diagnostics(self):
        return self._optional.diagnostics()

    def _reading_ports(self, required):
        from .reading_execution import response_types
        result = self.api.life_async_capabilities(plugin=self.plugin, registration_id=self.token,
            contract_version=1, response_types=response_types())
        if (result.get("api_version") != "companion.life-host.async.v1" or result.get("contract_version") != 1
                or not required.issubset(result.get("capabilities", ()))
                or not all(callable(getattr(result.get("ports"), key, None)) for key in required)):
            raise ValueError("async host capabilities unavailable")
        return result["ports"]

    def _bind_reading(self):
        from .reading_execution import ReadingExecutor, negotiate_reading_coordination
        reading = self.scheduler.reading_scheduler
        attempt = self._optional.attempt
        if reading is not None:
            required = {"resolve_authority", "get_persona_context", "read_material", "run_model_task",
                        "inspect_task", "execute_platform_action", "accept_life_event"}

            def executor(enabled):
                return ReadingExecutor(self.storage, self._reading_ports(required), enabled=enabled,
                                       clock=self.wall_clock, coordination_ports=reading.coordination_ports)

            if self.reading_executor_enabled and reading.execute_executor is None:
                attempt("reading_execute", lambda: setattr(reading, "execute_executor", executor(True)))

            def recovery():
                if reading.store.has_life_runs():
                    reading.recovery_executor = executor(False)

            if reading.recovery_executor is None:
                attempt("reading_recovery", recovery)
            if self.reading_abort_own_enabled and reading.coordination_ports is None:
                def coordination():
                    ports = negotiate_reading_coordination(self.api, plugin=self.plugin, registration_id=self.token)
                    if ports is None:
                        raise ValueError("abort-own coordination is unavailable")
                    reading.coordination_ports = ports
                    for bound in (reading.execute_executor, reading.recovery_executor):
                        if bound is not None:
                            bound.coordination_ports = ports
                attempt("reading_abort_own", coordination)
        if getattr(self.academics, "study_artifacts", None) is not None or self.has_study_history:
            self._bind_study_ports()

    def _bind_study_ports(self):
        if self.study_ports is None and self.ready:
            self._optional.attempt("study_material", lambda: setattr(self, "study_ports",
                self._reading_ports({"resolve_authority", "read_material"})))
        return self.study_ports

    async def bind(self, api):
        async with self._lifecycle:
            if self.closed:
                raise ValueError("life runtime was stopped")
            if self.api is not api or not self.ready:
                await self._unbind()
                with self._lease_lock:
                    lease = self._lease = object()
                self.api = api
                try:
                    self.token = api.register_life_provider(plugin=self.plugin, api_version="companion.life.v1",
                        handlers=self.handlers, is_active=lambda: not self.closed and self._lease is lease
                        and self.api is api and self.token is not None)
                    capabilities = api.life_host_capabilities(plugin=self.plugin, registration_id=self.token)
                    self.scheduler = DueSleepScheduler(self.storage, capabilities, clock=self.wall_clock)
                    self.scheduler.start()
                except BaseException:
                    await self._unbind()
                    raise
            await self._optional.refresh()
            self._bind_economy_access()
            self._bind_reading()

    def _bind_economy_access(self):
        wallet = self._optional.groups.get("wallet")
        if self._optional.modes.get("wallet") != "active" or wallet is None or self.kernel is None:
            return
        from .city import CityAccess
        from .economy import WalletAccess
        self._wallet = wallet.value
        city = self._optional.groups.get("city")
        access = self.kernel.access
        if city is not None and self._optional.modes.get("city") == "active":
            if not isinstance(access, CityAccess):
                base = access.host if isinstance(access, WalletAccess) else access
                self.kernel.access = CityAccess(base, wallet.value, city.value)
        elif not isinstance(access, WalletAccess):
            self.kernel.access = WalletAccess(access, wallet.value)

    def _active_city(self):
        from .contracts.models import ReasonCode
        from .kernel.codec import require
        city = self._optional.groups.get("city")
        require(self.ready and self.kernel is not None and city is not None
                and self._optional.modes.get("city") == "active", "city trips are disabled", ReasonCode.MISSING_DEPENDENCY)
        return city.value

    async def search_city_pois(self, scope, public_place_name, *, opportunity_key, city_code=None):
        """Return public POI names for indexed selection on this lease."""
        return await self._active_city().search_pois(self.kernel, scope, public_place_name,
                                                    opportunity_key=opportunity_key, city_code=city_code)

    async def start_city_trip(self, scope, *, opportunity_key, destination=None, index=None, stay_s=0):
        """Quote from the current stop and run standard kernel planning and activation gates."""
        return await self._active_city().start_trip(self.kernel, scope, opportunity_key=opportunity_key,
                                                  destination=destination, index=index, stay_s=stay_s)

    async def unbind(self):
        async with self._lifecycle:
            await self._unbind()

    async def _unbind(self):
        with self._lease_lock:
            self._lease = None
        scheduler, api, token = self.scheduler, self.api, self.token
        self.scheduler = self.api = self.token = self.study_ports = None
        task = asyncio.create_task(self._dispose(scheduler, api, token))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # The dependency watcher can be cancelled during a hot reload.
            # Complete the old teardown before releasing the lifecycle lock.
            await task
            raise

    async def _dispose(self, scheduler, api, token):
        try:
            await self._optional.stop_worker()
            if scheduler is not None:
                await scheduler.close()
        finally:
            try:
                await self._optional.close()
            finally:
                if api is not None and token is not None:
                    try:
                        api.unregister_life_provider(token)
                    except Exception:
                        # A host reload may already have closed its registry.
                        pass

    @property
    def has_study_history(self):
        from .academics.study_artifacts import VERSION_KEY
        from .storage import StorageReader
        return not self.closed and StorageReader(self.storage).one(
            "SELECT 1 FROM life_metadata WHERE key=?", (VERSION_KEY,)) is not None

    @property
    def study_artifact_reader(self):
        from .academics.study_artifacts import StudyArtifacts
        active = getattr(self.academics, "study_artifacts", None)
        if active is not None:
            return active
        if self.has_study_history:
            return StudyArtifacts(history_storage=self.storage)
        return None

    def read_management_page(self, *, owner_key, context_key, now, before=None):
        from .economy.access import wallet_page
        from .overview_reads import read_activities
        from .storage import StorageReader

        def feature(group, enabled):
            if not self._optional.enabled or not enabled:
                return {"state": "disabled", "reason": "capability_disabled"}
            if group not in self._optional.groups or self._optional.modes[group] != "active":
                return {"state": "unavailable", "reason": "missing_dependency"}
            return {"state": "active"}

        plugin = self.plugin
        features = {
            "class": feature("classes", plugin._class_enabled),
            "attendance": feature("classes", plugin._attendance_enabled),
            "city_trip": feature("city", plugin._city_settings.get("city_enabled") is True),
            "wallet": feature("wallet", plugin._city_settings.get("wallet_enabled") is True),
        }
        reader = StorageReader(self.storage)
        with reader.locked():
            tables = {row[0] for row in reader.all("SELECT name FROM sqlite_master WHERE name IN "
                "('life_kernel_instances', 'life_academic_sessions')", ())}
            activities = read_activities(self.storage, owner_key=owner_key, context_key=context_key, now=now,
                kernel="life_kernel_instances" in tables, academics="life_academic_sessions" in tables,
                management=True)
            group = self._optional.groups.get("wallet")
            wallet = self._wallet or (group.value if group else None)
            ledger = (wallet_page(wallet, owner_key=owner_key, context_key=context_key, before=before)
                      if wallet is not None else None)
        return {"features": features, "activities": activities, "wallet": ledger}

    def read_wallet_page(self, *, owner_key, context_key, limit=20, before=None):
        """Internal read for the host-authorized page, never an enable operation."""
        from .economy.access import wallet_page
        wallet = self._wallet
        if (self.closed or wallet is None or self.kernel is None or self.kernel.closed
                or not self.kernel.config.enabled or wallet.storage is None):
            return {"enabled": False}
        return wallet_page(wallet, owner_key=owner_key, context_key=context_key, limit=limit, before=before)

    def read_overview_activities(self, *, owner_key, context_key, now):
        """Project persisted records after the host has authorized this scope."""
        from .overview_reads import read_activities
        kernel = self.kernel is not None and not self.kernel.closed and self.kernel.config.enabled
        academics = self.academics is not None and self.academics.storage is not None
        if self.closed or not (kernel or academics):
            return {"items": [], "partial": False}
        return read_activities(self.storage, owner_key=owner_key, context_key=context_key, now=now,
                               kernel=kernel, academics=academics)

    def read_academic_places(self):
        """Internal read for the host-authorized course page: class flag and graph place options.

        Places are the same for every role on this installation; the host
        authorizes the role before calling. Nothing is enabled or written here.
        """
        from .academics.places import place_options
        settings = getattr(self.plugin, "_campus_settings", None) or {}
        graph, places = place_options(self.storage, settings.get("academics_graph_id", ""))
        classes = (not self.closed and self._optional.modes.get("classes") == "active"
                   and "classes" in self._optional.groups)
        return {"classes_enabled": classes, "graph": graph, "places": places}

    async def propose_study(self, scope, *, opportunity_key, now, session_s=1500):
        """Explicit study intent, using negotiated A3 materials and A6 shadow data."""
        from .activities.study import propose_study
        from .contracts.models import ReasonCode
        from .kernel.codec import require
        require(not self.closed and self.academics is not None and self.kernel is not None,
                "study is disabled", ReasonCode.MISSING_DEPENDENCY)
        return await propose_study(self.kernel, self.academics, scope, now=now,
            opportunity_key=opportunity_key, session_s=session_s, ports=self.study_ports,
            bind_material_ports=self._bind_study_ports, shadow_state=self.state_model)

    def advance_kernel(self, *, now):
        """Explicit local coordinator step; existing schedulers never call it."""
        if self.kernel is None or self.closed or (self._optional.access is not None and self._lease is None):
            return {"advanced": 0, "started": 0, "failures": ()}
        return self.kernel.tick(now=now)

    async def close(self):
        self.closed = True
        await self.unbind()
        if self.kernel is not None:
            self.kernel.close()
        if self.state_model is not None:
            self.state_model.close()
