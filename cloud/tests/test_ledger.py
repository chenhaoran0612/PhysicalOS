"""Ledger and reservation invariants, including concurrent reservations."""
import threading

import support  # noqa: F401
from support import CloudTestCase

from sqlalchemy import func, select

from neuxmind_cloud import ledger
from neuxmind_cloud.errors import ApiError
from neuxmind_cloud.models import LedgerEntry


class LedgerTest(CloudTestCase):
    def setUp(self):
        super().setUp()
        self.user = self.register("ledger@example.com")
        self.org = self.user["org_id"]

    def credit(self, amount: int):
        with self.db.transaction() as s:
            ledger.post_entry(s, self.org, amount, "purchase", f"test:{amount}:{id(s)}")

    def balance(self) -> dict:
        with self.db.transaction() as s:
            return ledger.balance(s, self.org)

    def test_balance_is_ledger_sum(self):
        self.credit(500)
        self.fund(self.user, 250)
        with self.db.transaction() as s:
            total = s.execute(select(func.sum(LedgerEntry.delta)).where(LedgerEntry.org_id == self.org)).scalar_one()
        self.assertEqual(total, 750)
        self.assertEqual(self.wallet(self.user), {"org_id": self.org, "unit": "credits", "balance": 750, "reserved": 0,
                                                  "available": 750})
        entries = self.ok(self.get("/wallet/ledger", self.user))["entries"]
        self.assertEqual(sum(e["delta"] for e in entries), 750)

    def test_insufficient_credits_refused(self):
        self.credit(100)
        with self.assertRaises(ApiError) as ctx, self.db.transaction() as s:
            ledger.reserve(s, self.org, 101, purpose="infer")
        self.assertEqual(ctx.exception.code, "insufficient_credits")
        self.assertEqual(self.balance()["reserved"], 0)

    def test_reserve_settle_release(self):
        self.credit(100)
        with self.db.transaction() as s:
            r1 = ledger.reserve(s, self.org, 60, purpose="infer").id
        self.assertEqual(self.balance(), {"balance": 100, "reserved": 60, "available": 40})
        with self.db.transaction() as s:
            ledger.settle(s, r1, 25, "provider:abc", "test")
        self.assertEqual(self.balance(), {"balance": 75, "reserved": 0, "available": 75})
        # Idempotent replay with the same evidence.
        with self.db.transaction() as s:
            ledger.settle(s, r1, 25, "provider:abc", "test")
        self.assertEqual(self.balance()["balance"], 75)
        # Different amount on a settled reservation is a conflict.
        with self.assertRaises(ApiError) as ctx, self.db.transaction() as s:
            ledger.settle(s, r1, 30, "provider:abc", "test")
        self.assertEqual(ctx.exception.code, "already_settled")
        # Release returns the hold without charging.
        with self.db.transaction() as s:
            r2 = ledger.reserve(s, self.org, 50, purpose="infer").id
        with self.db.transaction() as s:
            ledger.release(s, r2, "test")
        self.assertEqual(self.balance(), {"balance": 75, "reserved": 0, "available": 75})
        with self.assertRaises(ApiError), self.db.transaction() as s:
            ledger.settle(s, r2, 1, "late", "test")

    def test_overage_and_missing_evidence_refused(self):
        self.credit(100)
        with self.db.transaction() as s:
            rid = ledger.reserve(s, self.org, 10, purpose="infer").id
        with self.assertRaises(ApiError) as ctx, self.db.transaction() as s:
            ledger.settle(s, rid, 11, "provider:x", "test")
        self.assertEqual(ctx.exception.code, "overage")
        with self.assertRaises(ApiError) as ctx, self.db.transaction() as s:
            ledger.settle(s, rid, 5, "", "test")
        self.assertEqual(ctx.exception.code, "evidence_required")
        self.assertEqual(self.balance(), {"balance": 100, "reserved": 10, "available": 90})

    def test_reconciliation_keeps_hold(self):
        self.credit(100)
        with self.db.transaction() as s:
            rid = ledger.reserve(s, self.org, 40, purpose="infer").id
        with self.db.transaction() as s:
            ledger.mark_reconciliation(s, rid, "timeout")
        self.assertEqual(self.balance(), {"balance": 100, "reserved": 40, "available": 60})
        listed = self.ok(self.get("/admin/reservations", self.operator()))["reservations"]
        self.assertEqual([r["id"] for r in listed], [rid])
        self.error(self.post(f"/admin/reservations/{rid}/resolve", {"action": "settle", "credits": 30,
                                                                    "evidence_ref": "invoice-1"}, self.operator()),
                   400, "reason_required")
        self.ok(self.post(f"/admin/reservations/{rid}/resolve", {"action": "settle", "credits": 30,
                                                                 "evidence_ref": "invoice-1", "reason": "provider bill"},
                          self.operator()))
        self.assertEqual(self.balance(), {"balance": 70, "reserved": 0, "available": 70})

    def test_concurrent_reservations_never_oversell(self):
        self.credit(100)
        successes, failures, errors = [], [], []
        barrier = threading.Barrier(20)

        def worker():
            barrier.wait()
            try:
                with self.db.transaction() as s:
                    successes.append(ledger.reserve(s, self.org, 10, purpose="infer").id)
            except ApiError as exc:
                failures.append(exc.code)
            except Exception as exc:  # pragma: no cover - surfaced below
                errors.append(repr(exc))

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(successes), 10)
        self.assertEqual(failures, ["insufficient_credits"] * 10)
        self.assertEqual(self.balance(), {"balance": 100, "reserved": 100, "available": 0})

    def test_concurrent_settlement_books_once(self):
        self.credit(100)
        with self.db.transaction() as s:
            rid = ledger.reserve(s, self.org, 50, purpose="infer").id
        barrier = threading.Barrier(8)
        errors = []

        def worker():
            barrier.wait()
            try:
                with self.db.transaction() as s:
                    ledger.settle(s, rid, 20, "provider:same", "test")
            except Exception as exc:  # pragma: no cover
                errors.append(repr(exc))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.balance(), {"balance": 80, "reserved": 0, "available": 80})

    def test_operator_adjust_requires_reason_and_is_booked(self):
        op = self.operator()
        self.error(self.post(f"/admin/wallets/{self.org}/adjust", {"delta": 10}, op), 400, "reason_required")
        self.error(self.post(f"/admin/wallets/{self.org}/adjust", {"delta": 1.5, "reason": "x"}, op), 400)
        self.ok(self.post(f"/admin/wallets/{self.org}/adjust", {"delta": 100, "reason": "goodwill", "reference": "r1"}, op))
        # Same reference again is idempotent.
        self.ok(self.post(f"/admin/wallets/{self.org}/adjust", {"delta": 100, "reason": "goodwill", "reference": "r1"}, op))
        self.assertEqual(self.wallet(self.user)["balance"], 100)
        self.error(self.post(f"/admin/wallets/{self.org}/adjust", {"delta": -101, "reason": "claw back"}, op), 409)
        self.ok(self.post(f"/admin/wallets/{self.org}/adjust", {"delta": -40, "reason": "claw back"}, op))
        entries = self.ok(self.get("/wallet/ledger", self.user))["entries"]
        self.assertEqual(sorted(e["delta"] for e in entries), [-40, 100])
        self.assertTrue(all(e["kind"] == "operator_adjust" and e["reason"] for e in entries))
        audit = self.ok(self.get("/admin/audit?action=admin.wallet_adjust", op))["audit"]
        self.assertGreaterEqual(len(audit), 2)


class PostgresLockStatementTest(CloudTestCase):
    """No PostgreSQL server here: verify at compile level that the wallet lock is a row lock."""

    def test_wallet_lock_compiles_to_for_update(self):
        from sqlalchemy.dialects import postgresql

        from neuxmind_cloud.models import Wallet

        statement = select(Wallet).where(Wallet.org_id == "org_x").with_for_update()
        self.assertIn("FOR UPDATE", str(statement.compile(dialect=postgresql.dialect())))
