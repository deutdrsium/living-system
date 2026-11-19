"""Host-side read adapter for a life provider, not a replacement executor."""

from __future__ import annotations

from companion_v2.execution.models import TaskError, digest, encode, stamp
from companion_v2.foundation.refs import ReferenceValidationError, RoleOwnerRef, ScopeKind
from companion_v2.role_work.codec import activity_from_json, artifact_from_json
from companion_v2.role_work.models import RoleActivityState
from companion_v2.storage.repositories import OperationRepository


class LifeLedgerReadProxy:
    """Forward authorized internal reads without passing the host runtime to the provider."""

    def __init__(self, runtime, registry):
        self.runtime = runtime
        self.operations = OperationRepository(runtime)
        self._registry = registry

    def snapshot(self):
        method = getattr(self._registry, "snapshot", None)
        if not callable(method):
            raise TaskError("life_provider_unavailable", "life provider does not support snapshots")
        return method("activity_history", "snapshot")

    def shares_registration(self, lease):
        compare = getattr(self._registry, "same_registration", None)
        return callable(compare) and compare(lease) is True

    def _owner_key(self, owner):
        if not isinstance(owner, RoleOwnerRef) or owner.installation_id != self.runtime.installation_id:
            raise ReferenceValidationError("role owner belongs to another installation")
        return digest(owner)

    @staticmethod
    def _page(limit):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ReferenceValidationError("role inventory page size must be between 1 and 100")

    @staticmethod
    def _after_id(after_id):
        if after_id is not None and (not isinstance(after_id, str) or not after_id or len(after_id) > 512):
            raise ReferenceValidationError("role inventory cursor must be an original object ID")

    def _read(self, operation, **request):
        try:
            return self._registry.dispatch("activity_read", operation, **request)
        except Exception as error:
            if getattr(error, "code", None) == "life_activity_pending":
                raise TaskError("life_activity_pending", "life activity is awaiting host confirmation") from error
            raise

    @staticmethod
    def _activity(raw, *, owner, identifier=None, revision=None):
        try:
            activity = activity_from_json(raw)
            if (activity.owner != owner or (identifier is not None and activity.activity_id != identifier)
                    or (revision is not None and activity.revision != revision)):
                raise ValueError("activity identity changed")
            return activity
        except (AttributeError, TypeError, ValueError, KeyError, OverflowError) as error:
            raise TaskError("life_provider_data_invalid", "life provider returned an invalid activity") from error

    def activity(self, activity_id, *, owner):
        raw = self._read("activity", owner_key=self._owner_key(owner), activity_id=activity_id)
        return self._activity(raw, owner=owner, identifier=activity_id)

    def activity_revision(self, activity_id, revision, *, owner):
        if type(revision) is not int or revision < 1:
            raise ReferenceValidationError("activity revision must be positive")
        raw = self._read("activity_revision", owner_key=self._owner_key(owner), activity_id=activity_id,
                         revision=revision)
        return self._activity(raw, owner=owner, identifier=activity_id, revision=revision)

    def activity_receipt(self, operation_id, *, owner):
        raw = self._read("activity_receipt", owner_key=self._owner_key(owner), operation_id=operation_id)
        if raw is not None:
            self._activity(raw, owner=owner)
        return raw

    def activities_for_role(self, owner, *, state=None, limit=50, after_id=None):
        self._page(limit)
        self._after_id(after_id)
        if state is not None and not isinstance(state, RoleActivityState):
            raise ReferenceValidationError("activity state filter must be explicit")
        rows = self._read("activities_for_role", owner_key=self._owner_key(owner),
                          state=state.value if state else None, limit=limit, after_id=after_id)
        if not isinstance(rows, (tuple, list)) or len(rows) > limit:
            raise TaskError("life_provider_data_invalid", "life provider returned an invalid activity page")
        activities, cursor = [], after_id
        for raw in rows:
            activity = self._activity(raw, owner=owner)
            if (cursor is not None and activity.activity_id <= cursor) or (state is not None and activity.state is not state):
                raise TaskError("life_provider_data_invalid", "life provider returned invalid activity ordering")
            activities.append(activity)
            cursor = activity.activity_id
        return tuple(activities)

    def context_activities(self, owner, scope, *, observed_at, limit=32):
        self._page(limit)
        if scope.installation_id != owner.installation_id or scope.bot_id != owner.bot_id:
            raise ReferenceValidationError("activity scope crosses its role owner")
        rows = self._read("context_activities", owner_key=self._owner_key(owner), scope_json=encode(scope),
                          observed_at=stamp(observed_at), limit=limit)
        if not isinstance(rows, (tuple, list)) or len(rows) > limit:
            raise TaskError("life_provider_data_invalid", "life provider returned an invalid activity page")
        activities = tuple(self._activity(raw, owner=owner) for raw in rows)
        if any(activity.scope != scope or activity.updated_at > observed_at for activity in activities):
            raise TaskError("life_provider_data_invalid", "life provider returned an activity outside the requested scope")
        ranks = {RoleActivityState.ACTIVE: 0, RoleActivityState.PAUSED: 1,
                 RoleActivityState.COMPLETED: 2, RoleActivityState.PLANNED: 3}
        for previous, current in zip(activities, activities[1:]):
            previous_rank, current_rank = ranks.get(previous.state, 4), ranks.get(current.state, 4)
            if (current_rank < previous_rank or (current_rank == previous_rank and
                    (current.updated_at, current.activity_id) >= (previous.updated_at, previous.activity_id))):
                raise TaskError("life_provider_data_invalid", "life provider returned invalid context activity ordering")
        return activities

    def work_activities(self, owner, *, states=None, kind=None, limit=200, oldest_first=False):
        if type(limit) is not int or not 1 <= limit <= 200 or type(oldest_first) is not bool:
            raise ReferenceValidationError("invalid work activity page")
        states = tuple(states or ())
        if any(not isinstance(state, RoleActivityState) for state in states):
            raise ReferenceValidationError("work activity states must be explicit")
        if kind is not None and (type(kind) is not str or not kind):
            raise ReferenceValidationError("work activity kind must be nonempty")
        rows = self._read("work_activities", owner_key=self._owner_key(owner),
                          states=tuple(state.value for state in states), kind=kind,
                          limit=limit, oldest_first=oldest_first)
        if not isinstance(rows, (tuple, list)) or len(rows) > limit:
            raise TaskError("life_provider_data_invalid", "life provider returned an invalid work activity page")
        activities = tuple(self._activity(raw, owner=owner) for raw in rows)
        if any(activity.scope.kind is not ScopeKind.PRIVATE or (states and activity.state not in states)
               or (kind is not None and (activity.work is None or activity.work.kind != kind))
               for activity in activities):
            raise TaskError("life_provider_data_invalid", "life provider returned an activity outside the work filter")
        for previous, current in zip(activities, activities[1:]):
            first, second = (previous.updated_at, previous.activity_id), (current.updated_at, current.activity_id)
            if (oldest_first and first >= second) or (not oldest_first and first <= second):
                raise TaskError("life_provider_data_invalid", "life provider returned invalid work activity ordering")
        return tuple(activity for activity in activities if activity.work)

    @staticmethod
    def _artifact(raw, *, owner, identifier=None, scope=None):
        try:
            artifact = artifact_from_json(raw)
            if (artifact.owner != owner or (identifier is not None and artifact.artifact_id != identifier)
                    or (scope is not None and artifact.source.scope != scope)):
                raise ValueError("artifact identity changed")
            return artifact
        except (AttributeError, TypeError, ValueError, KeyError, OverflowError) as error:
            raise TaskError("life_provider_data_invalid", "life provider returned an invalid artifact") from error

    def artifact(self, artifact_id, *, owner):
        raw = self._read("artifact", owner_key=self._owner_key(owner), artifact_id=artifact_id)
        return self._artifact(raw, owner=owner, identifier=artifact_id)

    def artifacts_for_role(self, owner, *, limit=50, after_id=None):
        return self._artifacts(owner, scope=None, limit=limit, after_id=after_id)

    def artifacts_for_scope(self, scope, *, owner, limit=50, after_id=None):
        if scope.installation_id != owner.installation_id or scope.bot_id != owner.bot_id:
            raise ReferenceValidationError("artifact scope crosses the role owner")
        return self._artifacts(owner, scope=scope, limit=limit, after_id=after_id)

    def _artifacts(self, owner, *, scope, limit, after_id):
        self._page(limit)
        self._after_id(after_id)
        rows = self._read("artifacts", owner_key=self._owner_key(owner), scope_key=scope.key if scope else None,
                          limit=limit, after_id=after_id)
        if not isinstance(rows, (tuple, list)) or len(rows) > limit:
            raise TaskError("life_provider_data_invalid", "life provider returned an invalid artifact page")
        artifacts, cursor = [], after_id
        for raw in rows:
            artifact = self._artifact(raw, owner=owner, scope=scope)
            if cursor is not None and artifact.artifact_id <= cursor:
                raise TaskError("life_provider_data_invalid", "life provider returned invalid artifact ordering")
            artifacts.append(artifact)
            cursor = artifact.artifact_id
        return tuple(artifacts)

    def activity_revision_evidence(self, activity_id, revision, *, owner):
        row = self._read("activity_revision_evidence", owner_key=self._owner_key(owner),
                         activity_id=activity_id, revision=revision)
        if row is not None and (type(row) is not dict or set(row) != {"provenance", "operation_id"}
                or row["provenance"] not in ("recorded", "legacy_receipt", "legacy_head")
                or (row["operation_id"] is not None and type(row["operation_id"]) is not str)):
            raise TaskError("life_provider_data_invalid", "life provider returned invalid revision evidence")
        return row

    def activity_revision_coverage(self, activity_id, revision, *, owner):
        if type(revision) is not int or revision < 1:
            raise ReferenceValidationError("activity revision must be positive")
        row = self._read("activity_revision_coverage", owner_key=self._owner_key(owner),
                         activity_id=activity_id, revision=revision)
        if row is not None and (type(row) is not dict or set(row) != {"provenance", "updated_at", "prior_count"}
                or row["provenance"] not in ("recorded", "legacy_receipt", "legacy_head")
                or type(row["updated_at"]) is not str or type(row["prior_count"]) is not int
                or not 0 <= row["prior_count"] < revision):
            raise TaskError("life_provider_data_invalid", "life provider returned invalid revision coverage")
        return row

    def timeline_activity_ceiling(self, owner, scope):
        if scope.installation_id != owner.installation_id or scope.bot_id != owner.bot_id:
            raise ReferenceValidationError("activity scope crosses its role owner")
        ceiling = self._read("timeline_activity_ceiling", owner_key=self._owner_key(owner), scope_json=encode(scope))
        if type(ceiling) is not int or ceiling < 0:
            raise TaskError("life_provider_data_invalid", "life provider returned an invalid timeline ceiling")
        return ceiling

    def timeline_activity_candidates(self, owner, scope, *, ceiling, end, start=None, after=None, limit=65):
        if scope.installation_id != owner.installation_id or scope.bot_id != owner.bot_id:
            raise ReferenceValidationError("activity scope crosses its role owner")
        if type(ceiling) is not int or ceiling < 0 or type(limit) is not int or not 1 <= limit <= 65:
            raise ReferenceValidationError("invalid timeline activity page")
        rows = self._read("timeline_activity_candidates", owner_key=self._owner_key(owner), scope_json=encode(scope),
                          ceiling=ceiling, end=end, start=start, after=after, limit=limit)
        if not isinstance(rows, (tuple, list)) or len(rows) > limit:
            raise TaskError("life_provider_data_invalid", "life provider returned an invalid timeline page")
        result, previous = [], tuple(after) if after is not None else None
        for row in rows:
            if (type(row) is not dict or set(row) != {"scan_id", "occurred_at", "kind", "identifier", "revision", "record_json"}
                    or type(row["scan_id"]) is not int or not 1 <= row["scan_id"] <= ceiling
                    or row["kind"] != "activity" or type(row["identifier"]) is not str
                    or type(row["revision"]) is not int or row["revision"] < 1
                    or type(row["occurred_at"]) is not str):
                raise TaskError("life_provider_data_invalid", "life provider returned an invalid timeline record")
            activity = self._activity(row["record_json"], owner=owner, identifier=row["identifier"], revision=row["revision"])
            position = (row["occurred_at"], row["kind"], row["revision"], row["identifier"])
            if (activity.scope != scope or row["occurred_at"] != stamp(activity.updated_at)
                    or row["occurred_at"] > end or (start is not None and row["occurred_at"] < start)
                    or (previous is not None and position >= previous)):
                raise TaskError("life_provider_data_invalid", "life provider returned an unordered timeline record")
            result.append({name: row[name] for name in ("occurred_at", "kind", "identifier", "revision")})
            previous = position
        return tuple(result)

    def activity_revisions(self, *, owner, after, at, before=None, limit=50):
        self._page(limit)
        owner_key = self._owner_key(owner)
        rows = self._read("activity_revisions", owner_key=owner_key, after=stamp(after), at=stamp(at),
                          before=before, limit=limit)
        if not isinstance(rows, (tuple, list)) or len(rows) > limit:
            raise TaskError("life_provider_data_invalid", "life provider returned an invalid revision page")
        last = before
        for row in rows:
            if (type(row) is not dict or set(row) != {"owner_key", "activity_id", "revision", "updated_at",
                                                       "provenance", "operation_id", "record_json"}
                    or row["owner_key"] != owner_key or type(row["activity_id"]) is not str
                    or type(row["revision"]) is not int or row["revision"] < 1
                    or type(row["updated_at"]) is not str
                    or row["provenance"] not in ("recorded", "legacy_receipt", "legacy_head")
                    or (row["operation_id"] is not None and type(row["operation_id"]) is not str)):
                raise TaskError("life_provider_data_invalid", "life provider returned an invalid revision page")
            activity = self._activity(row["record_json"], owner=owner, identifier=row["activity_id"],
                                      revision=row["revision"])
            key = (row["updated_at"], row["activity_id"], row["revision"])
            if (row["updated_at"] != stamp(activity.updated_at) or not stamp(after) < row["updated_at"] <= stamp(at)
                    or (last is not None and key >= last)):
                raise TaskError("life_provider_data_invalid", "life provider returned unordered revisions")
            last = key
        return tuple(rows)

    def scan_activity_receipts(self, *, after, limit):
        if type(after) is not int or after < 0:
            raise ReferenceValidationError("activity receipt cursor must be nonnegative")
        self._page(limit)
        rows = self._read("scan_activity_receipts", after=after, limit=limit)
        if not isinstance(rows, (tuple, list)) or len(rows) > limit:
            raise TaskError("life_provider_data_invalid", "life provider returned an invalid receipt page")
        cursor = after
        for row in rows:
            if (type(row) is not dict or set(row) != {"scan_id", "operation_id", "owner_key"}
                    or type(row["scan_id"]) is not int or row["scan_id"] <= cursor
                    or any(type(row[key]) is not str or not row[key] for key in ("operation_id", "owner_key"))):
                raise TaskError("life_provider_data_invalid", "life provider returned invalid receipt ordering")
            cursor = row["scan_id"]
        return tuple(rows)

    def recent_activity_receipts(self, *, owner, limit):
        self._page(limit)
        rows = self._read("recent_activity_receipts", owner_key=self._owner_key(owner), limit=limit)
        if not isinstance(rows, (tuple, list)) or len(rows) > limit:
            raise TaskError("life_provider_data_invalid", "life provider returned an invalid receipt page")
        seen = set()
        for row in rows:
            if (type(row) is not dict or set(row) != {"operation_id"} or type(row["operation_id"]) is not str
                    or not row["operation_id"] or row["operation_id"] in seen
                    or self.activity_receipt(row["operation_id"], owner=owner) is None):
                raise TaskError("life_provider_data_invalid", "life provider returned an invalid receipt")
            seen.add(row["operation_id"])
        return tuple(rows)
