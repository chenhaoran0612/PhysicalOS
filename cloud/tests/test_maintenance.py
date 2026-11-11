"""Review C-05 (leaked reservations / sessions) and C-11 (operator resolving an in-flight hold)."""
import threading
import time

import support  # noqa: F401
from support import API, ECHO_LLM, CloudTestCase

from neuxmind_cloud import ledger
from neuxmind_cloud.maintenance import sweep
from neuxmind_cloud.models import GatewaySession, Reservation, UsageRecord
from neuxmind_cloud.upstream import EchoProvider


class BlockingEcho(EchoProvider):
    """Echo test double that waits for the test to release it (simulates a slow upstream)."""

    def __init__(self, gate: threading.Event, entered: threading.Event):
        super().__init__("ok")
        self.gate, self.entered = gate, entered

    def _result(self, messages, max_output_tokens):
        self.entered.set()
        self.gate.wait(10)
        return super()._result(messages, max_output_tokens)


class SweeperTest(CloudTestCase):
    def provider_factory(self):
        self.gate, self.entered = threading.Event(), threading.Event()
        self.blocking = False
        return lambda upstream: BlockingEcho(self.gate, self.entered) if self.blocking else EchoProvider("ok")

    def setUp(self):
        super().setUp()
        self.user = self.register("sweep@example.com")
        self.publish(ECHO_LLM)
        self.entitle(self.user, "echo-llm")
        self.fund(self.user, 1000)

    def open(self, request_id="s1", max_tokens=1000):
        return self.ok(self.post("/gateway/sessions", {"request_id": request_id, "model": "echo-llm",
                                                       "max_tokens": max_tokens}, self.user), 201)

    def call(self, session, body):
        return self.client.post(f"{API}/gateway/sessions/{session['session_id']}/v1/chat/completions", json=body,
                                headers={"Authorization": f"Bearer {session['access_token']}"})

    def age(self, model, **filters):
        with self.db.transaction() as s:
            for row in s.query(model).filter_by(**filters):
                row.created_at -= 10_000

    def test_crash_between_reserve_and_settle_goes_to_reconciliation(self):
        # Simulate a process that died after transaction 1 of /infer.
        with self.db.transaction() as s:
            rsv = ledger.reserve(s, self.user["org_id"], 7, purpose="infer", model_id="echo-llm",
                                 pricing=dict(ECHO_LLM["pricing"]))
            s.add(UsageRecord(org_id=self.user["org_id"], request_id="crashed", request_digest="d",
                              model_id="echo-llm", reservation_id=rsv.id, status="pending", hold_tokens=700))
            rid = rsv.id
        self.assertEqual(sweep(self.db, self.settings)["lost_calls"], 0)        # not stale yet
        self.age(UsageRecord, request_id="crashed")
        self.assertEqual(sweep(self.db, self.settings)["lost_calls"], 1)
        self.assertEqual(sweep(self.db, self.settings)["lost_calls"], 0)        # idempotent
        listed = self.ok(self.get("/admin/reservations", self.operator()))["reservations"]
        self.assertEqual([r["id"] for r in listed], [rid])
        usage = self.ok(self.get("/usage", self.user))["usage"][0]
        self.assertEqual(usage["status"], "reconciliation_required")
        self.assertEqual(self.wallet(self.user)["reserved"], 7)               # still held until an operator decides

    def test_crashed_session_call_no_longer_blocks_close(self):
        session = self.open()
        with self.db.transaction() as s:   # a call that never came back
            gws = s.get(GatewaySession, session["session_id"])
            gws.inflight_tokens += 300
            s.add(UsageRecord(org_id=gws.org_id, request_id="call_lost", request_digest="d", model_id="echo-llm",
                              session_id=gws.id, status="pending", hold_tokens=300))
        self.error(self.post(f"/gateway/sessions/{session['session_id']}/close", {"request_id": "c"}, self.user), 409,
                   "session_busy")
        self.age(UsageRecord, request_id="call_lost")
        self.assertEqual(self.ok(self.post("/admin/maintenance/sweep", {}, self.operator()))["lost_calls"], 1)
        state = self.ok(self.get(f"/gateway/sessions/{session['session_id']}", self.user))
        self.assertEqual((state["inflight_tokens"], state["unknown_calls"]), (0, 1))
        closed = self.ok(self.post(f"/gateway/sessions/{session['session_id']}/close", {"request_id": "c"}, self.user))
        self.assertEqual(closed["status"], "reconciliation_required")

    def test_expired_session_is_closed_and_settled_automatically(self):
        session = self.open()
        reply = self.ok(self.call(session, {"messages": [{"role": "user", "content": "hello"}]}))
        used = reply["neuxmind"]["used_tokens"]
        self.assertEqual(self.wallet(self.user)["reserved"], 10)
        with self.db.transaction() as s:
            s.get(GatewaySession, session["session_id"]).expires_at = time.time() - 1
        self.assertEqual(sweep(self.db, self.settings)["sessions_closed"], 1)
        wallet = self.wallet(self.user)
        self.assertEqual(wallet["reserved"], 0)
        self.assertEqual(wallet["balance"], 1000 - (-(-used * 10 // 1000)))
        state = self.ok(self.get(f"/gateway/sessions/{session['session_id']}", self.user))
        self.assertEqual((state["status"], state["close_result"]["metering_status"]), ("closed", "settled"))
        replay = self.ok(self.post(f"/gateway/sessions/{session['session_id']}/close", {"request_id": "late"},
                                   self.user))
        self.assertTrue(replay["replayed"])
        audit = self.ok(self.get("/admin/audit?action=gateway.session_close", self.operator()))["audit"]
        self.assertEqual(audit[0]["actor"], "system:sweeper")

    def test_orphan_held_reservation_is_flagged(self):
        with self.db.transaction() as s:
            rid = ledger.reserve(s, self.user["org_id"], 5, purpose="manual").id
        self.age(Reservation, id=rid)
        self.assertEqual(sweep(self.db, self.settings)["orphan_holds"], 1)
        with self.db.transaction() as s:
            self.assertEqual(s.get(Reservation, rid).status, "reconciliation_required")

    def test_operator_cannot_resolve_a_held_reservation(self):
        """C-11: resolving a hold whose call may still be running is refused."""
        with self.db.transaction() as s:
            rid = ledger.reserve(s, self.user["org_id"], 5, purpose="infer").id
        for action in ({"action": "release", "reason": "x"},
                       {"action": "settle", "credits": 1, "evidence_ref": "inv", "reason": "x"}):
            self.error(self.post(f"/admin/reservations/{rid}/resolve", action, self.operator()), 409,
                       "reservation_in_flight")
        held = self.ok(self.get("/admin/reservations?status=held", self.operator()))["reservations"]
        self.assertEqual([r["id"] for r in held], [rid])
        self.assertIn("age_seconds", held[0])

    def test_late_session_result_after_sweeper_marked_it_lost(self):
        session = self.open()
        self.blocking = True
        results = {}

        def worker():
            results["response"] = self.call(session, {"messages": [{"role": "user", "content": "slow"}]})

        thread = threading.Thread(target=worker)
        thread.start()
        self.assertTrue(self.entered.wait(10))
        sweep(self.db, self.settings, at=time.time() + 10_000)   # the call looks stale; session expired too
        self.gate.set()
        thread.join(10)
        self.error(results["response"], 409, "call_marked_lost")
        with self.db.transaction() as s:
            gws = s.get(GatewaySession, session["session_id"])
            self.assertEqual((gws.inflight_tokens, gws.unknown_calls, gws.used_tokens), (0, 1, 0))  # no double release
        # The same sweep closed the (expired) session once the lost call's hold was released.
        closed = self.ok(self.get(f"/gateway/sessions/{session['session_id']}", self.user))["close_result"]
        self.assertEqual(closed["status"], "reconciliation_required")
        self.assertEqual(self.wallet(self.user)["reserved"], 10)   # held for the operator, nothing double-booked

    def test_late_infer_result_after_operator_resolved_it(self):
        self.blocking = True
        results = {}

        def worker():
            results["response"] = self.post("/gateway/infer", {"model": "echo-llm", "input": "slow", "max_tokens": 10,
                                                               "request_id": "late-1"}, self.user)

        thread = threading.Thread(target=worker)
        thread.start()
        self.assertTrue(self.entered.wait(10))
        self.assertEqual(sweep(self.db, self.settings, at=time.time() + 10_000)["lost_calls"], 1)
        rid = self.ok(self.get("/admin/reservations", self.operator()))["reservations"][0]["id"]
        self.ok(self.post(f"/admin/reservations/{rid}/resolve", {"action": "release", "reason": "provider: nothing"},
                          self.operator()))
        self.gate.set()
        thread.join(10)
        self.error(results["response"], 409, "reservation_already_resolved")
        self.assertEqual(self.wallet(self.user), {"org_id": self.user["org_id"], "unit": "credits", "balance": 1000,
                                                  "reserved": 0, "available": 1000})
        usage = self.ok(self.get("/usage", self.user))["usage"][0]
        self.assertIsNotNone(usage["upstream_ref"])      # late evidence kept for the operator


class BackgroundSweeperTest(CloudTestCase):
    settings_overrides = {"maintenance_interval_seconds": 1}

    def test_background_thread_runs(self):
        user = self.register("bg@example.com")
        with self.db.transaction() as s:
            rid = ledger.reserve(s, user["org_id"], 0, purpose="manual").id
            s.get(Reservation, rid).created_at -= 100_000
        deadline = time.time() + 5
        while time.time() < deadline:
            with self.db.transaction() as s:
                if s.get(Reservation, rid).status == "reconciliation_required":
                    break
            time.sleep(0.1)
        with self.db.transaction() as s:
            self.assertEqual(s.get(Reservation, rid).status, "reconciliation_required")
        self.app.state.ctx.maintenance.stop()
