"""Explicitly enabled simulated economy domain; importing creates no tables."""
from datetime import timedelta

from .wallet import Evidence, Reservation, Transaction, Wallet, WalletQuote, WalletResult, SPEC, VERSION_KEY
from .access import WalletAccess
from .coordinator import WalletActivities, credit_due_allowances


def _has_history(reader):
    return reader.one("SELECT 1 FROM life_metadata WHERE key=?", (VERSION_KEY,)) is not None


def register_wallet_group(runtime, *, enabled, monthly_allowance=0, allowance_day=1):
    from ..activities.buy_drink import AtomicPurchaseLocal, PurchaseLocal
    from ..contracts.models import ActivityStatus
    from ..runtime import OptionalGroupBinding

    def bind(context):
        reader = context.access.reader
        account = reader.one("SELECT currency FROM life_wallet_accounts LIMIT 1", ()) if _has_history(reader) else None
        wallet = Wallet(account[0] if account else "CNY")
        context.registry.register_module(SPEC, wallet)
        # Retain the executors for pinned purchases without adding purchase intents.
        context.registry.register_executor(PurchaseLocal())
        context.registry.register_executor(AtomicPurchaseLocal())

        def settle(*, now):
            kernel = runtime.kernel
            for scope in context.access.scopes():
                if not context.access.authorize(scope, "kernel.cancel"):
                    continue
                for item in kernel.repo.active(scope.owner_key):
                    if (item.context_key == scope.context_key and item.template_id == "buy_drink"
                            and item.status != ActivityStatus.UNKNOWN
                            and kernel.repo.pending_result(scope.owner_key, item.instance_id) is None):
                        kernel.cancel(scope, item.instance_id, expected_revision=item.revision,
                                      command_id=f"disabled-purchase-{item.instance_id}-{item.revision}", now=now)

        next_allowance_check = None

        def commitments(*, now, force=False):
            nonlocal next_allowance_check
            if not force and next_allowance_check is not None and now < next_allowance_check:
                return
            credit_due_allowances(wallet, context.access, runtime.scheduler.capabilities, now=now,
                                  yuan=monthly_allowance, day=allowance_day)
            next_allowance_check = now + timedelta(seconds=30)

        def close():
            runtime._wallet = None

        return OptionalGroupBinding(value=wallet, settle=settle if context.mode == "settle_only" else None,
            commitments=commitments if context.mode == "active" and monthly_allowance > 0 else None, close=close)

    runtime.register_optional_group("wallet", bind, phase="pre_kernel", enabled=enabled, has_history=_has_history)


__all__ = ("Evidence", "Reservation", "Transaction", "Wallet", "WalletQuote", "WalletResult", "WalletAccess", "WalletActivities",
           "register_wallet_group")
