"""A2 embedding policy around the unchanged A1 coordinator.

Embedders drive purchases through this tick/advance/pause API. A refused first
reservation cancels the known unfunded purchase through A1, preserving a durable
reason audit. Executors stay pure; no callback writes kernel tables. Pending
cancellation decisions survive a crash between audit and lifecycle commit.
"""
from datetime import datetime

from ..contracts.models import ActivityInstance, ActivityStatus, ReasonCode
from ..kernel.codec import KernelError, integer, moment, name, require, restore


class WalletActivities:
    def __init__(self, kernel, wallet):
        require(not kernel.config.enabled or wallet.storage is kernel.storage,
                "purchase coordinator and wallet must share the same storage")
        self.kernel, self.wallet = kernel, wallet

    def __getattr__(self, name):
        # Preserve the A1 embedding surface (close, repo, config, etc.) while
        # replacing only the commands requiring this component's policy.
        return getattr(self.kernel, name)

    def _eligible(self, instance, scope):
        return (instance.context_key == scope.context_key and instance.template_id == "buy_drink"
                and instance.status == ActivityStatus.RUNNING and instance.phase == "awaiting_reservation"
                and not instance.checkpoint["reserved"] and not instance.operation_refs)

    def _finish(self, scope, decision):
        final = self.kernel.repo.instance(scope.owner_key, decision["instance_id"])
        require(final.context_key == scope.context_key, "refusal context mismatch", ReasonCode.UNAUTHORIZED)
        if final.status != ActivityStatus.CANCELLED:
            final = self.kernel.cancel(scope, decision["instance_id"], expected_revision=decision["instance_revision"],
                command_id=decision["command_id"], now=moment(datetime.fromisoformat(decision["observed_at"])))
        self.wallet.complete_refusal(scope.owner_key, decision["recovery_id"])
        return final

    def _sweep(self, scope, now):
        with self.kernel.storage._lock:
            return self._sweep_locked(scope, now)

    def _sweep_locked(self, scope, now):
        scope = self.kernel._scope(scope, "kernel.cancel")
        cancelled = 0
        for decision in self.wallet.recovery_audit(scope.owner_key):
            if (decision["kind"] == "reservation_refused" and decision["context_key"] == scope.context_key
                    and not decision["completed"]):
                self._finish(scope, decision)
                cancelled += 1
        for instance in self.kernel.repo.active(scope.owner_key):
            if (self._eligible(instance, scope) and self.wallet.quote_available(scope.owner_key).available_minor
                    < instance.checkpoint["params"]["reserve_minor"]):
                decision = self.wallet.audit_reservation_refusal(scope.owner_key, instance.instance_id,
                    expected_instance_revision=instance.revision, now=now)
                self._finish(scope, decision)
                cancelled += 1
        return cancelled

    def _run(self, method, scope, instance_id, **command):
        # Same lock order as A1. Own receipts retain the ORIGINAL command,
        # including when its response comes from an internal policy cancel.
        with self.kernel.storage._lock:
            scope = self.kernel._scope(scope, "kernel." + method)
            require(set(command) == {"command_id", "expected_revision", "now"}, "invalid activity command parameters")
            name(command["command_id"])
            name(instance_id)
            integer(command["expected_revision"], 1)
            now = moment(command["now"])
            instance = self.kernel.repo.instance(scope.owner_key, instance_id)
            require(instance.context_key == scope.context_key, "activity context mismatch", ReasonCode.UNAUTHORIZED)
            request = {"action": method, "instance_id": instance_id,
                       "expected_revision": command["expected_revision"], "now": now}
            receipt = self.wallet.begin_activity_command(scope.owner_key, scope.context_key, command["command_id"], request)
            if receipt["response"] is not None:
                return restore(ActivityInstance, receipt["response"])
            decisions = tuple(decision for decision in self.wallet.recovery_audit(scope.owner_key)
                              if decision["kind"] == "reservation_refused" and decision["instance_id"] == instance_id)
            for decision in decisions:
                if not decision["completed"]:
                    self._finish(scope, decision)  # Fence progress BEFORE delegation, even after new income.
            matching = tuple(decision for decision in decisions
                             if decision["instance_revision"] == command["expected_revision"])
            if matching:
                final = self._finish(scope, matching[0])
            else:
                try:
                    final = getattr(self.kernel, method)(scope, instance_id, **command)
                except KernelError as error:
                    if error.code != ReasonCode.INSUFFICIENT_RESOURCE.value:
                        raise
                    instance = self.kernel.repo.instance(scope.owner_key, instance_id)
                    if (not self._eligible(instance, scope) or self.wallet.quote_available(scope.owner_key).available_minor
                            >= instance.checkpoint["params"]["reserve_minor"]):
                        raise
                    decision = self.wallet.audit_reservation_refusal(scope.owner_key, instance_id,
                        expected_instance_revision=instance.revision, now=now)
                    final = self._finish(scope, decision)
            self.wallet.complete_activity_command(scope.owner_key, receipt["recovery_id"], final)
            return final

    def advance(self, scope, instance_id, **command):
        return self._run("advance", scope, instance_id, **command)

    def pause(self, scope, instance_id, **command):
        return self._run("pause", scope, instance_id, **command)

    def recover(self, scope, *, now):
        with self.kernel.storage._lock:
            self._sweep(scope, now)
            return self.kernel.recover(scope, now=now)

    def tick(self, *, now):
        if not self.kernel.config.enabled or self.kernel.closed:
            return self.kernel.tick(now=now)
        with self.kernel.storage._lock:
            return self._tick_locked(now=now)

    def _tick_locked(self, *, now):
        cancelled, failures = 0, []
        scopes = self.kernel.access.scopes()
        for scope in scopes:
            try:
                cancelled += self._sweep(scope, now)
            except KernelError as error:
                failures.append(error.code)
        result = self.kernel.tick(now=now)
        # Catch a quote/first-reserve race inside A1.tick, which deliberately
        # returns command failures rather than raising them to its embedding.
        for scope in scopes:
            try:
                cancelled += self._sweep(scope, now)
            except KernelError as error:
                failures.append(error.code)
        return {**result, "cancelled_unfunded": cancelled, "failures": (*failures, *result["failures"])}
