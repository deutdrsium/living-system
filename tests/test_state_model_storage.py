from dataclasses import replace
from datetime import timedelta
import json
import sqlite3
import unittest
from unittest.mock import patch

from test_state_model_support import NOW, StateModelCase, parameters
from good_life.contracts.models import StateQuality
from good_life.state_model import ShadowConfig, ShadowState
from good_life.state_model.models import Domain, owners
from good_life.state_model.repository import ShadowRepository


class BlackboardMigrationTests(StateModelCase):
    def test_initial_values_legacy_source_and_mapping_are_preserved(self):
        values = self.initial["values"]
        self.assertEqual(values["energy"], .37)
        self.assertEqual(values["hunger"], .65)
        self.assertEqual(values["sleep.pressure"], .42)
        self.assertEqual(values["sleep.debt_s"], 5400)
        self.assertEqual(self.initial["seed"]["legacy"], self.legacy)
        self.assertEqual(self.initial["parameter_hash"], self.parameters.content_hash)
        self.assertEqual(self.initial["parameters"]["version"], "example-uncalibrated-v1")
        self.assertTrue(all(not var.expose_to_llm for var in self.model.variables("role")))
        self.assertEqual({var.key: var.owner_module for var in self.model.variables("role")}, owners())
        with self.assertRaises(ValueError):
            owners((Domain("a", ("same",), ()), Domain("b", ("same",), ())))

    def test_unknown_descriptions_and_absent_sleep_history_remain_unavailable(self):
        unknown = dict(self.legacy, values={"energy": None, "hunger": "从未映射的文字", "sleep": "未知", "mood": "安静"})
        with self.storage.transaction() as db:
            self.add_role(db, "other")
            db.execute("UPDATE staged_role_state_records SET record_json=? WHERE owner_key=?",
                       (json.dumps(unknown), self.access.bindings["other"]))
        record = self.model.initialize("other", at=NOW)
        self.assertIsNone(record["values"]["energy"])
        self.model.advance("other", to=NOW + timedelta(hours=1))
        snapshot = self.model.snapshot("other")
        for key in ("energy", "hunger", "sleep.pressure", "sleep.debt_s", "sleep.sleepiness"):
            self.assertEqual(snapshot.values[key].quality, StateQuality.UNAVAILABLE)
        self.assertEqual(snapshot.values["legacy.hunger"].value, "从未映射的文字")

    def test_reads_never_advance_and_snapshot_is_immutable(self):
        before = tuple(self.storage._connection.iterdump())
        for _ in range(5):
            snapshot = self.model.snapshot("role")
            self.assertEqual(snapshot.values["energy"].quality, StateQuality.ESTIMATED)
            self.assertEqual(snapshot.values["legacy.energy"].value, 37)
            self.assertEqual(snapshot.values["legacy.mood"].value, "安静")
        self.assertEqual(tuple(self.storage._connection.iterdump()), before)
        with self.assertRaises(TypeError):
            snapshot.values["energy"] = None
        self.assertFalse(self.model.describe()["expose_to_llm"])

    def test_migration_is_additive_idempotent_and_does_not_change_storage_schema(self):
        before = tuple(self.storage._connection.iterdump())
        ShadowState(self.storage, config=self.config, access=self.access)
        self.model.initialize("role", at=NOW)
        self.assertEqual(tuple(self.storage._connection.iterdump()), before)
        self.assertEqual(self.storage._connection.execute("SELECT value FROM life_metadata WHERE key='schema_version'").fetchone()[0],
                         "good-life.storage.v1")

    def test_delete_shadow_data_returns_exact_legacy_and_outbox_rows(self):
        before = self.legacy_rows()
        self.meal("half", 300, .5)
        self.model.advance("role", to=NOW + timedelta(hours=1))
        self.assertEqual(self.legacy_rows(), before)
        self.assertEqual(self.count("life_events"), 0)
        self.assertEqual(self.count("life_event_deliveries"), 0)
        self.model.close()
        with self.storage.transaction() as db:
            names = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name GLOB 'life_state_model_*'")]
            for name in names:
                db.execute("DROP TABLE " + name)
            db.execute("DELETE FROM life_metadata WHERE key='state_model_schema_version'")
        self.assertEqual(self.legacy_rows(), before)
        self.assertIsNone(ShadowState(self.storage, access=self.access).snapshot("role"))

    def test_disabled_does_not_migrate_and_per_model_switches_are_off_by_default(self):
        self.assertEqual(ShadowConfig().switches, {"sleep": False, "hunger": False, "energy": False})
        before = tuple(self.storage._connection.iterdump())
        disabled = ShadowState(self.storage, access=self.access)
        self.assertEqual(tuple(self.storage._connection.iterdump()), before)
        snapshot = disabled.snapshot("role")
        self.assertEqual(snapshot.values["energy"].quality, StateQuality.UNAVAILABLE)
        self.assertEqual(snapshot.values["legacy.energy"].value, 37)
        with self.assertRaises(ValueError):
            disabled.advance("role", to=NOW)
        self.assertEqual(tuple(self.storage._connection.iterdump()), before)

    def test_models_can_be_individually_enabled_without_becoming_live_owners(self):
        with self.storage.transaction() as db:
            self.add_role(db, "hunger-only")
        partial = ShadowState(self.storage, config=ShadowConfig(enabled=True, hunger=True, parameters=self.parameters), access=self.access)
        partial.initialize("hunger-only", at=NOW)
        partial.advance("hunger-only", to=NOW + timedelta(hours=1))
        snapshot = partial.snapshot("hunger-only")
        self.assertEqual(snapshot.values["energy"].quality, StateQuality.UNAVAILABLE)
        self.assertEqual(snapshot.values["sleep.pressure"].quality, StateQuality.UNAVAILABLE)
        self.assertGreater(snapshot.values["hunger"].value, .65)
        self.assertEqual(snapshot.values["legacy.energy"].value, 37)

    def test_roles_are_isolated_and_initialization_does_not_reset_existing_trajectory(self):
        with self.storage.transaction() as db:
            self.add_role(db, "second")
        self.model.initialize("second", at=NOW)
        final = self.model.advance("role", to=NOW + timedelta(hours=1))
        self.assertEqual(self.model.initialize("role", at=NOW), final)
        self.assertEqual(self.model.repo.read("second")["values"]["energy"], .37)

    def test_parameters_and_switches_are_pinned_across_restart(self):
        self.model.advance("role", to=NOW + timedelta(hours=1))
        for config in (replace(self.config, parameters=parameters(sleep={"tau_awake_s": 99999})),
                       replace(self.config, energy=False)):
            other = ShadowState(self.storage, config=config, access=self.access)
            before = tuple(self.storage._connection.iterdump())
            with self.assertRaises(ValueError):
                other.advance("role", to=NOW + timedelta(hours=2))
            self.assertEqual(tuple(self.storage._connection.iterdump()), before)

    def test_future_and_incomplete_schema_are_rejected_without_mutation(self):
        with self.storage.transaction() as db:
            db.execute("UPDATE life_metadata SET value='99' WHERE key='state_model_schema_version'")
        before = tuple(self.storage._connection.iterdump())
        with self.assertRaises(ValueError):
            ShadowRepository(self.storage).install()
        self.assertEqual(tuple(self.storage._connection.iterdump()), before)
        with self.storage.transaction() as db:
            db.execute("UPDATE life_metadata SET value='2' WHERE key='state_model_schema_version'")
            db.execute("DROP INDEX life_state_model_due")
        with self.assertRaises(ValueError):
            ShadowRepository(self.storage).install()

    def test_bad_parameters_and_invalid_initial_time_do_not_silently_default(self):
        for changes in ({"tau_awake_s": 0}, {"period_s": -1}, {"amplitude": float("nan")}, {"phase_at": "2026-01-01"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                parameters(sleep=changes)
        with self.assertRaises(ValueError):
            ShadowConfig(enabled=True)
        with self.assertRaises(ValueError):
            self.model.advance("role", to=NOW.replace(tzinfo=None))
        with self.storage.transaction() as db:
            self.add_role(db, "wrong-time")
        with self.assertRaises(ValueError):
            self.model.initialize("wrong-time", at=NOW + timedelta(hours=1))


class ReviewRegressionTests(StateModelCase):
    def test_state_quality_shapes_and_explicit_legacy_identity_binding(self):
        snapshot = self.model.snapshot("role")
        self.assertEqual(snapshot.owner_key, "role")
        self.assertEqual(self.initial["legacy_storage_key"], self.access.bindings["role"])
        self.assertNotEqual(snapshot.owner_key, self.initial["legacy_storage_key"])
        for value in snapshot.values.values():
            if value.quality in (StateQuality.UNKNOWN, StateQuality.UNAVAILABLE):
                self.assertIsNone(value.value)
            if value.quality == StateQuality.ESTIMATED:
                self.assertTrue(value.assumptions.assumptions)
            else:
                self.assertIsNone(value.assumptions)
        with self.assertRaises(ValueError):
            self.model.snapshot(self.access.bindings["role"])
        self.access.allowed = False
        with self.assertRaises(ValueError):
            self.model.advance("role", to=NOW + timedelta(hours=1))
        with self.assertRaises(ValueError):
            self.model.snapshot("role")

    def test_changed_binding_and_switches_cannot_expose_unadvanced_values(self):
        incompatible = ShadowState(self.storage, config=replace(self.config, energy=False), access=self.access)
        with self.assertRaises(ValueError):
            incompatible.snapshot("role")
        self.access.bindings["role"] = "another-body-key"
        with self.assertRaises(ValueError):
            self.model.snapshot("role")
        with self.assertRaises(ValueError):
            self.model.advance("role", to=NOW + timedelta(hours=1))

    def test_legacy_projection_refresh_is_explicit_preserves_time_and_has_one_local_revision(self):
        old = self.model.snapshot("role")
        updated = dict(self.legacy, revision=8, updated_at=(NOW + timedelta(hours=1)).isoformat(),
                       values=dict(self.legacy["values"], energy=95))
        with self.storage.transaction() as db:
            db.execute("UPDATE staged_role_state_records SET revision=8,updated_at=?,record_json=? WHERE owner_key=?",
                       (updated["updated_at"], json.dumps(updated), self.access.bindings["role"]))
        self.assertEqual(self.model.snapshot("role"), old)
        self.model.advance("role", to=NOW + timedelta(hours=1))
        snapshot = self.model.snapshot("role")
        self.assertEqual(snapshot.values["legacy.energy"].value, 95)
        self.assertEqual(snapshot.values["legacy.energy"].updated_at, NOW + timedelta(hours=1))
        self.assertEqual(snapshot.revision, 2)
        self.assertNotEqual(snapshot.values["energy"].value, .95)
        self.assertEqual(self.model.repo.read("role")["seed"]["legacy"], self.legacy)

    def test_unknown_initial_sleep_debt_becomes_derived_after_one_full_new_window(self):
        with self.storage.transaction() as db:
            self.add_role(db, "no-debt-history")
        self.model.initialize("no-debt-history", at=NOW)
        self.model.observe_interval("no-debt-history", input_id="known-awake-week", start=NOW,
            end=NOW + timedelta(days=7), source="verified-window", instance_id="awake", end_reason="completed")
        end = NOW + timedelta(days=7)
        while self.model.repo.read("no-debt-history")["at"] < end.isoformat(timespec="microseconds"):
            record = self.model.advance("no-debt-history", to=end)
        self.assertEqual(record["values"]["sleep.debt_s"], 201600)

    def test_missing_adapter_never_installs_schema_and_pending_barrier_is_preserved(self):
        with self.assertRaises(ValueError):
            ShadowState(self.storage, config=self.config)
        with self.storage.transaction() as db:
            self.add_role(db, "pending")
            db.execute("CREATE TABLE life_prepared_commands (kind TEXT,state TEXT,payload_json TEXT)")
            db.execute("INSERT INTO life_prepared_commands VALUES ('state.change','committed',?)",
                       (json.dumps({"storage_key": self.access.bindings["pending"]}),))
        from good_life.state_records import StateTransitionPending
        with self.assertRaises(StateTransitionPending):
            self.model.initialize("pending", at=NOW)
        self.assertIsNone(self.model.repo.read("pending"))


class RecoveryAndEffectsTests(StateModelCase):
    def test_restart_and_repeated_effects_do_not_repeat_integration_or_intake(self):
        self.interval("sleep", 0, 7200, sleeping=True)
        self.meal("meal", 3600, .5)
        partial = self.model.advance("role", to=NOW + timedelta(hours=1))
        self.reopen()
        self.assertFalse(self.meal("meal", 3600, .5))
        self.assertFalse(self.interval("sleep", 0, 7200, sleeping=True))
        self.assertEqual(self.model.advance("role", to=NOW + timedelta(hours=1)), partial)
        final = self.model.advance("role", to=NOW + timedelta(hours=2))
        with self.storage.transaction() as db:
            self.add_role(db, "uninterrupted")
        self.model.initialize("uninterrupted", at=NOW, sleep_profile={"sleep_pressure": 42, "sleep_debt_minutes": 90, "observed_at": NOW.isoformat()})
        self.model.observe_interval("uninterrupted", input_id="sleep", start=NOW, end=NOW + timedelta(hours=2),
            sleeping=True, source="confirmed", instance_id="sleep", end_reason="completed")
        self.model.consume("uninterrupted", effect_id="meal", effective_at=NOW + timedelta(hours=1),
            kind="meal.consumed", fraction=.5, source="verified", instance_id="meal")
        uninterrupted = self.model.advance("uninterrupted", to=NOW + timedelta(hours=2))
        for key in final["values"]:
            self.assertAlmostEqual(final["values"][key], uninterrupted["values"][key], places=12)

    def test_offline_cap_preserves_pending_tail_and_never_generates_experiences(self):
        before = self.legacy_rows()
        final = self.model.advance("role", to=NOW + timedelta(days=30))
        self.assertTrue(final["capped"])
        self.assertEqual(final["at"], (NOW + timedelta(days=2)).isoformat(timespec="microseconds"))
        self.assertEqual(final["requested_at"], (NOW + timedelta(days=30)).isoformat(timespec="microseconds"))
        self.assertIn("unknown intervals use estimated awake mode with no invented intake or sleep", final["assumptions"])
        self.assertEqual(self.legacy_rows(), before)
        self.assertEqual(self.count("life_events"), 0)
        self.assertEqual(self.count("life_state_model_intervals"), 0)
        self.assertEqual(self.count("life_state_model_effects"), 0)
        self.reopen()
        self.assertEqual(self.model.repo.read("role"), final)
        with self.assertRaises(ValueError):
            self.model.advance("role", to=NOW)

    def test_effect_ids_are_owner_scoped_and_conflicts_reject_without_changes(self):
        self.meal("portion", 0, .5)
        before = tuple(self.storage._connection.iterdump())
        self.assertFalse(self.meal("portion", 0, .5))
        with self.assertRaises(ValueError):
            self.meal("portion", 0, .25)
        self.assertEqual(tuple(self.storage._connection.iterdump()), before)
        with self.storage.transaction() as db:
            self.add_role(db, "second")
        self.model.initialize("second", at=NOW)
        self.assertTrue(self.model.consume("second", effect_id="portion", effective_at=NOW,
            kind="meal.consumed", fraction=.5, source="verified-consumption", instance_id="meal-instance"))

    def test_late_invalid_and_overlapping_inputs_are_rejected(self):
        self.interval("first", 0, 100, sleeping=True)
        with self.assertRaises(ValueError):
            self.interval("overlap", 50, 150)
        with self.assertRaises(ValueError):
            self.interval("first", 0, 200, sleeping=True)
        with self.assertRaises(ValueError):
            self.interval("conflicting", 200, 300, sleeping=True, load=1)
        with self.assertRaises(ValueError):
            self.meal("too-much", 0, 1.5)
        self.model.advance("role", to=NOW + timedelta(seconds=200))
        with self.assertRaises(ValueError):
            self.meal("late", 100)
        with self.assertRaises(ValueError):
            self.interval("late", 100, 300)

    def test_commit_failure_rolls_back_state_clock_history_and_effect_dedup(self):
        self.meal("at-end", 3600, .5)
        before = tuple(self.storage._connection.iterdump())
        original = self.model.repo.save
        def fail_after_state_saved(db, record, kind):
            original(db, record, kind)
            raise RuntimeError("injected crash before effect ACK")
        with patch.object(self.model.repo, "save", side_effect=fail_after_state_saved):
            with self.assertRaisesRegex(RuntimeError, "injected crash"):
                self.model.advance("role", to=NOW + timedelta(hours=1))
        self.assertEqual(tuple(self.storage._connection.iterdump()), before)
        final = self.model.advance("role", to=NOW + timedelta(hours=1))
        self.assertAlmostEqual(final["values"]["hunger"], .65 + .00002 * 3600 - .3)
        self.assertEqual(self.count("life_state_model_history"), 2)

    def test_closed_model_cannot_advance_and_retains_unavailable_snapshot(self):
        before = tuple(self.storage._connection.iterdump())
        self.model.close()
        with self.assertRaises(ValueError):
            self.model.advance("role", to=NOW + timedelta(hours=1))
        self.assertEqual(self.model.snapshot("role").values["energy"].quality, StateQuality.UNAVAILABLE)
        self.assertEqual(tuple(self.storage._connection.iterdump()), before)
