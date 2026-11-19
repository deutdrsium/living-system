"""Wallet invariants on temporary SQLite; no real funds/configuration."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from tempfile import TemporaryDirectory
import unittest

from test_kernel_support import NOW, ROOT, m
from good_life.economy import Evidence, Wallet
from good_life.economy.wallet import MAX_MINOR, VERSION_KEY
from good_life.kernel import ActivityKernel, ComponentRegistry, KernelError
from good_life.activities.buy_drink import register_buy_drink
from good_life.storage import LifeStorage


class WalletDomainTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix=".wallet-test-", dir=ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.storage = LifeStorage.open(self.temporary.name)
        self.addCleanup(lambda: self.storage.close())
        self.wallet = Wallet("CNY")
        self.wallet.install(self.storage)
        self.owner = "owner-a"
        self.initial = self.command("income-initial", revision=0, instance=None)
        self.wallet.record_income(self.owner, "allowance-rule-v1:2026-11", m.Money(100, "CNY"), **self.initial)

    def command(self, operation_id, *, revision=None, instance="activity-a"):
        return dict(operation_id=operation_id,
                    expected_revision=self.wallet.quote_available(self.owner).revision if revision is None else revision,
                    evidence=Evidence(operation_id, NOW, m.Provenance.SIMULATED, instance))

    def reserve(self, minor=36, reservation="budget-a", **command):
        return self.wallet.reserve(self.owner, reservation, m.Money(minor, "CNY"),
                                   review_at=NOW + timedelta(hours=1), **(command or self.command("reserve-a")))

    def quote(self):
        q = self.wallet.quote_available(self.owner)
        return q.balance_minor, q.reserved_minor, q.available_minor

    def spend(self, step, minor, **command):
        return self.wallet.settle_step(self.owner, "budget-a", step, m.Money(minor, "CNY"),
                                       **(command or self.command("pay-" + step)))

    def test_100_reserve_36_pay_6_20_6_release_4_and_replay(self):
        reserve_command = self.command("reserve-a")
        original = self.reserve(**reserve_command)
        self.assertEqual(self.quote(), (100, 36, 64))
        for step, minor, expected in (("travel-out", 6, (94, 30, 64)), ("drink", 20, (74, 10, 64)),
                                      ("travel-back", 6, (68, 4, 64))):
            command = self.command("pay-" + step)
            result = self.spend(step, minor, **command)
            self.assertEqual(self.quote(), expected)
            self.assertEqual(self.spend(step, minor, **command), result)
        release_command = self.command("release")
        released = self.wallet.release(self.owner, "budget-a", **release_command)
        self.assertEqual(self.quote(), (68, 0, 68))
        self.assertEqual(self.wallet.release(self.owner, "budget-a", **release_command), released)
        self.assertEqual(self.reserve(**reserve_command), original)
        self.assertEqual(len(self.wallet.transactions(self.owner)), 4)
        self.assertEqual(self.wallet.reservation(self.owner, "budget-a").status, "released")
        self.assertEqual(self.storage._connection.execute("SELECT value FROM life_metadata WHERE key='schema_version'").fetchone()[0],
                         "good-life.storage.v1")

    def test_income_period_and_payment_step_deduplicate_even_with_new_operation_id(self):
        alias = {**self.initial, "operation_id": "income-replayed-by-another-worker", "expected_revision": 1}
        self.wallet.record_income(self.owner, "allowance-rule-v1:2026-11", m.Money(100, "CNY"), **alias)
        self.assertEqual(self.quote(), (100, 0, 100))
        self.reserve()
        command = self.command("stage-1")
        result = self.spend("step-1", 6, **command)
        duplicate = {**command, "operation_id": "different-command-same-receipt", "expected_revision": result.wallet.revision}
        self.assertEqual(self.spend("step-1", 6, **duplicate), result)
        self.assertEqual(self.quote(), (94, 30, 64))
        with self.assertRaises(KernelError):
            self.spend("step-1", 7, **duplicate)
        with self.assertRaises(KernelError):
            self.wallet.record_income(self.owner, "allowance-rule-v1:2026-11", m.Money(101, "CNY"),
                                      **self.command("different-income", instance=None))
        self.assertEqual(len(self.wallet.transactions(self.owner)), 2)

    def test_refunds_preserve_original_payment_and_cannot_exceed_it(self):
        self.reserve()
        payment = self.spend("drink", 20)
        command = self.command("refund-part-1")
        refund = self.wallet.refund(self.owner, payment.transaction_id, "merchant-refund-1", m.Money(7, "CNY"), **command)
        self.assertEqual(self.quote(), (87, 16, 71))
        self.assertEqual(self.wallet.refund(self.owner, payment.transaction_id, "merchant-refund-1", m.Money(7, "CNY"), **command), refund)
        self.wallet.refund(self.owner, payment.transaction_id, "merchant-refund-2", m.Money(13, "CNY"), **self.command("refund-part-2"))
        with self.assertRaises(KernelError):
            self.wallet.refund(self.owner, payment.transaction_id, "invented-refund-3", m.Money(1, "CNY"), **self.command("excess-refund"))
        self.assertEqual(self.quote(), (100, 16, 84))
        self.assertEqual([tx.kind for tx in self.wallet.transactions(self.owner)], ["income", "spend", "refund", "refund"])
        self.assertEqual(self.wallet.transactions(self.owner)[2].related_transaction_id, payment.transaction_id)

    def test_concurrent_reservations_cannot_double_book_balance(self):
        commands = (self.command("reserve-one", revision=1, instance="instance-one"),
                    self.command("reserve-two", revision=1, instance="instance-two"))
        def attempt(item):
            index, command = item
            try:
                return self.reserve(70, reservation="reservation-" + str(index), **command)
            except KernelError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = tuple(pool.map(attempt, enumerate(commands)))
        self.assertEqual(sum(not isinstance(result, str) for result in results), 1)
        self.assertIn("stale_version", results)
        self.assertEqual(self.quote(), (100, 70, 30))
        with self.assertRaises(KernelError) as caught:
            self.reserve(70, reservation="new-budget", **self.command("retry", instance="instance-three"))
        self.assertEqual(caught.exception.code, "insufficient_resource")

    def test_reservation_increase_rechecks_balance_and_step_limit(self):
        self.reserve()
        with self.assertRaises(KernelError):
            self.spend("too-much", 37)
        command = self.command("increase")
        result = self.wallet.increase_reservation(self.owner, "budget-a", m.Money(10, "CNY"), **command)
        self.assertEqual(self.wallet.increase_reservation(self.owner, "budget-a", m.Money(10, "CNY"), **command), result)
        self.assertEqual(self.quote(), (100, 46, 54))
        with self.assertRaises(KernelError):
            self.wallet.increase_reservation(self.owner, "budget-a", m.Money(55, "CNY"), **self.command("overdraft"))
        self.spend("larger", 40)
        self.assertEqual(self.quote(), (60, 6, 54))

    def test_wrong_instance_owner_revision_and_money_fail_without_writes(self):
        self.reserve()
        for money in (m.Money(1, "USD"), m.Money(-1, "CNY"), m.Money(True, "CNY"),
                      m.Money(1.5, "CNY"), m.Money(MAX_MINOR + 1, "CNY")):
            with self.subTest(money=money), self.assertRaises(KernelError):
                self.wallet.settle_step(self.owner, "budget-a", "bad", money, **self.command("bad"))
        with self.assertRaises(KernelError) as caught:
            self.spend("wrong-instance", 1, **self.command("wrong-instance", instance="other-instance"))
        self.assertEqual(caught.exception.code, "unauthorized")
        with self.assertRaises(KernelError) as caught:
            self.spend("stale", 1, **self.command("stale", revision=1))
        self.assertEqual(caught.exception.code, "stale_version")
        self.assertEqual(self.quote(), (100, 36, 64))
        with self.assertRaises(KernelError):
            Wallet("USD").install(self.storage)
        with self.assertRaises(KernelError):
            self.wallet.record_income(self.owner, "overflow", m.Money(MAX_MINOR, "CNY"), **self.command("overflow"))

    def test_cross_owner_ids_are_isolated_and_unknown_review_does_not_auto_release(self):
        self.reserve()
        self.wallet.record_income("owner-b", "allowance-rule-v1:2026-11", m.Money(50, "CNY"),
                                  **{**self.initial, "expected_revision": 0})
        self.assertEqual(self.wallet.quote_available("owner-b").balance_minor, 50)
        self.assertIsNone(self.wallet.reservation("owner-b", "budget-a"))
        self.assertEqual(self.wallet.reservation(self.owner, "budget-a").review_at, NOW + timedelta(hours=1))
        self.assertEqual(self.quote(), (100, 36, 64))
        self.assertEqual(self.wallet.quote_available("no-account").revision, 0)

    def test_default_disabled_does_not_install_or_write_wallet_schema(self):
        with TemporaryDirectory(prefix=".wallet-disabled-", dir=ROOT) as directory:
            storage = LifeStorage.open(directory)
            try:
                registry = ComponentRegistry()
                wallet, _ = register_buy_drink(registry, configured_currency="CNY", reserve_minor=36,
                                               payments=({"at_s": 60, "amount_minor": 32},))
                kernel = ActivityKernel(storage, registry)
                self.assertEqual(kernel.tick(now=NOW)["started"], 0)
                self.assertIsNone(storage._connection.execute("SELECT 1 FROM life_metadata WHERE key=?", (VERSION_KEY,)).fetchone())
                self.assertFalse(storage._connection.execute("SELECT 1 FROM sqlite_master WHERE name GLOB 'life_wallet_*'").fetchone())
                with self.assertRaises(KernelError):
                    wallet.record_income("owner", "initial", m.Money(100, "CNY"), **self.initial)
                kernel.close()
            finally:
                storage.close()

    def test_v1_migration_only_adds_audit_and_preserves_existing_financial_history(self):
        self.reserve()
        self.spend("already-paid", 6)
        before = (self.quote(), self.wallet.transactions(self.owner), self.wallet.reservation(self.owner, "budget-a"))
        with self.storage.transaction() as db:
            db.execute("DROP TABLE life_wallet_recovery_audit")
            db.execute("UPDATE life_metadata SET value='1' WHERE key=?", (VERSION_KEY,))
        self.wallet.install(self.storage)
        self.assertEqual((self.quote(), self.wallet.transactions(self.owner), self.wallet.reservation(self.owner, "budget-a")), before)
        self.assertEqual(self.storage._connection.execute("SELECT value FROM life_metadata WHERE key=?", (VERSION_KEY,)).fetchone()[0], "2")
        self.assertEqual(self.wallet.recovery_audit(self.owner), ())
        self.wallet.install(self.storage)
        self.assertEqual(self.quote(), (94, 30, 64))

    def test_unknown_or_lowercase_currency_configurations_are_rejected(self):
        for code in ("TST", "AAA", "cny", "CNY ", None):
            with self.subTest(code=code), self.assertRaises(KernelError):
                Wallet(code)

    def test_schema_install_is_idempotent_and_rejects_missing_or_unclaimed_objects(self):
        self.wallet.install(self.storage)
        self.assertEqual(self.quote(), (100, 0, 100))
        with self.storage.transaction() as db:
            db.execute("DROP INDEX life_wallet_business")
        with self.assertRaises(KernelError):
            self.wallet.install(self.storage)
        with TemporaryDirectory(prefix=".wallet-schema-", dir=ROOT) as directory:
            storage = LifeStorage.open(directory)
            try:
                with storage.transaction() as db:
                    db.execute("CREATE TABLE life_wallet_unclaimed (value TEXT)")
                with self.assertRaises(KernelError):
                    Wallet("CNY").install(storage)
                self.assertIsNone(storage._connection.execute("SELECT 1 FROM life_metadata WHERE key=?", (VERSION_KEY,)).fetchone())
            finally:
                storage.close()


if __name__ == "__main__":
    unittest.main()
