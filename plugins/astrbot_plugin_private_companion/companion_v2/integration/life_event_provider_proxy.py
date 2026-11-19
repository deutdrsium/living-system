"""Deliver life events through the activity reader's revocable provider lease."""

from threading import get_ident

from companion_v2.execution.models import TaskError


class LifeEventProviderProxy:
    def __init__(self, runtime, lease):
        self._runtime, self._lease = runtime, lease

    def dispatch(self, operation, **request):
        if operation not in ("get", "pending", "acknowledge"):
            raise ValueError("unknown life event operation")
        diagnostic = getattr(getattr(self._runtime, "_read_snapshot_diagnostics", None), "current", None)
        if getattr(self._runtime, "_write_owner_thread_id", None) == get_ident() or diagnostic is not None:
            raise TaskError("life_event_transaction_active", "life events require an independent host snapshot")
        try:
            return self._lease.dispatch("activity_history", operation, **request)
        except (TaskError, ValueError, KeyError, TypeError):
            raise
        except Exception as error:
            raise TaskError("life_provider_unavailable", "the life event provider is unavailable") from error
