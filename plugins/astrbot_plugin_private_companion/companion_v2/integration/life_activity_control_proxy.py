"""Use the life contract for activity command handoffs."""


class LifeActivityDeletionProxy:
    def __init__(self, lease):
        self._lease = lease

    def _call(self, operation, *args, **kwargs):
        return self._lease.dispatch("activity_control", "reading_deletion." + operation, *args, **kwargs)

    def describe(self, **kwargs):
        return self._call("describe", **kwargs)

    def prepare(self, request, **kwargs):
        return self._call("prepare", request, **kwargs)

    def proposal(self, request):
        return self._call("proposal", request)

    def verify_prepared(self, request):
        return self._call("verify_prepared", request)

    def resolve(self, decision, **kwargs):
        return self._call("resolve", decision, **kwargs)

    def verify_commit(self, decision):
        return self._call("verify_commit", decision)

    def acknowledge(self, command_id, **kwargs):
        return self._call("acknowledge", command_id, **kwargs)

    def request_for_replay(self, **kwargs):
        return self._call("request_for_replay", **kwargs)

    def stop_cancelled_readings(self, **kwargs):
        return self._call("stop_cancelled_readings", **kwargs)


class LifeActivityCreationProxy(LifeActivityDeletionProxy):
    def _call(self, operation, *args, **kwargs):
        return self._lease.dispatch("activity_control", "activity_creation." + operation, *args, **kwargs)

    def propose_manual_sleep(self, **kwargs):
        return self._call("propose_manual_sleep", **kwargs)

    def propose_scheduled_sleep(self, **kwargs):
        return self._call("propose_scheduled_sleep", **kwargs)

    def propose_social(self, **kwargs):
        return self._call("propose_social", **kwargs)

    def propose_reading_import(self, **kwargs):
        return self._call("propose_reading_import", **kwargs)


class LifeActivityTransitionProxy(LifeActivityDeletionProxy):
    def sleep_profile(self, **kwargs):
        return self._call("sleep_profile", **kwargs)

    def actual_sleep_seconds(self, **kwargs):
        return self._call("actual_sleep_seconds", **kwargs)

    def reading_metadata(self, **kwargs):
        return self._call("reading_metadata", **kwargs)

    def reading_choice(self, **kwargs):
        return self._call("reading_choice", **kwargs)

    def reading_retry(self, **kwargs):
        return self._call("reading_retry", **kwargs)

    def propose_reading_start(self, **kwargs):
        return self._call("propose_reading_start", **kwargs)

    def propose_reading_completion(self, **kwargs):
        return self._call("propose_reading_completion", **kwargs)

    def propose_reading_cancel(self, **kwargs):
        return self._call("propose_reading_cancel", **kwargs)

    def stop_cancelled_reading(self, **kwargs):
        return self._call("stop_cancelled_reading", **kwargs)

    def propose_interrupted_work(self, **kwargs):
        return self._call("propose_interrupted_work", **kwargs)

    def propose_interrupt_sleep(self, **kwargs):
        return self._call("propose_interrupt_sleep", **kwargs)

    def _call(self, operation, *args, **kwargs):
        return self._lease.dispatch("activity_control", "activity_transition." + operation, *args, **kwargs)

    def propose_manual_wake(self, **kwargs):
        return self._call("propose_manual_wake", **kwargs)

    def propose_manual_cancel_sleep(self, **kwargs):
        return self._call("propose_manual_cancel_sleep", **kwargs)

    def propose_due_sleep(self, **kwargs):
        return self._call("propose_due_sleep", **kwargs)

    def propose_resume_sleep(self, **kwargs):
        return self._call("propose_resume_sleep", **kwargs)
