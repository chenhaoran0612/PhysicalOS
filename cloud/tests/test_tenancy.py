"""Org isolation: A must never see or touch B's wallet, robots, orders, keys or usage."""
import support  # noqa: F401
from support import API, ECHO_LLM, CloudTestCase, quadruped_manifest


class TenancyTest(CloudTestCase):
    def setUp(self):
        super().setUp()
        self.a = self.register("a@tenant.test")
        self.b = self.register("b@tenant.test")
        self.ok(self.post("/admin/credit-packs", {"id": "starter", "name": "Starter", "credits": 1000,
                                                  "price_minor": 990, "currency": "CNY"}, self.operator()), 201)

    def test_wallets_isolated(self):
        self.fund(self.b, 500)
        self.assertEqual(self.wallet(self.a)["balance"], 0)
        self.assertEqual(self.wallet(self.b)["balance"], 500)
        self.assertEqual(self.ok(self.get("/wallet/ledger", self.a))["entries"], [])

    def test_org_header_requires_membership(self):
        headers = {**self.a["headers"], "X-NeuxMind-Org": self.b["org_id"]}
        self.error(self.client.get(f"{API}/wallet", headers=headers), 403, "not_member")
        self.error(self.client.get(f"{API}/robots", headers=headers), 403, "not_member")

    def test_robots_isolated(self):
        robot = self.ok(self.post("/robots", {"manifest": quadruped_manifest()}, self.b), 201)
        self.assertEqual(self.ok(self.get("/robots", self.a))["robots"], [])
        self.error(self.get(f"/robots/{robot['id']}", self.a), 404)
        self.error(self.client.delete(f"{API}/robots/{robot['id']}", headers=self.a["headers"]), 404)
        self.error(self.get(f"/catalog/models?robot_id={robot['id']}", self.a), 404)
        # Same instance_id in another org is a separate robot.
        mine = self.ok(self.post("/robots", {"manifest": quadruped_manifest()}, self.a), 201)
        self.assertNotEqual(mine["id"], robot["id"])

    def test_orders_isolated(self):
        order = self.ok(self.post("/purchases", {"kind": "credit_pack", "pack_id": "starter"}, self.b), 201)["order"]
        self.assertEqual(self.ok(self.get("/purchases", self.a))["orders"], [])
        self.error(self.get(f"/purchases/{order['id']}", self.a), 404)
        self.error(self.post("/payments/manual/checkout", {"order_id": order["id"]}, self.a), 404)

    def test_api_keys_and_usage_isolated(self):
        key = self.api_key(self.b, ["gateway", "usage:read", "wallet:read"])
        self.error(self.client.delete(f"{API}/api-keys/{key['id']}", headers=self.a["headers"]), 404)
        self.assertEqual(self.ok(self.get("/api-keys", self.a))["api_keys"], [])
        self.publish(ECHO_LLM)
        self.entitle(self.b, "echo-llm")
        self.fund(self.b, 100)
        self.ok(self.post("/gateway/infer", {"model": "echo-llm", "input": "hello", "max_tokens": 16},
                          headers=key["headers"]))
        self.assertEqual(len(self.ok(self.get("/usage", headers=key["headers"]))["usage"]), 1)
        self.assertEqual(self.ok(self.get("/usage", self.a))["usage"], [])
        self.assertEqual(self.ok(self.get("/wallet/reservations", self.a))["reservations"], [])
        # A's entitlement does not exist even though B bought it.
        self.error(self.post("/gateway/infer", {"model": "echo-llm", "input": "hello", "max_tokens": 16}, self.a),
                   403, "entitlement_required")

    def test_gateway_session_isolated(self):
        self.publish(ECHO_LLM)
        self.entitle(self.b, "echo-llm")
        self.fund(self.b, 100)
        session = self.ok(self.post("/gateway/sessions", {"request_id": "s1", "model": "echo-llm", "max_tokens": 1000},
                                    self.b), 201)
        self.error(self.get(f"/gateway/sessions/{session['session_id']}", self.a), 404)
        self.error(self.post(f"/gateway/sessions/{session['session_id']}/close", {"request_id": "c"}, self.a), 404)
        # A's session token cannot drive B's session.
        self.error(self.post(f"/gateway/sessions/{session['session_id']}/v1/chat/completions",
                             {"messages": [{"role": "user", "content": "x"}]}, self.a), 401, "invalid_session_token")
