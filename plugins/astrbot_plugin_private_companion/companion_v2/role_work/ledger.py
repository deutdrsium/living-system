"""Durable role activity and artifact ownership for trusted framework consumers.

Inventory access grants no audience permission. Sharing must still read the
original source/asset through its current governance before exposing content.
"""

from dataclasses import asdict, replace
from datetime import datetime, timezone

from companion_v2.execution.models import digest, encode, require, stamp
from companion_v2.foundation.refs import OperationRef, ReferenceValidationError, RoleOwnerRef, VersionRef
from companion_v2.storage.models import OperationState
from companion_v2.storage.activity_scan_floor import next_rowid
from companion_v2.storage.repositories import OperationRepository

from .codec import activity_from_json, artifact_from_json
from .models import RoleActivity, RoleArtifact, RoleActivityState, advance_activity


class PersistentRoleLifeLedger:
    """Share one V2 transaction owner; loading records performs no work or sending."""

    def __init__(self, runtime, *, now=None):
        self.runtime = runtime
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.operations = OperationRepository(runtime)

    def _owner_key(self, owner):
        if not isinstance(owner, RoleOwnerRef) or owner.installation_id != self.runtime.installation_id:
            raise ReferenceValidationError("role owner belongs to another installation")
        return digest(owner)

    @staticmethod
    def _page(limit, after_id):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ReferenceValidationError("role inventory page size must be between 1 and 100")
        if after_id is not None and (not isinstance(after_id, str) or not after_id or len(after_id) > 512):
            raise ReferenceValidationError("role inventory cursor must be an original object ID")

    def add_activity(self, activity: RoleActivity) -> RoleActivity:
        key = self._owner_key(activity.owner)
        if not activity.scope.is_resolved or activity.revision != 1:
            raise ReferenceValidationError("new activity needs its original resolved scope and first revision")
        with self.runtime.transaction() as connection:
            row = connection.execute("SELECT record_json FROM role_activities WHERE owner_key = ? AND activity_id = ?",
                                     (key, activity.activity_id)).fetchone()
            if row is not None:
                existing = activity_from_json(row["record_json"])
                if existing != activity:
                    raise ReferenceValidationError("activity identity is already bound to another record")
                return existing
            connection.execute("INSERT INTO role_activities VALUES (?, ?, ?, ?, ?, ?, ?)",
                (key, activity.activity_id, encode(activity.owner), activity.revision, activity.state.value,
                 stamp(activity.updated_at), encode(activity)))
            self._remember_revision(connection, activity)
        return activity

    def activity(self, activity_id: str, *, owner: RoleOwnerRef) -> RoleActivity:
        row = self.runtime.read_one("SELECT record_json FROM role_activities WHERE owner_key = ? AND activity_id = ?",
                                    (self._owner_key(owner), activity_id))
        if row is None:
            raise KeyError(activity_id)
        activity = activity_from_json(row["record_json"])
        if activity.owner != owner or activity.activity_id != activity_id:
            raise ReferenceValidationError("activity identity does not match its owner index")
        return activity

    def delete_activity(self, activity_id: str, *, owner: RoleOwnerRef) -> RoleActivity:
        """Remove one role-owned activity and every historical revision it owns."""
        key = self._owner_key(owner)
        activity = self.activity(activity_id, owner=owner)
        with self.runtime.transaction() as connection:
            connection.execute("DELETE FROM role_artifacts WHERE owner_key = ? AND activity_id = ?",
                               (key, activity_id))
            connection.execute("DELETE FROM role_activity_revisions WHERE owner_key = ? AND activity_id = ?",
                               (key, activity_id))
            connection.execute("DELETE FROM role_activities WHERE owner_key = ? AND activity_id = ?",
                               (key, activity_id))
        return activity

    def _remember_revision(self, connection, activity, operation_id=None):
        key = self._owner_key(activity.owner)
        previous = connection.execute("""SELECT record_json FROM role_activity_revisions
            WHERE owner_key = ? AND activity_id = ? AND revision = ?""",
            (key, activity.activity_id, activity.revision)).fetchone()
        if previous is not None:
            if activity_from_json(previous["record_json"]) != activity:
                raise ReferenceValidationError("activity history cannot replace an existing revision")
            return
        connection.execute("INSERT INTO role_activity_revisions(rowid, owner_key, activity_id, revision, updated_at, provenance, operation_id, record_json) "
                           "VALUES (?, ?, ?, ?, ?, 'recorded', ?, ?)",
            (next_rowid(connection, "role_activity_revisions"), key, activity.activity_id, activity.revision,
             stamp(activity.updated_at), operation_id, encode(activity)))

    def activity_revision(self, activity_id, revision, *, owner):
        if type(revision) is not int or revision < 1:
            raise ReferenceValidationError("activity revision must be positive")
        row = self.runtime.read_one("""SELECT record_json FROM role_activity_revisions
            WHERE owner_key = ? AND activity_id = ? AND revision = ?""", (self._owner_key(owner), activity_id, revision))
        if row is None:
            raise KeyError((activity_id, revision))
        activity = activity_from_json(row["record_json"])
        if (activity.owner, activity.activity_id, activity.revision) != (owner, activity_id, revision):
            raise ReferenceValidationError("activity history identity does not match its owner index")
        return activity

    def activity_receipt(self, operation_id, *, owner):
        """Return the original operation payload without granting audience access."""
        row = self.runtime.read_one(
            "SELECT result_json FROM role_life_operations WHERE operation_id = ? AND owner_key = ?",
            (operation_id, self._owner_key(owner)))
        return row["result_json"] if row else None

    def activity_revision_evidence(self, activity_id, revision, *, owner):
        row = self.runtime.read_one("""SELECT provenance, operation_id FROM role_activity_revisions
            WHERE owner_key = ? AND activity_id = ? AND revision = ?""",
            (self._owner_key(owner), activity_id, revision))
        return dict(row) if row is not None else None

    def activity_revision_coverage(self, activity_id, revision, *, owner):
        """Return the original revision's provenance and number of earlier versions."""
        row = self.runtime.read_one("""SELECT provenance, updated_at,
            (SELECT COUNT(*) FROM role_activity_revisions p WHERE p.owner_key = r.owner_key
             AND p.activity_id = r.activity_id AND p.revision < r.revision) AS prior_count
            FROM role_activity_revisions r WHERE owner_key = ? AND activity_id = ? AND revision = ?""",
            (self._owner_key(owner), activity_id, revision))
        return dict(row) if row is not None else None

    def timeline_activity_ceiling(self, owner, scope):
        if scope.installation_id != owner.installation_id or scope.bot_id != owner.bot_id:
            raise ReferenceValidationError("activity scope crosses its role owner")
        row = self.runtime.read_one("""SELECT COALESCE(MAX(rowid), 0) AS ceiling FROM role_activity_revisions
            WHERE owner_key = ? AND json_extract(record_json, '$.scope') = json(?)""",
            (self._owner_key(owner), encode(scope)))
        return row["ceiling"]

    def timeline_activity_candidates(self, owner, scope, *, ceiling, end, start=None, after=None, limit=65):
        if scope.installation_id != owner.installation_id or scope.bot_id != owner.bot_id:
            raise ReferenceValidationError("activity scope crosses its role owner")
        if type(ceiling) is not int or ceiling < 0 or type(limit) is not int or not 1 <= limit <= 65:
            raise ReferenceValidationError("invalid timeline activity page")
        sql = """SELECT updated_at AS occurred_at, 'activity' AS kind, activity_id AS identifier, revision
            FROM role_activity_revisions WHERE owner_key = ? AND json_extract(record_json, '$.scope') = json(?)
            AND rowid <= ? AND updated_at <= ?"""
        args = [self._owner_key(owner), encode(scope), ceiling, end]
        if start is not None:
            sql += " AND updated_at >= ?"
            args.append(start)
        if after is not None:
            sql += " AND (updated_at, 'activity', revision, activity_id) < (?, ?, ?, ?)"
            args.extend(after)
        sql += " ORDER BY occurred_at DESC, kind DESC, revision DESC, identifier DESC LIMIT ?"
        return tuple(dict(row) for row in self.runtime.read_all(sql, (*args, limit)))

    def scan_activity_receipts(self, *, after, limit):
        if type(after) is not int or after < 0:
            raise ReferenceValidationError("activity receipt cursor must be nonnegative")
        self._page(limit, None)
        rows = self.runtime.read_all("""SELECT rowid AS scan_id, operation_id, owner_key
            FROM role_life_operations WHERE rowid > ? ORDER BY rowid LIMIT ?""", (after, limit))
        return tuple(dict(row) for row in rows)

    def recent_activity_receipts(self, *, owner, limit):
        self._page(limit, None)
        rows = self.runtime.read_all("""SELECT operation_id FROM role_life_operations
            WHERE owner_key = ? ORDER BY rowid DESC LIMIT ?""", (self._owner_key(owner), limit))
        return tuple(dict(row) for row in rows)

    def activity_revisions(self, *, owner, after, at, before=None, limit=50):
        """Read one descending page of revisions for this exact role owner."""
        self._page(limit, None)
        params = [self._owner_key(owner), stamp(after), stamp(at)]
        continuation = ""
        if before is not None:
            continuation = " AND (updated_at, activity_id, revision) < (?, ?, ?)"
            params.extend(before)
        rows = self.runtime.read_all("""SELECT * FROM role_activity_revisions
            WHERE owner_key = ? AND updated_at > ? AND updated_at <= ?""" + continuation + """
            ORDER BY updated_at DESC, activity_id DESC, revision DESC LIMIT ?""", (*params, limit))
        return tuple(dict(row) for row in rows)

    def activities_for_role(self, owner, *, state=None, limit=50, after_id=None) -> tuple[RoleActivity, ...]:
        self._page(limit, after_id)
        if state is not None and not isinstance(state, RoleActivityState):
            raise ReferenceValidationError("activity state filter must be explicit")
        rows = self.runtime.read_all("""SELECT record_json FROM role_activities
            WHERE owner_key = ? AND (? IS NULL OR state = ?) AND (? IS NULL OR activity_id > ?)
            ORDER BY activity_id LIMIT ?""",
            (self._owner_key(owner), state.value if state else None, state.value if state else None,
             after_id, after_id, limit))
        return tuple(activity_from_json(row["record_json"]) for row in rows)

    def recent_activities_for_scope(self, owner, scope, *, kind, limit=50, before=None):
        """Read one kind in descending (updated_at, activity_id) order."""
        self._page(limit, None)
        if scope.installation_id != owner.installation_id or scope.bot_id != owner.bot_id:
            raise ReferenceValidationError("activity scope crosses its role owner")
        continuation = "" if before is None else " AND (updated_at, activity_id) < (?, ?)"
        rows = self.runtime.read_all("""SELECT record_json FROM role_activities
            WHERE owner_key = ? AND json_extract(record_json, '$.scope') = json(?)
            AND json_extract(record_json, '$.work.kind') = ?""" + continuation + """
            ORDER BY updated_at DESC, activity_id DESC LIMIT ?""",
            (self._owner_key(owner), encode(scope), kind, *(before or ()), limit))
        return tuple(activity_from_json(row["record_json"]) for row in rows)

    def work_activities(self, owner, *, states=None, kind=None, limit=200, oldest_first=False):
        """Read private work receipts for the role across its private windows."""
        if type(limit) is not int or not 1 <= limit <= 200 or type(oldest_first) is not bool:
            raise ReferenceValidationError("invalid work activity page")
        states = tuple(states or ())
        if any(not isinstance(state, RoleActivityState) for state in states):
            raise ReferenceValidationError("work activity states must be explicit")
        if kind is not None and (type(kind) is not str or not kind):
            raise ReferenceValidationError("work activity kind must be nonempty")
        state_clause = "" if not states else " AND state IN (" + ",".join("?" for _ in states) + ")"
        kind_clause = "" if kind is None else " AND json_extract(record_json, '$.work.kind') = ?"
        order = " ASC" if oldest_first else " DESC"
        rows = self.runtime.read_all("""SELECT record_json FROM role_activities
            WHERE owner_key = ? AND json_extract(record_json, '$.scope.kind') = 'private'"""
            + state_clause + kind_clause + " ORDER BY updated_at" + order + ", activity_id" + order + " LIMIT ?",
            (self._owner_key(owner), *(state.value for state in states), *((kind,) if kind else ()), limit))
        activities = []
        for row in rows:
            try:
                activity = activity_from_json(row["record_json"])
            except (ValueError, TypeError, KeyError):
                continue
            if activity.work:
                activities.append(activity)
        return tuple(activities)

    def transition_activity(self, activity_id, *, owner, expected_revision, state,
                            operation_id, checkpoint=None, observed_at=None, reading=None, work=None, sleep=None) -> RoleActivity:
        key = self._owner_key(owner)
        at = self.now() if observed_at is None else observed_at
        with self.runtime.transaction() as connection:
            activity = self.activity(activity_id, owner=owner)
            operation = OperationRef(operation_id, "role.life", activity.scope,
                                     VersionRef(operation_id, 1), operation_id)
            parameters = [owner, activity_id, expected_revision, state, checkpoint]
            if reading is not None:
                document = asdict(reading)
                if not reading.reflection:
                    document.pop("reflection")  # Preserve hashes of existing reading operations.
                parameters.append(document)
            if work is not None:
                parameters.append(work)
            if sleep is not None:
                parameters.append(sleep)
            receipt = require(self.operations.reserve(operation, parameter_hash=digest(parameters), observed_at=at))
            if receipt.replayed:
                saved = connection.execute("SELECT result_json FROM role_life_operations WHERE operation_id = ? AND owner_key = ?",
                                           (operation_id, key)).fetchone()
                if saved is None or receipt.state is not OperationState.COMPLETED:
                    raise ReferenceValidationError("original role activity operation is incomplete")
                return activity_from_json(saved["result_json"])
            updated = advance_activity(activity, expected_revision=expected_revision, state=state,
                                       observed_at=at, checkpoint=checkpoint, reading=reading, work=work, sleep=sleep)
            self._remember_revision(connection, activity)
            changed = connection.execute("""UPDATE role_activities SET revision = ?, state = ?, updated_at = ?, record_json = ?
                WHERE owner_key = ? AND activity_id = ? AND revision = ?""",
                (updated.revision, updated.state.value, stamp(updated.updated_at), encode(updated), key,
                 activity_id, expected_revision))
            if changed.rowcount != 1:
                raise ReferenceValidationError("activity revision conflict")
            connection.execute("INSERT INTO role_life_operations(rowid, operation_id, owner_key, result_json) VALUES (?, ?, ?, ?)",
                (next_rowid(connection, "role_life_operations"), operation_id, key, encode(updated)))
            self._remember_revision(connection, updated, operation_id)
            require(self.operations.transition(operation_id, expected_state=OperationState.ACCEPTED,
                next_state=OperationState.COMPLETED, observed_at=at))
            return updated

    def context_activities(self, owner, scope, *, observed_at, limit=32):
        """Bounded current inventory for a trusted consumer, without audience authority."""
        self._page(limit, None)
        if scope.installation_id != owner.installation_id or scope.bot_id != owner.bot_id:
            raise ReferenceValidationError("activity scope crosses its role owner")
        rows = self.runtime.read_all("""SELECT record_json FROM role_activities
            WHERE owner_key = ? AND json_extract(record_json, '$.scope') = json(?) AND updated_at <= ?
            ORDER BY CASE state WHEN 'active' THEN 0 WHEN 'paused' THEN 1
                WHEN 'completed' THEN 2 WHEN 'planned' THEN 3 ELSE 4 END,
                updated_at DESC, activity_id DESC LIMIT ?""",
            (self._owner_key(owner), encode(scope), stamp(observed_at), limit))
        return tuple(activity_from_json(row["record_json"]) for row in rows)

    def add_artifact(self, artifact: RoleArtifact, *, owner: RoleOwnerRef) -> RoleArtifact:
        key = self._owner_key(owner)
        with self.runtime.transaction() as connection:
            activity = self.activity(artifact.activity_id, owner=owner)
            if activity.scope != artifact.source.scope or (artifact.owner is not None and artifact.owner != owner):
                raise ReferenceValidationError("artifact must retain the activity's original scope and role owner")
            artifact = replace(artifact, owner=owner)
            row = connection.execute("SELECT record_json FROM role_artifacts WHERE owner_key = ? AND artifact_id = ?",
                                     (key, artifact.artifact_id)).fetchone()
            if row is not None:
                existing = artifact_from_json(row["record_json"])
                if existing != artifact:
                    raise ReferenceValidationError("artifact identity is already bound to another original")
                return existing
            connection.execute("INSERT INTO role_artifacts VALUES (?, ?, ?, ?, ?)",
                (key, artifact.artifact_id, artifact.activity_id, artifact.source.scope.key, encode(artifact)))
        return artifact

    def artifact(self, artifact_id: str, *, owner: RoleOwnerRef) -> RoleArtifact:
        row = self.runtime.read_one("SELECT record_json FROM role_artifacts WHERE owner_key = ? AND artifact_id = ?",
                                    (self._owner_key(owner), artifact_id))
        if row is None:
            raise KeyError(artifact_id)
        return artifact_from_json(row["record_json"])

    def artifacts_for_role(self, owner, *, limit=50, after_id=None) -> tuple[RoleArtifact, ...]:
        return self._artifacts(owner, scope_key=None, limit=limit, after_id=after_id)

    def artifacts_for_scope(self, scope, *, owner, limit=50, after_id=None) -> tuple[RoleArtifact, ...]:
        if scope.installation_id != owner.installation_id or scope.bot_id != owner.bot_id:
            raise ReferenceValidationError("artifact scope crosses the role owner")
        return self._artifacts(owner, scope_key=scope.key, limit=limit, after_id=after_id)

    def _artifacts(self, owner, *, scope_key, limit, after_id):
        self._page(limit, after_id)
        rows = self.runtime.read_all("""SELECT record_json FROM role_artifacts
            WHERE owner_key = ? AND (? IS NULL OR scope_key = ?) AND (? IS NULL OR artifact_id > ?)
            ORDER BY artifact_id LIMIT ?""", (self._owner_key(owner), scope_key, scope_key, after_id, after_id, limit))
        return tuple(artifact_from_json(row["record_json"]) for row in rows)
