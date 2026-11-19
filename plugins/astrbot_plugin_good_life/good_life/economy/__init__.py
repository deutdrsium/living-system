"""Explicitly enabled simulated economy domain; importing creates no tables."""
from .wallet import Evidence, Reservation, Transaction, Wallet, WalletQuote, WalletResult, SPEC, VERSION_KEY
from .access import WalletAccess
from .coordinator import WalletActivities


def _has_history(reader):
    return reader.one("SELECT 1 FROM life_metadata WHERE key=?", (VERSION_KEY,)) is not None


def register_wallet_group(runtime, *, enabled):
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

        def close():
            runtime._wallet = None

        return OptionalGroupBinding(value=wallet, settle=settle if context.mode == "settle_only" else None, close=close)

    runtime.register_optional_group("wallet", bind, phase="pre_kernel", enabled=enabled, has_history=_has_history)


__all__ = ("Evidence", "Reservation", "Transaction", "Wallet", "WalletQuote", "WalletResult", "WalletAccess", "WalletActivities",
           "register_wallet_group")
