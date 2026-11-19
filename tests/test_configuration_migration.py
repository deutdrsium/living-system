from contextlib import closing
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "astrbot_plugin_good_life"))
sys.path.insert(0, str(ROOT / "plugins" / "astrbot_plugin_private_companion"))
from companion_v2.role_work.life.configuration import DEFAULTS
from companion_v2.configuration.life_provider import LIFE_FIELDS as HOST_LIFE_FIELDS, OWNER_KEY
from companion_v2.configuration.life_write_plan import build_plan
from companion_v2.configuration.models import ConfigLayer, ConfigTarget, encode
from companion_v2.execution.models import TaskError
from companion_v2.foundation.refs import ScopeKind, ScopeRef, SubjectKind, SubjectRef
from companion_v2.role_work.life.configuration import LifeConfigurationOwner, configuration_fields as life_fields
from companion_v2.role_work.state.configuration import StateConfigurationOwner, configuration_fields as state_fields
from good_life.configuration_import import HOST_FIELDS, LIFE_FIELDS, SHARED_FIELDS, stage_configuration
from good_life import configuration_import
from good_life.configuration_records import ConfigurationTransitionPending, StagedConfigurationRecords
from good_life.configuration_commands import StagedConfigurationCommands, _target_key
from good_life.prepared_commands import PreparedCommands
from good_life.state_import import stage_state_configuration
from good_life.storage import LifeStorage


class ConfigurationMigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "configuration.sqlite3"
        self.target = self.root / "isolated-destination"
        with closing(sqlite3.connect(self.source)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.executescript("""
                CREATE TABLE runtime_metadata(key TEXT PRIMARY KEY, value TEXT);
                INSERT INTO runtime_metadata VALUES ('schema_version', 'configuration-store.v1');
                CREATE TABLE operations(operation_id TEXT PRIMARY KEY, owner TEXT, state TEXT, commit_state TEXT);
                CREATE TABLE configuration_targets(target_key TEXT PRIMARY KEY, target_json TEXT, revision INTEGER, updated_at TEXT);
                CREATE TABLE configuration_values(target_key TEXT REFERENCES configuration_targets(target_key),
                    field_id TEXT, value_json TEXT, revision INTEGER, PRIMARY KEY(target_key, field_id));
                CREATE TABLE configuration_changes(sequence INTEGER PRIMARY KEY, operation_id TEXT REFERENCES operations(operation_id),
                    target_key TEXT REFERENCES configuration_targets(target_key), group_id TEXT, changed INTEGER,
                    result_json TEXT, created_at TEXT);
                CREATE TABLE configuration_consumers(consumer_id TEXT PRIMARY KEY, generation INTEGER, started_sequence INTEGER);
                CREATE TABLE configuration_applications(target_key TEXT, group_id TEXT,
                    consumer_id TEXT REFERENCES configuration_consumers(consumer_id), generation INTEGER,
                    resolution_token TEXT, state TEXT, adopted_values_json TEXT, updated_at TEXT);
            """)
            db.execute("INSERT INTO configuration_targets VALUES ('role', ?, 3, 'time')", ('{"private":"identity"}',))
            for field, value in (("enabled", "true"), ("reading_chunk_chars", "3000"),
                                 ("diary_style_prompt", '"private writing style"'),
                                 ("rest_reply_mode", '"probability"')):
                db.execute("INSERT INTO configuration_values VALUES ('role', ?, ?, 2)", ("role.life." + field, value))
            db.execute("INSERT INTO operations VALUES ('operation', 'role.life.settings', 'completed', 'committed')")
            db.execute("INSERT INTO configuration_changes VALUES (1, 'operation', 'role', 'role.life', 1, '{}', 'time')")
            db.execute("INSERT INTO configuration_consumers VALUES ('role.life', 1, 1)")
            db.execute("INSERT INTO configuration_applications VALUES ('role', 'role.life', 'role.life', 1, 'token', 'active', '{}', 'time')")
            db.commit()

    def stage(self, **kwargs):
        return stage_configuration(self.source, expected_version="configuration-store.v1", **kwargs)

    def digest(self):
        return hashlib.sha256(self.source.read_bytes()).hexdigest()

    def test_every_existing_life_setting_has_an_explicit_owner(self):
        self.assertEqual(set(DEFAULTS), LIFE_FIELDS | SHARED_FIELDS | HOST_FIELDS)
        self.assertFalse(LIFE_FIELDS & SHARED_FIELDS or LIFE_FIELDS & HOST_FIELDS or SHARED_FIELDS & HOST_FIELDS)
        self.assertEqual(HOST_LIFE_FIELDS, LIFE_FIELDS | SHARED_FIELDS)

    def test_dry_run_is_read_only_and_does_not_expose_values(self):
        before = self.digest()
        result = self.stage()
        self.assertEqual(result["state"], "dry_run")
        self.assertEqual(result["summary"]["configuration_values"]["rows"], 2)
        self.assertNotIn("private writing style", str(result))
        self.assertNotIn("identity", str(result))
        self.assertFalse(self.target.exists())
        self.assertEqual(self.digest(), before)

    def test_import_keeps_scope_revision_and_only_selected_fields(self):
        self.assertEqual(self.stage(destination=self.target)["state"], "staged")
        self.assertEqual(self.stage(destination=self.target)["state"], "unchanged")
        with closing(sqlite3.connect(self.target / "good-life.sqlite3")) as db:
            values = db.execute("SELECT field_id, value_json, revision FROM staged_configuration_values ORDER BY field_id").fetchall()
            target = db.execute("SELECT target_key, revision FROM staged_configuration_targets").fetchone()
        self.assertEqual(values, [("role.life.enabled", "true", 2), ("role.life.reading_chunk_chars", "3000", 2)])
        self.assertEqual(target, ("role", 3))
        storage = LifeStorage.open(self.target)
        self.addCleanup(storage.close)
        reader = StagedConfigurationRecords(storage)
        result = reader.dispatch("target", domain="life", target_key="role",
                                 fields=["role.life.enabled", "role.life.reading_chunk_chars"])
        self.assertEqual((result["revision"], len(result["values"])), (3, 2))
        with self.assertRaises(ValueError):
            reader.dispatch("target", domain="life", target_key="role", fields=["role.life.diary_style_prompt"])

    def test_configuration_commit_is_fenced_until_host_confirmation(self):
        self.stage(destination=self.target)
        storage = LifeStorage.open(self.target)
        self.addCleanup(storage.close)
        PreparedCommands(storage).install()
        commands = StagedConfigurationCommands(storage)
        commands.install()
        document = {"role": "test"}
        key = _target_key(document)
        field = "role.life.reading_chunk_chars"
        request = {"command_id": "config-test", "owner_key": "owner", "context_key": "context",
                   "kind": "configuration.patch", "expected_revision": 0,
                   "payload": {"domain": "life", "targets": [{"key": key, "document": document,
                       "revision": 0, "changes": [{"field_id": field, "value_json": "2400"}]}],
                       "operation_id": "operation-test", "parameter_hash": "a" * 64,
                       "observed_at": "2026-11-16T00:00:00+00:00",
                       "identity": {"installation_id": "installation", "bot_id": "bot", "role_id": "role"}}}
        prepared = commands.prepare(request, prepared_at=datetime.now(timezone.utc))
        self.assertEqual(commands.proposal(request)["targets"][0]["revision"], 1)
        decision = {"command_id": request["command_id"], "request_hash": prepared["request_hash"],
                    "decision": "commit", "decision_id": "decision-test"}
        commands.resolve(decision, verify_host_decision=lambda _: True)
        reader = StagedConfigurationRecords(storage)
        with self.assertRaises(ConfigurationTransitionPending):
            reader.dispatch("target", domain="life", target_key=key, fields=[field])
        self.assertTrue(commands.verify_commit(decision))
        self.assertTrue(commands.acknowledge(request["command_id"], verify_host_completion=lambda _: True))
        result = reader.dispatch("target", domain="life", target_key=key, fields=[field])
        self.assertEqual(result, {"revision": 1, "values": [{"field_id": field, "value_json": "2400", "revision": 1}]})
        self.assertEqual(commands.resolve(decision, verify_host_decision=lambda _: True)["state"], "acknowledged")

    def test_existing_owners_read_migrated_values_and_host_only_values(self):
        role = SubjectRef("installation", SubjectKind.BOT, "test-role")
        scope = ScopeRef("installation", "test", ScopeKind.PRIVATE, "test-window")
        window = ConfigTarget("installation", ConfigLayer.WINDOW, role, scope)
        role_target = ConfigTarget("installation", ConfigLayer.ROLE, role)
        with closing(sqlite3.connect(self.source)) as db:
            for target in (role_target, window):
                db.execute("INSERT INTO configuration_targets VALUES (?, ?, 4, 'time')", (target.key, encode(target.document())))
            for target, field, value in (
                (role_target, "role.life.sleep_minutes", "520"),
                (window, "role.life.reading_chunk_chars", "2800"),
                (window, "role.life.diary_style_prompt", '"my diary style"'),
                (role_target, "role.state.hunger_enabled", "false"),
                (window, "role.state.typing_enabled", "true"),
            ):
                db.execute("INSERT INTO configuration_values VALUES (?, ?, ?, 4)", (target.key, field, value))
            db.commit()
        self.stage(destination=self.target)
        stage_state_configuration(self.source, expected_version="configuration-store.v1", destination=self.target)
        storage = LifeStorage.open(self.target)
        self.addCleanup(storage.close)
        records = StagedConfigurationRecords(storage)

        class Lease:
            def dispatch(self, handler, operation, **kwargs):
                assert handler == "configuration_read"
                return records.dispatch(operation, **kwargs)

        class Store:
            def __init__(self, db):
                self.db, self.configuration_store = db, self
                self._life_configuration_lease = Lease()

            def read_one(self, statement, parameters):
                return self.db.execute(statement, parameters).fetchone()

            def read_all(self, statement, parameters):
                return self.db.execute(statement, parameters).fetchall()

        db = sqlite3.connect(self.source)
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        store = Store(db)
        life_owner, state_owner = LifeConfigurationOwner(store), StateConfigurationOwner(store)
        before_life = life_owner.read(window, life_fields())
        before_state = state_owner.read(window, state_fields())
        db.execute("INSERT INTO runtime_metadata VALUES (?, 'good_life')", (OWNER_KEY,))
        db.execute("UPDATE configuration_values SET value_json='false' WHERE field_id='role.state.typing_enabled'")
        db.execute("UPDATE configuration_values SET value_json='1000' WHERE field_id='role.life.reading_chunk_chars'")
        after_life = life_owner.read(window, life_fields())
        after_state = state_owner.read(window, state_fields())
        self.assertEqual({key: value.value for key, value in before_life.fields.items()},
                         {key: value.value for key, value in after_life.fields.items()})
        self.assertEqual({key: value.value for key, value in before_state.fields.items()},
                         {key: value.value for key, value in after_state.fields.items()})
        self.assertEqual(after_life.fields["role.life.diary_style_prompt"].value, "my diary style")
        plan = build_plan(life_owner, domain="life", target=window, fields=life_fields(),
            changes={"role.life.reading_chunk_chars": 2900, "role.life.diary_style_prompt": "revised"},
            reset=(), observed_at=datetime.now(timezone.utc))
        planned_life = {change["field_id"]: change["value_json"] for target in plan["life_targets"]
                        for change in target["changes"]}
        planned_host = {change["field_id"]: change["value_json"] for target in plan["host_targets"]
                        for change in target["changes"]}
        self.assertEqual(planned_life["role.life.reading_chunk_chars"], "2900")
        self.assertEqual(planned_host["role.life.diary_style_prompt"], '"revised"')
        state_plan = build_plan(state_owner, domain="state", target=window, fields=state_fields(),
            changes={"role.state.typing_enabled": False}, reset=(), observed_at=datetime.now(timezone.utc))
        self.assertEqual(state_plan["life_targets"][0]["changes"],
                         [{"field_id": "role.state.typing_enabled", "value_json": "false"}])
        self.assertFalse(state_plan["host_targets"])
        store._life_configuration_lease = None
        with self.assertRaises(TaskError):
            life_owner.read(window, life_fields())

    def test_unknown_fields_and_unconfirmed_changes_fail_before_destination_creation(self):
        with closing(sqlite3.connect(self.source)) as db:
            db.execute("INSERT INTO configuration_values VALUES ('role', 'role.life.future_setting', '1', 1)")
            db.commit()
        with self.assertRaisesRegex(ValueError, "unclassified"):
            self.stage(destination=self.target)
        self.assertFalse(self.target.exists())
        with closing(sqlite3.connect(self.source)) as db:
            db.execute("DELETE FROM configuration_values WHERE field_id='role.life.future_setting'")
            db.execute("UPDATE operations SET state='unknown'")
            db.commit()
        with self.assertRaisesRegex(ValueError, "unconfirmed"):
            self.stage(destination=self.target)
        self.assertFalse(self.target.exists())

    def test_completed_change_with_unknown_commit_state_is_rejected(self):
        with closing(sqlite3.connect(self.source)) as db:
            db.execute("UPDATE operations SET commit_state='unknown'")
            db.commit()
        with self.assertRaisesRegex(ValueError, "unconfirmed"):
            self.stage(destination=self.target)
        self.assertFalse(self.target.exists())

    def test_changed_source_and_interruption_do_not_silently_overwrite(self):
        with patch.object(configuration_import, "_staged_rows", side_effect=RuntimeError("interrupted")):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                self.stage(destination=self.target)
        self.assertEqual(self.stage(destination=self.target)["state"], "staged")
        with closing(sqlite3.connect(self.source)) as db:
            db.execute("UPDATE configuration_values SET value_json='false' WHERE field_id='role.life.enabled'")
            db.commit()
        with self.assertRaisesRegex(ValueError, "source changed"):
            self.stage(destination=self.target)


if __name__ == "__main__":
    unittest.main()
