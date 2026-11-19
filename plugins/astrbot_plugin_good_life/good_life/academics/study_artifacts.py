"""Versioned W6 study records and the local port used by scheduling and the dashboard."""
import asyncio
import json
from types import SimpleNamespace

from ..activities.study_work import GENERATOR_VERSION, KINDS, allocate_window, generate_v1, work_milestone
from ..contracts.models import ReasonCode
from ..kernel.codec import digest, encode, freeze, integer, moment, name, require, wire
from ..storage import StorageReader
from .service import Academics
from .study import authorize_materials, progress


SCHEMA_VERSION = 1
VERSION_KEY = "academics_study_work_schema_version"
DDL = (
    """CREATE TABLE life_study_work_cursors (
        owner_key TEXT NOT NULL, chunk_id TEXT NOT NULL, record_json TEXT NOT NULL,
        PRIMARY KEY(owner_key,chunk_id))""",
    """CREATE TABLE life_study_work_artifacts (
        owner_key TEXT NOT NULL, artifact_id TEXT NOT NULL, assignment_id TEXT NOT NULL,
        record_json TEXT NOT NULL, PRIMARY KEY(owner_key,artifact_id))""",
    """CREATE INDEX life_study_work_by_assignment ON life_study_work_artifacts(owner_key,assignment_id)""",
    """CREATE TABLE life_study_work_followups (
        owner_key TEXT NOT NULL, session_id TEXT NOT NULL, kind TEXT NOT NULL,
        record_json TEXT NOT NULL, PRIMARY KEY(owner_key,session_id,kind))""",
)
FOLLOWUP_KINDS = frozenset({"catch_up_notes", "borrow_notes", "extra_load"})
MATERIAL_ERRORS = frozenset({"unauthorized", "source_unavailable", "stale_version", "missing_dependency",
                             "provider_unavailable", "result_unknown"})


def _artifact_id(owner, chunk_id, kind):
    return "study_" + digest((owner, chunk_id, kind))


def install(db):
    version = db.execute("SELECT value FROM life_metadata WHERE key=?", (VERSION_KEY,)).fetchone()
    objects = {row[0]: row[1] for row in db.execute(
        "SELECT name,sql FROM sqlite_master WHERE name GLOB 'life_study_work_*'")}
    if version is None:
        require(not objects, "unclaimed study work schema")
        for statement in DDL:
            db.execute(statement)
        db.execute("INSERT INTO life_metadata VALUES (?,?)", (VERSION_KEY, str(SCHEMA_VERSION)))
    else:
        expected = {statement.split()[2]: " ".join(statement.split()) for statement in DDL}
        require(version[0] == str(SCHEMA_VERSION) and
                {key: " ".join(sql.split()) for key, sql in objects.items()} == expected,
                "unsupported study work schema")


class StudyArtifacts:
    def __init__(self, academics=None, *, history_storage=None):
        self.academics = academics
        self.history_storage = history_storage

    def _storage(self):
        self.academics._enabled()
        require(self.academics.config.study_enabled, "study artifacts are disabled", ReasonCode.MISSING_DEPENDENCY)
        return self.academics.storage

    @staticmethod
    def _cursor(chunk, saved):
        if saved is not None:
            cursor = json.loads(saved)
            require(cursor["schema_version"] == SCHEMA_VERSION and cursor["generator_version"] == GENERATOR_VERSION,
                    "unsupported study cursor version")
            return cursor
        baseline = chunk["completed_work_s"]
        return {"schema_version": SCHEMA_VERSION, "generator_version": GENERATOR_VERSION,
                "assignment_id": chunk["assignment_id"], "chunk_id": chunk["chunk_id"],
                "chunk_index": chunk["chunk_index"], "step": "walk", "revision": 0,
                "baseline_s": baseline, "elapsed_work_s": baseline, "next_kind": 0}

    @staticmethod
    def _claimed(db, owner):
        return {row[0] for row in db.execute("""SELECT assignment_id FROM life_study_chunks
            WHERE owner_key=? AND json_extract(record_json,'$.active')=1""", (owner,))}

    @staticmethod
    def _finished(chunk, cursor):
        # An unstarted W6 cursor can inherit A5 completion. Once persisted,
        # only the W6 checkpoint can finish the cursor and its artifacts.
        return cursor["step"] == "done" or (cursor["revision"] == 0 and not chunk["remaining_work_s"])

    def plan(self, owner, context, **window):
        """Project one window over all eligible chunks and identify the first pending cursor."""
        storage = self._storage()
        with StorageReader(storage).locked():
            db = storage._connection
            self.academics._owner(db, owner, context)
            assignments = self.academics._records(db, owner, "assignment")
            claimed = self._claimed(db, owner)
            saved = dict(db.execute("SELECT chunk_id, record_json FROM life_study_work_cursors WHERE owner_key=?", (owner,)))
            rows, first = [], None
            for chunk in progress(self.academics, db, owner, assignments):
                if chunk["assignment_id"] in claimed:
                    continue
                cursor = self._cursor(chunk, saved.get(chunk["chunk_id"]))
                if self._finished(chunk, cursor):
                    continue
                if first is None:
                    first = cursor
                rows.append({**chunk, "remaining_work_s": chunk["required_work_s"] - cursor["elapsed_work_s"]})
        return {**allocate_window(rows, **window), "cursor": first}

    def _snapshot(self, db, owner, context, assignment_id, chunk_index):
        self.academics._owner(db, owner, context)
        assignment = self.academics._records(db, owner, "assignment").get(assignment_id)
        require(assignment is not None, "study assignment unavailable", ReasonCode.MISSING_DEPENDENCY)
        require(assignment_id not in self._claimed(db, owner), "assignment already claimed", ReasonCode.STALE_VERSION)
        for chunk in progress(self.academics, db, owner, {assignment_id: assignment}):
            saved = db.execute("SELECT record_json FROM life_study_work_cursors WHERE owner_key=? AND chunk_id=?",
                               (owner, chunk["chunk_id"])).fetchone()
            cursor = self._cursor(chunk, saved[0] if saved else None)
            if chunk["chunk_index"] == chunk_index:
                unit = self.academics._records(db, owner, "syllabus")[assignment["unit_id"]]
                return assignment, unit, chunk, cursor
            require(self._finished(chunk, cursor), "previous study chunk is pending", ReasonCode.STALE_VERSION)
        require(False, "study chunk unavailable", ReasonCode.MISSING_DEPENDENCY)

    def cursor(self, owner, context, assignment_id, chunk_index):
        """Resume a specific chunk even if deadline order has changed since planning."""
        storage = self._storage()
        with StorageReader(storage).locked():
            _, _, chunk, cursor = self._snapshot(storage._connection, owner, context, name(assignment_id), integer(chunk_index))
            return {**cursor, "step": "done"} if self._finished(chunk, cursor) else cursor

    async def advance(self, owner, context, assignment_id, chunk_index, *, expected_revision, now,
                      work_s=0, allocated_s=0, walk=None, ports=None):
        """Advance one step. The coordinator supplies observed online work and its window cap.

        External I/O happens outside the transaction. A cursor and academic snapshot
        fence rejects stale work and prevents a duplicated call from spending time twice.
        """
        storage = self._storage()
        name(assignment_id)
        integer(chunk_index)
        integer(expected_revision)
        now = moment(now)
        seconds = min(integer(work_s), integer(allocated_s))
        lookup = (owner, context, assignment_id, chunk_index)
        with StorageReader(storage).locked():
            db = storage._connection
            snapshot = self._snapshot(db, *lookup)
            assignment, unit, chunk, cursor = snapshot
            checkpoint_sources = ()
            if cursor["step"] == "checkpoint":
                rows = db.execute("SELECT record_json FROM life_study_work_artifacts WHERE owner_key=? AND artifact_id IN (?,?,?)",
                                  (owner, *(_artifact_id(owner, chunk["chunk_id"], kind) for kind in KINDS))).fetchall()
                require(len(rows) == len(KINDS), "study checkpoint lacks artifact evidence", ReasonCode.STALE_VERSION)
                sources = [freeze(json.loads(row[0])["materials"]) for row in rows]
                checkpoint_sources = tuple({encode(materials): materials for materials in sources if materials}.values())
        require(cursor["revision"] == expected_revision, "study cursor changed", ReasonCode.STALE_VERSION)
        updated, artifacts = dict(cursor), []
        credited = chunk["completed_work_s"]
        step = cursor["step"]
        scope = SimpleNamespace(owner_key=owner, context_key=context)
        if self._finished(chunk, cursor):
            return {**cursor, "step": "done", "used_s": 0}
        if step == "walk":
            if walk is None or await walk.arrived(owner, context, dict(cursor)) is not True:
                return {**cursor, "used_s": 0}
            updated["step"] = "work"
            used = 0
        elif step == "work":
            elapsed = cursor["elapsed_work_s"]
            used = min(seconds, chunk["required_work_s"] - elapsed)
            if not used:
                return {**cursor, "used_s": 0}
            await authorize_materials(ports, scope, unit["materials"])
            updated["elapsed_work_s"] = elapsed + used
            for index in range(cursor["next_kind"], len(KINDS)):
                milestone = work_milestone(chunk["required_work_s"], cursor["baseline_s"], index)
                if milestone > updated["elapsed_work_s"]:
                    break
                artifacts.append(generate_v1(KINDS[index], unit, assignment))
                credited = max(credited, milestone)
                updated["next_kind"] = index + 1
            if updated["next_kind"] == len(KINDS):
                updated["step"] = "checkpoint"
            await authorize_materials(ports, scope, unit["materials"])
        else:
            for materials in checkpoint_sources:
                await authorize_materials(ports, scope, materials)
            updated["step"] = "done"
            used = 0

        updated["revision"] += 1
        with storage.transaction() as db:
            require(self._snapshot(db, *lookup) == snapshot, "study inputs changed", ReasonCode.STALE_VERSION)
            for artifact in artifacts:
                artifact_id = _artifact_id(owner, cursor["chunk_id"], artifact["kind"])
                record = {**artifact, "schema_version": SCHEMA_VERSION, "generator_version": GENERATOR_VERSION,
                          "artifact_id": artifact_id, "assignment_id": assignment_id, "chunk_id": cursor["chunk_id"],
                          "chunk_index": chunk_index, "revision": 1, "updated_at": wire(now),
                          "source": "simulated", "materials": unit["materials"]}
                db.execute("INSERT INTO life_study_work_artifacts VALUES (?,?,?,?)",
                           (owner, artifact_id, assignment_id, encode(record)))
            db.execute("INSERT INTO life_study_work_cursors VALUES (?,?,?) ON CONFLICT(owner_key,chunk_id) "
                       "DO UPDATE SET record_json=excluded.record_json", (owner, cursor["chunk_id"], encode(updated)))
            if artifacts:
                remaining = chunk["required_work_s"] - credited
                record = {**chunk, "completed_work_s": credited, "remaining_work_s": remaining,
                          "status": "in_progress" if remaining else "completed", "revision": chunk["revision"] + 1,
                          "evaluation": "study_artifacts_v1:partial_work" if remaining else "study_artifacts_v1:chunk_complete",
                          "instance_id": None, "sequence": 0, "active": False}
                db.execute("INSERT INTO life_study_chunks VALUES (?,?,?,?,?) ON CONFLICT(owner_key,chunk_id) "
                           "DO UPDATE SET revision=excluded.revision,record_json=excluded.record_json",
                           (owner, cursor["chunk_id"], assignment_id, record["revision"], encode(record)))
            elif step == "walk":
                # Pin the chunk structure via the existing A5 guard before any
                # partial online work is saved. Do not replace a legacy receipt.
                record = {**chunk, "instance_id": None, "sequence": 0, "active": False}
                db.execute("INSERT INTO life_study_chunks VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING",
                           (owner, cursor["chunk_id"], assignment_id, chunk["revision"], encode(record)))
        return {**updated, "used_s": used}

    def accept_followup(self, owner, context, *, session_id, kind, now):
        """Accept a single catch-up item from a trusted attendance caller."""
        return self._accept_followups(self._storage(), owner, context, session_id, ((kind, None, 0),), now)[0]

    def consume_attendance(self, owner, context, payload, *, now):
        """Consume the v1 academics.class.catch_up payload before acknowledging delivery."""
        action = payload["action"]
        require(payload["schema_version"] == 1 and action in ("catch_up_notes", "borrow_notes"),
                "unsupported class catch-up event")
        assignment_id = payload["assignment_id"]
        work_s = integer(payload["extra_work_s"])
        items = [(action, assignment_id, 0)]
        if work_s:
            items.append(("extra_load", name(assignment_id), work_s))
        storage = self.history_storage or self._storage()
        return self._accept_followups(storage, owner, context, payload["session_id"], items, now)

    def _accept_followups(self, storage, owner, context, session_id, items, now):
        params = (owner, name(session_id))
        updated_at = wire(moment(now))
        result = []
        with storage.transaction() as db:
            Academics._owner(db, owner, context)
            session = db.execute("SELECT 1 FROM life_academic_sessions WHERE owner_key=? AND session_id=?", params).fetchone()
            require(session is not None, "class session unavailable", ReasonCode.MISSING_DEPENDENCY)
            for kind, assignment_id, work_s in items:
                require(type(kind) is str and kind in FOLLOWUP_KINDS, "unknown study follow-up kind")
                if assignment_id is not None:
                    name(assignment_id)
                key = (*params, kind)
                item = {"schema_version": SCHEMA_VERSION, "followup_id": "catchup_" + digest(key),
                        "session_id": session_id, "kind": kind, "updated_at": updated_at,
                        "status": "pending", "source": "simulated", "assignment_id": assignment_id, "extra_work_s": work_s}
                db.execute("INSERT INTO life_study_work_followups VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
                           (*key, encode(item)))
                row = db.execute("SELECT record_json FROM life_study_work_followups WHERE owner_key=? AND session_id=? AND kind=?",
                                 key).fetchone()
                result.append(json.loads(row[0]))
        return tuple(result)

    def _rows(self, owner, context, assignment_id=None, limit=1, before=None, artifact_id=None):
        storage = self.history_storage or self._storage()
        with StorageReader(storage).locked():
            db = storage._connection
            Academics._owner(db, owner, context)
            if artifact_id is not None:
                row = db.execute("SELECT record_json FROM life_study_work_artifacts WHERE owner_key=? AND artifact_id=?",
                                 (owner, name(artifact_id))).fetchone()
                require(row is not None, "study artifact unavailable", ReasonCode.UNAUTHORIZED)
                return json.loads(row[0])
            args = (owner, name(assignment_id))
            sql = "SELECT record_json FROM life_study_work_artifacts WHERE owner_key=? AND assignment_id=?"
            if before is not None:
                marker = db.execute("SELECT rowid FROM life_study_work_artifacts WHERE owner_key=? AND assignment_id=? AND artifact_id=?",
                                    (*args, name(before))).fetchone()
                require(marker is not None, "invalid study artifact cursor")
                sql += " AND rowid < ?"
                args += (marker[0],)
            return [json.loads(row[0]) for row in db.execute(sql + " ORDER BY rowid DESC LIMIT ?", (*args, limit + 1))]

    async def read(self, owner, context, kind, query, *, ports=None):
        is_detail = kind == "study_artifact"
        limit = query.get("limit", 20)
        if not is_detail:
            require(kind == "study_artifacts" and 1 <= integer(limit, 1) <= 50, "invalid study artifact limit")
        try:
            if is_detail:
                rows = [await asyncio.to_thread(self._rows, owner, context, artifact_id=query["artifact_id"])]
            else:
                rows = await asyncio.to_thread(self._rows, owner, context, query["assignment_id"], limit, query.get("before"))
        except ValueError as error:
            if getattr(error, "code", None) == "unauthorized":
                require(False, "study artifact unavailable", ReasonCode.UNAUTHORIZED)
            raise
        next_before = rows[limit - 1]["artifact_id"] if not is_detail and len(rows) > limit else None
        scope = SimpleNamespace(owner_key=owner, context_key=context)
        items, checked = [], {}
        for record in rows[:limit]:
            materials = freeze(record.pop("materials"))
            text, next_step = record.pop("text"), record.pop("next_step")
            key = encode(materials)
            if key not in checked:
                try:
                    await authorize_materials(ports, scope, materials)
                    checked[key] = True
                except ValueError as error:
                    if getattr(error, "code", None) not in MATERIAL_ERRORS:
                        raise
                    checked[key] = False
            record["availability"] = "available" if checked[key] else "blocked"
            if is_detail:
                record.update(text=text if checked[key] else None, next_step=next_step if checked[key] else None)
                return record
            items.append(record)
        return {"schema_version": SCHEMA_VERSION, "items": items, "next_before": next_before}
