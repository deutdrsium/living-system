"""Preserve imported activity scan ceilings when an old executor resumes."""

import sqlite3


FLOORS = {
    "role_life_operations": "life.rollback.activity_receipt_floor.v1",
    "role_activity_revisions": "life.rollback.activity_revision_floor.v1",
}
MAX_ROWID = 9223372036854775807


def next_rowid(connection, table):
    key = FLOORS[table]
    row = connection.execute("SELECT value FROM runtime_metadata WHERE key=?", (key,)).fetchone()
    if row is None:
        return None
    if not connection.in_transaction:
        raise sqlite3.IntegrityError("activity scan floor requires a transaction")
    raw = row[0]
    if type(raw) is not str or len(raw) > 19 or not raw.isascii() or not raw.isdecimal() or str(int(raw)) != raw:
        raise sqlite3.IntegrityError("activity scan floor is invalid")
    current = connection.execute(f"SELECT COALESCE(MAX(rowid), 0) FROM {table}").fetchone()[0]
    value = max(int(raw), current) + 1
    if value > MAX_ROWID:
        raise sqlite3.IntegrityError("activity scan sequence is exhausted")
    connection.execute("UPDATE runtime_metadata SET value=? WHERE key=?", (str(value), key))
    return value
