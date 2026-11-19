"""Persist a message-triggered sleep interruption with the admitted input."""

import json

from companion_v2.execution.models import digest, encode, stamp


PREFIX = "life.sleep-interruption.v1:"


def enqueue(runtime, *, owner, activity, context, event_key, observed_at, grace_minutes):
    if type(event_key) is not str or not event_key or activity.work is None:
        raise ValueError("a sleep interruption requires its original message")
    identity = digest([activity.activity_id, activity.revision])
    key = PREFIX + identity
    record = {"owner_key": digest(owner), "activity_id": activity.activity_id, "revision": activity.revision,
              "sleep_context_key": activity.work.context_key, "event_context": json.loads(encode(context)),
              "event_key": event_key, "observed_at": stamp(observed_at), "grace_minutes": grace_minutes}
    with runtime.transaction() as connection:
        connection.execute("INSERT OR IGNORE INTO runtime_metadata(key,value) VALUES (?,?)", (key, encode(record)))
    return key


def pending(runtime):
    rows = runtime.read_all("SELECT key,value FROM runtime_metadata WHERE key>=? AND key<? ORDER BY key", (PREFIX, PREFIX + "\uffff"))
    result = []
    for row in rows:
        record = json.loads(row["value"])
        result.append({"id": row["key"], "context_key": record["sleep_context_key"], "owner_key": record["owner_key"]})
    return tuple(result)


def get(runtime, key):
    if type(key) is not str or not key.startswith(PREFIX) or len(key) != len(PREFIX) + 64:
        raise ValueError("invalid sleep interruption identity")
    row = runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?", (key,))
    return json.loads(row["value"]) if row else None


def acknowledge(runtime, key, record):
    with runtime.transaction() as connection:
        changed = connection.execute("DELETE FROM runtime_metadata WHERE key=? AND value=?", (key, encode(record)))
        return changed.rowcount == 1
