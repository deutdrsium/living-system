"""Single-writer simulated wallet on the existing LifeStorage transaction boundary.

Amounts are contract-v1 Money in integer minor units. All income and payments
are simulated. The hosted economy coordinator can credit a configured monthly
allowance for the current local month using a stable income key.
Callers provide stable business keys and evidence; command retries retain the
original operation ID, expected revision and evidence. The allowance scheduler
skips periods with committed income. Even a new operation ID cannot pay the same
reservation step or configured income period twice.
"""
from dataclasses import dataclass
from datetime import datetime
import json

from ..contracts.models import ActivityInstance, ActivityResult, ActivityStatus, Money, OutcomeStatus, Provenance, ReasonCode
from ..kernel.codec import decode, digest, encode, freeze, integer, moment, name, require, restore
from ..kernel.registry import ModuleSpec
from ..storage import StorageReader

MAX_MINOR = 2**63 - 1
# Deliberately supported ISO 4217 codes; unknown configurations fail closed.
SUPPORTED_CURRENCIES = frozenset(("CNY", "USD", "EUR", "JPY", "GBP", "HKD", "TWD", "SGD", "KRW", "AUD", "CAD", "CHF", "NZD"))
VERSION_KEY = "economy_schema_version"
MONTHLY_ALLOWANCE_PREFIX = "monthly-allowance:"
SPEC = ModuleSpec("economy", "1", ("economy.balance_minor", "economy.available_minor", "economy.purchase_budget_minor"),
                  emits=("kernel.purchase.progressed", "kernel.purchase.completed", "kernel.purchase.cancelled"),
                  consumes=("economy.purchase", "economy.city_trip", "economy.abandon_release"))
DDL = (
    """CREATE TABLE life_wallet_accounts (
        owner_key TEXT PRIMARY KEY, currency TEXT NOT NULL,
        balance_minor INTEGER NOT NULL CHECK(typeof(balance_minor)='integer' AND balance_minor>=0),
        revision INTEGER NOT NULL CHECK(revision>=1))""",
    """CREATE TABLE life_wallet_reservations (
        owner_key TEXT NOT NULL, reservation_id TEXT NOT NULL, instance_id TEXT NOT NULL,
        limit_minor INTEGER NOT NULL CHECK(typeof(limit_minor)='integer' AND limit_minor>0),
        amount_remaining_minor INTEGER NOT NULL CHECK(typeof(amount_remaining_minor)='integer'
            AND amount_remaining_minor>=0 AND amount_remaining_minor<=limit_minor),
        status TEXT NOT NULL CHECK(status IN ('active','released')), review_at TEXT NOT NULL,
        PRIMARY KEY(owner_key,reservation_id), UNIQUE(owner_key,instance_id),
        FOREIGN KEY(owner_key) REFERENCES life_wallet_accounts(owner_key))""",
    """CREATE TABLE life_wallet_transactions (
        owner_key TEXT NOT NULL, transaction_id TEXT NOT NULL, operation_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('income','spend','refund')),
        amount_minor INTEGER NOT NULL CHECK(typeof(amount_minor)='integer' AND amount_minor>0),
        currency TEXT NOT NULL, effective_at TEXT NOT NULL, instance_id TEXT,
        source_ref TEXT NOT NULL, provenance TEXT NOT NULL CHECK(provenance='simulated'),
        related_transaction_id TEXT, business_key TEXT NOT NULL,
        PRIMARY KEY(owner_key,transaction_id), UNIQUE(owner_key,kind,business_key),
        FOREIGN KEY(owner_key) REFERENCES life_wallet_accounts(owner_key))""",
    """CREATE TABLE life_wallet_operations (
        owner_key TEXT NOT NULL, operation_id TEXT NOT NULL, expected_revision INTEGER NOT NULL,
        request_hash TEXT NOT NULL, business_key TEXT NOT NULL, semantic_hash TEXT NOT NULL, response_json TEXT NOT NULL,
        PRIMARY KEY(owner_key,operation_id))""",
    """CREATE INDEX life_wallet_business ON life_wallet_operations(owner_key,business_key)""",
    """CREATE TABLE life_wallet_recovery_audit (
        owner_key TEXT NOT NULL, recovery_id TEXT NOT NULL, kind TEXT NOT NULL,
        instance_id TEXT NOT NULL, record_json TEXT NOT NULL,
        PRIMARY KEY(owner_key,recovery_id))""",
)


def amount(value, minimum=0):
    integer(value, minimum)
    require(value <= MAX_MINOR, "amount exceeds supported integer range")
    return value


def currency(value):
    require(type(value) is str and value in SUPPORTED_CURRENCIES,
            "currency must be a supported uppercase ISO 4217 code")
    return value


@dataclass(frozen=True)
class Evidence:
    source_ref: str
    effective_at: datetime
    provenance: Provenance
    instance_id: str | None = None


@dataclass(frozen=True)
class WalletQuote:
    owner_key: str
    currency: str
    balance_minor: int
    reserved_minor: int
    available_minor: int
    revision: int


@dataclass(frozen=True)
class WalletResult:
    wallet: WalletQuote
    reservation_id: str | None = None
    transaction_id: str | None = None


@dataclass(frozen=True)
class Reservation:
    reservation_id: str
    instance_id: str
    limit_minor: int
    amount_remaining_minor: int
    status: str
    review_at: datetime


@dataclass(frozen=True)
class Transaction:
    transaction_id: str
    operation_id: str
    kind: str
    amount_minor: int
    currency: str
    effective_at: datetime
    instance_id: str | None
    source_ref: str
    provenance: Provenance
    related_transaction_id: str | None


class Wallet:
    """Trusted local domain API; expose through an authorizing adapter if needed.

    Installation is explicit (normally ActivityKernel's enabled module install).
    An absent account is unknown and has revision 0; only an evidenced income
    creates it. No expiry job releases reservations: unknown operations retain
    their funds until an explicit, evidenced release/settlement decision.
    """
    def __init__(self, configured_currency):
        self.currency = currency(configured_currency)
        self.storage = None

    def install(self, storage):
        with storage.transaction() as db:
            row = db.execute("SELECT value FROM life_metadata WHERE key=?", (VERSION_KEY,)).fetchone()
            objects = {r["name"]: r["sql"] for r in db.execute(
                "SELECT name,sql FROM sqlite_master WHERE name GLOB 'life_wallet_*' AND sql IS NOT NULL")}
            if row is None:
                require(not objects, "unclaimed wallet schema already exists")
                for statement in DDL:
                    db.execute(statement)
                db.execute("INSERT INTO life_metadata VALUES (?, '2')", (VERSION_KEY,))
            else:
                require(row[0] in ("1", "2"), "unsupported wallet schema")
                for statement in DDL[:-1] if row[0] == "1" else DDL:
                    key = statement.split()[2]
                    require(key in objects and " ".join(objects[key].lower().split()) == " ".join(statement.lower().split()),
                            "wallet schema is incomplete or changed")
                if row[0] == "1":
                    require("life_wallet_recovery_audit" not in objects, "unclaimed wallet recovery audit")
                    db.execute(DDL[-1])
                    db.execute("UPDATE life_metadata SET value='2' WHERE key=?", (VERSION_KEY,))
            require(not db.execute("SELECT 1 FROM life_wallet_accounts WHERE currency<>? LIMIT 1",
                                   (self.currency,)).fetchone(), "configured wallet currency changed")
        self.storage = storage

    def _installed(self):
        require(self.storage is not None, "wallet has not been explicitly enabled", ReasonCode.MISSING_DEPENDENCY)

    def _quote(self, db, owner_key):
        row = db.execute("SELECT currency,balance_minor,revision FROM life_wallet_accounts WHERE owner_key=?",
                         (owner_key,)).fetchone()
        if row is None:
            return WalletQuote(owner_key, self.currency, 0, 0, 0, 0)
        require(row[0] == self.currency, "wallet currency mismatch")
        reserved = db.execute("""SELECT COALESCE(SUM(amount_remaining_minor),0) FROM life_wallet_reservations
            WHERE owner_key=? AND status='active'""", (owner_key,)).fetchone()[0]
        require(0 <= reserved <= row[1], "wallet reservation invariant violated")
        return WalletQuote(owner_key, row[0], row[1], reserved, row[1] - reserved, row[2])

    def quote_available(self, owner_key):
        self._installed()
        name(owner_key)
        with StorageReader(self.storage).locked():
            return self._quote(self.storage._connection, owner_key)

    def reservation(self, owner_key, reservation_id):
        self._installed()
        row = StorageReader(self.storage).one("""SELECT reservation_id,instance_id,limit_minor,
            amount_remaining_minor,status,review_at FROM life_wallet_reservations
            WHERE owner_key=? AND reservation_id=?""", (name(owner_key), name(reservation_id)))
        if row is None:
            return None
        return Reservation(*row[:5], moment(datetime.fromisoformat(row[5])))

    def transactions(self, owner_key):
        self._installed()
        rows = StorageReader(self.storage).all("""SELECT transaction_id,operation_id,kind,amount_minor,currency,
            effective_at,instance_id,source_ref,provenance,related_transaction_id FROM life_wallet_transactions
            WHERE owner_key=? ORDER BY rowid""", (name(owner_key),))
        return tuple(Transaction(*row[:5], moment(datetime.fromisoformat(row[5])), *row[6:8],
                                 Provenance(row[8]), row[9]) for row in rows)

    def _money(self, money):
        require(isinstance(money, Money) and currency(money.currency) == self.currency, "wallet currency mismatch")
        return amount(money.amount_minor, 1)

    def _execute(self, db, owner_key, *, operation_id, expected_revision, evidence, kind, payload, business_key,
                 acknowledgement=None):
        name(owner_key)
        name(operation_id)
        integer(expected_revision)
        require(isinstance(evidence, Evidence) and evidence.provenance == Provenance.SIMULATED,
                "only explicitly simulated financial evidence is supported")
        name(evidence.source_ref)
        moment(evidence.effective_at)
        if evidence.instance_id is not None:
            name(evidence.instance_id)
        semantic_hash = digest({"kind": kind, "payload": payload, "evidence": evidence})
        request_hash = digest({"semantic": semantic_hash, "expected_revision": expected_revision})
        prior = db.execute("SELECT request_hash,response_json FROM life_wallet_operations WHERE owner_key=? AND operation_id=?",
                           (owner_key, operation_id)).fetchone()
        if prior:
            require(prior[0] == request_hash, "wallet operation ID conflicts with original parameters")
            return decode(WalletResult, prior[1])
        prior = db.execute("""SELECT semantic_hash,response_json FROM life_wallet_operations
            WHERE owner_key=? AND business_key=? LIMIT 1""", (owner_key, business_key)).fetchone()
        if prior:
            require(prior[0] == semantic_hash, "wallet business key conflicts with original evidence or amount")
            result = decode(WalletResult, prior[1])
        else:
            quote = self._quote(db, owner_key)
            require(quote.revision == expected_revision, "wallet changed", ReasonCode.STALE_VERSION)
            if acknowledgement is None:
                result = self._mutate(db, owner_key, quote, operation_id, evidence, kind, payload, business_key)
            else:
                require(kind == "abandon_release_ack" and isinstance(acknowledgement, WalletResult),
                        "invalid wallet acknowledgement")
                result = acknowledgement
        db.execute("INSERT INTO life_wallet_operations VALUES (?,?,?,?,?,?,?)",
                   (owner_key, operation_id, expected_revision, request_hash, business_key, semantic_hash, encode(result)))
        return result

    def _mutate(self, db, owner, quote, operation_id, evidence, kind, payload, business_key):
        reservation_id = payload.get("reservation_id")
        transaction_id = None
        balance = quote.balance_minor
        if kind == "income":
            balance = amount(balance + payload["amount_minor"])
        else:
            require(quote.revision > 0, "wallet has no evidenced income", ReasonCode.INSUFFICIENT_RESOURCE)
            if kind == "planned_spend":
                require(payload["amount_minor"] <= quote.available_minor, "insufficient available balance",
                        ReasonCode.INSUFFICIENT_RESOURCE)
                balance -= payload["amount_minor"]
            elif kind == "reserve":
                review_at = restore(datetime, payload["review_at"])
                require(review_at > moment(evidence.effective_at), "reservation review must follow its creation")
                require(evidence.instance_id is not None, "reservation requires an activity instance")
                require(payload["amount_minor"] <= quote.available_minor, "insufficient available balance", ReasonCode.INSUFFICIENT_RESOURCE)
                require(not db.execute("SELECT 1 FROM life_wallet_reservations WHERE owner_key=? AND (reservation_id=? OR instance_id=?)",
                                       (owner, reservation_id, evidence.instance_id)).fetchone(), "reservation identity already used")
                db.execute("INSERT INTO life_wallet_reservations VALUES (?,?,?,?,?,'active',?)",
                           (owner, reservation_id, evidence.instance_id, payload["amount_minor"], payload["amount_minor"], payload["review_at"]))
            elif kind in ("increase", "spend", "release"):
                row = db.execute("""SELECT instance_id,limit_minor,amount_remaining_minor,status
                    FROM life_wallet_reservations WHERE owner_key=? AND reservation_id=?""", (owner, reservation_id)).fetchone()
                require(row is not None and row[0] == evidence.instance_id, "reservation belongs to another instance", ReasonCode.UNAUTHORIZED)
                require(row[3] == "active", "reservation has been released")
                if kind == "increase":
                    extra = payload["amount_minor"]
                    require(extra <= quote.available_minor, "insufficient available balance", ReasonCode.INSUFFICIENT_RESOURCE)
                    limit, remaining = amount(row[1] + extra), amount(row[2] + extra)
                    db.execute("UPDATE life_wallet_reservations SET limit_minor=?,amount_remaining_minor=? WHERE owner_key=? AND reservation_id=?",
                               (limit, remaining, owner, reservation_id))
                elif kind == "spend":
                    require(payload["amount_minor"] <= row[2], "payment exceeds remaining reservation", ReasonCode.INSUFFICIENT_RESOURCE)
                    balance -= payload["amount_minor"]
                    db.execute("UPDATE life_wallet_reservations SET amount_remaining_minor=? WHERE owner_key=? AND reservation_id=?",
                               (row[2] - payload["amount_minor"], owner, reservation_id))
                else:
                    db.execute("UPDATE life_wallet_reservations SET amount_remaining_minor=0,status='released' WHERE owner_key=? AND reservation_id=?",
                               (owner, reservation_id))
            elif kind == "refund":
                original = db.execute("""SELECT amount_minor,currency,instance_id FROM life_wallet_transactions
                    WHERE owner_key=? AND transaction_id=? AND kind='spend'""", (owner, payload["related_transaction_id"])).fetchone()
                require(original is not None and original[1] == self.currency and original[2] == evidence.instance_id,
                        "refund requires an original payment from this instance", ReasonCode.UNAUTHORIZED)
                refunded = db.execute("""SELECT COALESCE(SUM(amount_minor),0) FROM life_wallet_transactions
                    WHERE owner_key=? AND related_transaction_id=? AND kind='refund'""", (owner, payload["related_transaction_id"])).fetchone()[0]
                require(refunded + payload["amount_minor"] <= original[0], "refund exceeds original payment")
                balance = amount(balance + payload["amount_minor"])
            else:
                require(False, "unsupported wallet operation")
        if quote.revision == 0:
            db.execute("INSERT INTO life_wallet_accounts VALUES (?,?,?,1)", (owner, self.currency, balance))
        else:
            db.execute("UPDATE life_wallet_accounts SET balance_minor=?,revision=revision+1 WHERE owner_key=? AND revision=?",
                       (balance, owner, quote.revision))
        if kind == "planned_spend":
            kind = "spend"
        if kind in ("income", "spend", "refund"):
            transaction_id = "wallet-tx-" + digest({"owner": owner, "business_key": business_key})
            db.execute("INSERT INTO life_wallet_transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       (owner, transaction_id, operation_id, kind, payload["amount_minor"], self.currency,
                        moment(evidence.effective_at).isoformat(), evidence.instance_id, evidence.source_ref,
                        Provenance.SIMULATED.value, payload.get("related_transaction_id"), business_key))
        return WalletResult(self._quote(db, owner), reservation_id, transaction_id)

    def _write(self, owner_key, *, kind, payload, business_key, **command):
        self._installed()
        with self.storage.transaction() as db:
            if kind == "release":
                row = db.execute("SELECT instance_id FROM life_wallet_reservations WHERE owner_key=? AND reservation_id=?",
                                 (owner_key, payload["reservation_id"])).fetchone()
                instance = self._kernel_instance(db, owner_key, row[0]) if row else None
                require(instance is None or instance.status in (ActivityStatus.COMPLETED, ActivityStatus.CANCELLED),
                        "kernel reservation requires lifecycle settlement or checked abandonment cleanup",
                        ReasonCode.RESULT_UNKNOWN if instance and instance.status == ActivityStatus.UNKNOWN else ReasonCode.UNAUTHORIZED)
            return self._execute(db, owner_key, kind=kind, payload=payload, business_key=business_key, **command)

    def reserve(self, owner_key, reservation_id, money, *, review_at, **command):
        reservation_id = name(reservation_id)
        review_at = moment(review_at)
        require(review_at > moment(command["evidence"].effective_at), "reservation review must follow its creation")
        return self._write(owner_key, kind="reserve", payload={"reservation_id": reservation_id,
            "amount_minor": self._money(money), "review_at": review_at.isoformat()}, business_key=encode(("reserve", reservation_id)), **command)

    def increase_reservation(self, owner_key, reservation_id, money, **command):
        return self._write(owner_key, kind="increase", payload={"reservation_id": name(reservation_id),
            "amount_minor": self._money(money)}, business_key=encode(("increase", name(command["operation_id"]))), **command)

    def settle_step(self, owner_key, reservation_id, step_id, money, **command):
        return self._write(owner_key, kind="spend", payload={"reservation_id": name(reservation_id),
            "step_id": name(step_id), "amount_minor": self._money(money)}, business_key=encode(("spend", reservation_id, step_id)), **command)

    def release(self, owner_key, reservation_id, **command):
        return self._write(owner_key, kind="release", payload={"reservation_id": name(reservation_id)},
                           business_key=encode(("release", reservation_id)), **command)

    def _spend_planned(self, db, owner_key, expense_id, minor, at, expected_revision):
        """Settle a scheduled expense in the transaction that records its outcome."""
        return self._execute(db, owner_key, operation_id=expense_id, expected_revision=expected_revision,
            evidence=Evidence(expense_id, at, Provenance.SIMULATED), kind="planned_spend",
            payload={"amount_minor": amount(minor, 1)}, business_key=encode(("planned_spend", expense_id)))

    def record_income(self, owner_key, income_key, money, **command):
        """income_key is a stable configured rule+period or earned-work receipt ID."""
        return self._write(owner_key, kind="income", payload={"income_key": name(income_key), "amount_minor": self._money(money)},
                           business_key=encode(("income", income_key)), **command)

    def credit_monthly_allowance(self, owner_key, month, money, *, now):
        self._installed()
        income_key = MONTHLY_ALLOWANCE_PREFIX + month
        reader = StorageReader(self.storage)
        # The single writer lock covers both the receipt check and the command.
        # A committed period keeps its original amount, revision and evidence.
        with reader.locked():
            prior = reader.one("""SELECT 1 FROM life_wallet_transactions
                WHERE owner_key=? AND kind='income' AND business_key=?""", (owner_key, encode(("income", income_key))))
            if prior is not None:
                return
            self.record_income(owner_key, income_key, money, operation_id=income_key,
                expected_revision=self.quote_available(owner_key).revision,
                evidence=Evidence(income_key, now, Provenance.SIMULATED))

    def refund(self, owner_key, transaction_id, refund_key, money, **command):
        return self._write(owner_key, kind="refund", payload={"related_transaction_id": name(transaction_id),
            "refund_key": name(refund_key), "amount_minor": self._money(money)}, business_key=encode(("refund", transaction_id, refund_key)), **command)

    def _kernel_instance(self, db, owner_key, instance_id):
        # Read-only integration: the wallet never writes kernel records.
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='life_kernel_instances'").fetchone():
            return None
        row = db.execute("SELECT record_json FROM life_kernel_instances WHERE owner_key=? AND instance_id=?",
                         (owner_key, instance_id)).fetchone()
        return decode(ActivityInstance, row[0]) if row else None

    def recovery_audit(self, owner_key):
        self._installed()
        rows = StorageReader(self.storage).all("""SELECT record_json FROM life_wallet_recovery_audit
            WHERE owner_key=? AND kind<>'activity_command' ORDER BY rowid""", (name(owner_key),))
        return tuple(json.loads(row[0]) for row in rows)

    def begin_activity_command(self, owner_key, context_key, command_id, request):
        """Own durable receipts for the A2 policy's original advance/pause call."""
        self._installed()
        recovery_id = "wallet-command-" + digest((name(owner_key), name(command_id)))
        request_hash = digest({"namespace": "wallet.activities/v1", "owner_key": owner_key,
                               "context_key": context_key, "request": request})
        with self.storage.transaction() as db:
            row = db.execute("SELECT record_json FROM life_wallet_recovery_audit WHERE owner_key=? AND recovery_id=?",
                             (owner_key, recovery_id)).fetchone()
            if row:
                record = json.loads(row[0])
                require(record["request_hash"] == request_hash, "activity command ID conflicts with original parameters")
                return record
            prior = db.execute("SELECT request_hash FROM life_kernel_commands WHERE owner_key=? AND command_id=?",
                               (owner_key, command_id)).fetchone()
            if prior:
                kernel_hash = digest({"namespace": "life.kernel.command/v1", "owner_key": owner_key,
                                      "context_key": context_key, "request": request})
                require(prior[0] == kernel_hash, "activity command ID conflicts with an existing kernel command")
            record = {"recovery_id": recovery_id, "kind": "activity_command", "context_key": context_key,
                      "request_hash": request_hash, "request": request, "response": None}
            db.execute("INSERT INTO life_wallet_recovery_audit VALUES (?,?,?,?,?)",
                       (owner_key, recovery_id, record["kind"], request["instance_id"], encode(record)))
            return json.loads(encode(record))

    def complete_activity_command(self, owner_key, recovery_id, final):
        self._installed()
        with self.storage.transaction() as db:
            row = db.execute("SELECT record_json FROM life_wallet_recovery_audit WHERE owner_key=? AND recovery_id=?",
                             (owner_key, recovery_id)).fetchone()
            require(row is not None, "activity command receipt unavailable")
            record = json.loads(row[0])
            require(record["kind"] == "activity_command" and record["request"]["instance_id"] == final.instance_id
                    and owner_key == final.owner_key and record["context_key"] == final.context_key,
                    "activity command response belongs to another instance", ReasonCode.UNAUTHORIZED)
            response = json.loads(encode(final))
            require(record["response"] is None or record["response"] == response, "activity command response changed")
            db.execute("UPDATE life_wallet_recovery_audit SET record_json=? WHERE owner_key=? AND recovery_id=?",
                       (encode(freeze({**record, "response": response})), owner_key, recovery_id))

    def audit_reservation_refusal(self, owner_key, instance_id, *, expected_instance_revision, now):
        """Persist the cancellation decision before invoking the A1 command.

        The embedding coordinator resumes incomplete decisions after a crash.
        No financial fact or wallet revision changes for this audit-only write.
        """
        self._installed()
        integer(expected_instance_revision, 1)
        recovery_id = "wallet-refused-" + digest((owner_key, instance_id, expected_instance_revision))
        with self.storage.transaction() as db:
            row = db.execute("SELECT record_json FROM life_wallet_recovery_audit WHERE owner_key=? AND recovery_id=?",
                             (owner_key, recovery_id)).fetchone()
            if row:
                return json.loads(row[0])
            instance = self._kernel_instance(db, name(owner_key), name(instance_id))
            require(instance is not None and instance.revision == expected_instance_revision,
                    "purchase instance changed", ReasonCode.STALE_VERSION)
            require(instance.status == ActivityStatus.RUNNING and instance.phase == "awaiting_reservation"
                    and not instance.checkpoint["reserved"] and not instance.operation_refs,
                    "only a known unfunded purchase can be cancelled by this policy", ReasonCode.UNAUTHORIZED)
            require(not db.execute("SELECT 1 FROM life_kernel_results WHERE owner_key=? AND instance_id=? AND final_json IS NULL",
                                   (owner_key, instance_id)).fetchone(), "terminal proposal must be settled first", ReasonCode.RESULT_UNKNOWN)
            require(not db.execute("SELECT 1 FROM life_wallet_reservations WHERE owner_key=? AND instance_id=?",
                                   (owner_key, instance_id)).fetchone(), "purchase already has a reservation", ReasonCode.UNAUTHORIZED)
            quote = self._quote(db, owner_key)
            required = instance.checkpoint["params"]["reserve_minor"]
            require(quote.available_minor < required, "reservation is currently affordable", ReasonCode.STALE_VERSION)
            require(moment(now) >= instance.updated_at, "refusal audit cannot precede the instance", ReasonCode.STALE_VERSION)
            record = {"recovery_id": recovery_id, "kind": "reservation_refused", "instance_id": instance_id,
                      "context_key": instance.context_key, "instance_revision": instance.revision,
                      "reason_code": ReasonCode.INSUFFICIENT_RESOURCE.value, "available_minor": quote.available_minor,
                      "required_minor": required, "wallet_revision": quote.revision, "observed_at": moment(now),
                      "command_id": recovery_id + ":cancel", "completed": False}
            db.execute("INSERT INTO life_wallet_recovery_audit VALUES (?,?,?,?,?)",
                       (owner_key, recovery_id, record["kind"], instance_id, encode(record)))
            return json.loads(encode(record))

    def complete_refusal(self, owner_key, recovery_id):
        self._installed()
        with self.storage.transaction() as db:
            row = db.execute("SELECT record_json FROM life_wallet_recovery_audit WHERE owner_key=? AND recovery_id=?",
                             (name(owner_key), name(recovery_id))).fetchone()
            require(row is not None, "reservation refusal decision unavailable")
            record = json.loads(row[0])
            instance = self._kernel_instance(db, owner_key, record["instance_id"])
            require(record["kind"] == "reservation_refused" and instance is not None
                    and instance.status == ActivityStatus.CANCELLED and not instance.operation_refs,
                    "refusal cancellation has not committed", ReasonCode.UNAUTHORIZED)
            db.execute("UPDATE life_wallet_recovery_audit SET record_json=? WHERE owner_key=? AND recovery_id=?",
                       (encode({**record, "completed": True, "terminal_revision": instance.revision}), owner_key, recovery_id))

    def release_abandoned(self, owner_key, result_id, *, abandon_command_id, abandon_reason,
                          expected_instance_revision, operation_id, expected_revision, evidence):
        """Release only verified A1 abandonment; never pay abandoned proposals.

        Kernel checkpoint remains the immutable, explicitly abandoned proposal.
        The audit records actual paid/remaining amounts separately from it.
        """
        from ..kernel.repository import KernelRepository
        self._installed()
        name(owner_key)
        name(result_id)
        name(abandon_command_id)
        integer(expected_instance_revision, 1)
        with self.storage.transaction() as db:
            proposal, final = KernelRepository(self.storage).result(owner_key, result_id)
            require(final is not None and final.status == ActivityStatus.FAILED
                    and final.outcome.status == OutcomeStatus.FAILED and not final.operation_refs and not proposal.receipts,
                    "release requires a known failed abandoned local result", ReasonCode.UNAUTHORIZED)
            instance = self._kernel_instance(db, owner_key, final.instance_id)
            require(instance == final and instance.revision == expected_instance_revision,
                    "abandoned instance changed", ReasonCode.STALE_VERSION)
            require(isinstance(evidence, Evidence) and evidence.instance_id == instance.instance_id
                    and moment(evidence.effective_at) >= instance.updated_at, "invalid abandonment release evidence")
            request = {"action": "abandon_result", "result_id": result_id, "expected_revision": proposal.expected_revision,
                       "reason": abandon_reason, "now": final.updated_at}
            command = db.execute("SELECT request_hash,response_json FROM life_kernel_commands WHERE owner_key=? AND command_id=?",
                                 (owner_key, abandon_command_id)).fetchone()
            expected_hash = digest({"namespace": "life.kernel.command/v1", "owner_key": owner_key,
                                    "context_key": final.context_key, "request": request})
            require(command is not None and command[0] == expected_hash
                    and decode(ActivityInstance, command[1]) == final,
                    "no matching durable abandon command", ReasonCode.UNAUTHORIZED)
            require(all(not db.execute("SELECT 1 FROM life_kernel_effects WHERE owner_key=? AND effect_id=?",
                                       (owner_key, effect.effect_id)).fetchone() for effect in proposal.effects),
                    "abandoned effects were already committed", ReasonCode.RESULT_UNKNOWN)
            reservation_id = instance.instance_id + ":budget"
            reservation = db.execute("SELECT amount_remaining_minor FROM life_wallet_reservations WHERE owner_key=? AND reservation_id=?",
                                     (owner_key, reservation_id)).fetchone()
            require(reservation is not None, "abandoned reservation unavailable")
            released_minor = reservation[0]
            automatic = db.execute("SELECT response_json FROM life_wallet_operations WHERE owner_key=? AND operation_id=?",
                                   (owner_key, instance.instance_id + ":abandon-release")).fetchone()
            if automatic is not None:
                require(released_minor == 0, "automatic abandonment cleanup is incomplete")
                # Even a no-op is a durable caller command: preserve same-ID
                # conflict checks and replay before checking the current quote.
                return self._execute(db, owner_key, operation_id=operation_id, expected_revision=expected_revision,
                    evidence=evidence, kind="abandon_release_ack", payload={"result_id": result_id,
                        "abandon_command_id": abandon_command_id, "abandon_reason": abandon_reason,
                        "expected_instance_revision": expected_instance_revision, "reservation_id": reservation_id},
                    business_key=encode(("abandon_release_ack", operation_id)), acknowledgement=decode(WalletResult, automatic[0]))
            result = self._execute(db, owner_key, operation_id=operation_id, expected_revision=expected_revision,
                evidence=evidence, kind="release", payload={"reservation_id": reservation_id},
                business_key=encode(("release", reservation_id)))
            recovery_id = "wallet-abandoned-" + digest((owner_key, result_id))
            if not db.execute("SELECT 1 FROM life_wallet_recovery_audit WHERE owner_key=? AND recovery_id=?",
                              (owner_key, recovery_id)).fetchone():
                paid = db.execute("SELECT COALESCE(SUM(amount_minor),0) FROM life_wallet_transactions WHERE owner_key=? AND instance_id=? AND kind='spend'",
                                  (owner_key, instance.instance_id)).fetchone()[0]
                record = {"kind": "abandoned_release", "result_id": result_id, "instance_id": instance.instance_id,
                          "instance_revision": instance.revision, "abandon_command_id": abandon_command_id,
                          "abandon_reason": abandon_reason, "operation_id": operation_id, "evidence": evidence,
                          "reason_code": ReasonCode.INVALID_CONFIG, "actual_paid_minor": paid,
                          "released_minor": released_minor, "actual_remaining_minor": 0,
                          "abandoned_checkpoint": instance.checkpoint}
                db.execute("INSERT INTO life_wallet_recovery_audit VALUES (?,?,?,?,?)",
                           (owner_key, recovery_id, "abandoned_release", instance.instance_id, encode(record)))
            return result

    def _abandon_effect(self, db, instance, effect):
        # Disabled safety settlement may use a fresh, never-installed Wallet.
        # Every read/write in this hook must use the caller's transaction.
        require(instance.status == ActivityStatus.FAILED and not instance.operation_refs
                and self._kernel_instance(db, instance.owner_key, instance.instance_id) == instance,
                "cleanup requires the staged known local failure", ReasonCode.UNAUTHORIZED)
        result_id = "kernel-result-" + instance.instance_id
        row = db.execute("SELECT proposal_json,final_json FROM life_kernel_results WHERE owner_key=? AND result_id=?",
                         (instance.owner_key, result_id)).fetchone()
        require(row is not None, "cleanup proposal unavailable")
        proposal = decode(ActivityResult, row[0])
        require(row[1] is None and not proposal.receipts and proposal.expected_revision == instance.revision - 1,
                "cleanup requires an unsettled local proposal", ReasonCode.UNAUTHORIZED)
        require(effect.effect_id == instance.instance_id + ":abandon-release"
                and effect.payload == {"reservation_id": instance.instance_id + ":budget"}
                and not effect.source_refs and moment(effect.effective_at) == instance.updated_at,
                "invalid abandonment cleanup effect")
        require(all(not db.execute("SELECT 1 FROM life_kernel_effects WHERE owner_key=? AND effect_id=?",
                                  (instance.owner_key, item.effect_id)).fetchone() for item in proposal.effects),
                "abandoned effects were already committed", ReasonCode.RESULT_UNKNOWN)
        reservation_id = effect.payload["reservation_id"]
        reservation = db.execute("""SELECT amount_remaining_minor FROM life_wallet_reservations
            WHERE owner_key=? AND reservation_id=? AND instance_id=?""",
            (instance.owner_key, reservation_id, instance.instance_id)).fetchone()
        require(reservation is not None, "abandoned reservation unavailable")
        quote = self._quote(db, instance.owner_key)
        evidence = Evidence(effect.effect_id, effect.effective_at, Provenance.SIMULATED, instance.instance_id)
        self._execute(db, instance.owner_key, operation_id=effect.effect_id, expected_revision=quote.revision,
                      evidence=evidence, kind="release", payload={"reservation_id": reservation_id},
                      business_key=encode(("release", reservation_id)))
        paid = db.execute("""SELECT COALESCE(SUM(amount_minor),0) FROM life_wallet_transactions
            WHERE owner_key=? AND instance_id=? AND kind='spend'""", (instance.owner_key, instance.instance_id)).fetchone()[0]
        record = {"kind": "abandoned_release", "result_id": result_id, "instance_id": instance.instance_id,
                  "instance_revision": instance.revision, "cleanup_effect_id": effect.effect_id,
                  "reason_code": instance.reason_code, "actual_paid_minor": paid,
                  "released_minor": reservation[0], "actual_remaining_minor": 0,
                  "abandoned_checkpoint": instance.checkpoint}
        db.execute("INSERT INTO life_wallet_recovery_audit VALUES (?,?,?,?,?)",
                   (instance.owner_key, "wallet-abandoned-" + digest((instance.owner_key, result_id)),
                    "abandoned_release", instance.instance_id, encode(record)))
        return True

    def apply_effect(self, db, instance, effect):
        require(effect.target_owner == SPEC.name and effect.instance_id == instance.instance_id, "invalid wallet effect")
        if effect.kind == "economy.abandon_release":
            return self._abandon_effect(db, instance, effect)
        # Pure executor proposals are checked against durable local progress,
        # then wallet+kernel effects+outbox commit together in the caller's txn.
        if effect.kind == "economy.city_trip":
            from ..activities.city_trip import trip_commands
            require(instance.template_id.startswith("city_trip_"), "invalid city trip wallet effect")
            commands = trip_commands(instance)
        else:
            from ..activities.buy_drink import purchase_commands
            require(effect.kind == "economy.purchase", "invalid wallet effect")
            commands = purchase_commands(instance)
        require(instance.checkpoint["params"]["currency"] == self.currency, "purchase currency differs from wallet")
        matching = [command for command in commands if command["effect_id"] == effect.effect_id]
        require(len(matching) == 1 and effect.payload == matching[0]["payload"], "payment is not backed by persisted purchase progress")
        command = matching[0]
        quote = self._quote(db, instance.owner_key)
        prior = db.execute("SELECT expected_revision FROM life_wallet_operations WHERE owner_key=? AND operation_id=?",
                           (instance.owner_key, effect.effect_id)).fetchone()
        evidence = Evidence(effect.effect_id, effect.effective_at, Provenance.SIMULATED, instance.instance_id)
        payload = dict(command["payload"])
        expected_revision = payload.pop("expected_revision", prior[0] if prior else quote.revision)
        self._execute(db, instance.owner_key, operation_id=effect.effect_id, expected_revision=expected_revision,
                      evidence=evidence, kind=command["kind"], payload=payload, business_key=command["business_key"])
        return True
