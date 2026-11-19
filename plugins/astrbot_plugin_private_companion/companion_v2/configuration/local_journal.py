"""Private undo records, committed with the owning configuration receipt."""


def exists(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='lsr_config_undo'").fetchone() is not None


def append(db, *, change_id, target_key, field_id, old_json, revision, changed_at, reverts):
    db.execute("""CREATE TABLE IF NOT EXISTS lsr_config_undo (
        sequence INTEGER PRIMARY KEY, change_id TEXT NOT NULL UNIQUE,
        target_key TEXT NOT NULL, field_id TEXT NOT NULL, old_json TEXT,
        revision INTEGER NOT NULL, changed_at TEXT NOT NULL, reverts TEXT)""")
    db.execute("""INSERT INTO lsr_config_undo
        (change_id, target_key, field_id, old_json, revision, changed_at, reverts)
        VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (change_id, target_key, field_id, old_json, revision, changed_at, reverts))
    db.execute("DELETE FROM lsr_config_undo WHERE sequence <= (SELECT MAX(sequence) - 1000 FROM lsr_config_undo)")


def old_value(row):
    if row is None:
        return None
    if len(row.encode("utf-8")) > 6 * 32768:
        from .models import ConfigurationError
        raise ConfigurationError("old_value_too_large", "previous value exceeds the undo limit")
    return row
