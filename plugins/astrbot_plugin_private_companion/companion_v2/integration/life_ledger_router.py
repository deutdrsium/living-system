"""Select one activity-ledger owner without falling back after cutover."""

from contextlib import contextmanager
from threading import RLock, get_ident, local

from companion_v2.execution.models import TaskError


LIFE_ACTIVITY_OWNER_KEY = "life.activity.execution_owner.v1"
READ_METHODS = frozenset({"activity", "activity_revision", "activity_receipt", "activity_revision_evidence",
    "activity_revision_coverage", "timeline_activity_ceiling", "timeline_activity_candidates",
    "scan_activity_receipts", "recent_activity_receipts", "activity_revisions", "activities_for_role",
    "work_activities", "context_activities", "artifact", "artifacts_for_role", "artifacts_for_scope"})
WRITE_METHODS = frozenset({"add_activity", "delete_activity", "transition_activity", "add_artifact"})


class LifeLedgerRouter:
    """Keep legacy behavior until a stopped, quiescent migration changes ownership.

    This router only supplies life-owned reads. Other snapshot consumers,
    writes and the scheduler still need transfer before ownership can change.
    """

    def __init__(self, legacy):
        self._legacy = legacy
        self.runtime, self.now, self.operations = legacy.runtime, legacy.now, legacy.operations
        self._lock = RLock()
        self._reader = None
        self._deletion_provider = None
        self._creation_provider = None
        self._transition_provider = None
        self._events_provider = None
        self.browsing_reader = None
        self._generation = 0
        self._snapshot = local()

    def _owner(self):
        row = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?", (LIFE_ACTIVITY_OWNER_KEY,))
        if row is None or row["value"] == "legacy":
            return "legacy"
        if row["value"] == "good_life":
            return "good_life"
        raise TaskError("life_activity_owner_invalid", "activity execution ownership is invalid")

    def activity_owner(self):
        return self._owner()

    def bind_reader(self, reader, *, deletion_provider=None, creation_provider=None, transition_provider=None, events_provider=None):
        self._require_boundary()
        if not all(callable(getattr(reader, name, None)) for name in READ_METHODS):
            raise ValueError("activity reader does not support the complete read contract")
        with self._lock:
            if self._reader is not None and self._reader is not reader:
                raise ValueError("another activity reader is still bound")
            if self._reader is None:
                self._reader = reader
                self._deletion_provider = deletion_provider
                self._creation_provider = creation_provider
                self._transition_provider = transition_provider
                self._events_provider = events_provider
                self._generation += 1
            elif (self._deletion_provider is not deletion_provider or self._creation_provider is not creation_provider
                    or self._transition_provider is not transition_provider
                    or self._events_provider is not events_provider):
                raise ValueError("activity provider is already bound with another control interface")

    def unbind_reader(self, reader):
        self._require_boundary()
        with self._lock:
            if self._reader is not reader:
                return False
            self._reader = None
            self._deletion_provider = None
            self._creation_provider = None
            self._transition_provider = None
            self._events_provider = None
            self._generation += 1
            return True

    def deletion_provider(self):
        if self._owner() != "good_life":
            raise TaskError("life_activity_owner_invalid", "reading deletion belongs to the legacy ledger")
        self._require_boundary()
        with self._lock:
            if self._reader is None or self._deletion_provider is None:
                raise TaskError("life_provider_unavailable", "the activity life provider is unavailable")
            return self._deletion_provider

    def events_provider(self):
        if self._owner() != "good_life":
            raise TaskError("life_activity_owner_invalid", "life events belong to the legacy ledger")
        self._require_boundary()
        with self._lock:
            if self._reader is None or self._events_provider is None:
                raise TaskError("life_provider_unavailable", "the life event provider is unavailable")
            return self._events_provider

    def creation_provider(self):
        if self._owner() != "good_life":
            raise TaskError("life_activity_owner_invalid", "activity creation belongs to the legacy ledger")
        self._require_boundary()
        with self._lock:
            if self._reader is None or self._creation_provider is None:
                raise TaskError("life_provider_unavailable", "the activity life provider is unavailable")
            return self._creation_provider

    def transition_provider(self):
        if self._owner() != "good_life":
            raise TaskError("life_activity_owner_invalid", "activity transitions belong to the legacy ledger")
        self._require_boundary()
        with self._lock:
            if self._reader is None or self._transition_provider is None:
                raise TaskError("life_provider_unavailable", "the activity life provider is unavailable")
            return self._transition_provider

    def _require_boundary(self):
        diagnostic = getattr(getattr(self.runtime, "_read_snapshot_diagnostics", None), "current", None)
        pinned = getattr(self._snapshot, "reader", None)
        if ((getattr(self.runtime, "_write_owner_thread_id", None) == get_ident() or diagnostic is not None)
                and (pinned is None or pinned is not self._reader)):
            raise TaskError("life_activity_transaction_active", "activity access requires an independent host snapshot")

    def snapshot_factory(self):
        """Bind future snapshots to this reader generation, never a replacement."""
        if self._owner() == "legacy":
            return None
        with self._lock:
            reader, generation = self._reader, self._generation
        if reader is None or not callable(getattr(reader, "snapshot", None)):
            raise TaskError("life_provider_unavailable", "life provider does not support snapshots")

        @contextmanager
        def fixed_snapshot():
            with self._lock:
                if (self._reader is not reader or self._generation != generation
                        or self._owner() != "good_life"):
                    raise TaskError("life_provider_unavailable", "the original life provider changed")
                with self.consistent_snapshot():
                    yield
        return fixed_snapshot

    def is_pinned_provider(self, lease):
        reader = getattr(self._snapshot, "reader", None)
        compare = getattr(reader, "shares_registration", None)
        return reader is not None and reader is self._reader and callable(compare) and compare(lease) is True

    @contextmanager
    def consistent_snapshot(self):
        if self._owner() == "legacy":
            yield
            return
        pinned = getattr(self._snapshot, "reader", None)
        if pinned is not None:
            if pinned is not self._reader:
                raise TaskError("life_provider_unavailable", "life provider changed during the snapshot")
            yield
            return
        self._require_boundary()
        with self._lock:
            reader, generation = self._reader, self._generation
            if reader is None or not callable(getattr(reader, "snapshot", None)):
                raise TaskError("life_provider_unavailable", "life provider does not support snapshots")
            with reader.snapshot():
                if self._reader is not reader or self._generation != generation or self._owner() != "good_life":
                    raise TaskError("life_provider_unavailable", "life provider changed during the snapshot")
                self._snapshot.reader = reader
                try:
                    yield
                finally:
                    del self._snapshot.reader

    def __getattr__(self, name):
        if name not in READ_METHODS and name not in WRITE_METHODS:
            raise AttributeError(name)
        selected = self._owner()
        if selected == "good_life" and name in READ_METHODS:
            self._require_boundary()
        with self._lock:
            generation = self._generation

        def call(*args, **kwargs):
            current = self._owner()
            if current != selected:
                raise TaskError("life_activity_owner_changed", "activity ownership changed during the operation")
            if current == "legacy":
                return getattr(self._legacy, name)(*args, **kwargs)
            if name in WRITE_METHODS:
                raise TaskError("life_activity_write_unavailable", "activity writes have not been transferred")
            self._require_boundary()
            with self._lock:
                if generation != self._generation or self._reader is None:
                    raise TaskError("life_provider_unavailable", "the activity life provider is unavailable")
                return getattr(self._reader, name)(*args, **kwargs)
        return call
