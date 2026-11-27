"""Additive kernel repositories on LifeStorage's existing transaction boundary."""
from dataclasses import replace
from datetime import timedelta
import json
from uuid import uuid4

from ..contracts.models import ActivityInstance, ActivityResult, ActivityTemplate, AudienceBound, Candidate, CandidateStatus, EventEnvelope, ReasonCode, Revision
from ..storage import StorageReader
from .codec import decode, digest, encode, name, require, wire
from .history import compact_legacy_history, effect_receipt, encode_response, read_response, revision_delta


SCHEMA_VERSION = "3"
MIGRATING_SCHEMA_VERSION = "3-compacting"
READABLE_SCHEMA_VERSIONS = ("1", "2", SCHEMA_VERSION)
VERSION_KEY = "kernel_schema_version"
DDL = (
    """CREATE TABLE life_kernel_candidates (
        owner_key TEXT NOT NULL, candidate_id TEXT NOT NULL, context_key TEXT NOT NULL,
        dedupe_key TEXT NOT NULL, status TEXT NOT NULL, record_json TEXT NOT NULL,
        PRIMARY KEY(owner_key,candidate_id), UNIQUE(owner_key,dedupe_key))""",
    """CREATE TABLE life_kernel_instances (
        owner_key TEXT NOT NULL, instance_id TEXT NOT NULL, status TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK(revision>=1), record_json TEXT NOT NULL,
        template_json TEXT NOT NULL, prepared_json TEXT NOT NULL, audience_json TEXT NOT NULL,
        PRIMARY KEY(owner_key,instance_id))""",
    """CREATE UNIQUE INDEX life_kernel_foreground ON life_kernel_instances(owner_key)
        WHERE status IN ('preparing','running','unknown')""",
    """CREATE TABLE life_kernel_revisions (
        owner_key TEXT NOT NULL, instance_id TEXT NOT NULL, revision INTEGER NOT NULL,
        record_json TEXT NOT NULL, PRIMARY KEY(owner_key,instance_id,revision))""",
    """CREATE TABLE life_kernel_commands (
        owner_key TEXT NOT NULL, command_id TEXT NOT NULL, request_hash TEXT NOT NULL,
        response_json TEXT NOT NULL, PRIMARY KEY(owner_key,command_id))""",
    """CREATE TABLE life_kernel_gates (
        owner_key TEXT NOT NULL, command_id TEXT NOT NULL, stage TEXT NOT NULL,
        gate_json TEXT NOT NULL, PRIMARY KEY(owner_key,command_id,stage))""",
    """CREATE TABLE life_kernel_results (
        owner_key TEXT NOT NULL, result_id TEXT NOT NULL, instance_id TEXT NOT NULL,
        proposal_json TEXT NOT NULL, final_json TEXT,
        PRIMARY KEY(owner_key,result_id), UNIQUE(owner_key,instance_id))""",
    """CREATE TABLE life_kernel_effects (
        owner_key TEXT NOT NULL, effect_id TEXT NOT NULL, effect_hash TEXT NOT NULL,
        effect_json TEXT NOT NULL, PRIMARY KEY(owner_key,effect_id))""",
    """CREATE TABLE life_kernel_event_envelopes (
        event_id TEXT PRIMARY KEY REFERENCES life_events(event_id), envelope_json TEXT NOT NULL)""",
    """CREATE INDEX life_kernel_pending_candidates ON life_kernel_candidates(owner_key,status,candidate_id)""",
    """CREATE TABLE life_kernel_snapshots (
        owner_key TEXT PRIMARY KEY, revision INTEGER NOT NULL CHECK(revision>=1),
        snapshot_hash TEXT NOT NULL)""",
)


def _normalized(sql):
    return " ".join(sql.lower().split())


class KernelRepository:
    def __init__(self, storage):
        self.storage, self.reader = storage, StorageReader(storage)
        self._compact_writes = False

    def install(self):
        """Install or migrate kernel-owned data when explicitly enabled."""
        with self.storage._lock:
            if self._install_schema() != SCHEMA_VERSION:
                compact_legacy_history(self.storage)
                with self.storage.transaction() as db:
                    db.execute("UPDATE life_metadata SET value=? WHERE key=?", (SCHEMA_VERSION, VERSION_KEY))
            self._compact_writes = True

    def _use_compact_history(self, db):
        if self._compact_writes:
            return True
        # Disabled safety settlement can write to an existing schema without
        # installing or migrating it. Match that database's current format.
        row = db.execute("SELECT value FROM life_metadata WHERE key=?", (VERSION_KEY,)).fetchone()
        return row is not None and row[0] == SCHEMA_VERSION

    def _install_schema(self):
        with self.storage.transaction() as db:
            row = db.execute("SELECT value FROM life_metadata WHERE key=?", (VERSION_KEY,)).fetchone()
            objects = {row["name"]: row["sql"] for row in db.execute(
                "SELECT name,sql FROM sqlite_master WHERE name LIKE 'life_kernel_%' AND sql IS NOT NULL")}
            if row is None:
                require(not objects, "unclaimed kernel schema already exists")
                for statement in DDL:
                    db.execute(statement)
                db.execute("INSERT INTO life_metadata VALUES (?,?)", (VERSION_KEY, SCHEMA_VERSION))
                return SCHEMA_VERSION
            version = row[0]
            require(version in (*READABLE_SCHEMA_VERSIONS, MIGRATING_SCHEMA_VERSION), "unsupported kernel schema")
            for statement in DDL[:-1] if version == "1" else DDL:
                parts = statement.split()
                object_name = parts[3] if parts[1] == "UNIQUE" else parts[2]
                require(object_name in objects and _normalized(objects[object_name]) == _normalized(statement),
                        "kernel schema is incomplete or changed")
            if version == "1":
                require("life_kernel_snapshots" not in objects, "unclaimed kernel snapshot schema")
                db.execute(DDL[-1])
            if version in ("1", "2"):
                # Older releases must refuse this database from the first
                # rewrite, including when a migration is interrupted.
                db.execute("UPDATE life_metadata SET value=? WHERE key=?", (MIGRATING_SCHEMA_VERSION, VERSION_KEY))
            return version

    def coordinate_snapshot(self, db, snapshot):
        """Allocate an owner-local revision for the complete composed gate view.

        The fingerprint includes the host version, every domain value/version,
        agenda and capability view. It is never a sum of independent revisions.
        Existing candidate/gate history keeps its original snapshot numbers.
        """
        snapshot_hash = digest(snapshot)
        owner = snapshot.state.owner_key
        row = db.execute("SELECT revision,snapshot_hash FROM life_kernel_snapshots WHERE owner_key=?", (owner,)).fetchone()
        if row is None:
            # Schema 1 stored host snapshot numbers directly. Start beyond all
            # retained numbers so migration cannot reuse a historical version.
            legacy = db.execute("""SELECT MAX(revision) FROM (
                SELECT json_extract(record_json,'$.snapshot_revision') AS revision
                    FROM life_kernel_candidates WHERE owner_key=?
                UNION ALL SELECT json_extract(gate_json,'$.snapshot_revision') AS revision
                    FROM life_kernel_gates WHERE owner_key=?)""", (owner, owner)).fetchone()[0]
            revision = max(snapshot.state.revision, (legacy or 0) + 1)
        else:
            revision = row[0] if row[1] == snapshot_hash else row[0] + 1
        if row is None or row[1] != snapshot_hash:
            db.execute("""INSERT INTO life_kernel_snapshots VALUES (?,?,?)
                ON CONFLICT(owner_key) DO UPDATE SET revision=excluded.revision,snapshot_hash=excluded.snapshot_hash""",
                (owner, revision, snapshot_hash))
        return replace(snapshot, state=replace(snapshot.state, revision=Revision(revision)))

    def command(self, db, scope, command_id, request):
        name(command_id)
        request_hash = digest({"namespace": "life.kernel.command/v1", "owner_key": scope.owner_key,
                               "context_key": scope.context_key, "request": request})
        row = db.execute("SELECT request_hash,response_json FROM life_kernel_commands WHERE owner_key=? AND command_id=?",
                         (scope.owner_key, command_id)).fetchone()
        if row is not None:
            require(row[0] == request_hash, "command ID conflicts with original business parameters")
            return request_hash, read_response(db, scope.owner_key, row[1])
        return request_hash, None

    def save_command(self, db, scope, command_id, request_hash, response):
        db.execute("INSERT INTO life_kernel_commands VALUES (?,?,?,?)",
                   (scope.owner_key, command_id, request_hash,
                    encode_response(response, compact=self._use_compact_history(db))))

    def candidate(self, owner_key, candidate_id):
        row = self.reader.one("SELECT context_key,record_json FROM life_kernel_candidates WHERE owner_key=? AND candidate_id=?",
                              (owner_key, candidate_id))
        require(row is not None, "candidate is unavailable")
        return row[0], decode(Candidate, row[1])

    def propose(self, db, scope, candidate):
        row = db.execute("SELECT context_key,record_json FROM life_kernel_candidates WHERE owner_key=? AND dedupe_key=?",
                         (scope.owner_key, candidate.dedupe_key)).fetchone()
        if row:
            require(row[0] == scope.context_key, "opportunity belongs to a different context", ReasonCode.UNAUTHORIZED)
            old = decode(Candidate, row[1])
            # Reasons from overlapping triggers merge without adding choice weight.
            if old.status == CandidateStatus.PENDING:
                old = replace(old, reasons=tuple(sorted(set(old.reasons + candidate.reasons))))
                self.save_candidate(db, old)
            return old
        db.execute("INSERT INTO life_kernel_candidates VALUES (?,?,?,?,?,?)",
                   (scope.owner_key, candidate.candidate_id, scope.context_key,
                    candidate.dedupe_key, candidate.status.value, encode(candidate)))
        return candidate

    def save_candidate(self, db, candidate):
        db.execute("UPDATE life_kernel_candidates SET status=?,record_json=? WHERE owner_key=? AND candidate_id=?",
                   (candidate.status.value, encode(candidate), candidate.owner_key, candidate.candidate_id))

    def instance(self, owner_key, instance_id):
        row = self.reader.one("SELECT record_json FROM life_kernel_instances WHERE owner_key=? AND instance_id=?",
                              (owner_key, instance_id))
        require(row is not None, "instance is unavailable")
        return decode(ActivityInstance, row[0])

    def pinned(self, owner_key, instance_id):
        row = self.reader.one("SELECT template_json,prepared_json,audience_json FROM life_kernel_instances WHERE owner_key=? AND instance_id=?",
                              (owner_key, instance_id))
        require(row is not None, "instance is unavailable")
        from .gating import PreparedActivity
        return decode(ActivityTemplate, row[0]), decode(PreparedActivity, row[1]), decode(AudienceBound, row[2])

    def create(self, db, instance, template, prepared, audience):
        db.execute("INSERT INTO life_kernel_instances VALUES (?,?,?,?,?,?,?,?)",
                   (instance.owner_key, instance.instance_id, instance.status.value, instance.revision,
                    encode(instance), encode(template), encode(prepared), encode(audience)))
        self._history(db, instance, encode(instance))

    def _history(self, db, instance, record):
        db.execute("INSERT INTO life_kernel_revisions VALUES (?,?,?,?)",
                   (instance.owner_key, instance.instance_id, instance.revision, record))

    def save(self, db, old, instance):
        require(instance.revision == old.revision + 1, "revision must advance once")
        record = encode(instance)
        changed = db.execute("""UPDATE life_kernel_instances SET status=?,revision=?,record_json=?
            WHERE owner_key=? AND instance_id=? AND revision=?""",
            (instance.status.value, instance.revision, record, instance.owner_key, instance.instance_id, old.revision))
        require(changed.rowcount == 1, "instance changed", ReasonCode.STALE_VERSION)
        history = revision_delta(wire(old), wire(instance)) if self._use_compact_history(db) else record
        self._history(db, instance, history)

    def foreground_busy(self, owner_key, excluding=None):
        return self.reader.one("""SELECT 1 FROM life_kernel_instances WHERE owner_key=?
            AND status IN ('preparing','running','unknown') AND (? IS NULL OR instance_id!=?) LIMIT 1""",
            (owner_key, excluding, excluding)) is not None

    def cooldown_until(self, owner_key, template):
        if not template.frequency:
            return None
        rows = self.reader.all("SELECT record_json FROM life_kernel_instances WHERE owner_key=?", (owner_key,))
        starts = [item.started_at for row in rows for item in (decode(ActivityInstance, row[0]),)
                  if item.template_id == template.id and item.started_at is not None]
        return max(starts) + timedelta(seconds=template.frequency["cooldown_s"]) if starts else None

    def gate(self, db, scope, command_id, stage, gate):
        db.execute("INSERT INTO life_kernel_gates VALUES (?,?,?,?)", (scope.owner_key, command_id, stage, encode(gate)))

    def stage_result(self, db, scope, result):
        result_id = "kernel-result-" + result.instance_id
        row = db.execute("SELECT proposal_json FROM life_kernel_results WHERE owner_key=? AND result_id=?",
                         (scope.owner_key, result_id)).fetchone()
        if row:
            require(row[0] == encode(result), "terminal proposal changed")
        else:
            db.execute("INSERT INTO life_kernel_results VALUES (?,?,?,?,NULL)",
                       (scope.owner_key, result_id, result.instance_id, encode(result)))
        return result_id

    def result(self, owner_key, result_id):
        row = self.reader.one("SELECT proposal_json,final_json FROM life_kernel_results WHERE owner_key=? AND result_id=?",
                              (owner_key, result_id))
        require(row is not None, "result proposal unavailable")
        return decode(ActivityResult, row[0]), decode(ActivityInstance, row[1]) if row[1] else None

    def finalize_result(self, db, owner_key, result_id, instance):
        db.execute("UPDATE life_kernel_results SET final_json=? WHERE owner_key=? AND result_id=? AND final_json IS NULL",
                   (encode(instance), owner_key, result_id))

    def pending_results(self, owner_key, context_key):
        rows = self.reader.all("""SELECT r.result_id FROM life_kernel_results r
            JOIN life_kernel_instances i ON i.owner_key=r.owner_key AND i.instance_id=r.instance_id
            WHERE r.owner_key=? AND json_extract(i.record_json,'$.context_key')=? AND r.final_json IS NULL
            ORDER BY r.result_id""", (owner_key, context_key))
        return tuple(row[0] for row in rows)

    def pending_result(self, owner_key, instance_id):
        row = self.reader.one("SELECT result_id FROM life_kernel_results WHERE owner_key=? AND instance_id=? AND final_json IS NULL",
                              (owner_key, instance_id))
        return row[0] if row else None

    def commit_effect(self, db, instance, effect, module):
        require(effect.instance_id == instance.instance_id, "effect belongs to another instance", ReasonCode.UNAUTHORIZED)
        name(effect.effect_id)
        row = db.execute("SELECT effect_hash FROM life_kernel_effects WHERE owner_key=? AND effect_id=?",
                         (instance.owner_key, effect.effect_id)).fetchone()
        effect_hash = digest(effect)
        if row:
            require(row[0] == effect_hash, "effect ID conflicts with original parameters")
            return
        # Domain validates and writes ONLY its own tables using this same local
        # transaction. An exception rolls back instance, effects and outbox.
        from ..prepared_commands import _synchronous
        require(_synchronous(module.apply_effect(db, instance, effect)) is True, "domain rejected effect")
        receipt = effect_receipt(effect) if self._use_compact_history(db) else encode(effect)
        db.execute("INSERT INTO life_kernel_effects VALUES (?,?,?,?)",
                   (instance.owner_key, effect.effect_id, effect_hash, receipt))

    def emit(self, db, instance, proposal, occurred_at, recorded_at):
        event = EventEnvelope("kernel-event-" + uuid4().hex, proposal.type, proposal.schema_version,
                              instance.owner_key, instance.context_key, instance.instance_id, instance.revision,
                              occurred_at, recorded_at, instance.causal_event_id, proposal.payload,
                              proposal.source_refs, proposal.provenance, proposal.audience_bound)
        # The existing v1 wire stays EXACTLY unchanged. This consumer is a local
        # audit sink only, never one of the legacy host activity consumers.
        self.storage.record_event(event_id=event.event_id, owner_key=event.owner_key, context_key=event.context_key,
                                  kind=event.type, payload=wire(event.payload), occurred_at=event.occurred_at,
                                  consumers=("kernel.audit.v1",))
        db.execute("INSERT INTO life_kernel_event_envelopes VALUES (?,?)", (event.event_id, encode(event)))
        return event

    def envelope(self, event_id):
        row = self.reader.one("SELECT envelope_json FROM life_kernel_event_envelopes WHERE event_id=?", (event_id,))
        return decode(EventEnvelope, row[0]) if row else None

    def active(self, owner_key):
        rows = self.reader.all("""SELECT record_json FROM life_kernel_instances WHERE owner_key=?
            AND status IN ('preparing','running','paused','unknown') ORDER BY instance_id""", (owner_key,))
        return tuple(decode(ActivityInstance, row[0]) for row in rows)

    def pending_candidates(self, owner_key):
        rows = self.reader.all("SELECT record_json FROM life_kernel_candidates WHERE owner_key=? AND status='pending' ORDER BY candidate_id LIMIT 32",
                              (owner_key,))
        return tuple(decode(Candidate, row[0]) for row in rows)
