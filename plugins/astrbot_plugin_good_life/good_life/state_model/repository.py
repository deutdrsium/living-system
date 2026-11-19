"""Additive shadow storage on LifeStorage; never write migrated state or outbox."""
import json

from ..storage import StorageReader
from .config import require

VERSION_KEY = "state_model_schema_version"
SCHEMA_VERSION = "2"
DDL = (
    """CREATE TABLE life_state_model_heads (
        owner_key TEXT PRIMARY KEY, legacy_storage_key TEXT NOT NULL UNIQUE,
        revision INTEGER NOT NULL CHECK(revision>=1),
        last_advanced_at TEXT NOT NULL, record_json TEXT NOT NULL)""",
    """CREATE TABLE life_state_model_history (
        owner_key TEXT NOT NULL, revision INTEGER NOT NULL, kind TEXT NOT NULL,
        record_json TEXT NOT NULL, PRIMARY KEY(owner_key,revision))""",
    """CREATE TABLE life_state_model_intervals (
        owner_key TEXT NOT NULL, input_id TEXT NOT NULL, content_hash TEXT NOT NULL,
        start_at TEXT NOT NULL, end_at TEXT, record_json TEXT NOT NULL,
        PRIMARY KEY(owner_key,input_id))""",
    """CREATE INDEX life_state_model_boundaries ON life_state_model_intervals(owner_key,start_at,end_at)""",
    """CREATE TABLE life_state_model_effects (
        owner_key TEXT NOT NULL, effect_id TEXT NOT NULL, content_hash TEXT NOT NULL,
        effective_at TEXT NOT NULL, applied INTEGER NOT NULL CHECK(applied IN (0,1)),
        record_json TEXT NOT NULL, PRIMARY KEY(owner_key,effect_id))""",
    """CREATE INDEX life_state_model_due ON life_state_model_effects(owner_key,applied,effective_at)""",
)


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class ShadowRepository:
    def __init__(self, storage):
        self.storage, self.reader = storage, StorageReader(storage)

    def install(self):
        with self.storage.transaction() as db:
            row = db.execute("SELECT value FROM life_metadata WHERE key=?", (VERSION_KEY,)).fetchone()
            objects = {row[0]: row[1] for row in db.execute(
                "SELECT name,sql FROM sqlite_master WHERE name GLOB 'life_state_model_*' AND sql IS NOT NULL")}
            if row is None:
                require(not objects, "unclaimed state model schema")
                for statement in DDL:
                    db.execute(statement)
                db.execute("INSERT INTO life_metadata VALUES (?,?)", (VERSION_KEY, SCHEMA_VERSION))
            else:
                require(row[0] in ("1", SCHEMA_VERSION), "unsupported state model schema")
                previous = tuple(statement.replace("end_at TEXT,", "end_at TEXT NOT NULL,") for statement in DDL)
                expected = {statement.split()[2]: " ".join(statement.split()).lower()
                            for statement in (previous if row[0] == "1" else DDL)}
                require({key: " ".join(value.split()).lower() for key, value in objects.items()} == expected,
                        "incomplete or changed state model schema")
                if row[0] == "1":
                    # Rebuild only our interval table to allow an unknown end.
                    # Copy every old row/hash unchanged, in the same transaction
                    # as the version marker; failed upgrades preserve v1 intact.
                    db.execute("DROP INDEX life_state_model_boundaries")
                    db.execute("ALTER TABLE life_state_model_intervals RENAME TO life_state_model_intervals_v1")
                    db.execute(DDL[2])
                    db.execute("INSERT INTO life_state_model_intervals SELECT * FROM life_state_model_intervals_v1")
                    db.execute("DROP TABLE life_state_model_intervals_v1")
                    db.execute(DDL[3])
                    db.execute("UPDATE life_metadata SET value=? WHERE key=?", (SCHEMA_VERSION, VERSION_KEY))

    @staticmethod
    def load(db, owner):
        row = db.execute("SELECT record_json FROM life_state_model_heads WHERE owner_key=?", (owner,)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def save(db, record, kind):
        raw = encode(record)
        db.execute("""INSERT INTO life_state_model_heads VALUES (?,?,?,?,?)
            ON CONFLICT(owner_key) DO UPDATE SET revision=excluded.revision,
            last_advanced_at=excluded.last_advanced_at,record_json=excluded.record_json""",
            (record["owner_key"], record["legacy_storage_key"], record["revision"], record["at"], raw))
        db.execute("INSERT INTO life_state_model_history VALUES (?,?,?,?)",
                   (record["owner_key"], record["revision"], kind, raw))

    @staticmethod
    def intervals(db, owner):
        return [json.loads(row[0]) for row in db.execute(
            "SELECT record_json FROM life_state_model_intervals WHERE owner_key=? ORDER BY start_at,input_id", (owner,))]

    def read(self, owner):
        with self.reader.locked():
            if self.reader.one("SELECT 1 FROM sqlite_master WHERE name='life_state_model_heads'", ()) is None:
                return None
            return self.load(self.storage._connection, owner)
