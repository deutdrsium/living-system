"""Lease the single external life provider to one live AstrBot host."""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import inspect
from threading import RLock
from uuid import uuid4

from companion_v2.execution.models import TaskError


API_VERSION = "companion.life.v1"
PROVIDER_ID = "astrbot_plugin_good_life"
REGISTRY_ATTRIBUTE = "_private_companion_life_provider"
HANDLERS = frozenset({"calendar_read", "calendar_command", "configuration_read", "configuration_command", "activity_read", "browsing_read", "browsing_command",
                      "activity_control", "activity_history", "state_read", "state_command", "state_schedule"})
_contexts_lock = RLock()


@dataclass(frozen=True)
class _Registration:
    token: str
    plugin: object
    handlers: dict
    is_active: object


class _ProviderLease:
    """A dispatch handle that cannot begin calling a replacement provider."""

    def __init__(self, registry, entry):
        self._registry, self._entry = registry, entry
        self._lock = RLock()
        self._revoked = False

    def revoke(self):
        with self._lock:
            self._revoked = True

    @property
    def revoked(self):
        with self._lock:
            return self._revoked

    def is_active(self):
        # The provider's liveness callback may unregister itself, so never
        # hold the lease lock while invoking it.
        return not self.revoked and self._registry._active(self._entry) and not self.revoked

    def same_registration(self, other):
        return (isinstance(other, _ProviderLease) and self._registry is other._registry and self._entry is other._entry
                and self.is_active() and other.is_active())

    def dispatch(self, handler, *args, **kwargs):
        if handler not in HANDLERS or not self._registry._active(self._entry):
            raise TaskError("life_provider_unavailable", "life provider is not active")
        with self._lock:
            if self._revoked:
                raise TaskError("life_provider_unavailable", "life provider lease was revoked")
            self._registry._admit(self._entry)
        return self._registry._invoke(self._entry, handler, *args, **kwargs)

    @contextmanager
    def snapshot(self, handler, *args, **kwargs):
        if handler not in HANDLERS or not self._registry._active(self._entry):
            raise TaskError("life_provider_unavailable", "life provider is not active")
        with self._lock:
            if self._revoked:
                raise TaskError("life_provider_unavailable", "life provider lease was revoked")
            self._registry._admit(self._entry)
        try:
            manager = self._entry.handlers[handler](*args, **kwargs)
            if inspect.isawaitable(manager):
                if inspect.iscoroutine(manager):
                    manager.close()
                raise TaskError("life_provider_invalid", "life snapshot must be synchronous")
            if not callable(getattr(manager, "__enter__", None)) or not callable(getattr(manager, "__exit__", None)):
                raise TaskError("life_provider_invalid", "life snapshot is unavailable")
            manager.__enter__()
            try:
                yield
            finally:
                manager.__exit__(None, None, None)
        finally:
            self._registry._release()


class LifeProviderRegistry:
    """Keep callbacks private and dispatch only while their owner is active.

    This registry does not grant access to a role. Domain callers must still
    authorize each request before dispatching it, as they do without a provider.
    """

    def __init__(self, context, *, owner, host_active):
        self.context = context
        self.owner = owner
        self._host_active = host_active
        self._lock = RLock()
        self._entry = None
        self._closed = False
        self._inflight = 0
        with _contexts_lock:
            current = getattr(context, REGISTRY_ATTRIBUTE, None)
            if current is not None:
                with current._lock:
                    if not current._closed or current._inflight:
                        raise ValueError("a life provider host is still active or draining")
            setattr(context, REGISTRY_ATTRIBUTE, self)

    def _host_is_active(self):
        try:
            if self._closed or getattr(self.context, REGISTRY_ATTRIBUTE, None) is not self:
                return False
            result = self._host_active()
            if inspect.isawaitable(result):
                if inspect.iscoroutine(result):
                    result.close()
                return False
            return result is True
        except Exception:
            return False

    def _plugin_is_active(self, plugin):
        try:
            matches = [star for star in self.context.get_all_stars()
                       if getattr(star, "root_dir_name", None) == PROVIDER_ID
                       and getattr(star, "activated", False) is True]
            return len(matches) == 1 and getattr(matches[0], "star_cls", None) is plugin
        except Exception:
            return False

    def _active(self, entry):
        if (entry is None or self._entry is not entry or not self._host_is_active()
                or not self._plugin_is_active(entry.plugin)):
            return False
        try:
            if entry.is_active is None:
                return True
            result = entry.is_active()
            if inspect.isawaitable(result):
                if inspect.iscoroutine(result):
                    result.close()
                return False
            return (result is True and self._entry is entry and self._host_is_active()
                    and self._plugin_is_active(entry.plugin))
        except Exception:
            return False

    def register(self, *, plugin, api_version, handlers, is_active=None):
        if not self._host_is_active():
            raise ValueError("life provider host is stopped")
        if api_version != API_VERSION:
            raise ValueError("incompatible life provider API version")
        if not self._plugin_is_active(plugin):
            raise ValueError("life provider must be the active good-life plugin")
        if (type(handlers) is not dict or set(handlers) != HANDLERS
                or not all(callable(callback) and not inspect.iscoroutinefunction(callback)
                           and not inspect.iscoroutinefunction(getattr(callback, "__call__", None))
                           for callback in handlers.values())
                or (is_active is not None and not callable(is_active))):
            raise ValueError("life provider must supply the complete handler contract")
        with self._lock:
            previous = self._entry
        if previous is not None and previous.plugin is not plugin and self._active(previous):
            raise ValueError("another life provider is active")
        token = uuid4().hex
        with self._lock:
            if self._closed or getattr(self.context, REGISTRY_ATTRIBUTE, None) is not self:
                raise ValueError("life provider host is stopped")
            if self._entry is not previous:
                raise ValueError("life provider changed during registration")
            if self._inflight:
                raise ValueError("the previous life provider is still draining")
            self._entry = _Registration(token, plugin, dict(handlers), is_active)
        return token

    def unregister(self, token):
        with self._lock:
            if self._entry is None or self._entry.token != token:
                return False
            self._entry = None
            return True

    def dispatch(self, handler, *args, **kwargs):
        with self._lock:
            entry = self._entry
        return self._dispatch(entry, handler, *args, **kwargs)

    def lease(self):
        with self._lock:
            entry = self._entry
        # Bind even while the registered plugin is temporarily unready. Each
        # dispatch still checks liveness, so readiness can recover without a
        # new session or exposing a replacement through an old lease.
        return _ProviderLease(self, entry) if entry is not None and self._host_is_active() else None

    def owns_registration(self, token, plugin):
        with self._lock:
            entry = self._entry
        return bool(entry is not None and entry.token == token and entry.plugin is plugin and self._active(entry))

    def _dispatch(self, entry, handler, *args, **kwargs):
        if handler not in HANDLERS or not self._active(entry):
            raise TaskError("life_provider_unavailable", "life provider is not active")
        self._admit(entry)
        return self._invoke(entry, handler, *args, **kwargs)

    def _admit(self, entry):
        with self._lock:
            if self._closed or self._entry is not entry or getattr(self.context, REGISTRY_ATTRIBUTE, None) is not self:
                raise TaskError("life_provider_unavailable", "life provider is not active")
            self._inflight += 1

    def _invoke(self, entry, handler, *args, **kwargs):
        try:
            result = entry.handlers[handler](*args, **kwargs)
            if inspect.isawaitable(result):
                if inspect.iscoroutine(result):
                    result.close()
                raise TaskError("life_provider_invalid", "life provider callbacks must complete synchronously")
            return result
        finally:
            self._release()

    def _release(self):
        with _contexts_lock, self._lock:
            self._inflight -= 1
            if self._closed and not self._inflight and getattr(self.context, REGISTRY_ATTRIBUTE, None) is self:
                delattr(self.context, REGISTRY_ATTRIBUTE)

    def status(self):
        with self._lock:
            entry = self._entry
        return {"api_version": API_VERSION, "provider_id": PROVIDER_ID,
                "host_ready": self._host_is_active(), "ready": self._active(entry)}

    def close(self):
        with _contexts_lock, self._lock:
            self._closed = True
            self._entry = None
            if not self._inflight and getattr(self.context, REGISTRY_ATTRIBUTE, None) is self:
                delattr(self.context, REGISTRY_ATTRIBUTE)


def shared_life_provider_registry(context, *, owner, host_active):
    with _contexts_lock:
        current = getattr(context, REGISTRY_ATTRIBUTE, None)
        if current is not None:
            with current._lock:
                closed, inflight, same_owner = current._closed, current._inflight, current.owner is owner
            if not closed and same_owner:
                return current
            if inflight:
                raise ValueError("the previous life provider is still draining")
            if not closed and current._host_is_active():
                raise ValueError("a different life provider host is still active")
            current.close()
        return LifeProviderRegistry(context, owner=owner, host_active=host_active)
