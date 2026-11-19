"""Single-writer manual academic store and class participation effect owner."""
from datetime import datetime
import json

from ..contracts.models import ReasonCode
from ..kernel.codec import digest, encode, freeze, integer, moment, name, require, wire
from ..kernel.registry import ModuleSpec
from ..storage import StorageReader
from .models import AcademicsConfig, day, expand, fields, validate_term

SPEC = ModuleSpec("academics", "1", ("academics.participation",),
    emits=("academics.class.completed", "academics.class.cancelled", "academics.class.progressed",
           "academics.study.completed", "academics.study.cancelled", "academics.study.progressed"),
    consumes=("academics.claim", "academics.participation", "academics.abandon",
              "academics.study_claim", "academics.study_progress", "academics.study_abandon"))
VERSION_KEY = "academics_schema_version"
CLASS_CUTOVER = "hosted_classes_started_at"
DDL = (
    """CREATE TABLE life_academic_owners (
        owner_key TEXT PRIMARY KEY, context_key TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>=1))""",
    """CREATE TABLE life_academic_records (
        owner_key TEXT NOT NULL, kind TEXT NOT NULL, record_id TEXT NOT NULL,
        revision INTEGER NOT NULL, deleted INTEGER NOT NULL, record_json TEXT NOT NULL,
        PRIMARY KEY(owner_key,kind,record_id))""",
    """CREATE TABLE life_academic_history (
        owner_key TEXT NOT NULL, revision INTEGER NOT NULL, kind TEXT NOT NULL,
        record_id TEXT NOT NULL, deleted INTEGER NOT NULL, record_json TEXT NOT NULL, recorded_at TEXT NOT NULL,
        PRIMARY KEY(owner_key,revision))""",
    """CREATE TABLE life_academic_sessions (
        owner_key TEXT NOT NULL, session_id TEXT NOT NULL, term_id TEXT NOT NULL, record_json TEXT NOT NULL,
        PRIMARY KEY(owner_key,session_id))""",
    """CREATE TABLE life_academic_commands (
        owner_key TEXT NOT NULL, command_id TEXT NOT NULL, request_hash TEXT NOT NULL, response_json TEXT NOT NULL,
        PRIMARY KEY(owner_key,command_id))""",
    """CREATE TABLE life_academic_participation (
        owner_key TEXT NOT NULL, session_id TEXT NOT NULL, instance_id TEXT NOT NULL, record_json TEXT NOT NULL,
        PRIMARY KEY(owner_key,session_id), UNIQUE(owner_key,instance_id))""",
)


def timestamp(value):
    require(type(value) is str, "UTC timestamp required")
    return moment(datetime.fromisoformat(value.replace("Z", "+00:00")))


class Academics:
    """Local read/write port. Host validates identity before invoking this API.

    It is not a companion.life.v1 handler or a frozen host-port extension. The
    only automatic mutation is explicit enabled installation. All reads remain
    pure, and owner/context are supplied by a trusted host, never a page body.
    """
    def __init__(self, *, config=None):
        self.config = config or AcademicsConfig()
        require(isinstance(self.config, AcademicsConfig), "AcademicsConfig required")
        self.storage = None
        self._template_refs = {}  # Process-local owner/context index; active pins remain durable.

    def install(self, storage):
        require(self.config.enabled, "academics is disabled", ReasonCode.MISSING_DEPENDENCY)
        with storage.transaction() as db:
            row = db.execute("SELECT value FROM life_metadata WHERE key=?", (VERSION_KEY,)).fetchone()
            objects = {r["name"]: r["sql"] for r in db.execute(
                "SELECT name,sql FROM sqlite_master WHERE name GLOB 'life_academic_*' AND sql IS NOT NULL")}
            if row is None:
                require(not objects, "unclaimed academic schema")
                for statement in DDL:
                    db.execute(statement)
                db.execute("INSERT INTO life_metadata VALUES (?, '1')", (VERSION_KEY,))
            else:
                require(row[0] == "1", "unsupported academic schema")
                require(set(objects) == {statement.split()[2] for statement in DDL}, "incomplete academic schema")
                for statement in DDL:
                    require(" ".join(objects[statement.split()[2]].lower().split()) == " ".join(statement.lower().split()),
                            "academic schema changed")
            from .study import install
            install(db)
            if self.config.study_enabled:
                from . import study_artifacts
                study_artifacts.install(db)
        self.storage = storage
        if self.config.study_enabled:
            self.study_artifacts = study_artifacts.StudyArtifacts(self)

    def _enabled(self):
        require(self.config.enabled and self.storage is not None, "academics is disabled", ReasonCode.MISSING_DEPENDENCY)

    @staticmethod
    def _owner(db, owner_key, context_key):
        name(owner_key)
        name(context_key)
        row = db.execute("SELECT context_key,revision FROM life_academic_owners WHERE owner_key=?", (owner_key,)).fetchone()
        require(row is None or row[0] == context_key, "academic workspace belongs to another private context", ReasonCode.UNAUTHORIZED)
        return row[1] if row else 0

    def _records(self, db, owner_key, kind):
        return {row["record_id"]: freeze(json.loads(row["record_json"])) for row in db.execute(
            "SELECT record_id,record_json FROM life_academic_records WHERE owner_key=? AND kind=? AND deleted=0",
            (owner_key, kind))}

    def _session_rows(self, db, owner_key):
        return {row[0]: json.loads(row[1]) for row in db.execute(
            "SELECT session_id,record_json FROM life_academic_sessions WHERE owner_key=?", (owner_key,))}

    def read(self, owner_key, context_key, kind, query):
        self._enabled()
        with StorageReader(self.storage).locked():
            db = self.storage._connection
            revision = self._owner(db, owner_key, context_key)
            result = {"schema_version": 1, "revision": revision}
            if kind == "term":
                fields(query, "", "term query")
                rows = db.execute("SELECT record_json,revision FROM life_academic_records WHERE owner_key=? AND kind='term' AND deleted=0 ORDER BY record_id", (owner_key,))
                return {**result, "terms": [{**json.loads(row[0]), "schema_version": 1, "revision": row[1]} for row in rows]}
            if kind in ("syllabus", "assignments", "study_progress"):
                fields(query, "term_id course_id", "study query")
                term_id, course_id = name(query["term_id"]), name(query["course_id"])
                entries = self._records(db, owner_key, "timetable")
                require(any(item["term_id"] == term_id and item["course"]["course_id"] == course_id for item in entries.values()),
                        "study course unavailable", ReasonCode.MISSING_DEPENDENCY)
                record_kind = "syllabus" if kind == "syllabus" else "assignment"
                rows = db.execute("SELECT record_json,revision FROM life_academic_records WHERE owner_key=? AND kind=? AND deleted=0 ORDER BY record_id", (owner_key, record_kind))
                records = [{**json.loads(row[0]), "schema_version": 1, "revision": row[1]} for row in rows
                           if json.loads(row[0])["term_id"] == term_id and json.loads(row[0])["course_id"] == course_id]
                key = "units" if kind == "syllabus" else "assignments"
                if kind == "study_progress":
                    from .study import progress
                    key, records = "items", [wire(item) for item in progress(self, db, owner_key, {item["assignment_id"]: item for item in records})]
                return {**result, "term_id": term_id, "course_id": course_id, key: records}
            require(kind in ("timetable", "occurrences"), "unknown academic read")
            fields(query, "term_id" if kind == "timetable" else "term_id start end", "academic query")
            term_id = name(query["term_id"])
            result["term_id"] = term_id
            term = db.execute("SELECT record_json FROM life_academic_records WHERE owner_key=? AND kind='term' AND record_id=? AND deleted=0", (owner_key, term_id)).fetchone()
            require(term is not None, "term is unavailable", ReasonCode.MISSING_DEPENDENCY)
            if kind == "timetable":
                rows = db.execute("SELECT record_json,revision FROM life_academic_records WHERE owner_key=? AND kind='timetable' AND deleted=0 ORDER BY record_id", (owner_key,))
                return {**result, "entries": [{**json.loads(row[0]), "schema_version": 1, "revision": row[1]} for row in rows if json.loads(row[0])["term_id"] == term_id]}
            first, last = day(query["start"]), day(query["end"])
            require(0 < (last - first).days <= 31, "query one to 31 local dates; end is exclusive")
            items = [item for item in self._session_rows(db, owner_key).values()
                     if item["term_id"] == term_id and query["start"] <= item["local_date"] < query["end"]]
            return {**result, "timezone": json.loads(term[0])["timezone"], "start": query["start"], "end": query["end"],
                    "items": sorted(items, key=lambda item: (item["start_at"], item["session_id"]))}

    def command(self, owner_key, context_key, body, *, now):
        self._enabled()
        now = moment(now)
        require(hasattr(body, "get"), "academic command must be an object")
        action, kind = body.get("action"), body.get("kind")
        require(action in ("create", "update", "delete") and kind in ("term", "timetable", "syllabus", "assignment", "course"), "unsupported academic command")
        # A course is not a stored record: it is the metadata shared by its slots, updated together.
        require(kind != "course" or action == "update", "courses can only be updated")
        fields(body, "command_id expected_revision action kind id" + (" record" if action != "delete" else ""), "academic command")
        command_id, record_id = name(body["command_id"]), name(body["id"])
        expected = integer(body["expected_revision"])
        if action != "delete":
            require(hasattr(body["record"], "get"), "academic record must be an object")
        request_hash = digest({"context_key": context_key, "request": freeze(body)})
        with self.storage.transaction() as db:
            revision = self._owner(db, owner_key, context_key)
            prior = db.execute("SELECT request_hash,response_json FROM life_academic_commands WHERE owner_key=? AND command_id=?", (owner_key, command_id)).fetchone()
            if prior:
                require(prior[0] == request_hash, "same command_id has different parameters")
                return json.loads(prior[1])
            require(expected == revision, "academic revision changed", ReasonCode.STALE_VERSION)
            if kind != "course":
                existing = db.execute("SELECT deleted FROM life_academic_records WHERE owner_key=? AND kind=? AND record_id=?", (owner_key, kind, record_id)).fetchone()
                require((action == "create" and existing is None) or (action != "create" and existing is not None and existing[0] == 0),
                        "record already exists or is unavailable", ReasonCode.STALE_VERSION)
            terms, entries = self._records(db, owner_key, "term"), self._records(db, owner_key, "timetable")
            units, assignments = self._records(db, owner_key, "syllabus"), self._records(db, owner_key, "assignment")
            previous_term = terms.get(record_id) if kind == "term" else None
            records = {"term": terms, "timetable": entries, "syllabus": units, "assignment": assignments, "course": entries}[kind]
            written = None  # Slot ids rewritten by a course update; None means the one record itself.
            if kind in ("syllabus", "assignment") and action != "create":
                claims = [json.loads(row[0]) for row in db.execute("SELECT record_json FROM life_study_chunks WHERE owner_key=?", (owner_key,))]
                active = {item["assignment_id"] for item in claims if item["active"]}
                if kind == "assignment":
                    require(record_id not in active, "active study assignment is pinned", ReasonCode.STALE_VERSION)
                    if action == "update" and any(item["assignment_id"] == record_id for item in claims):
                        require(all(body["record"].get(key) == assignments[record_id][key] for key in
                            ("term_id", "course_id", "unit_id", "chunk_work_s", "estimated_work_s")),
                            "studied assignment work structure is immutable", ReasonCode.STALE_VERSION)
                elif any(item["unit_id"] == record_id and item["assignment_id"] in active for item in assignments.values()):
                    require(action == "update" and {key: value for key, value in freeze(body["record"]).items() if key != "assignment_ids"}
                        == {key: value for key, value in units[record_id].items() if key != "assignment_ids"},
                        "active study syllabus content is pinned", ReasonCode.STALE_VERSION)
            if action == "delete":
                record = records.pop(record_id)
                require(kind != "term" or not any(item["term_id"] == record_id for item in entries.values()),
                        "delete the term's timetable slots first")
            elif kind == "course":
                record = freeze(body["record"])
                fields(record, "term_id course_id name kind credits teacher_ref", "course update")
                require(record["course_id"] == record_id, "record id mismatch")
                course = {key: record[key] for key in ("course_id", "name", "kind", "credits", "teacher_ref")}
                written = sorted(slot_id for slot_id, entry in entries.items()
                                 if entry["term_id"] == record["term_id"] and entry["course"]["course_id"] == record_id)
                require(written, "course is unavailable", ReasonCode.STALE_VERSION)
                for slot_id in written:
                    entries[slot_id] = freeze({**entries[slot_id], "course": course})
            else:
                record = freeze(body["record"])
                require(hasattr(record, "get"), "academic record must be an object")
                require(record.get({"term": "term_id", "timetable": "slot_id", "syllabus": "unit_id", "assignment": "assignment_id"}[kind]) == record_id, "record id mismatch")
                records[record_id] = record
            require(len(terms) <= 8 and len(entries) <= 128, "academic workspace record limit exceeded")
            expanded, courses = {}, {}
            for term in terms.values():
                validate_term(term)
            for entry in entries.values():
                require(entry.get("term_id") in terms, "slot references an unavailable term")
            for term in terms.values():
                slots = [entry for entry in entries.values() if entry["term_id"] == term["term_id"]]
                for item in expand(term, slots):
                    expanded[item["session_id"]] = dict(item)
                for entry in slots:
                    key = (entry["term_id"], entry["course"]["course_id"])
                    require(key not in courses or courses[key] == entry["course"], "same course_id has inconsistent metadata")
                    courses[key] = entry["course"]
            from .study import validate_records
            validate_records(db, owner_key, terms, entries, units, assignments)
            old = self._session_rows(db, owner_key)
            claimed = {row[0] for row in db.execute("SELECT session_id FROM life_academic_participation WHERE owner_key=?", (owner_key,))}
            if action == "update" and previous_term and previous_term["timezone"] != record["timezone"]:
                require(not any(item["term_id"] == record_id and (timestamp(item["start_at"]) <= now or session_id in claimed)
                                for session_id, item in old.items()),
                        "term timezone cannot change after a class has started or been claimed")
            combined = {}
            for session_id in old.keys() | expanded.keys():
                previous, current = old.get(session_id), expanded.get(session_id)
                if previous and (timestamp(previous["end_at"]) <= now or session_id in claimed):
                    combined[session_id] = previous  # Ended history and claimed targets are immutable.
                    continue
                if previous is None and current is not None and timestamp(current["start_at"]) <= now:
                    continue  # A replacement never invents historical occurrences.
                if current is None:
                    current = {**previous, "cancelled": True}
                if previous:
                    current["revision"] = previous["revision"]
                    if current != previous:
                        current["revision"] += 1
                combined[session_id] = current
            live = sorted((item for item in combined.values() if not item["cancelled"]), key=lambda item: item["start_at"])
            require(all(a["end_at"] <= b["start_at"] for a, b in zip(live, live[1:])), "expanded terms overlap preserved class history")
            revision += 1
            db.execute("INSERT INTO life_academic_owners VALUES (?,?,?) ON CONFLICT(owner_key) DO UPDATE SET revision=excluded.revision", (owner_key, context_key, revision))
            rows = ([("timetable", slot_id, entries[slot_id]) for slot_id in written] if written is not None
                    else [(kind, record_id, record)])
            for row_kind, row_id, row_record in rows:
                db.execute("INSERT INTO life_academic_records VALUES (?,?,?,?,?,?) ON CONFLICT(owner_key,kind,record_id) DO UPDATE SET revision=excluded.revision,deleted=excluded.deleted,record_json=excluded.record_json",
                           (owner_key, row_kind, row_id, revision, int(action == "delete"), encode(row_record)))
            db.execute("INSERT INTO life_academic_history VALUES (?,?,?,?,?,?,?)", (owner_key, revision, kind, record_id, int(action == "delete"), encode(record), encode(now)))
            hosted = db.execute("SELECT 1 FROM life_metadata WHERE key=?", (CLASS_CUTOVER,)).fetchone()
            for item in combined.values():
                previous = old.get(item["session_id"])
                if previous == item:
                    continue
                db.execute("INSERT INTO life_academic_sessions VALUES (?,?,?,?) ON CONFLICT(owner_key,session_id) DO UPDATE SET record_json=excluded.record_json", (owner_key, item["session_id"], item["term_id"], encode(item)))
                if hosted and previous is not None:
                    # The pending class cue and its route are tied to the old occurrence.
                    db.execute("""UPDATE life_kernel_candidates
                        SET status='expired', record_json=json_set(record_json,'$.status','expired')
                        WHERE owner_key=? AND context_key=? AND status='pending'
                        AND json_extract(record_json,'$.target_ref.kind')='academics.session'
                        AND json_extract(record_json,'$.target_ref.id')=?
                        AND json_extract(record_json,'$.target_ref.revision')=?""",
                        (owner_key, context_key, item["session_id"], previous["revision"]))
            response = {"schema_version": 1, "revision": revision, "kind": kind, "id": record_id, "deleted": action == "delete"}
            db.execute("INSERT INTO life_academic_commands VALUES (?,?,?,?)", (owner_key, command_id, request_hash, encode(response)))
            return response

    def session_context(self, owner_key, context_key, session_id):
        self._enabled()
        with StorageReader(self.storage).locked():
            self._owner(self.storage._connection, owner_key, context_key)
            row = self.storage._connection.execute("SELECT record_json FROM life_academic_sessions WHERE owner_key=? AND session_id=?", (owner_key, name(session_id))).fetchone()
            require(row is not None, "class session unavailable", ReasonCode.MISSING_DEPENDENCY)
            return freeze(json.loads(row[0]))

    def next_sessions(self, owner_key, context_key, *, now, limit=16):
        self._enabled()
        require(1 <= integer(limit, 1) <= 128, "invalid session limit")
        with StorageReader(self.storage).locked():
            self._owner(self.storage._connection, owner_key, context_key)
            return tuple(freeze(item) for item in sorted(self._session_rows(self.storage._connection, owner_key).values(), key=lambda item: item["start_at"])
                         if not item["cancelled"] and timestamp(item["end_at"]) > moment(now))[:limit]

    def unclaimed_ended(self, owner_key, context_key, *, since, now):
        with StorageReader(self.storage).locked():
            db = self.storage._connection
            self._owner(db, owner_key, context_key)
            return tuple(freeze(json.loads(row[0])) for row in db.execute("""SELECT s.record_json
                FROM life_academic_sessions s LEFT JOIN life_academic_participation p USING (owner_key,session_id)
                WHERE s.owner_key=? AND p.session_id IS NULL AND json_extract(s.record_json,'$.cancelled')=0
                AND julianday(json_extract(s.record_json,'$.end_at')) BETWEEN julianday(?) AND julianday(?)
                ORDER BY json_extract(s.record_json,'$.end_at') LIMIT 16""", (owner_key, wire(since), wire(now))))

    def participation(self, owner_key, context_key, session_id):
        self._enabled()
        with StorageReader(self.storage).locked():
            self._owner(self.storage._connection, owner_key, context_key)
            row = self.storage._connection.execute("SELECT record_json FROM life_academic_participation WHERE owner_key=? AND session_id=?", (owner_key, name(session_id))).fetchone()
            return freeze(json.loads(row[0])) if row else None

    def apply_effect(self, db, instance, effect):
        if effect.kind.startswith("academics.study_"):
            from .study import apply_effect
            return apply_effect(self, db, instance, effect)
        from ..activities.class_session import participation_payload
        cp = instance.checkpoint
        require(effect.target_owner == SPEC.name and effect.instance_id == instance.instance_id, "academic effect scope mismatch")
        self._owner(db, instance.owner_key, instance.context_key)
        session = cp["session"]
        row = db.execute("SELECT instance_id,record_json FROM life_academic_participation WHERE owner_key=? AND session_id=?", (instance.owner_key, session["session_id"])).fetchone()
        if effect.kind == "academics.claim":
            current = db.execute("SELECT record_json FROM life_academic_sessions WHERE owner_key=? AND session_id=?", (instance.owner_key, session["session_id"])).fetchone()
            require(current and json.loads(current[0]) == dict(session) and not session["cancelled"], "class session changed", ReasonCode.STALE_VERSION)
            require(row is None or row[0] == instance.instance_id, "class already has a participation decision", ReasonCode.STALE_VERSION)
            require(effect.effect_id == instance.instance_id + ":claim" and effect.payload == {"session_id": session["session_id"], "revision": session["revision"]}, "invalid class claim")
            require(cp["params"]["decision"] == "skip" or moment(effect.effective_at) < timestamp(session["end_at"]), "class opportunity ended", ReasonCode.NO_TIME_WINDOW)
            if row:
                return True
            if "attendance" in cp["params"]:
                from .attendance import verify_wake_claim
                verify_wake_claim(db, instance, effect.effective_at)
            payload = participation_payload(instance, final=False)
        else:
            require(row is not None and row[0] == instance.instance_id, "class participation claim missing", ReasonCode.STALE_VERSION)
            previous = json.loads(row[1])
            if effect.kind == "academics.abandon":
                require(effect.effect_id == instance.instance_id + ":abandon" and effect.payload == {"session_id": session["session_id"]}, "invalid academic cleanup")
                payload = freeze({**previous, "status": "abandoned"})
            else:
                require(effect.kind == "academics.participation", "unsupported academic effect")
                final = cp["final"]
                payload = participation_payload(instance, final=final)
                require(effect.effect_id == instance.instance_id + ":participation:" + str(cp["sequence"]) and effect.payload == payload, "participation effect differs from checkpoint")
                require(previous["sequence"] + 1 == cp["sequence"] and previous["attended_s"] <= payload["attended_s"], "invalid participation sequence")
                attendance = payload.get("attendance")
                if attendance is not None and attendance["outcome"] in ("late", "absent"):
                    from .attendance import emit_follow_up
                    emit_follow_up(self.storage, instance, attendance, effect.effective_at)
        db.execute("INSERT INTO life_academic_participation VALUES (?,?,?,?) ON CONFLICT(owner_key,session_id) DO UPDATE SET record_json=excluded.record_json", (instance.owner_key, session["session_id"], instance.instance_id, encode(payload)))
        return True
