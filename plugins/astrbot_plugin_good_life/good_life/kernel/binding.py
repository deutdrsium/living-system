"""Life-local inputs and optional group hooks for a hosted kernel lease."""

import asyncio
import inspect
import json
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from sqlite3 import Error as SQLiteError
from types import MappingProxyType

from ..calendar_schedule import Schedule
from ..contracts.models import ReasonCode, StateQuality, StateValue
from ..prepared_commands import _synchronous, _ValidationView
from ..staged_calendar_commands import StagedCalendarCommands
from ..storage import StorageReader
from ..staged_activity_commands import staged_foreground_busy
from .codec import digest, freeze, moment, require


@dataclass(frozen=True)
class OptionalGroupBinding:
    value: object = None
    settle: object = None
    commitments: object = None
    propose: object = None
    deliver: object = None
    close: object = None
    projections: Mapping | None = None
    capabilities: tuple[str, ...] = ()


@dataclass(frozen=True)
class OptionalGroupContext:
    runtime: object
    storage: object
    access: object
    registry: object
    kernel: object
    groups: Mapping
    mode: str


@dataclass(frozen=True)
class SnapshotContext:
    scope: object
    now: datetime
    reader: _ValidationView


def _agenda(reader, owner_keys, now, free_until):
    """Project staged hard commitments without re-entering the host's proxy."""
    owner_keys = tuple(dict.fromkeys(owner_keys))
    placeholders = ','.join('?' for _ in owner_keys)
    pending = reader.one(f"""SELECT 1 FROM life_prepared_commands WHERE state IN ('prepared','committed') AND (
        (kind='calendar.change' AND json_extract(payload_json,'$.storage_key') IN ({placeholders})) OR
        (kind='calendar.batch' AND EXISTS (SELECT 1 FROM json_each(payload_json,'$.storage_keys')
            WHERE value IN ({placeholders})))) LIMIT 1""", (*owner_keys, *owner_keys))
    if pending is not None:
        return None, ()
    rows = reader.all(f"""SELECT record_json FROM staged_calendar_records
        WHERE owner_key IN ({placeholders})
          AND json_extract(record_json, '$.state') = 'accepted'
          AND json_extract(record_json, '$.kind') IN ('role_plan', 'commitment')
        ORDER BY owner_key, record_id LIMIT 201""", owner_keys)
    if len(rows) == 201:
        return None, ()
    facts = []
    for row in rows:
        record = json.loads(row[0])
        facts.append(row[0])
        if free_until is None:
            continue
        schedule = Schedule.restore(record['schedule'])
        duration = timedelta(minutes=record['duration_minutes'])
        start = now - duration
        sequence = schedule.first_at_or_after(start)
        while sequence is not None:
            at = schedule.at(sequence)
            if at is None or at >= free_until:
                break
            if str(sequence) not in record['exceptions'] and at + duration > now:
                free_until = max(now, at)
                break
            sequence += 1
        for moved in record['exceptions'].values():
            if moved:
                at = moment(datetime.fromisoformat(moved))
                if at + duration > now:
                    free_until = min(free_until, max(now, at))
    return free_until, tuple(facts)


class LeaseAccess:
    def __init__(self, runtime, lease, host):
        self.runtime, self.lease, self.host = runtime, lease, host
        self.reader = StorageReader(runtime.storage)
        self.revoked = False

    def _active(self):
        return not self.revoked and self.runtime._lease is self.lease

    def revoke(self):
        with self.runtime._lease_lock:
            self.revoked = True

    def scopes(self):
        return self.host.scopes() if self._active() else ()

    def authorize(self, scope, action):
        return self._active() and self.host.authorize(scope, action) is True

    @contextmanager
    def write_guard(self, scope, action):
        # Callers already hold the life store lock and have finished callbacks.
        with self.runtime._lease_lock:
            require(self.authorize(scope, action), "kernel lease or authority expired", ReasonCode.UNAUTHORIZED)
            yield

    def audience(self, scope):
        if not self._active():
            raise ValueError("kernel lease expired")
        return self.host.audience(scope)

    def snapshot(self, scope, now):
        if not self._active():
            raise ValueError("kernel lease expired")
        with self.reader.locked():
            snapshot = self.host.snapshot(scope, now)
            # The host supplies verified calendar storage keys as an optional
            # C1 adapter detail. Without them availability remains host-defined.
            calendar_keys = getattr(self.host, 'calendar_keys', None)
            free_until, facts = snapshot.free_until, ()
            if 'host.calendar.life_owned' in snapshot.capabilities and callable(calendar_keys):
                try:
                    keys = calendar_keys(scope)
                    certificate = self.host.calendar_adoption_certificate(scope)
                    local = StagedCalendarCommands(self.runtime.storage).adoption_certificate() if keys else None
                    if local is not None and local == certificate:
                        free_until, facts = _agenda(self.reader, keys, now, now + timedelta(days=1))
                except (ValueError, KeyError, TypeError, SQLiteError):
                    free_until = None
            busy = True if staged_foreground_busy(self.reader, scope.owner_key) or free_until == now else snapshot.busy
            values = dict(snapshot.state.values)
            capabilities = dict.fromkeys(snapshot.capabilities)
            context = SnapshotContext(scope, now, _ValidationView(self.reader))
            for name, binding in tuple(self.runtime._optional.groups.items()):
                if self.runtime._optional.modes[name] == 'active':
                    capabilities.update(dict.fromkeys(binding.capabilities))
                if not binding.projections:
                    continue
                try:
                    group_values = {key: freeze(_synchronous(project(context)))
                                    for key, project in binding.projections.items() if key not in values}
                    if len(group_values) != len(binding.projections):
                        raise ValueError("projection shadows a snapshot key")
                except Exception:
                    self.runtime._optional.projection_failures.add(name)
                    group_values = {key: None for key in binding.projections if key not in values}
                    quality = StateQuality.UNKNOWN
                else:
                    self.runtime._optional.projection_failures.discard(name)
                    quality = StateQuality.VALID
                for key, value in group_values.items():
                    revision = int(digest((key, value, quality))[:15], 16) + 1
                    values[key] = StateValue(value, now, revision, (), quality)
            revision = int(digest((snapshot.state.revision, facts))[:15], 16) + 1
            result = replace(snapshot, state=replace(snapshot.state, values=MappingProxyType(values), revision=revision),
                             busy=busy, free_until=free_until, capabilities=tuple(capabilities))
            if not self.authorize(scope, 'kernel.snapshot'):
                raise ValueError("kernel scope expired during snapshot")
            return result


class OptionalKernel:
    """Bind optional components and drive one ordered loop per provider lease."""

    def __init__(self, runtime, *, enabled):
        self.runtime, self.enabled = runtime, enabled
        self.registrations, self.modes = {}, {}
        self.groups, self.failures, self.work_failures = {}, {}, {}
        self.projection_failures = set()
        self.worker = self.access = self.registry = None
        self.installed_modules = set()

    def register(self, name, bind, *, phase, requires, enabled, has_history):
        from .codec import name as identifier
        identifier(name)
        if (self.runtime.closed or self.runtime._lease is not None or name in self.registrations
                or phase not in ('pre_kernel', 'post_kernel') or not callable(bind)
                or type(requires) is not tuple or name in requires or type(enabled) is not bool
                or (has_history is not None and not callable(has_history))
                or name in {'kernel', 'reading_execute', 'reading_recovery', 'reading_abort_own', 'study_material'}):
            raise ValueError('invalid optional group registration')
        for dependency in requires:
            identifier(dependency)
        self.registrations[name] = (bind, phase, requires, enabled, has_history)

    def diagnostics(self):
        result = {name: {'reason': reason, 'attempts': attempts}
                  for name, (attempts, _, reason) in self.failures.items()}
        result.update({f'{name}.{phase}': {'reason': reason}
                       for (name, phase), (_, _, reason) in self.work_failures.items()})
        result.update({name: {'reason': 'capability_disabled'} for name in self.groups
                       if self.modes[name] == 'settle_only'})
        result.update({f'{name}.projections': {'reason': 'projection_unavailable'} for name in self.projection_failures})
        return result

    def attempt(self, name, bind):
        attempts, retry_at, _ = self.failures.get(name, (0, float('-inf'), None))
        now = self.runtime.monotonic_clock()
        if now < retry_at:
            return
        try:
            bind()
        except Exception:
            # A bounded burst is followed by one recovery probe per 30 seconds.
            self.failures[name] = (min(attempts + 1, 5), now + min(30, 2 ** attempts), 'optional_binding_failed')
        else:
            self.failures.pop(name, None)

    def _bind_group(self, name, bind, phase, mode):
        from .codec import name as identifier
        staged = (self.registry._stage_group(settlement_only=mode == 'settle_only')
                  if phase == 'pre_kernel' else None)
        context = OptionalGroupContext(self.runtime, self.runtime.storage, self.access, staged,
            self.runtime.kernel if phase == 'post_kernel' else None,
            MappingProxyType({key: group.value for key, group in self.groups.items()}), mode)
        result = _synchronous(bind(context))
        if not isinstance(result, OptionalGroupBinding):
            raise ValueError('optional bind must return OptionalGroupBinding')
        for callback in (result.settle, result.commitments, result.propose, result.deliver, result.close):
            if callback is not None and not callable(callback):
                raise ValueError('invalid optional callback')
        if type(result.capabilities) is not tuple:
            raise ValueError('group capabilities must be a tuple')
        for capability in result.capabilities:
            identifier(capability)
        keys = {key for group in self.groups.values() for key in group.projections}
        projections = dict(result.projections or {})
        for key, callback in projections.items():
            identifier(key)
            if '.' not in key or key in keys or not callable(callback) or inspect.iscoroutinefunction(callback):
                raise ValueError('duplicate or invalid state projection')
        with self.runtime.storage._lock:
            if staged is not None:
                added = staged.modules.keys() - self.registry.modules.keys()
                for key in sorted(added):
                    install = getattr(staged.modules[key][1], 'install', None)
                    if callable(install):
                        _synchronous(install(self.runtime.storage))
                self.registry._publish_group(staged)
                self.installed_modules.update(added)
            self.groups[name] = replace(result, projections=MappingProxyType(projections))

    def _resolve_modes(self):
        reader = _ValidationView(StorageReader(self.runtime.storage))
        for name, (_, _, _, enabled, has_history) in self.registrations.items():
            if name in self.modes:
                continue
            def resolve():
                if enabled and self.enabled:
                    self.modes[name] = 'active'
                elif has_history is not None and _synchronous(has_history(reader)) is True:
                    self.modes[name] = 'settle_only'
                else:
                    self.modes[name] = None
            self.attempt(name, resolve)
        # A disabled dependency still permits settlement of already-owned work.
        for name, (_, _, dependencies, _, has_history) in self.registrations.items():
            if (self.modes.get(name) == 'active' and name not in self.groups and has_history is not None
                    and any(dep in self.modes and self.modes[dep] != 'active' for dep in dependencies)):
                def resolve_settlement():
                    if _synchronous(has_history(reader)) is True:
                        self.modes[name] = 'settle_only'
                self.attempt(name, resolve_settlement)
        # Settlement can need a lookup or executor group with no history of its own.
        pending = [name for name, mode in self.modes.items() if mode == 'settle_only']
        while pending:
            name = pending.pop()
            for dependency in self.registrations[name][2]:
                if dependency in self.modes and self.modes[dependency] is None:
                    self.modes[dependency] = 'settle_only'
                    pending.append(dependency)

    def _bind_phase(self, phase):
        pending = [name for name, (_, group_phase, _, _, _) in self.registrations.items()
                   if group_phase == phase and name not in self.groups and self.modes.get(name) is not None]
        while pending:
            blocked = []
            for name in pending:
                binder, _, dependencies, _, _ = self.registrations[name]
                mode = self.modes[name]
                if all(dependency in self.groups and (mode == 'settle_only' or self.modes[dependency] == 'active')
                       for dependency in dependencies):
                    self.attempt(name, lambda: self._bind_group(name, binder, phase, mode))
                else:
                    self.failures.setdefault(name, (0, float('-inf'), 'optional_dependency_unavailable'))
                    blocked.append(name)
            if blocked == pending:
                break
            pending = blocked

    async def refresh(self):
        if self.access is not None:
            current = getattr(self.access.host, 'is_current', None)
            try:
                stale = callable(current) and current() is False
            except Exception:
                stale = False  # Temporary probe failure, retried by the dependency watcher.
            if stale or self.access.revoked:
                self.access.revoke()
                await self.stop_worker()
                await self.close(preserve_reading=True)
        self.bind()

    def _bind_kernel(self):
        from .engine import ActivityKernel, KernelConfig
        from .registry import ComponentRegistry
        runtime = self.runtime
        if self.access is None:
            host = _synchronous(runtime.api.life_kernel_access(plugin=runtime.plugin, registration_id=runtime.token, version=1))
            if not all(callable(getattr(host, method, None)) for method in ('scopes', 'authorize', 'snapshot', 'audience')):
                raise ValueError('invalid host kernel access')
            self.access = LeaseAccess(runtime, runtime._lease, host)
            self.registry = ComponentRegistry()
        self._bind_phase('pre_kernel')
        runtime.kernel = ActivityKernel(runtime.storage, self.registry, config=KernelConfig(enabled=True),
            access=self.access, clock=runtime._clock, installed_modules=self.installed_modules)

    def bind(self):
        self._resolve_modes()
        if not self.enabled and self.access is None and (self.runtime.kernel is not None
                or 'settle_only' not in self.modes.values()):
            return
        if self.runtime.kernel is None:
            self.attempt('kernel', self._bind_kernel)
        else:
            self._bind_phase('pre_kernel')
        if self.runtime.kernel is not None:
            self._bind_phase('post_kernel')
            if self.worker is None or self.worker.done():
                self.worker = asyncio.create_task(self._run(self.runtime._lease), name='good-life:kernel')

    async def _run_phase(self, lease, phase, now):
        # Preflights run outside both stores with one in flight per lease.
        for name, group in tuple(self.groups.items()):
            if self.runtime._lease is not lease:
                return
            if phase != 'settle' and self.modes[name] == 'settle_only':
                continue
            callback = getattr(group, phase)
            key = (name, phase)
            attempts, retry_at, _ = self.work_failures.get(key, (0, float('-inf'), None))
            if callback is None or self.runtime.monotonic_clock() < retry_at:
                continue
            try:
                result = callback(now=now)
                if inspect.isawaitable(result):
                    # Run the callback in its own task so self-cancellation
                    # cannot set the worker's cancellation state.
                    await asyncio.wait_for(asyncio.ensure_future(result), timeout=5)
            except (Exception, asyncio.CancelledError) as error:
                if isinstance(error, asyncio.CancelledError) and asyncio.current_task().cancelling():
                    raise
                self.work_failures[key] = (min(attempts + 1, 5),
                    self.runtime.monotonic_clock() + min(30, 2 ** attempts), 'optional_work_failed')
            else:
                self.work_failures.pop(key, None)

    async def _run(self, lease):
        key = ('kernel', 'tick')
        while self.runtime._lease is lease:
            try:
                now = moment(self.runtime.wall_clock())
                for scope in self.access.scopes():
                    try:
                        self.runtime.kernel.recover_pending(scope, now=now)
                    except Exception:
                        continue  # tick retries and reports the scope failure.
                for phase in ('settle', 'commitments', 'propose'):
                    await self._run_phase(lease, phase, now)
                if self.runtime._lease is lease:
                    stats = self.runtime.advance_kernel(now=now)
                    if stats['failures']:
                        self.work_failures[key] = (1, 0, 'kernel_scope_failed')
                    else:
                        self.work_failures.pop(key, None)
                    await self._run_phase(lease, 'deliver', now)
            except Exception:
                self.work_failures[key] = (1, 0, 'kernel_tick_failed')
            await asyncio.sleep(1)

    async def stop_worker(self):
        worker, self.worker = self.worker, None
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def close(self, *, preserve_reading=False):
        for group in reversed(tuple(self.groups.values())):
            if group.close is not None:
                try:
                    result = group.close()
                    if inspect.isawaitable(result):
                        await asyncio.wait_for(result, timeout=5)
                except (Exception, asyncio.CancelledError) as error:
                    if isinstance(error, asyncio.CancelledError) and asyncio.current_task().cancelling():
                        raise
        if self.access is not None and self.runtime.kernel is not None:
            self.runtime.kernel.close()
            self.runtime.kernel = None
        self.groups.clear()
        self.modes.clear()
        if preserve_reading:
            for name in ('kernel', *self.registrations):
                self.failures.pop(name, None)
        else:
            self.failures.clear()
        self.work_failures.clear()
        self.projection_failures.clear()
        self.installed_modules.clear()
        self.access = self.registry = None