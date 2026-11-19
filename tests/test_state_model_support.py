"""A6 isolated migrated records and explicitly opted-in simulation fixtures."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/astrbot_plugin_good_life"))
from good_life.state_model import Parameters, ShadowConfig, ShadowState
from good_life.storage import LifeStorage

NOW = datetime(2026, 11, 16, 8, tzinfo=timezone.utc)
EXAMPLE = ROOT / "plugins/astrbot_plugin_good_life/good_life/state_model/example-parameters.json"


def parameters(**sections):
    data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    for name, values in sections.items():
        data[name].update(values)
    return Parameters.from_dict(data)


class StateAccess:
    """Host-verified bindings, deliberately distinct from legacy body keys."""
    def __init__(self):
        self.bindings = {}
        self.allowed = True

    def authorize(self, owner, action):
        return self.allowed and owner in self.bindings

    def storage_key(self, owner):
        return self.bindings[owner]


class StateModelCase(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix=".state-model-test-", dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.storage = LifeStorage.open(self.temp.name)
        self.addCleanup(lambda: self.storage.close())
        self.parameters = parameters()
        self.access = StateAccess()
        self.config = ShadowConfig(enabled=True, sleep=True, hunger=True, energy=True, parameters=self.parameters)
        self.legacy = {"revision": 7, "updated_at": NOW.isoformat(),
                       "values": {"energy": 37, "hunger": "有点饿", "sleep": "有些困", "mood": "安静",
                                  "health": "良好", "conditions": []}, "sources": [{"source_id": "kept-source"}]}
        with self.storage.transaction() as db:
            db.execute("""CREATE TABLE staged_role_state_records (owner_key TEXT,record_id TEXT,
                revision INTEGER,updated_at TEXT,record_json TEXT,PRIMARY KEY(owner_key,record_id))""")
            self.add_role(db, "role")
        self.model = ShadowState(self.storage, config=self.config, access=self.access)
        self.initial = self.model.initialize("role", at=NOW,
            sleep_profile={"sleep_pressure": 42, "sleep_debt_minutes": 90, "observed_at": NOW.isoformat()})

    def add_role(self, db, owner):
        storage_key = "verified-body-key-for-" + owner
        self.access.bindings[owner] = storage_key
        db.execute("INSERT INTO staged_role_state_records VALUES (?,'current',7,?,?)",
                   (storage_key, NOW.isoformat(), json.dumps(self.legacy, ensure_ascii=False)))

    def interval(self, ident, start, end, **mode):
        return self.model.observe_interval("role", input_id=ident,
            start=NOW + timedelta(seconds=start), end=NOW + timedelta(seconds=end),
            source="confirmed-activity-revision", instance_id=ident, end_reason="completed", **mode)

    def meal(self, ident, seconds, fraction=1, **options):
        return self.model.consume("role", effect_id=ident, effective_at=NOW + timedelta(seconds=seconds),
            kind=options.get("kind", "meal.consumed"), fraction=fraction,
            source="verified-consumption", instance_id=options.get("instance_id", "meal-instance"))

    def reopen(self):
        self.model.close()
        self.storage.close()
        self.storage = LifeStorage.open(self.temp.name)
        self.model = ShadowState(self.storage, config=self.config, access=self.access)

    def legacy_rows(self):
        return tuple(tuple(row) for row in self.storage._connection.execute("SELECT * FROM staged_role_state_records"))

    def count(self, table):
        return self.storage._connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
