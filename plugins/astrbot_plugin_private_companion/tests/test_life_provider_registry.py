from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import threading
import unittest

from astrbot_host.life_provider_registry import (
    API_VERSION, HANDLERS, PROVIDER_ID, LifeProviderRegistry, shared_life_provider_registry,
)
from companion_v2.execution.models import TaskError


class LifeProviderRegistryTests(unittest.TestCase):
    def setUp(self):
        self.plugin = object()
        self.star = SimpleNamespace(root_dir_name=PROVIDER_ID, star_cls=self.plugin, activated=True)
        self.context = SimpleNamespace(get_all_stars=lambda: [self.star])
        self.running = True
        self.owner = object()
        self.registry = self.new_registry()
        self.addCleanup(self.registry.close)
        self.handlers = {name: (lambda *args, name=name: (name, args)) for name in HANDLERS}

    def new_registry(self):
        return shared_life_provider_registry(self.context, owner=self.owner,
                                             host_active=lambda: self.running)

    def register(self, **kwargs):
        return self.registry.register(**{"plugin": self.plugin, "api_version": API_VERSION,
                                         "handlers": self.handlers, **kwargs})

    def test_only_active_matching_plugin_with_complete_versioned_contract_registers(self):
        with self.assertRaises(ValueError):
            self.register(api_version="companion.life.v2")
        with self.assertRaises(ValueError):
            self.register(handlers={})
        with self.assertRaises(ValueError):
            self.register(plugin=object())
        self.star.activated = False
        with self.assertRaises(ValueError):
            self.register()
        self.star.activated = True
        self.register()
        self.assertEqual(self.registry.dispatch("calendar_read", "authorized-context"),
                         ("calendar_read", ("authorized-context",)))

    def test_unregistration_and_plugin_deactivation_prevent_dispatch(self):
        token = self.register()
        self.star.activated = False
        with self.assertRaises(TaskError):
            self.registry.dispatch("activity_control", {})
        self.star.activated = True
        self.assertTrue(self.registry.unregister(token))
        with self.assertRaises(TaskError):
            self.registry.dispatch("activity_control", {})

    def test_old_token_cannot_remove_a_replacement(self):
        old = self.register()
        new = self.register()
        self.assertFalse(self.registry.unregister(old))
        self.assertTrue(self.registry.status()["ready"])
        self.assertTrue(self.registry.unregister(new))

    def test_lease_is_invalid_after_unregistration_or_explicit_revoke(self):
        old = self.register()
        lease = self.registry.lease()
        self.assertEqual(lease.dispatch("activity_read", "x"), ("activity_read", ("x",)))
        self.assertTrue(self.registry.unregister(old))
        new = self.register()
        with self.assertRaises(TaskError):
            lease.dispatch("activity_read", "x")
        current = self.registry.lease()
        current.revoke()
        with self.assertRaises(TaskError):
            current.dispatch("activity_read", "x")
        self.assertEqual(self.registry.lease().dispatch("activity_read", "x"), ("activity_read", ("x",)))
        self.assertTrue(self.registry.unregister(new))

    def test_facades_share_one_owner_and_stale_close_keeps_new_host(self):
        self.assertIs(self.new_registry(), self.registry)
        self.running = False
        replacement = shared_life_provider_registry(self.context, owner=object(), host_active=lambda: True)
        self.addCleanup(replacement.close)
        self.registry.close()
        self.assertIs(getattr(self.context, "_private_companion_life_provider"), replacement)

    def test_live_different_host_and_ambiguous_plugin_fail_closed(self):
        with self.assertRaises(ValueError):
            shared_life_provider_registry(self.context, owner=object(), host_active=lambda: True)
        self.context.get_all_stars = lambda: [self.star, self.star]
        with self.assertRaises(ValueError):
            self.register()

    def test_provider_liveness_callback_and_host_shutdown_prevent_dispatch(self):
        active = [True]
        self.register(is_active=lambda: active[0])
        active[0] = False
        with self.assertRaises(TaskError):
            self.registry.dispatch("settings_read", {})
        active[0] = True
        self.running = False
        with self.assertRaises(TaskError):
            self.registry.dispatch("settings_read", {})

    def test_async_liveness_callback_fails_closed(self):
        async def active():
            return True
        self.register(is_active=active)
        self.assertFalse(self.registry.status()["ready"])

    def test_replacement_waits_until_an_unregistered_call_has_finished(self):
        started, release = threading.Event(), threading.Event()
        def blocked():
            started.set()
            if not release.wait(timeout=5):
                raise RuntimeError("test callback did not finish")
            return "done"
        token = self.register(handlers={**self.handlers, "activity_read": blocked})
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self.registry.dispatch, "activity_read")
            try:
                self.assertTrue(started.wait(timeout=5))
                self.assertTrue(self.registry.unregister(token))
                with self.assertRaisesRegex(ValueError, "draining"):
                    self.register()
                self.registry.close()
                with self.assertRaisesRegex(ValueError, "draining"):
                    shared_life_provider_registry(self.context, owner=object(), host_active=lambda: True)
            finally:
                release.set()
            self.assertEqual(future.result(timeout=5), "done")
        replacement = shared_life_provider_registry(self.context, owner=object(), host_active=lambda: True)
        self.addCleanup(replacement.close)
        self.assertIs(getattr(self.context, "_private_companion_life_provider"), replacement)

    def test_callback_can_close_its_own_registry_without_deadlock(self):
        def closing_callback():
            self.registry.close()
            return "done"
        self.register(handlers={**self.handlers, "activity_read": closing_callback})
        self.assertEqual(self.registry.dispatch("activity_read"), "done")
        replacement = shared_life_provider_registry(self.context, owner=object(), host_active=lambda: True)
        self.addCleanup(replacement.close)
        self.assertTrue(replacement.status()["host_ready"])

    def test_coroutine_handlers_and_returned_awaitables_are_rejected(self):
        async def asynchronous():
            return "unfinished"
        with self.assertRaises(ValueError):
            self.register(handlers={**self.handlers, "activity_read": asynchronous})
        self.register(handlers={**self.handlers, "activity_read": lambda: asynchronous()})
        with self.assertRaises(TaskError) as error:
            self.registry.dispatch("activity_read")
        self.assertEqual(error.exception.code, "life_provider_invalid")

    def test_liveness_callback_cannot_leave_a_closed_registry_ready(self):
        def closing():
            self.registry.close()
            return True
        self.register(is_active=closing)
        self.assertFalse(self.registry.status()["ready"])

    def test_concurrent_hosts_cannot_replace_one_another(self):
        self.registry.close()
        barrier = threading.Barrier(2)
        def create(_):
            barrier.wait(timeout=5)
            try:
                return shared_life_provider_registry(self.context, owner=object(), host_active=lambda: True)
            except ValueError:
                return None
        with ThreadPoolExecutor(max_workers=2) as executor:
            instances = list(executor.map(create, range(2)))
        winners = [instance for instance in instances if instance is not None]
        self.assertEqual(len(winners), 1)
        self.addCleanup(winners[0].close)


if __name__ == "__main__":
    unittest.main()
