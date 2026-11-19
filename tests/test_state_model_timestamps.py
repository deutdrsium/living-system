"""Profile observation anchors and lossless timestamp adaptation after import."""
from contextlib import closing
from datetime import timedelta, timezone
import json
from pathlib import Path
import sqlite3

from test_state_model_support import NOW, StateAccess, StateModelCase
from good_life.state_import import stage_state
from good_life.state_model import ShadowState
from good_life.state_model import models
from good_life.storage import LifeStorage


class InitialProfileTimeTests(StateModelCase):
    def test_profile_without_calculation_time_or_from_a_later_time_is_rejected_atomically(self):
        with self.storage.transaction() as db:
            self.add_role(db, "new-profile")
        for profile in ({"sleep_pressure": 70, "sleep_debt_minutes": 90},
                        {"sleep_pressure": 70, "sleep_debt_minutes": 90,
                         "observed_at": (NOW + timedelta(hours=3)).isoformat()},
                        {"sleep_pressure": 70, "observed_at": NOW.replace(tzinfo=None).isoformat()}):
            with self.subTest(profile=profile):
                before = tuple(self.storage._connection.iterdump())
                with self.assertRaises(ValueError):
                    self.model.initialize("new-profile", at=NOW, sleep_profile=profile)
                self.assertEqual(tuple(self.storage._connection.iterdump()), before)

    def test_profile_at_initial_observation_advances_elapsed_awake_time_exactly_once(self):
        from good_life.sleep_physiology import sleep_profile
        # This is the actual legacy calculator: its `now` must become the
        # profile's observed_at when an adapter hands it to the new model.
        episode = {"activity_id": "night", "state": "completed", "started_at": (NOW - timedelta(hours=8)).isoformat(),
                   "updated_at": NOW.isoformat(), "sleep": {"slept_seconds": 28800, "kind": "night",
                   "base_need_minutes": 480, "target_minutes": 480, "quality_min": 80, "quality_max": 90}, "work": None}
        profile = dict(sleep_profile((episode,), workload_score=0, now=NOW), observed_at=NOW.isoformat())
        with self.storage.transaction() as db:
            self.add_role(db, "anchored")
        self.model.initialize("anchored", at=NOW, sleep_profile=profile)
        actual = self.model.advance("anchored", to=NOW + timedelta(hours=3))
        initial, _ = models.initialize(self.legacy, profile, NOW, self.parameters)
        expected = models.advance(initial, 10800, {"sleeping": False, "resting": False, "load": 0},
                                  self.parameters, self.config.switches)
        self.assertAlmostEqual(actual["values"]["sleep.pressure"], expected["sleep.pressure"], places=14)
        later = dict(sleep_profile((episode,), workload_score=0, now=NOW + timedelta(hours=3)),
                     observed_at=(NOW + timedelta(hours=3)).isoformat())
        self.assertGreater(later["sleep_pressure"], profile["sleep_pressure"])
        with self.assertRaises(ValueError):
            models.initialize(self.legacy, later, NOW, self.parameters)


class ImportedTimestampTests(StateModelCase):
    def import_source(self, directory, text):
        source = directory / "source.sqlite3"
        raw = json.dumps(dict(self.legacy, updated_at=text), ensure_ascii=False)
        storage_key = self.access.bindings["role"]
        with closing(sqlite3.connect(source)) as db:
            db.executescript("""
                CREATE TABLE runtime_metadata(key TEXT PRIMARY KEY,value TEXT);
                INSERT INTO runtime_metadata VALUES('schema_version','state-import-fixture-v1');
                CREATE TABLE operations(operation_id TEXT PRIMARY KEY,owner TEXT,state TEXT,commit_state TEXT);
                INSERT INTO operations VALUES('op','role_state','completed','committed');
                CREATE TABLE role_state_records(owner_key TEXT,record_id TEXT,revision INTEGER,updated_at TEXT,record_json TEXT);
                CREATE TABLE role_state_history(owner_key TEXT,record_id TEXT,revision INTEGER,operation_id TEXT,record_json TEXT);
                CREATE TABLE role_state_receipts(operation_id TEXT,owner_key TEXT,record_id TEXT,result_json TEXT);
                CREATE TABLE role_state_schedules(context_key TEXT,context_json TEXT,settings_json TEXT,next_at TEXT,
                    revision INTEGER,enabled INTEGER,result_json TEXT);
            """)
            db.execute("INSERT INTO role_state_records VALUES (?,'current',7,?,?)", (storage_key, text, raw))
            db.execute("INSERT INTO role_state_history VALUES (?,'current',7,'op',?)", (storage_key, raw))
            db.execute("INSERT INTO role_state_receipts VALUES ('op',?,'current','{}')", (storage_key,))
            db.commit()
        destination = directory / "imported"
        self.assertEqual(stage_state(source, expected_version="state-import-fixture-v1", destination=destination)["state"], "staged")
        return LifeStorage.open(destination), raw

    def test_imported_aware_and_naive_utc_timestamps_remain_unchanged_in_legacy_tables(self):
        formats = (NOW.isoformat(), NOW.astimezone(timezone(timedelta(hours=8))).isoformat(),
                   NOW.isoformat().replace("+00:00", "Z"), NOW.replace(tzinfo=None).isoformat(),
                   NOW.replace(tzinfo=None).isoformat(sep=" "))
        for index, text in enumerate(formats):
            with self.subTest(text=text):
                directory = Path(self.temp.name) / f"format-{index}"
                directory.mkdir()
                storage, raw = self.import_source(directory, text)
                try:
                    model = ShadowState(storage, config=self.config, access=self.access)
                    initial = model.initialize("role", at=NOW)
                    self.assertEqual(models.moment(initial["at"]), NOW)
                    snapshot = model.snapshot("role")
                    self.assertEqual(snapshot.values["legacy.energy"].updated_at, NOW)
                    naive = index >= 3
                    self.assertEqual(initial["projection"]["timestamp_assumption"] is not None, naive)
                    if naive:
                        self.assertIn("legacy imported timestamp without offset interpreted as UTC", initial["assumptions"])
                        self.assertIn("legacy imported timestamp without offset interpreted as UTC",
                                      snapshot.values["legacy.energy"].assumptions.assumptions)
                    model.advance("role", to=NOW + timedelta(hours=1))
                    self.assertEqual(tuple(storage._connection.execute(
                        "SELECT updated_at,record_json FROM staged_role_state_records").fetchone()), (text, raw))
                    model.close()
                finally:
                    storage.close()

    def test_new_commands_still_reject_naive_times(self):
        with self.assertRaises(ValueError):
            self.model.observe_interval("role", input_id="naive-start", start=NOW.replace(tzinfo=None),
                source="actual-start", instance_id="sleep", sleeping=True)
        with self.assertRaises(ValueError):
            self.model.advance("role", to=NOW.replace(tzinfo=None))
