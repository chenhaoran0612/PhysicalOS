"""Orders, payment providers, webhook signatures and idempotent crediting."""
import support  # noqa: F401
from support import CloudTestCase, SANDBOX_SECRET

from neuxmind_cloud.payments import sign_webhook


class PaymentsTest(CloudTestCase):
    def setUp(self):
        super().setUp()
        self.user = self.register("buyer@example.com")
        self.ok(self.post("/admin/credit-packs", {"id": "starter", "name": "Starter", "credits": 1000,
                                                  "price_minor": 990, "currency": "cny"}, self.operator()), 201)

    def order(self) -> dict:
        return self.ok(self.post("/purchases", {"kind": "credit_pack", "pack_id": "starter"}, self.user), 201)["order"]

    def paid_event(self, order: dict, event_id: str = "evt_1", **extra) -> dict:
        return {"event_id": event_id, "type": "payment.succeeded", "order_id": order["id"],
                "amount_minor": order["price_minor"], "currency": order["currency"], **extra}

    def test_order_is_pending_and_does_not_credit(self):
        order = self.order()
        self.assertEqual(order["status"], "pending")
        self.assertEqual((order["credits"], order["price_minor"], order["currency"]), (1000, 990, "CNY"))
        self.assertEqual(self.wallet(self.user)["balance"], 0)
        packs = self.ok(self.get("/catalog/credit-packs", self.user))["credit_packs"]
        self.assertEqual([p["id"] for p in packs], ["starter"])

    def test_real_channels_report_not_configured(self):
        order = self.order()
        for provider in ("stripe", "alipay", "wechatpay"):
            self.error(self.post(f"/payments/{provider}/checkout", {"order_id": order["id"]}, self.user), 503,
                       "provider_not_configured")
            self.error(self.webhook(provider, self.paid_event(order)), 503, "provider_not_configured")
        self.error(self.post("/payments/bitcoin/checkout", {"order_id": order["id"]}, self.user), 404, "unknown_provider")
        self.assertEqual(self.wallet(self.user)["balance"], 0)
        self.assertEqual(self.ok(self.get(f"/purchases/{order['id']}", self.user))["order"]["status"], "pending")

    def test_sandbox_checkout_and_signed_webhook(self):
        order = self.order()
        checkout = self.ok(self.post("/payments/sandbox/checkout", {"order_id": order["id"]}, self.user))
        self.assertTrue(checkout["simulated"])
        self.assertIn("非真实支付", checkout["notice"])
        self.assertEqual(self.wallet(self.user)["balance"], 0)
        result = self.ok(self.webhook("sandbox", self.paid_event(order)))
        self.assertTrue(result["credited"])
        self.assertEqual(self.wallet(self.user)["balance"], 1000)
        self.assertEqual(self.ok(self.get(f"/purchases/{order['id']}", self.user))["order"]["status"], "paid")

    def test_bad_signatures_rejected(self):
        order = self.order()
        self.ok(self.post("/payments/sandbox/checkout", {"order_id": order["id"]}, self.user))
        event = self.paid_event(order)
        self.error(self.webhook("sandbox", event, secret="wrong-secret"), 400, "signature_invalid")
        self.error(self.webhook("sandbox", event, signature=""), 400, "signature_missing")
        self.error(self.webhook("sandbox", event, signature="t=abc,v1=00"), 400, "signature_invalid")
        import json
        stale = sign_webhook(SANDBOX_SECRET, json.dumps(event).encode(), timestamp=1_000_000)
        self.error(self.webhook("sandbox", event, signature=stale), 400, "signature_expired")
        self.assertEqual(self.wallet(self.user)["balance"], 0)
        audit = self.ok(self.get("/admin/audit?action=payment.webhook_rejected", self.operator()))["audit"]
        self.assertEqual(len(audit), 4)

    def test_duplicate_webhooks_credit_once(self):
        order = self.order()
        self.ok(self.post("/payments/sandbox/checkout", {"order_id": order["id"]}, self.user))
        self.ok(self.webhook("sandbox", self.paid_event(order)))
        replay = self.ok(self.webhook("sandbox", self.paid_event(order)))
        self.assertTrue(replay["duplicate"])
        # A different event for the same order still cannot book twice.
        other = self.ok(self.webhook("sandbox", self.paid_event(order, event_id="evt_2")))
        self.assertFalse(other["credited"])
        self.assertEqual(self.wallet(self.user)["balance"], 1000)
        self.assertEqual(len(self.ok(self.get("/wallet/ledger", self.user))["entries"]), 1)

    def test_amount_mismatch_not_credited(self):
        order = self.order()
        self.ok(self.post("/payments/sandbox/checkout", {"order_id": order["id"]}, self.user))
        self.error(self.webhook("sandbox", self.paid_event(order, amount_minor=1)), 409, "amount_mismatch")
        self.error(self.webhook("sandbox", self.paid_event(order, event_id="e3", currency="USD")), 409, "amount_mismatch")
        self.assertEqual(self.wallet(self.user)["balance"], 0)
        # The failed event was rolled back, so a correct retry with the same id still works.
        self.assertTrue(self.ok(self.webhook("sandbox", self.paid_event(order)))["credited"])

    def test_non_success_events_ignored(self):
        order = self.order()
        self.ok(self.post("/payments/sandbox/checkout", {"order_id": order["id"]}, self.user))
        self.assertEqual(self.ok(self.webhook("sandbox", {**self.paid_event(order), "type": "payment.failed"}))["ignored"],
                         "payment.failed")
        self.assertEqual(self.wallet(self.user)["balance"], 0)

    def test_manual_channel_requires_operator_confirmation(self):
        order = self.order()
        checkout = self.ok(self.post("/payments/manual/checkout", {"order_id": order["id"]}, self.user))
        self.assertEqual(checkout["status"], "awaiting_manual_confirmation")
        self.assertEqual(self.wallet(self.user)["balance"], 0)
        self.error(self.post(f"/admin/orders/{order['id']}/confirm", {"payment_reference": "x", "reason": "y"}, self.user),
                   403)
        self.error(self.post("/payments/sandbox/checkout", {"order_id": order["id"]}, self.user), 409, "provider_mismatch")
        op = self.operator()
        self.error(self.post(f"/admin/orders/{order['id']}/confirm", {"reason": "bank"}, op), 400, "evidence_required")
        confirmed = self.ok(self.post(f"/admin/orders/{order['id']}/confirm",
                                      {"payment_reference": "BANK-2026-001", "reason": "到账核对"}, op))
        self.assertTrue(confirmed["credited"])
        again = self.ok(self.post(f"/admin/orders/{order['id']}/confirm",
                                  {"payment_reference": "BANK-2026-001", "reason": "重复点击"}, op))
        self.assertFalse(again["credited"])
        self.assertEqual(self.wallet(self.user)["balance"], 1000)
        orders = self.ok(self.get("/admin/orders?status=paid", op))["orders"]
        self.assertEqual([o["id"] for o in orders], [order["id"]])

    def test_model_license_paid_from_wallet(self):
        card = {"id": "nav-policy", "name": "Nav policy", "source": "native", "kind": "policy", "license": "commercial",
                "pricing": {"type": "license_once", "credits": 300},
                "package": {"ref": "oci://models/nav:1", "sha256": "a" * 64, "runtime": "onnxruntime"}}
        self.publish(card)
        self.error(self.post("/purchases", {"kind": "model_license", "model_id": "nav-policy"}, self.user), 402,
                   "insufficient_credits")
        self.fund(self.user, 500)
        bought = self.entitle(self.user, "nav-policy")
        self.assertEqual(bought["order"]["status"], "paid")
        self.assertEqual(bought["order"]["provider"], "wallet")
        self.assertEqual(self.wallet(self.user)["balance"], 200)
        self.error(self.post("/purchases", {"kind": "model_license", "model_id": "nav-policy"}, self.user), 409,
                   "already_entitled")
        self.assertEqual(self.wallet(self.user)["balance"], 200)


class SandboxInProdTest(CloudTestCase):
    env = "prod"

    def test_sandbox_unavailable_in_prod(self):
        user = self.register("prod@example.com")
        self.ok(self.post("/admin/credit-packs", {"id": "p", "name": "P", "credits": 10, "price_minor": 100,
                                                  "currency": "USD"}, self.operator()), 201)
        order = self.ok(self.post("/purchases", {"kind": "credit_pack", "pack_id": "p"}, user), 201)["order"]
        self.error(self.post("/payments/sandbox/checkout", {"order_id": order["id"]}, user), 403, "sandbox_disabled")
        event = {"event_id": "e", "type": "payment.succeeded", "order_id": order["id"], "amount_minor": 100,
                 "currency": "USD"}
        self.error(self.webhook("sandbox", event), 403, "sandbox_disabled")
        self.assertEqual(self.wallet(user)["balance"], 0)
