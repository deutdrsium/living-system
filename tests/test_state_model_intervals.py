"""Actual open phases, engine event splitting, cap boundaries and v1 upgrades."""
from dataclasses import replace
from datetime import timedelta
import random
import sqlite3

from test_state_model_support import NOW, StateModelCase, parameters
from good_life.state_model import ShadowState
from good_life.state_model import models
from good_life.state_model.repository import DDL


class OpenIntervalTests(StateModelCase):
    def open_sleep(self, ident, seconds):
        return self.model.observe_interval("role", input_id=ident, start=NOW + timedelta(seconds=seconds),
            sleeping=True, source="confirmed-start", instance_id=ident)

    def close_sleep(self, ident, seconds, reason="natural_wake"):
        return self.model.close_interval("role", input_id=ident, end=NOW + timedelta(seconds=seconds),
            end_reason=reason, source="confirmed-end")

    def reference(self, intervals, seconds):
        with self.storage.transaction() as db:
            self.add_role(db, "reference")
        self.model.initialize("reference", at=NOW, sleep_profile={
            "sleep_pressure": 42, "sleep_debt_minutes": 90, "observed_at": NOW.isoformat()})
        for ident, start, end, sleeping in intervals:
            self.model.observe_interval("reference", input_id=ident, start=NOW + timedelta(seconds=start),
                end=NOW + timedelta(seconds=end), sleeping=sleeping,
                source="confirmed", instance_id=ident, end_reason="completed")
        return self.model.advance("reference", to=NOW + timedelta(seconds=seconds))

    def assert_values_match(self, actual, expected):
        for key, value in actual["values"].items():
            self.assertAlmostEqual(value, expected["values"][key], places=12, msg=key)

    def test_ongoing_night_advances_as_sleep_then_closes_after_restart(self):
        hour = 3600
        self.interval("awake", 0, 15 * hour)
        before_sleep = self.model.advance("role", to=NOW + timedelta(hours=15))
        self.open_sleep("night", 15 * hour)  # 23:00, no planned end registered.
        at_two = self.model.advance("role", to=NOW + timedelta(hours=18))
        self.assertLess(at_two["values"]["sleep.pressure"], before_sleep["values"]["sleep.pressure"])
        self.assertGreater(at_two["values"]["energy"], before_sleep["values"]["energy"])
        self.reopen()
        self.model.advance("role", to=NOW + timedelta(hours=21))
        self.close_sleep("night", 23 * hour)  # Actual 07:00 wake.
        actual = self.model.advance("role", to=NOW + timedelta(hours=24))
        expected = self.reference((("awake", 0, 15 * hour, False),
                                   ("night", 15 * hour, 23 * hour, True)), 24 * hour)
        self.assert_values_match(actual, expected)
        self.assertFalse(self.open_sleep("night", 15 * hour))  # Original start retry after close.
        self.assertFalse(self.close_sleep("night", 23 * hour))
        with self.assertRaises(ValueError):
            self.close_sleep("night", 22 * hour)
        item = next(item for item in self.model.repo.intervals(self.storage._connection, "role") if item["input_id"] == "night")
        self.assertEqual(item["end_reason"], "natural_wake")
        self.assertEqual(item["close_source"], "confirmed-end")
        self.assertEqual(self.count("life_events"), 0)

    def test_early_message_wake_and_resumed_sleep_exclude_awake_grace(self):
        hour = 3600
        self.interval("awake", 0, 15 * hour)
        self.model.advance("role", to=NOW + timedelta(hours=15))
        self.open_sleep("night-first", 15 * hour)
        self.model.advance("role", to=NOW + timedelta(hours=18))
        self.close_sleep("night-first", 18 * hour, "message_wakeup")
        self.interval("awake-grace", 18 * hour, 19 * hour)
        self.open_sleep("night-resumed", 19 * hour)
        self.model.advance("role", to=NOW + timedelta(hours=20, minutes=13))
        self.close_sleep("night-resumed", 21 * hour, "cancelled")
        actual = self.model.advance("role", to=NOW + timedelta(hours=24))
        expected = self.reference((("awake", 0, 15 * hour, False),
                                   ("night-first", 15 * hour, 18 * hour, True),
                                   ("awake-grace", 18 * hour, 19 * hour, False),
                                   ("night-resumed", 19 * hour, 21 * hour, True)), 24 * hour)
        self.assert_values_match(actual, expected)

    def test_open_overlap_and_late_closes_reject_without_rewriting_history(self):
        self.open_sleep("night", 0)
        with self.assertRaises(ValueError):
            self.interval("conflicting-phase", 100, 200)
        self.model.advance("role", to=NOW + timedelta(seconds=200))
        before = tuple(self.storage._connection.iterdump())
        with self.assertRaises(ValueError):
            self.close_sleep("night", 100)
        self.assertEqual(tuple(self.storage._connection.iterdump()), before)
        self.close_sleep("night", 200, "interrupted")
        self.open_sleep("cancelled-immediately", 200)
        self.close_sleep("cancelled-immediately", 200, "cancelled")
        actual = self.model.advance("role", to=NOW + timedelta(seconds=400))
        expected = self.reference((("night", 0, 200, True),), 400)
        self.assert_values_match(actual, expected)


class EngineStepInvarianceTests(StateModelCase):
    def test_non_aligned_advances_and_cap_replays_match_one_complete_engine_advance(self):
        end = 48 * 3600
        intervals = [(113, 5503, False, False, .7), (5503, 28711, True, False, 0),
                     (28711, 29603, False, False, 0), (29603, 36787, True, False, 0),
                     (36787, 49003, False, False, 1), (50231, 55813, False, True, 0),
                     (61234, 63111, True, False, 0), (63111, 88777, False, False, .2),
                     (90101, 119003, True, False, 0), (119003, 141001, False, False, .4),
                     (144113, end, True, False, 0)]
        meals = [(250.25, .25, "meal.consumed"), (5010.5, .5, "meal.consumed"),
                 (45001.75, .75, "meal.consumed"), (80011.125, 1, "drink.consumed"),
                 (130031.2, .5, "meal.consumed"), (130031.2, 1, "drink.consumed")]
        capped_config = replace(self.config, parameters=parameters(offline={"max_advance_s": 7513}))
        stepped = ShadowState(self.storage, config=capped_config, access=self.access)
        with self.storage.transaction() as db:
            self.add_role(db, "stepped")
        stepped.initialize("stepped", at=NOW, sleep_profile={
            "sleep_pressure": 42, "sleep_debt_minutes": 90, "observed_at": NOW.isoformat()})
        for model, owner in ((self.model, "role"), (stepped, "stepped")):
            for index, (left, right, sleeping, resting, load) in enumerate(intervals):
                model.observe_interval(owner, input_id=f"phase-{index}", start=NOW + timedelta(seconds=left),
                    end=NOW + timedelta(seconds=right), sleeping=sleeping, resting=resting, load=load,
                    source="verified-phase", instance_id=f"instance-{index}", end_reason="completed")
            for index, (seconds, fraction, kind) in enumerate(meals):
                model.consume(owner, effect_id=f"portion-{index}", effective_at=NOW + timedelta(seconds=seconds),
                    kind=kind, fraction=fraction, source="verified-intake", instance_id=f"meal-{index}")
        expected = self.model.advance("role", to=NOW + timedelta(seconds=end))
        self.assertFalse(expected["capped"])
        rng = random.Random(20261116)  # Test tick schedule only; body models have no randomness.
        request, cap_hits, advances = 0, 0, 0
        while request < end:
            request = min(end, request + rng.randint(1973, 19013) + .123456)
            target = NOW + timedelta(seconds=request)
            while True:
                actual = stepped.advance("stepped", to=target)
                advances += 1
                cap_hits += int(actual["capped"])
                if not actual["capped"]:
                    break
        self.assertGreater(cap_hits, 0)
        difference = max(abs(actual["values"][key] - expected["values"][key]) for key in expected["values"])
        print(f"A6 engine 48h non-aligned/capped: max_abs_error={difference:.12g}; "
              f"advances={advances}; cap_hits={cap_hits}; tolerance=1e-10")
        self.assertLess(difference, 1e-10)
        self.assertEqual(models.moment(actual["at"]), NOW + timedelta(seconds=end))
        effects = tuple(self.storage._connection.execute(
            "SELECT effect_id,applied FROM life_state_model_effects WHERE owner_key='stepped' ORDER BY effect_id"))
        self.assertEqual([row[1] for row in effects], [1] * len(meals))
        self.assertEqual(stepped.advance("stepped", to=NOW + timedelta(seconds=end)), actual)


class OpenIntervalMigrationTests(StateModelCase):
    def make_v1(self):
        self.interval("old-night", 0, 3600, sleeping=True)
        self.meal("old-portion", 7200, .5)
        with self.storage.transaction() as db:
            db.execute("DROP INDEX life_state_model_boundaries")
            db.execute("ALTER TABLE life_state_model_intervals RENAME TO life_state_model_intervals_fixture")
            db.execute("""CREATE TABLE life_state_model_intervals (
                owner_key TEXT NOT NULL, input_id TEXT NOT NULL, content_hash TEXT NOT NULL,
                start_at TEXT NOT NULL, end_at TEXT NOT NULL, record_json TEXT NOT NULL,
                PRIMARY KEY(owner_key,input_id))""")
            db.execute("INSERT INTO life_state_model_intervals SELECT * FROM life_state_model_intervals_fixture")
            db.execute("DROP TABLE life_state_model_intervals_fixture")
            db.execute(DDL[3])
            db.execute("UPDATE life_metadata SET value='1' WHERE key='state_model_schema_version'")

    def test_v1_upgrade_preserves_existing_states_inputs_effects_and_request_hashes(self):
        self.make_v1()
        tables = ("life_state_model_heads", "life_state_model_history", "life_state_model_intervals", "life_state_model_effects")
        before = {table: tuple(tuple(row) for row in self.storage._connection.execute("SELECT * FROM " + table)) for table in tables}
        legacy = self.legacy_rows()
        self.model.repo.install()
        for table in tables:
            self.assertEqual(tuple(tuple(row) for row in self.storage._connection.execute("SELECT * FROM " + table)), before[table])
        self.assertEqual(self.storage._connection.execute(
            "SELECT value FROM life_metadata WHERE key='state_model_schema_version'").fetchone()[0], "2")
        self.assertFalse(self.interval("old-night", 0, 3600, sleeping=True))
        self.assertFalse(self.meal("old-portion", 7200, .5))
        self.assertEqual(self.legacy_rows(), legacy)
        self.reopen()
        self.model.observe_interval("role", input_id="new-open", start=NOW + timedelta(hours=3),
            sleeping=True, source="start", instance_id="new-open")

    def test_v1_upgrade_failure_rolls_back_schema_rows_and_version(self):
        self.make_v1()
        before = tuple(self.storage._connection.iterdump())
        db = self.storage._connection
        def deny_drop_v1(action, name, *_):
            return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_DROP_TABLE and name == "life_state_model_intervals_v1" else sqlite3.SQLITE_OK
        db.set_authorizer(deny_drop_v1)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                self.model.repo.install()
        finally:
            db.set_authorizer(None)
        self.assertEqual(tuple(db.iterdump()), before)
