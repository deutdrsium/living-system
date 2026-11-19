from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from contextlib import closing
import sqlite3
import sys
from tempfile import TemporaryDirectory
from threading import Event, get_ident
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life"), str(ROOT / "tools")]

from astrbot_host.local_config import LocalConfig, Refused
from companion_v2.configuration.life_handoff import LifeConfigurationHandoff
from companion_v2.configuration.life_provider import OWNER_KEY
from companion_v2.configuration.management import RoleProfileManagement
from companion_v2.configuration.models import ConfigLayer, ConfigTarget, ConfigurationError
from companion_v2.role_work.life.configuration import GROUP as LIFE_GROUP, OWNER as LIFE_OWNER, target_for as life_target
from companion_v2.role_work.state.configuration import GROUP as STATE_GROUP, OWNER as STATE_OWNER, target_for as state_target
from companion_v2.storage.runtime import StorageRuntime
from good_life.configuration_commands import StagedConfigurationCommands
from good_life.configuration_import import stage_configuration
from good_life.configuration_records import StagedConfigurationRecords
from good_life.prepared_commands import PreparedCommands
from good_life.sleep_scheduler import DueSleepScheduler
from good_life.state_import import stage_state_configuration
from good_life.storage import LifeStorage
from rehearse_configuration_rollback import rehearse as rollback_configuration
from tests.test_role_state import StateFixture


class Lease:
    def __init__(self, storage):
        self.reader = StagedConfigurationRecords(storage)
        self.commands = StagedConfigurationCommands(storage)
        self.commands.install()
        self.interrupt_after_commit = False
        self.after_prepare = None

    def dispatch(self, handler, operation, *args, **kwargs):
        if handler == "configuration_read":
            return self.reader.dispatch(operation, *args, **kwargs)
        if handler == "configuration_command":
            result = self.commands.dispatch(operation, *args, **kwargs)
            if operation == "prepare" and self.after_prepare is not None:
                callback, self.after_prepare = self.after_prepare, None
                callback()
            if operation == "resolve" and self.interrupt_after_commit:
                self.interrupt_after_commit = False
                raise RuntimeError("interrupted after life commit")
            return result
        raise ValueError("unsupported test handler")


class ConfigurationLifeHandoffTests(StateFixture, unittest.IsolatedAsyncioTestCase):
    async def prepare(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        config = StorageRuntime.open_configuration(data_root=Path(temporary.name) / "configuration",
                                                    installation_id=self.runtime.installation_id)
        self.addCleanup(config.close)
        host, app, context, _manager = self.setup_state(configuration_store=config)
        await self.start(host)
        root = Path(temporary.name) / "life"
        version = config.read_one("SELECT value FROM runtime_metadata WHERE key='schema_version'", ())["value"]
        stage_configuration(config.database_path, expected_version=version, destination=root)
        stage_state_configuration(config.database_path, expected_version=version, destination=root)
        storage = LifeStorage.open(root)
        self.addCleanup(storage.close)
        PreparedCommands(storage).install()
        lease = Lease(storage)
        self.runtime.configuration_store._life_configuration_lease = lease
        with self.runtime.configuration_store.transaction() as connection:
            connection.execute("INSERT INTO runtime_metadata VALUES (?, 'good_life')", (OWNER_KEY,))
        return app, context, storage, lease

    def patch(self, app, target, group, owner, changes):
        snapshot = app.configuration.read(self.core, target=target, group_id=group, observed_at=self.clock.now()).unwrap()
        operation = self.operation(target.authorization_scope, owner=owner)
        request = dict(target=target, group_id=group, operation=operation, expected_edit_token=snapshot.edit_token,
                       changes=changes, observed_at=self.clock.now())
        return request, app.configuration.patch(self.core, **request)

    async def test_mixed_life_save_and_state_grant_use_one_life_ledger(self):
        app, context, storage, _lease = await self.prepare()
        target = life_target(context)
        request, outcome = self.patch(app, target, LIFE_GROUP, LIFE_OWNER,
            {"role.life.reading_chunk_chars": 2600, "role.life.diary_style_prompt": "personal diary"})
        receipt = outcome.unwrap()
        self.assertFalse(receipt.replayed)
        current = app.configuration.read(self.core, target=target, group_id=LIFE_GROUP, observed_at=self.clock.now()).unwrap()
        self.assertEqual(receipt.revision, current.revision)
        self.assertEqual(current.values["role.life.reading_chunk_chars"], 2600)
        self.assertEqual(current.values["role.life.diary_style_prompt"], "personal diary")
        self.assertIsNone(app.configuration.store.read_one("SELECT 1 FROM configuration_values WHERE field_id='role.life.reading_chunk_chars'", ()))
        self.assertTrue(app.configuration.patch(self.core, **request).unwrap().replayed)
        state = state_target(context)
        _, outcome = self.patch(app, state, STATE_GROUP, STATE_OWNER, {"role.state.typing_enabled": True})
        outcome.unwrap()
        self.assertTrue(app.configuration.read(self.core, target=state, group_id=STATE_GROUP,
            observed_at=self.clock.now()).unwrap().values["role.state.typing_enabled"])
        self.assertTrue(app.tasks.check_context_access(context, action="platform.typing", observed_at=self.clock.now()).is_success)
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path, runtime_path, life_path = (root / name for name in ("config.sqlite3", "runtime.sqlite3", "life.sqlite3"))
            for source, destination in ((app.configuration.store.database_path, config_path), (self.runtime.database_path, runtime_path)):
                with closing(sqlite3.connect(source)) as original, closing(sqlite3.connect(destination)) as backup:
                    original.backup(backup)
            with closing(sqlite3.connect(life_path)) as backup:
                storage._connection.backup(backup)
            version = app.configuration.store.read_one("SELECT value FROM runtime_metadata WHERE key='schema_version'", ())["value"]
            output = root / "restored.sqlite3"
            self.assertEqual(rollback_configuration(config_path, life_path, runtime_path,
                                                    expected_version=version, destination=output)["state"], "rehearsed")
            with closing(sqlite3.connect(output)) as db:
                rows = dict(db.execute("SELECT field_id,value_json FROM configuration_values WHERE field_id IN (?,?,?)",
                    ("role.life.reading_chunk_chars", "role.life.diary_style_prompt", "role.state.typing_enabled")))
            self.assertEqual(rows, {"role.life.reading_chunk_chars": "2600", "role.life.diary_style_prompt": '"personal diary"',
                                    "role.state.typing_enabled": "true"})

    async def test_local_edit_recovers_undo_with_the_normal_handoff(self):
        app, context, storage, lease = await self.prepare()
        target = life_target(context)
        field = "role.life.reading_chunk_chars"
        self.patch(app, target, LIFE_GROUP, LIFE_OWNER, {field: 2600})[1].unwrap()
        adapter = LocalConfig(app)
        original = adapter.get(target.key, field)
        self.assertTrue(original["writable"])
        lease.interrupt_after_commit = True
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            adapter.set(target.key, field, original["revision"], original["edit_token"], 2800)
        self.assertEqual(adapter.history(10), [])
        pending, = PreparedCommands(storage).pending()
        request = PreparedCommands(storage).recovery_request(**{
            key: pending[key] for key in ("command_id", "owner_key", "context_key", "request_hash")})
        LifeConfigurationHandoff(app.configuration).recover(request)
        change, = adapter.history(10)
        self.assertEqual(adapter.get(target.key, field)["value"], 2800)
        with self.assertRaisesRegex(Refused, "stale_revision"):
            adapter.set(target.key, field, original["revision"], original["edit_token"], 2900)
        adapter.revert(change["change_id"])
        self.assertEqual(adapter.get(target.key, field)["value"], 2600)

    async def test_local_revision_is_fenced_at_host_decision(self):
        app, context, storage, lease = await self.prepare()
        window = life_target(context)
        role = ConfigTarget(window.installation_id, ConfigLayer.ROLE, window.role, bot_id=window.bot_id)
        profile = app.configuration.registry.fields["role.profile.personality"]
        def save_profile(value):
            self.patch(app, role, profile.group_id, profile.owner_id, {profile.field_id: value})[1].unwrap()
        save_profile("initial synthetic profile")
        adapter = LocalConfig(app)
        field = "role.life.sleep_minutes"
        original = adapter.get(role.key, field)
        lease.after_prepare = lambda: save_profile("concurrent Dashboard profile")
        with self.assertRaisesRegex(ConfigurationError, "changed before the decision") as error:
            adapter.set(role.key, field, original["revision"], original["edit_token"], 460)
        self.assertEqual(error.exception.code, "stale_revision")
        self.assertEqual(adapter.get(role.key, field)["value"], original["value"])
        self.assertFalse(PreparedCommands(storage).pending())
        self.assertEqual(adapter.history(10), [])

    async def test_local_edit_reverts_transferred_state_default(self):
        app, context, _, _ = await self.prepare()
        window = state_target(context)
        role = ConfigTarget(window.installation_id, ConfigLayer.ROLE, window.role, bot_id=window.bot_id)
        field = "role.state.mood_update_max_delta"
        self.patch(app, role, STATE_GROUP, STATE_OWNER, {"role.state.mood_decay_half_life_minutes": 120})[1].unwrap()
        adapter = LocalConfig(app)
        original = adapter.get(role.key, field)
        self.assertFalse(original["explicit"])
        change = adapter.set(role.key, field, original["revision"], original["edit_token"], 4)
        self.assertEqual(adapter.get(role.key, field)["value"], 4)
        adapter.revert(change["change_id"])
        self.assertFalse(adapter.get(role.key, field)["explicit"])
        self.assertEqual(adapter.get(role.key, field)["value"], original["value"])

    async def test_local_state_handoff_and_dashboard_profile_save_finish_concurrently(self):
        app, context, _, lease = await self.prepare()
        dashboard = RoleProfileManagement(app, context)
        target = dashboard._target()
        field = "role.state.mood_update_max_delta"
        self.patch(app, target, STATE_GROUP, STATE_OWNER, {field: 3})[1].unwrap()
        adapter = LocalConfig(app)
        before = adapter.get(target.key, field)
        profile = dashboard.status(self.core).unwrap()["edit_token"]
        dashboard_has_runtime = Event()
        cli_waiting_for_runtime = Event()
        cli_finalizing = None
        timeout = 5
        runtime_lock = self.runtime._lock
        config_lock = app.configuration.store._lock
        dispatch = lease.dispatch
        authorize = dashboard._authorize

        def after_life_commit(handler, operation, *args, **kwargs):
            nonlocal cli_finalizing
            result = dispatch(handler, operation, *args, **kwargs)
            if handler == "configuration_command" and operation == "resolve":
                self.assertTrue(dashboard_has_runtime.wait(timeout), "Dashboard never locked the runtime")
                cli_finalizing = get_ident()
            return result

        class ObservedRuntimeLock:
            def acquire(self):
                if get_ident() == cli_finalizing:
                    cli_waiting_for_runtime.set()
                return runtime_lock.acquire()

            def release(self):
                runtime_lock.release()

            def __enter__(self):
                self.acquire()

            def __exit__(self, *_):
                self.release()

        def authorize_dashboard(*args):
            result = authorize(*args)
            dashboard_has_runtime.set()
            self.assertTrue(cli_waiting_for_runtime.wait(timeout), "CLI never reached state finalization")
            # A bounded probe breaks the lock cycle on a regression so both
            # workers can exit and fixture cleanup can close the databases.
            self.assertTrue(config_lock.acquire(timeout=timeout), "CLI locked configuration before runtime")
            config_lock.release()
            return result

        with (patch.object(lease, "dispatch", after_life_commit),
              patch.object(dashboard, "_authorize", authorize_dashboard),
              patch.object(self.runtime, "_lock", ObservedRuntimeLock()),
              ThreadPoolExecutor(max_workers=2) as workers):
            cli = workers.submit(adapter.set, target.key, field, before["revision"], before["edit_token"], 4)
            profile_save = workers.submit(dashboard.configure, self.core,
                {"operation_key": "concurrent-profile", "edit_token": profile, "changes": {"personality": "synthetic"}})
            self.assertFalse(profile_save.result(timeout=timeout * 3).unwrap()["replayed"])
            self.assertEqual(cli.result(timeout=timeout * 3)["change_id"], adapter.history(1)[0]["change_id"])
        self.assertEqual(adapter.get(target.key, field)["value"], 4)
        self.assertEqual(adapter.get(target.key, "role.profile.personality")["value"], "synthetic")

    async def test_committed_life_setting_recovers_after_interruption(self):
        app, context, storage, lease = await self.prepare()
        lease.interrupt_after_commit = True
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            self.patch(app, life_target(context), LIFE_GROUP, LIFE_OWNER, {"role.life.reading_chunk_chars": 2700})
        pending = PreparedCommands(storage).pending()
        self.assertEqual(len(pending), 1)
        item = pending[0]
        async def recover_configuration(request):
            return LifeConfigurationHandoff(app.configuration).recover(request)
        async def unused(*_args):
            raise AssertionError("no unrelated task should run")
        def host_starting():
            raise RuntimeError("host is not running yet")
        scheduler = DueSleepScheduler(storage, {"api_version": "companion.life-host.v1", "contexts": host_starting,
            "wake": unused, "recover": unused, "recover_configuration": recover_configuration})
        with self.assertRaisesRegex(RuntimeError, "host is not running yet"):
            await scheduler.tick(now=self.clock.now())
        current = app.configuration.read(self.core, target=life_target(context), group_id=LIFE_GROUP,
                                         observed_at=self.clock.now()).unwrap()
        self.assertEqual(current.values["role.life.reading_chunk_chars"], 2700)
        self.assertFalse(PreparedCommands(storage).pending())
