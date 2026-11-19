"""Adopt verified old calendar ownership inside an isolated staging database.

The companion host supplies the plan after inspecting one consistent snapshot.
This module never changes the execution owner or the host database.
"""

import hashlib
import json

from .calendar_import import TABLES, _identity_fingerprint, _rows, _summaries


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _plan(plan):
    if type(plan) is not dict or set(plan) != {"source_version", "source_summary", "identity_sha256", "summary", "moves"}:
        raise ValueError("calendar adoption plan is invalid")
    if type(plan["source_version"]) is not str or not plan["source_version"]:
        raise ValueError("calendar source version is required")
    if type(plan["source_summary"]) is not dict or type(plan["summary"]) is not dict or type(plan["moves"]) is not list:
        raise ValueError("calendar adoption plan is invalid")
    if (type(plan["identity_sha256"]) is not str or len(plan["identity_sha256"]) != 64
            or any(c not in "0123456789abcdef" for c in plan["identity_sha256"])):
        raise ValueError("calendar identity snapshot is invalid")
    if plan["summary"].get("adoptable") != len(plan["moves"]):
        raise ValueError("calendar adoption count is inconsistent")
    sources, targets = set(), set()
    for move in plan["moves"]:
        if type(move) is not dict or set(move) != {"from_owner", "to_owner", "record_id", "revision", "head_sha256"}:
            raise ValueError("calendar adoption move is invalid")
        for field in ("from_owner", "to_owner", "record_id"):
            if type(move[field]) is not str or not move[field].strip() or len(move[field]) > 512:
                raise ValueError("calendar adoption identity is invalid")
        if (type(move["revision"]) is not int or move["revision"] < 1
                or type(move["head_sha256"]) is not str or len(move["head_sha256"]) != 64
                or any(c not in "0123456789abcdef" for c in move["head_sha256"])):
            raise ValueError("calendar adoption version is invalid")
        source, target = (move["from_owner"], move["record_id"]), (move["to_owner"], move["record_id"])
        if source == target or source in sources or target in targets:
            raise ValueError("calendar adoption contains duplicate ownership")
        sources.add(source)
        targets.add(target)
    if sources & targets:
        raise ValueError("calendar adoption cannot overwrite another source")
    return hashlib.sha256(_json(plan).encode("utf-8")).hexdigest()


def _project(rows, name, moves):
    columns = TABLES[name]
    owner, identifier = columns.index("owner_key"), columns.index("record_id")
    replacements = {(move["from_owner"], move["record_id"]): move["to_owner"] for move in moves}
    expected = []
    for original in rows:
        row = list(original)
        row[owner] = replacements.get((row[owner], row[identifier]), row[owner])
        expected.append(tuple(row))
    if name == "calendar_receipts":
        expected.sort(key=lambda row: row[columns.index("operation_id")])
    elif name == "calendar_history":
        expected.sort(key=lambda row: (row[owner], row[identifier], row[columns.index("revision")]))
    else:
        expected.sort(key=lambda row: (row[owner], row[identifier]))
    return tuple(expected)


def _stored_adoption(db):
    fields = tuple(row[1] for row in db.execute("PRAGMA table_info(staged_calendar_adoption)"))
    if fields != ("migration_id", "request_hash", "plan_json", "result_json"):
        raise ValueError("calendar adoption manifest schema is invalid")
    rows = db.execute("SELECT * FROM staged_calendar_adoption").fetchall()
    if len(rows) != 1 or rows[0][0] != "calendar-adoption.v1":
        raise ValueError("calendar adoption manifest is invalid")
    plan, result = json.loads(rows[0][2]), json.loads(rows[0][3])
    if (_plan(plan) != rows[0][1] or type(result) is not dict or set(result) != {"adopted", "tables"}
            or result["adopted"] != len(plan["moves"])):
        raise ValueError("calendar adoption manifest changed")
    return plan, result


def adoption_certificate(db):
    """Identify the original adoption without exposing private calendar rows."""
    plan, result = _stored_adoption(db)
    manifest = db.execute("SELECT source_version, summary_json FROM staged_calendar_manifest WHERE migration_id=?",
                          ("calendar.v1",)).fetchone()
    if manifest is None or manifest[0] != plan["source_version"] or json.loads(manifest[1]) != plan["source_summary"]:
        raise ValueError("calendar adoption does not match its import manifest")
    return {"schema": "good-life.calendar-adoption-certificate.v1", "source_version": plan["source_version"],
            "plan_sha256": _plan(plan), "identity_sha256": plan["identity_sha256"],
            "source_summary_sha256": hashlib.sha256(_json(plan["source_summary"]).encode("utf-8")).hexdigest(),
            "adopted_tables_sha256": hashlib.sha256(_json(result["tables"]).encode("utf-8")).hexdigest()}


def verify_staged_adoption(source, target, *, expected_version, source_summary):
    """Recheck an adopted staging store against the same source snapshot."""
    plan, result = _stored_adoption(target)
    if (plan["source_version"] != expected_version or plan["source_summary"] != source_summary
            or plan["identity_sha256"] != _identity_fingerprint(source)):
        raise ValueError("calendar adoption source or identity changed")
    if _summaries(target, staged=True) != result["tables"]:
        raise ValueError("adopted calendar changed after staging")
    for name in TABLES:
        if _project(_rows(source, name), name, plan["moves"]) != tuple(tuple(row) for row in _rows(target, name, staged=True)):
            raise ValueError("adopted calendar differs from its source")


def adopt_staged_calendar(storage, plan):
    """Atomically rekey staged records, history and receipts; replay is checked.

    The host must verify role identities and recheck the same plan at cutover.
    No life command may have begun in this staging database before adoption.
    """
    plan = json.loads(_json(plan))
    request_hash = _plan(plan)
    with storage.transaction() as db:
        manifest = db.execute("SELECT source_version, summary_json FROM staged_calendar_manifest WHERE migration_id=?",
                              ("calendar.v1",)).fetchone()
        if manifest is None or manifest[0] != plan["source_version"] or json.loads(manifest[1]) != plan["source_summary"]:
            raise ValueError("calendar staging was not made from the audited snapshot")
        adoption = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='staged_calendar_adoption'").fetchone()
        if adoption:
            prior_plan, result = _stored_adoption(db)
            if _plan(prior_plan) != request_hash:
                raise ValueError("calendar adoption plan changed")
            if _summaries(db, staged=True) != result["tables"]:
                raise ValueError("adopted calendar changed after staging")
            return {"state": "unchanged", "adopted": result["adopted"]}
        command_table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='life_prepared_commands'").fetchone()
        if command_table and db.execute("SELECT 1 FROM life_prepared_commands LIMIT 1").fetchone():
            raise ValueError("calendar adoption requires an unused life command journal")
        if _summaries(db, staged=True) != {name: plan["source_summary"].get(name) for name in TABLES}:
            raise ValueError("staged calendar differs from the audited snapshot")
        originals = {name: tuple(tuple(row) for row in _rows(db, name, staged=True)) for name in TABLES}
        for move in plan["moves"]:
            source, target, identifier = move["from_owner"], move["to_owner"], move["record_id"]
            head = db.execute("SELECT revision, record_json FROM staged_calendar_records WHERE owner_key=? AND record_id=?",
                              (source, identifier)).fetchone()
            if (head is None or head["revision"] != move["revision"]
                    or hashlib.sha256(head["record_json"].encode("utf-8")).hexdigest() != move["head_sha256"]):
                raise ValueError("calendar adoption source changed")
            record = json.loads(head["record_json"])
            if (record.get("kind") != "role_plan" or record.get("id") != identifier
                    or record.get("revision") != move["revision"]
                    or (record.get("reminder_id") and not record.get("reminder_context_key"))):
                raise ValueError("calendar adoption would change pinned or invalid data")
            if db.execute("SELECT 1 FROM staged_calendar_records WHERE owner_key=? AND record_id=?", (target, identifier)).fetchone():
                raise ValueError("calendar adoption target already exists")
            db.execute("""INSERT INTO staged_calendar_records
                SELECT ?, record_id, revision, updated_at, record_json FROM staged_calendar_records
                WHERE owner_key=? AND record_id=?""", (target, source, identifier))
            db.execute("""INSERT INTO staged_calendar_history
                SELECT ?, record_id, revision, operation_id, record_json FROM staged_calendar_history
                WHERE owner_key=? AND record_id=?""", (target, source, identifier))
            db.execute("UPDATE staged_calendar_receipts SET owner_key=? WHERE owner_key=? AND record_id=?",
                       (target, source, identifier))
            db.execute("DELETE FROM staged_calendar_history WHERE owner_key=? AND record_id=?", (source, identifier))
            db.execute("DELETE FROM staged_calendar_records WHERE owner_key=? AND record_id=?", (source, identifier))
        for name in TABLES:
            if _project(originals[name], name, plan["moves"]) != tuple(tuple(row) for row in _rows(db, name, staged=True)):
                raise ValueError("calendar adoption did not preserve all original rows")
        result = {"adopted": len(plan["moves"]), "tables": _summaries(db, staged=True)}
        db.execute("""CREATE TABLE staged_calendar_adoption (
            migration_id TEXT PRIMARY KEY, request_hash TEXT NOT NULL, plan_json TEXT NOT NULL, result_json TEXT NOT NULL)""")
        db.execute("INSERT INTO staged_calendar_adoption VALUES (?, ?, ?, ?)",
                   ("calendar-adoption.v1", request_hash, _json(plan), _json(result)))
        return {"state": "adopted", "adopted": result["adopted"]}
