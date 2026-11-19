"""Forward the calendar handoff over one revocable provider lease."""


class LifeCalendarStorageProxy:
    def __init__(self, lease):
        self._lease = lease

    def dispatch(self, operation, **request):
        return self._lease.dispatch("calendar_read", operation, **request)


class LifeCalendarCommandsProxy:
    def __init__(self, lease):
        self._lease = lease

    def _call(self, operation, *args, **kwargs):
        return self._lease.dispatch("calendar_command", operation, *args, **kwargs)

    def batch(self, operation, *args, **kwargs):
        return self._call("batch." + operation, *args, **kwargs)

    def adoption_certificate(self):
        return self._call("adoption_certificate")

    def prepare(self, request, **kwargs):
        return self._call("prepare", request, **kwargs)

    def status(self, request):
        return self._call("status", request)

    def recover(self, **kwargs):
        return self._call("recover", **kwargs)

    def find_request(self, **kwargs):
        return self._call("find_request", **kwargs)

    def verify_prepared(self, request):
        return self._call("verify_prepared", request)

    def resolve(self, decision, **kwargs):
        return self._call("resolve", decision, **kwargs)

    def verify_commit(self, decision):
        return self._call("verify_commit", decision)

    def acknowledge(self, command_id, **kwargs):
        return self._call("acknowledge", command_id, **kwargs)
