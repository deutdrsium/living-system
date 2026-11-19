"""Explicit shadow coordinator. No scheduler, live-state handler or event publisher.

Embedding callers supply verified actual intervals and consumed portions. The
explicit access adapter verifies owner bindings on every read/write; this local
API does not establish new host authority or accept unguarded RPC requests.
"""
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
import json
from types import MappingProxyType

from ..contracts.models import (EstimateAssumptions, OwnerKey, Revision, SchemaVersion, StateQuality,
                                StateRange, StateSnapshot, StateValue, StateVar, ValueType)
from ..prepared_commands import _synchronous
from ..state_primitives import digest
from ..state_records import StagedStateRecords
from .config import ShadowConfig, frozen, number, require
from . import models
from .repository import ShadowRepository, encode


def plain(value):
    if isinstance(value, Mapping):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [plain(item) for item in value]
    return value


class ShadowState:
    def __init__(self, storage, *, config=None, access=None):
        self.storage, self.access = storage, access
        self.config = config if config is not None else ShadowConfig()
        require(isinstance(self.config, ShadowConfig), "ShadowConfig required")
        self.repo = ShadowRepository(storage)
        self.closed = False
        if self.config.enabled:
            require(access is not None and all(callable(getattr(access, method, None))
                    for method in ("authorize", "storage_key")), "explicit verified state owner adapter required")
            self.repo.install()

    def _binding(self, owner, action):
        models.identifier(owner)
        require(self.access is not None and callable(getattr(self.access, "authorize", None))
                and callable(getattr(self.access, "storage_key", None)), "verified state owner adapter required")
        require(_synchronous(self.access.authorize(owner, action)) is True, "state owner is not authorized")
        storage_key = models.identifier(_synchronous(self.access.storage_key(owner)))
        require(owner != storage_key, "legacy body storage key is not an envelope owner")
        return storage_key

    def _projection(self, db, storage_key, at):
        legacy = StagedStateRecords(self.storage).dispatch("get", owner_key=storage_key, identifier="current")
        require(type(legacy) is dict and type(legacy.get("values")) is dict, "migrated current state required")
        row = db.execute("SELECT revision,updated_at FROM staged_role_state_records WHERE owner_key=? AND record_id='current'",
                         (storage_key,)).fetchone()
        # state_import copies this field byte-for-byte. Its current producer,
        # VersionedRecords.change -> execution.models.stamp, writes UTC aware
        # timestamps. For older exports that omitted the offset, this adapter
        # explicitly interprets the retained value as UTC, never machine local
        # time. New model commands still require aware times via models.moment.
        observed = datetime.fromisoformat(row[1])
        assumption = None
        if observed.utcoffset() is None:
            observed = observed.replace(tzinfo=timezone.utc)
            assumption = "legacy imported timestamp without offset interpreted as UTC"
        observed = models.moment(observed)
        if observed > at:
            return None
        return {"record": legacy, "revision": row[0], "at": models.stamp(observed),
                "timestamp_assumption": assumption}

    def _enabled(self):
        require(self.config.enabled and not self.closed, "shadow state is disabled or closed")

    def _record(self, db, owner, storage_key):
        record = self.repo.load(db, models.identifier(owner))
        require(record is not None, "initialize this owner from migrated state first")
        require(record["legacy_storage_key"] == storage_key, "state owner binding changed; explicit migration required")
        require(record["model_version"] == models.MODEL_VERSION, "model version requires explicit migration")
        require(record["parameter_hash"] == self.config.parameters.content_hash,
                "parameter change requires explicit migration; existing trajectory is pinned")
        require(record["switches"] == self.config.switches,
                "switch change requires explicit migration; existing trajectory is pinned")
        return record

    def initialize(self, owner_key, *, at, sleep_profile=None):
        self._enabled()
        owner_key, at = models.identifier(owner_key), models.moment(at)
        storage_key = self._binding(owner_key, "initialize")
        with self.storage.transaction() as db:
            existing = self.repo.load(db, owner_key)
            if existing is not None:
                return self._record(db, owner_key, storage_key)
            # The trusted adapter supplies the legacy role-body key. Preserve
            # the existing pending-command barrier; never recalculate identity.
            projection = self._projection(db, storage_key, at)
            require(projection is not None and at == models.moment(projection["at"]),
                    "initial time must match migrated observation; advance afterwards")
            legacy = projection["record"]
            # Validate and normalize the profile clock before detaching it.
            # Accept aware datetime inputs at this Python boundary, but persist
            # only canonical UTC text in the shadow record.
            profile = None
            if sleep_profile is not None:
                require(type(sleep_profile) is dict, "sleep profile must be an object")
                require("observed_at" in sleep_profile, "sleep profile requires observed_at")
                observed_at = models.moment(sleep_profile["observed_at"])
                require(observed_at == at, "sleep profile calculation time must equal initial observation")
                profile = dict(sleep_profile)
                profile["observed_at"] = models.stamp(observed_at)
                profile = json.loads(encode(profile))
            values, seed = models.initialize(legacy, profile, at, self.config.parameters)
            record = {"owner_key": owner_key, "legacy_storage_key": storage_key, "projection": projection,
                      "revision": 1, "at": models.stamp(at),
                      "model_version": models.MODEL_VERSION, "parameter_version": self.config.parameters.version,
                      "parameter_hash": self.config.parameters.content_hash,
                      "parameters": plain(self.config.parameters.data), "switches": self.config.switches,
                      "values": values, "initial_values": dict(values), "seed": seed,
                      "assumptions": list(seed["assumptions"]) + ([projection["timestamp_assumption"]]
                          if projection["timestamp_assumption"] else []),
                      "requested_at": models.stamp(at), "capped": False}
            self.repo.save(db, record, "initialize")
            return record

    def observe_interval(self, owner_key, *, input_id, start, end=None, sleeping=False, resting=False,
                         load=0, source, instance_id, end_reason=None):
        """Register an actual phase, with end=None while it is in progress.

        Open phases retain their asserted mode across advances/restarts. Close
        them at an actual wake/pause/cancellation before advancing past that
        boundary. Finite intervals represent completed observations, never
        planned durations. IDs/source/instance are evidence labels, not proof
        of host authorization; this API generates no activity completions.
        """
        self._enabled()
        storage_key = self._binding(owner_key, "observe_interval")
        start = models.moment(start)
        end = models.moment(end) if end is not None else None
        require(end is None or end > start, "interval must have positive duration")
        require(end is not None or end_reason is None, "open interval cannot have an end reason")
        require(type(sleeping) is bool and type(resting) is bool, "mode flags must be booleans")
        load = number(load, maximum=1)
        require(not sleeping or not load and not resting, "sleep cannot overlap active load/rest mode")
        item = {"input_id": models.identifier(input_id), "start": models.stamp(start),
                "end": models.stamp(end) if end is not None else None,
                "sleeping": sleeping, "resting": resting, "load": load,
                "source": models.identifier(source), "instance_id": models.identifier(instance_id),
                "end_reason": models.identifier(end_reason) if end is not None else None}
        content_hash = digest(item)
        with self.storage.transaction() as db:
            record = self._record(db, owner_key, storage_key)
            old = db.execute("SELECT content_hash FROM life_state_model_intervals WHERE owner_key=? AND input_id=?",
                             (owner_key, input_id)).fetchone()
            if old:
                require(old[0] == content_hash, "input ID conflicts with original interval")
                return False
            require(start >= models.moment(record["at"]), "late interval requires historical revision")
            overlap = db.execute("""SELECT 1 FROM life_state_model_intervals
                WHERE owner_key=? AND (? IS NULL OR start_at<?)
                AND (end_at IS NULL OR end_at>?) LIMIT 1""",
                (owner_key, item["end"], item["end"], item["start"])).fetchone()
            require(overlap is None, "overlapping aggregate activity intervals")
            db.execute("INSERT INTO life_state_model_intervals VALUES (?,?,?,?,?,?)",
                       (owner_key, input_id, content_hash, item["start"], item["end"], encode(item)))
            return True

    def close_interval(self, owner_key, *, input_id, end, end_reason, source):
        """Close an open phase once, including early wakeups and zero-time cancels.

        End cannot precede the committed clock: late corrections require the
        explicit historical-revision path, not silent re-integration. Closing
        does not advance or invent a completion event. Start-request retries
        and close-request retries each retain their original content identity.
        """
        self._enabled()
        storage_key = self._binding(owner_key, "close_interval")
        input_id, end = models.identifier(input_id), models.moment(end)
        closing = {"end": models.stamp(end), "end_reason": models.identifier(end_reason),
                   "source": models.identifier(source)}
        close_hash = digest(closing)
        with self.storage.transaction() as db:
            record = self._record(db, owner_key, storage_key)
            row = db.execute("SELECT record_json FROM life_state_model_intervals WHERE owner_key=? AND input_id=?",
                             (owner_key, input_id)).fetchone()
            require(row is not None, "unknown open interval")
            item = json.loads(row[0])
            if "close_hash" in item:
                require(item["close_hash"] == close_hash, "close conflicts with original end observation")
                return False
            require(item["end"] is None, "completed interval is immutable; only open intervals can close")
            require(end >= models.moment(item["start"]), "end precedes interval start")
            require(end >= models.moment(record["at"]), "late close requires historical revision")
            item.update(end=closing["end"], end_reason=closing["end_reason"],
                        close_source=closing["source"], close_hash=close_hash)
            db.execute("UPDATE life_state_model_intervals SET end_at=?,record_json=? WHERE owner_key=? AND input_id=?",
                       (closing["end"], encode(item), owner_key, input_id))
            return True

    def consume(self, owner_key, *, effect_id, effective_at, kind, fraction, source, instance_id):
        """Record an already consumed portion, after the caller's availability checks."""
        self._enabled()
        storage_key = self._binding(owner_key, "consume")
        effect = {"effect_id": models.identifier(effect_id), "effective_at": models.stamp(effective_at),
                  "kind": kind, "fraction": number(fraction, maximum=1),
                  "source": models.identifier(source), "instance_id": models.identifier(instance_id)}
        require(kind in ("meal.consumed", "drink.consumed"), "unsupported state effect")
        content_hash = digest(effect)
        with self.storage.transaction() as db:
            record = self._record(db, owner_key, storage_key)
            old = db.execute("SELECT content_hash FROM life_state_model_effects WHERE owner_key=? AND effect_id=?",
                             (owner_key, effect_id)).fetchone()
            if old:
                require(old[0] == content_hash, "effect ID conflicts with original payload")
                return False
            require(models.moment(effective_at) >= models.moment(record["at"]),
                    "late effect requires historical revision")
            applied = effect["effective_at"] == record["at"]
            db.execute("INSERT INTO life_state_model_effects VALUES (?,?,?,?,?,?)",
                       (owner_key, effect_id, content_hash, effect["effective_at"], int(applied), encode(effect)))
            if applied:
                record["values"] = models.apply_effect(record["values"], effect, self.config.parameters, self.config.switches)
                record["revision"] += 1
                self.repo.save(db, record, "effect")
            return True

    def advance(self, owner_key, *, to):
        """Advance all owners of keys atomically, splitting only at known boundaries.

        A capped call leaves the unprocessed tail pending at the honest clock.
        Repeated explicit calls may drain that backlog, each within the cap.
        """
        self._enabled()
        storage_key = self._binding(owner_key, "advance")
        requested = models.moment(to)
        with self.storage.transaction() as db:
            record = self._record(db, owner_key, storage_key)
            start = models.moment(record["at"])
            require(requested >= start, "clock moved backwards; explicit correction required")
            end = min(requested, start + timedelta(seconds=self.config.parameters.data["offline"]["max_advance_s"]))
            projection = self._projection(db, storage_key, end)
            if requested == start:
                if projection is not None and projection != record["projection"]:
                    record.update(projection=projection, revision=record["revision"] + 1)
                    self.repo.save(db, record, "projection")
                return record
            if projection is not None:
                record["projection"] = projection
            intervals = self.repo.intervals(db, owner_key)
            effects = [json.loads(row[0]) for row in db.execute("""SELECT record_json FROM life_state_model_effects
                WHERE owner_key=? AND applied=0 AND effective_at<=? ORDER BY effective_at,effect_id""",
                (owner_key, models.stamp(end)))]
            boundaries = {start, end}
            for item in intervals:
                for field in ("start", "end"):
                    if item[field] is not None:
                        time = models.moment(item[field])
                        if start < time < end:
                            boundaries.add(time)
            for effect in effects:
                boundaries.add(models.moment(effect["effective_at"]))
            ordered = sorted(boundaries)
            values = dict(record["values"])
            unknown = False
            for left, right in zip(ordered, ordered[1:]):
                mode = next((item for item in intervals if models.moment(item["start"]) <= left
                             and (item["end"] is None or left < models.moment(item["end"]))), None)
                if mode is None:
                    mode = {"sleeping": False, "resting": False, "load": 0}
                    unknown = True
                values = models.advance(values, (right - left).total_seconds(), mode,
                                        self.config.parameters, self.config.switches)
                for effect in effects:
                    if models.moment(effect["effective_at"]) == right:
                        values = models.apply_effect(values, effect, self.config.parameters, self.config.switches)
            if self.config.sleep:
                values["sleep.debt_s"] = models.debt_at(record["seed"], intervals, end, self.config.parameters)
                values = models.project(values, end, self.config.parameters)
            if unknown and "unknown intervals use estimated awake mode with no invented intake or sleep" not in record["assumptions"]:
                record["assumptions"].append("unknown intervals use estimated awake mode with no invented intake or sleep")
            record.update(values=values, at=models.stamp(end), requested_at=models.stamp(requested),
                          capped=end < requested, revision=record["revision"] + 1)
            self.repo.save(db, record, "advance")
            for effect in effects:
                db.execute("UPDATE life_state_model_effects SET applied=1 WHERE owner_key=? AND effect_id=?",
                           (owner_key, effect["effect_id"]))
            return record

    def snapshot(self, owner_key):
        """Read-only contract-v1 snapshot, with separate unchanged legacy projections."""
        storage_key = self._binding(owner_key, "snapshot")
        with self.storage.read_snapshot():
            record = self.repo.read(owner_key)
            if record is None:
                return None
            require(record["legacy_storage_key"] == storage_key, "state owner binding changed")
            if self.config.enabled:
                self._record(self.storage._connection, owner_key, storage_key)
        projection = record["projection"]
        assumptions = EstimateAssumptions(models.MODEL_VERSION, tuple(record["assumptions"]))
        revision, at = Revision(record["revision"]), models.moment(record["at"])
        values = {}
        for key, owner in models.owners().items():
            enabled = self.config.enabled and not self.closed and self.config.switches[owner]
            value = record["values"][key] if enabled else None
            quality = StateQuality.ESTIMATED if value is not None else StateQuality.UNAVAILABLE
            values[key] = StateValue(value, at, revision, (), quality,
                                     assumptions if quality == StateQuality.ESTIMATED else None)
        for key, value in projection["record"]["values"].items():
            quality = StateQuality.ESTIMATED if value is not None else StateQuality.UNKNOWN
            values["legacy." + key] = StateValue(frozen(value), models.moment(projection["at"]),
                Revision(projection["revision"]), (), quality,
                EstimateAssumptions(models.MODEL_VERSION, ("retained legacy simulation projection; provenance not upgraded",)
                    + ((projection["timestamp_assumption"],) if projection.get("timestamp_assumption") else ()))
                if quality == StateQuality.ESTIMATED else None)
        return StateSnapshot(OwnerKey(owner_key), at, revision, MappingProxyType(values))

    def variables(self, owner_key):
        storage_key = self._binding(owner_key, "variables")
        record = self.repo.read(models.identifier(owner_key))
        require(record is not None, "state not initialized")
        require(record["legacy_storage_key"] == storage_key, "state owner binding changed")
        result = []
        for key, owner in models.owners().items():
            unit = "s" if key in ("sleep.phase_s", "sleep.debt_s") else "1"
            p = record["parameters"]["sleep"]
            bounds = ((p["lower"], p["upper"]) if key == "sleep.pressure" else
                      (-p["amplitude"], p["amplitude"]) if key == "sleep.circadian" else
                      (0, p["period_s"]) if key == "sleep.phase_s" else
                      (0, p["target_sleep_s"]) if key == "sleep.debt_s" else (0, 1))
            result.append(StateVar(key, owner, ValueType.NUMBER, unit, StateRange(*bounds),
                record["initial_values"][key], None, None, False, (), SchemaVersion(1)))
        return tuple(result)

    def describe(self):
        require(self.config.parameters is not None, "parameters required for model description")
        return models.describe(self.config.parameters)

    def close(self):
        self.closed = True
