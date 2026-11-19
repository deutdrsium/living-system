"""Read-only wallet projections composed with an explicit trusted A1 adapter."""
from dataclasses import replace
import json

from ..contracts.models import StateQuality, StateValue
from ..kernel.codec import freeze, require
from ..kernel.repository import KernelRepository
from ..storage import StorageReader
from .wallet import SPEC


def wallet_page(wallet, *, owner_key, context_key, limit=20, before=None):
    """Project an already authorized owner without installing or mutating anything.

    The short storage lock keeps the quote and rows consistent with the local
    single writer. No SQL transaction, kernel snapshot or recovery is started.
    Only a pinned, same-context activity can supply a human-readable label.
    """
    wallet._installed()
    with StorageReader(wallet.storage).locked():
        db = wallet.storage._connection
        quote = wallet._quote(db, owner_key)
        reservations = db.execute("""SELECT reservation_id,instance_id,limit_minor,
            amount_remaining_minor,status,review_at FROM life_wallet_reservations
            WHERE owner_key=? AND status='active' ORDER BY reservation_id""", (owner_key,)).fetchall()
        sql = """SELECT transaction_id,kind,amount_minor,currency,effective_at,instance_id
            FROM life_wallet_transactions WHERE owner_key=?"""
        args = [owner_key]
        if before is not None:
            sql += " AND (effective_at,transaction_id)<(?,?)"
            args.extend(before)
        rows = db.execute(sql + " ORDER BY effective_at DESC,transaction_id DESC LIMIT ?",
                          (*args, limit + 1)).fetchall()
        labels = {}
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='life_kernel_instances'").fetchone():
            for identifier in {row["instance_id"] for row in (*reservations, *rows[:limit])} - {None}:
                row = db.execute("""SELECT json_extract(record_json,'$.template_id') FROM life_kernel_instances
                    WHERE owner_key=? AND instance_id=? AND json_extract(record_json,'$.context_key')=?""",
                    (owner_key, identifier, context_key)).fetchone()
                # Current templates have no presentation title. Do not display
                # an opaque template/instance ID as if it were an activity name.
                labels[identifier] = {"buy_drink": "买饮料"}.get(row[0]) if row else None
    last = rows[limit - 1] if len(rows) > limit else None
    return {"enabled": True, "currency": quote.currency,
        "minor_unit_digits": 0 if quote.currency in {"JPY", "KRW"} else 2,
        "balance_minor": quote.balance_minor, "reserved_minor": quote.reserved_minor,
        "available_minor": quote.available_minor,
        "reservations": [{"reservation_id": row["reservation_id"], "activity_label": labels.get(row["instance_id"]),
            "limit_minor": row["limit_minor"], "amount_remaining_minor": row["amount_remaining_minor"],
            "status": row["status"], "review_at": row["review_at"]} for row in reservations],
        "transactions": [{"transaction_id": row["transaction_id"], "kind": row["kind"],
            "amount_minor": row["amount_minor"], "currency": row["currency"], "occurred_at": row["effective_at"],
            "activity_label": labels.get(row["instance_id"])} for row in rows[:limit]],
        "next_before": json.dumps([last["effective_at"], last["transaction_id"]], separators=(",", ":")) if last else None}


class WalletAccess:
    """Delegate authority/audience/agenda to the host; project no invented income.

    available_minor always means actual balance minus all active reservations.
    purchase_budget_minor is a gate-only proof of this context's already secured
    purchase budget (including stages already paid). Recovery must not demand
    the full upper quote a second time. New candidates gate on available_minor.
    Ambiguous multiple paused purchases fail closed on the resume resource.
    """
    def __init__(self, host_access, wallet):
        self.host = host_access
        self.wallet = wallet

    def __getattr__(self, name):
        return getattr(self.host, name)

    def scopes(self):
        return self.host.scopes()

    def authorize(self, scope, action):
        return self.host.authorize(scope, action)

    def audience(self, scope):
        return self.host.audience(scope)

    def snapshot(self, scope, now):
        from ..activities.buy_drink import AVAILABLE, PURCHASE_BUDGET
        snapshot = self.host.snapshot(scope, now)
        require(snapshot.state.owner_key == scope.owner_key, "snapshot owner mismatch")
        self.wallet._installed()
        with StorageReader(self.wallet.storage).locked():
            quote = self.wallet.quote_available(scope.owner_key)
            budget = quote.available_minor
            active = tuple(instance for instance in KernelRepository(self.wallet.storage).active(scope.owner_key)
                           if instance.context_key == scope.context_key and instance.template_id == "buy_drink")
            if len(active) > 1:
                budget = None
            elif active:
                instance = active[0]
                reservation = self.wallet.reservation(scope.owner_key, instance.instance_id + ":budget")
                if instance.checkpoint.get("reserved"):
                    expected_remaining = instance.checkpoint["params"]["reserve_minor"] - sum(
                        payment["amount_minor"] for payment in instance.checkpoint["params"]["payments"]
                        [:instance.checkpoint["completed_units"]["paid_steps"]])
                    budget = (instance.checkpoint["params"]["reserve_minor"] if reservation is not None
                              and reservation.status == "active" and reservation.amount_remaining_minor == expected_remaining
                              and reservation.limit_minor == instance.checkpoint["params"]["reserve_minor"] else None)
            values = dict(snapshot.state.values)
            for key, value in (("economy.balance_minor", quote.balance_minor), (AVAILABLE, quote.available_minor),
                               (PURCHASE_BUDGET, budget)):
                known = quote.revision > 0 and value is not None
                values[key] = StateValue(value if known else None, now, max(1, quote.revision), (),
                                         StateQuality.VALID if known else StateQuality.UNKNOWN)
            # A1 has no instance argument on snapshot(): two purchases in one
            # context cannot identify which secured budget the gate is asking
            # about. Mark the capability unavailable so the reason is
            # missing_dependency, rather than pretending the owner lacks cash.
            capabilities = tuple(key for key in snapshot.capabilities if key != SPEC.name)
            if len(active) <= 1:
                capabilities = tuple(dict.fromkeys((*capabilities, SPEC.name)))
            return replace(snapshot, state=replace(snapshot.state, values=freeze(values)), capabilities=capabilities)
