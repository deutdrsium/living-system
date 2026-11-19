"""Route the activity-control contract to this plugin's own writers."""

from .staged_activity_commands import StagedActivityTransitions
from .staged_activity_creations import StagedActivityCreations
from .staged_activity_deletions import StagedActivityDeletions


class ActivityControl:
    def __init__(self, storage, stop_reading=None):
        self._stop_reading = stop_reading
        self._handlers = {"activity_creation": StagedActivityCreations(storage),
                          "activity_transition": StagedActivityTransitions(storage),
                          "reading_deletion": StagedActivityDeletions(storage)}

    def dispatch(self, operation, *args, **kwargs):
        if type(operation) is not str:
            raise ValueError("unknown life activity operation")
        if operation == "activity_transition.stop_cancelled_reading":
            context_key, activity_id = self._handlers["activity_transition"].cancelled_reading(*args, **kwargs)
            return bool(self._stop_reading(context_key, activity_id)) if self._stop_reading is not None else False
        if operation == "reading_deletion.stop_cancelled_readings":
            readings = self._handlers["reading_deletion"].cancelled_readings(*args, **kwargs)
            return sum(bool(self._stop_reading(context_key, activity_id)) for context_key, activity_id in readings) if self._stop_reading is not None else 0
        handler = self._handlers.get(operation.partition(".")[0])
        if handler is None:
            raise ValueError("unknown life activity operation")
        return handler.dispatch(operation, *args, **kwargs)
