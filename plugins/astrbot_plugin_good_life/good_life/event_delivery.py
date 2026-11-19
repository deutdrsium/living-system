"""Expose bounded event delivery and require a host receipt before acknowledgement."""

import inspect


class LifeEventDelivery:
    def __init__(self, storage):
        self.storage = storage

    def dispatch(self, operation, **request):
        if operation == "snapshot" and not request:
            return self.storage.read_snapshot()
        if operation == "pending" and {"consumer"} <= set(request) <= {"consumer", "context_key", "limit", "after_sequence"}:
            return self.storage.pending_events(request["consumer"], context_key=request.get("context_key"),
                                               limit=request.get("limit", 50), after_sequence=request.get("after_sequence", 0))
        if operation == "get" and set(request) == {"event_id", "consumer"}:
            return self.storage.event_for_consumer(request["event_id"], request["consumer"])
        if operation == "acknowledge" and set(request) == {"event_id", "consumer", "acknowledged_at", "verify_host_receipt"}:
            return self.acknowledge(**request)
        raise ValueError("unknown life event operation")

    def acknowledge(self, *, event_id, consumer, acknowledged_at, verify_host_receipt):
        if not callable(verify_host_receipt):
            raise ValueError("host event receipt is required")
        event = self.storage.event_for_consumer(event_id, consumer)
        if event is None:
            raise ValueError("event is unavailable to this consumer")
        result = verify_host_receipt(event, consumer)
        if inspect.isawaitable(result):
            if inspect.iscoroutine(result):
                result.close()
            raise ValueError("host event receipt must be verified synchronously")
        if result is not True:
            raise ValueError("host event receipt is unavailable or unverified")
        with self.storage.transaction():
            if self.storage.event_for_consumer(event_id, consumer) != event:
                raise ValueError("event changed before acknowledgement")
            return self.storage.acknowledge(event_id=event_id, consumer=consumer, acknowledged_at=acknowledged_at)
