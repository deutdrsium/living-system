"""Manual syllabus and simulated work ledger, owned by academics.

No source text is copied here. Material references use the same async host port
as A3; simulated work never advances a reading cursor or claims a real grade.
"""
import json
import math

from ..contracts.models import AudienceBound, SourceRef, ReasonCode
from ..contracts.ports import AuthorizedMaterial, AuthorityRequest
from ..kernel.codec import encode, freeze, integer, name, require, restore, wire
from .models import fields, sequence

VERSION_KEY = "academics_study_schema_version"
DDL = """CREATE TABLE life_study_chunks (
    owner_key TEXT NOT NULL, chunk_id TEXT NOT NULL, assignment_id TEXT NOT NULL,
    revision INTEGER NOT NULL, record_json TEXT NOT NULL,
    PRIMARY KEY(owner_key,chunk_id))"""


def install(db):
    """Join the academic installation transaction; never leave a partial enable."""
    row = db.execute("SELECT value FROM life_metadata WHERE key=?", (VERSION_KEY,)).fetchone()
    table = db.execute("SELECT sql FROM sqlite_master WHERE name='life_study_chunks'").fetchone()
    if row is None:
        require(table is None, "unclaimed study schema")
        db.execute(DDL)
        db.execute("INSERT INTO life_metadata VALUES (?, '1')", (VERSION_KEY,))
    else:
        require(row[0] == "1" and table and " ".join(table[0].split()) == " ".join(DDL.split()), "unsupported study schema")


def material_ref(raw):
    fields(raw, "source_ref audience_bound", "study material")
    ref = restore(SourceRef, wire(freeze(raw["source_ref"])))
    bound = restore(AudienceBound, wire(freeze(raw["audience_bound"])))
    name(ref.source_id)
    name(ref.permission_ref)
    name(bound.permission_ref)
    require(bool(ref.version or ref.content_digest), "material version or digest required")
    require((ref.fragment_start is None) == (ref.fragment_end is None), "material range must be paired")
    if ref.fragment_start is not None:
        require(integer(ref.fragment_start) < integer(ref.fragment_end), "empty material range")
    require(bound.context_keys and len(set(bound.context_keys)) == len(bound.context_keys), "private material audience required")
    return ref, bound


def validate_records(db, owner, terms, entries, units, assignments):
    from .service import timestamp
    courses = {(item["term_id"], item["course"]["course_id"]) for item in entries.values()}
    require(len(units) <= 512 and len(assignments) <= 512, "study record limit exceeded")
    for kind, records, identity in (("syllabus", units, "unit_id"), ("assignment", assignments, "assignment_id")):
        for record in records.values():
            fields(record, ("unit_id term_id course_id week topic concepts materials questions assignment_ids source" if kind == "syllabus"
                else "assignment_id term_id course_id unit_id title due_at chunk_work_s estimated_work_s source"), kind)
            name(record[identity])
            require(record["source"] == "manual", "study records require manual entry")
            require((record["term_id"], record["course_id"]) in courses and record["term_id"] in terms, "study course unavailable")
            if kind == "syllabus":
                require(1 <= integer(record["week"], 1) <= len(terms[record["term_id"]]["week_starts"]), "week outside term")
                name(record["topic"])
                for key in ("concepts", "questions", "assignment_ids"):
                    for item in sequence(record[key], 64, key):
                        name(item)
                for raw in sequence(record["materials"], 32, "materials"):
                    _ref, bound = material_ref(raw)
                    require(bound.context_keys == (db.execute("SELECT context_key FROM life_academic_owners WHERE owner_key=?", (owner,)).fetchone()[0],), "material must retain workspace audience", ReasonCode.UNAUTHORIZED)
            else:
                require(len(record["assignment_id"]) <= 252, "assignment id must leave room for the chunk suffix")
                name(record["title"])
                timestamp(record["due_at"])
                require(60 <= integer(record["chunk_work_s"], 1) <= 2400, "chunk work must be 60..2400 seconds")
                require(1 <= integer(record["estimated_work_s"], 1) <= 2400 * 128, "assignment work limit exceeded")
                require(math.ceil(record["estimated_work_s"] / record["chunk_work_s"]) <= 128, "too many chunks")
                unit = units.get(record["unit_id"])
                require(unit and (unit["term_id"], unit["course_id"]) == (record["term_id"], record["course_id"]), "assignment unit unavailable")
    for unit in units.values():
        require(len(set(unit["assignment_ids"])) == len(unit["assignment_ids"]), "duplicate assignment link")
        for assignment_id in unit["assignment_ids"]:
            require(assignment_id in assignments and assignments[assignment_id]["unit_id"] == unit["unit_id"], "assignment link unavailable")


def chunks(assignment):
    total, size = assignment["estimated_work_s"], assignment["chunk_work_s"]
    return tuple({"assignment_id": assignment["assignment_id"], "chunk_id": assignment["assignment_id"] + ":" + str(index),
        "chunk_index": index, "due_at": assignment["due_at"], "required_work_s": min(size, total - index * size), "completed_work_s": 0,
        "remaining_work_s": min(size, total - index * size), "status": "pending", "revision": 0,
        "evaluation": "not_started", "provenance": "simulated"} for index in range(math.ceil(total / size)))


def progress(service, db, owner, assignments):
    saved = {row[0]: json.loads(row[1]) for row in db.execute("SELECT chunk_id,record_json FROM life_study_chunks WHERE owner_key=?", (owner,))}
    return tuple(freeze({**item, **{key: value for key, value in saved.get(item["chunk_id"], {}).items()
        if key in ("completed_work_s", "remaining_work_s", "status", "revision", "evaluation")}})
        for assignment in sorted(assignments.values(), key=lambda item: (timestamp_key(item), item["assignment_id"])) for item in chunks(assignment))


def timestamp_key(item):
    from .service import timestamp
    return timestamp(item["due_at"])


def next_chunk(service, owner, context):
    from ..storage import StorageReader
    service._enabled()
    with StorageReader(service.storage).locked():
        db = service.storage._connection
        service._owner(db, owner, context)
        assignments = service._records(db, owner, "assignment")
        claimed = {row[0] for row in db.execute("""SELECT assignment_id FROM life_study_chunks
            WHERE owner_key=? AND json_extract(record_json,'$.active')=1""", (owner,))}
        for item in progress(service, db, owner, assignments):
            if item["remaining_work_s"] and item["assignment_id"] not in claimed:
                assignment = assignments[item["assignment_id"]]
                unit = service._records(db, owner, "syllabus")[assignment["unit_id"]]
                return freeze({"chunk": item, "assignment": assignment, "unit": unit})
    return None


async def authorize_materials(ports, scope, materials):
    """Fresh A3 read_material permission/version checks; discard all text.

    This preflight is outside kernel transactions. It authorizes references for
    this study opportunity, never records reading completion or copies content.
    """
    from ..reading_execution import immutable, read_activity_material
    if not materials:
        return
    require(ports is not None, "study material host port unavailable", ReasonCode.MISSING_DEPENDENCY)
    # Authority references are scoped to this negotiated async port, not the
    # independent synchronous kernel adapter. Never forward the kernel token.
    material_scope = await ports.resolve_authority(AuthorityRequest(scope.context_key, "role.reading.record", ()))
    require(material_scope.owner_key == scope.owner_key and material_scope.context_key == scope.context_key,
            "material authority owner/context mismatch", ReasonCode.UNAUTHORIZED)
    for raw in materials:
        ref, bound = material_ref(raw)
        require(scope.context_key in bound.context_keys, "material context denied", ReasonCode.UNAUTHORIZED)
        material = await read_activity_material(ports, material_scope, ref)
        require(isinstance(material, AuthorizedMaterial), "invalid authorized study material", ReasonCode.SOURCE_UNAVAILABLE)
        material = immutable(material)
        actual = material.source_ref
        require(actual.source_id == ref.source_id and (ref.version is None or actual.version == ref.version)
            and (ref.content_digest is None or actual.content_digest == ref.content_digest), "study material version changed", ReasonCode.STALE_VERSION)
        require(actual.permission_ref == ref.permission_ref and scope.context_key in material.audience_bound.context_keys
            and material.audience_bound.permission_ref == bound.permission_ref,
            "study material permission changed", ReasonCode.UNAUTHORIZED)
        if ref.fragment_start is not None:
            require(actual.fragment_start == ref.fragment_start and actual.fragment_end == ref.fragment_end, "study material range changed", ReasonCode.STALE_VERSION)


def apply_effect(service, db, instance, effect):
    from ..activities.study import study_payload
    cp, owner = instance.checkpoint, instance.owner_key
    service._owner(db, owner, instance.context_key)
    target = cp["params"]["target"]
    chunk = target["chunk"]
    row = db.execute("SELECT record_json FROM life_study_chunks WHERE owner_key=? AND chunk_id=?", (owner, chunk["chunk_id"])).fetchone()
    previous = json.loads(row[0]) if row else {**dict(chunk), "instance_id": None, "sequence": 0, "active": False}
    require(effect.target_owner == "academics" and effect.instance_id == instance.instance_id, "study effect scope mismatch")
    if effect.kind == "academics.study_claim":
        require(effect.effect_id == instance.instance_id + ":claim" and effect.payload == chunk, "invalid study claim")
        assignments = service._records(db, owner, "assignment")
        units = service._records(db, owner, "syllabus")
        require(assignments.get(chunk["assignment_id"]) == target["assignment"] and units.get(target["unit"]["unit_id"]) == target["unit"], "study target changed", ReasonCode.STALE_VERSION)
        if previous["instance_id"] == instance.instance_id:
            return True
        require(not previous["active"] and previous["revision"] == chunk["revision"] and previous["completed_work_s"] == chunk["completed_work_s"], "study chunk already claimed or changed", ReasonCode.STALE_VERSION)
        payload = {**previous, "instance_id": instance.instance_id, "sequence": 0, "active": True}
    else:
        require(previous["instance_id"] == instance.instance_id and previous["active"], "study claim missing", ReasonCode.STALE_VERSION)
        if effect.kind == "academics.study_abandon":
            require(effect.effect_id == instance.instance_id + ":abandon" and effect.payload == {"chunk_id": chunk["chunk_id"]}, "invalid study cleanup")
            payload = {**previous, "active": False, "revision": previous["revision"] + 1}
        else:
            require(effect.kind == "academics.study_progress", "unsupported study effect")
            payload = dict(study_payload(instance))
            require(effect.effect_id == instance.instance_id + ":progress:" + str(cp["sequence"]) and effect.payload == payload, "study progress differs from checkpoint")
            require(previous["sequence"] + 1 == cp["sequence"] and previous["completed_work_s"] <= payload["completed_work_s"], "study sequence mismatch")
            payload["revision"] = previous["revision"] + 1
    db.execute("INSERT INTO life_study_chunks VALUES (?,?,?,?,?) ON CONFLICT(owner_key,chunk_id) DO UPDATE SET revision=excluded.revision,record_json=excluded.record_json",
        (owner, chunk["chunk_id"], chunk["assignment_id"], payload["revision"], encode(freeze(payload))))
    return True
