"""Lossless instance history and compact receipts for the kernel's own tables."""
import json

from .codec import require, wire


BATCH_ROWS = 64
BATCH_BYTES = 16 * 1024 * 1024
REVISION_REF = "$revision"


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _changes(old, new, path=()):
    if old is new:
        return
    if type(old) is dict and type(new) is dict:
        for key in sorted(old.keys() - new.keys()):
            yield ("remove", (*path, key))
        for key, value in new.items():
            if key in old:
                yield from _changes(old[key], value, (*path, key))
            else:
                yield ("set", (*path, key), value)
    elif type(old) is list and type(new) is list:
        for index, (before, after) in enumerate(zip(old, new)):
            yield from _changes(before, after, (*path, index))
        if len(old) != len(new):
            keep = min(len(old), len(new))
            yield ("tail", path, keep, new[keep:])
    elif type(old) is not type(new) or old != new or (type(old) is float and repr(old) != repr(new)):
        yield ("set", path, new)


def revision_delta(old, new):
    return _json(list(_changes(old, new)))


def _apply_changes(record, changes):
    for operation, path, *args in changes:
        target = record
        for key in path[:-1]:
            target = target[key]
        if operation == "set":
            target[path[-1]] = args[0]
        elif operation == "remove":
            del target[path[-1]]
        else:
            keep, tail = args
            target[path[-1]][keep:] = tail


def read_revision(db, owner, instance_id, revision):
    key = (owner, instance_id, revision)
    # Retries of the latest command and legacy rows need no history scan.
    current = db.execute("""SELECT record_json FROM life_kernel_instances
        WHERE owner_key=? AND instance_id=? AND revision=?""", key).fetchone()
    if current is not None:
        return json.loads(current[0])
    archived = db.execute("""SELECT record_json FROM life_kernel_revisions
        WHERE owner_key=? AND instance_id=? AND revision=?""", key).fetchone()
    require(archived is not None, "command receipt refers to a missing instance revision")
    record = json.loads(archived[0])
    if type(record) is dict:
        return record
    # A legacy database may still have full snapshots after an interrupted
    # migration. Starting at revision 1 works for either representation.
    for row in db.execute("""SELECT record_json FROM life_kernel_revisions
            WHERE owner_key=? AND instance_id=? AND revision<=? ORDER BY revision""", key):
        value = json.loads(row[0])
        if type(value) is dict:
            record = value
        else:
            _apply_changes(record, value)
    return record


def _response_instance(response):
    return response.get("instance", response)


def _revision_key(instance):
    if isinstance(instance, dict) and "instance_id" in instance and "revision" in instance:
        return (instance["instance_id"], instance["revision"])
    return None


def _compact_response(response, key):
    ref = {REVISION_REF: key}
    return {**response, "instance": ref} if "instance" in response else ref


def encode_response(response, *, compact):
    key = _revision_key(_response_instance(response)) if compact else None
    return _json(_compact_response(response, key) if key is not None else response)


def read_response(db, owner, raw):
    response = json.loads(raw)
    instance = _response_instance(response)
    if isinstance(instance, dict) and REVISION_REF in instance:
        record = read_revision(db, owner, *instance[REVISION_REF])
        return {**response, "instance": record} if "instance" in response else record
    return response


def effect_receipt(effect):
    # The full hash is computed separately. The payload was applied in the
    # same transaction and is not used to replay an already committed effect.
    return _json(wire({"instance_id": effect.instance_id, "effect_id": effect.effect_id,
                       "kind": effect.kind, "target_owner": effect.target_owner,
                       "effective_at": effect.effective_at, "source_refs": effect.source_refs}))


def _compact_legacy_command(db, key, raw):
    response = json.loads(raw)
    instance = _response_instance(response)
    ref = _revision_key(instance)
    if ref is None:
        return raw
    # Verify the historical response before replacing it with a reference.
    row = db.execute("""SELECT record_json FROM life_kernel_revisions
        WHERE owner_key=? AND instance_id=? AND revision=?""", (key[0], *ref)).fetchone()
    if row is not None and row[0] == _json(instance):
        return _json(_compact_response(response, ref))
    return raw


def _compact_legacy_revision(db, key, raw):
    if key[-1] == 1 or raw.startswith("["):
        return raw
    owner, instance_id, revision = key
    before = read_revision(db, owner, instance_id, revision - 1)
    return revision_delta(before, json.loads(raw))


def _compact_legacy_effect(_db, _key, raw):
    record = json.loads(raw)
    record.pop("payload", None)
    return _json(record)


def compact_legacy_history(storage):
    """Rewrite bounded batches. Every committed batch is readable and retryable."""
    db = storage._connection
    wal = db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"

    def checkpoint():
        if wal:
            require(db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0,
                    "kernel history migration requires WAL readers to close")

    checkpoint()
    # Commands must see their old full revisions. Revisions are processed in
    # reverse key order so the preceding revision is still a full snapshot.
    for table, keys, column, convert in (
            ("commands", ("owner_key", "command_id"), "response_json", _compact_legacy_command),
            ("effects", ("owner_key", "effect_id"), "effect_json", _compact_legacy_effect),
            ("revisions", ("owner_key", "instance_id", "revision"), "record_json", _compact_legacy_revision)):
        table = "life_kernel_" + table
        fields = ",".join(keys)
        parameters = ",".join("?" for _ in keys)
        order = ",".join(f"{key} DESC" for key in keys)
        seek = f"({fields})<({parameters})"
        match = f"({fields})=({parameters})"
        cursor = ()
        while True:
            with storage.transaction() as db:
                where = f"WHERE {seek}" if cursor else ""
                rows = db.execute(f"SELECT {fields} FROM {table} {where} ORDER BY {order} LIMIT ?",
                                  (*cursor, BATCH_ROWS)).fetchall()
                if not rows:
                    break
                size = 0
                for row in rows:
                    cursor = tuple(row)
                    raw = db.execute(f"SELECT {column} FROM {table} WHERE {match}", cursor).fetchone()[0]
                    compact = convert(db, cursor, raw)
                    if compact != raw:
                        db.execute(f"UPDATE {table} SET {column}=? WHERE {match}", (compact, *cursor))
                        size += len(raw.encode("utf-8")) + len(compact.encode("utf-8"))
                    if size >= BATCH_BYTES:
                        break
            checkpoint()
