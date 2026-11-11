"""Model catalog: card validation, filtering by capability/manifest, package gating, price history."""
import support  # noqa: F401
from support import ECHO_LLM, CloudTestCase, arm_manifest, quadruped_manifest

from neuxmind_cloud.spec_bridge import capabilities

VLA = {"id": "pi-vla", "name": "Pi VLA", "source": "open", "kind": "vla", "license": "Apache-2.0",
       "pricing": {"type": "free"}, "requires_capabilities": ["policy.action_chunk", "sensor.camera.rgb"],
       "families": ["manipulator"], "package": {"ref": "hf://org/pi-vla@v1", "sha256": "b" * 64, "runtime": "tensorrt"}}
DETECTOR = {"id": "yolo-nano", "name": "YOLO nano", "source": "open", "kind": "vision", "license": "AGPL-3.0",
            "pricing": {"type": "license_once", "credits": 50}, "requires_capabilities": ["camera"],  # legacy alias
            "package": {"ref": "oci://yolo:8n", "sha256": "c" * 64, "runtime": "onnxruntime"}}
GAIT = {"id": "dog-gait", "name": "Gait policy", "source": "native", "kind": "policy", "license": "NeuxMind EULA",
        "pricing": {"type": "license_once", "credits": 200}, "requires_capabilities": ["base.velocity"],
        "families": ["quadruped"], "package": {"ref": "nx://gait:1", "sha256": "d" * 64, "runtime": "onnxruntime"}}


class CatalogTest(CloudTestCase):
    def setUp(self):
        super().setUp()
        self.user = self.register("shopper@example.com")
        for card in (ECHO_LLM, VLA, DETECTOR, GAIT):
            self.publish(card)

    def ids(self, query: str = "", user=None) -> list[str]:
        return [m["id"] for m in self.ok(self.get(f"/catalog/models{query}", user or self.user))["models"]]

    def test_card_validation(self):
        op = self.operator()
        bad = [
            {**GAIT, "id": "bad-cap", "requires_capabilities": ["teleport.now"]},
            {**ECHO_LLM, "id": "no-upstream", "upstream": None},
            {**ECHO_LLM, "id": "leaky", "upstream": {"provider": "openai_compatible", "base_url": "https://x.test",
                                                     "model": "m", "api_key": "sk-123"}},
            {**ECHO_LLM, "id": "plain-http", "upstream": {"provider": "openai_compatible",
                                                          "base_url": "http://models.example.com", "model": "m"}},
            {**GAIT, "id": "bad-sha", "package": {"ref": "x", "sha256": "123", "runtime": "onnxruntime"}},
            {**GAIT, "id": "bad-family", "families": ["submarine"]},
            {**GAIT, "id": "bad-price", "pricing": {"type": "per_1k_tokens", "credits": 0}},
            {**GAIT, "id": "Bad ID"},
            {**ECHO_LLM, "id": "license-gw", "pricing": {"type": "license_once", "credits": 5}},
        ]
        for card in bad:
            response = self.post("/admin/models", card, op)
            self.assertEqual(response.status_code, 400, (card["id"], response.text))
        self.error(self.post("/admin/models", GAIT, op), 409, "model_exists")
        self.error(self.post("/admin/models", {**GAIT, "id": "x"}, self.user), 403)

    def test_capability_ids_normalised_by_spec(self):
        card = self.ok(self.get("/catalog/models/yolo-nano", self.user))
        self.assertEqual(card["requires_capabilities"], ["sensor.camera.rgb"])
        for cid in card["requires_capabilities"]:
            capabilities.get(cid)
        registry = self.ok(self.get("/capabilities/registry"))
        self.assertEqual(registry, capabilities.registry())

    def test_public_view_hides_operator_fields(self):
        card = self.ok(self.get("/catalog/models/pi-vla", self.user))
        self.assertNotIn("ref", card["package"])
        self.assertNotIn("upstream", card)
        self.assertTrue(self.ok(self.get("/catalog/models/echo-llm", self.user))["gateway"])
        admin = {m["id"]: m for m in self.ok(self.get("/admin/models", self.operator()))["models"]}
        self.assertEqual(admin["echo-llm"]["upstream"]["provider"], "echo")

    def test_filters(self):
        self.assertEqual(sorted(self.ids()), ["dog-gait", "echo-llm", "pi-vla", "yolo-nano"])
        self.assertEqual(self.ids("?kind=vla"), ["pi-vla"])
        self.assertEqual(sorted(self.ids("?source=open")), ["pi-vla", "yolo-nano"])
        self.assertEqual(sorted(self.ids("?capability=sensor.camera.rgb")), ["pi-vla", "yolo-nano"])
        self.assertEqual(self.ids("?capability=cmd_vel"), ["dog-gait"])
        self.error(self.get("/catalog/models?capability=warp.drive", self.user), 400, "unknown_capability")

    def test_compatible_with_manifest(self):
        dog = self.ok(self.post("/catalog/models/compatible", {"manifest": quadruped_manifest()}, self.user))
        # Dog: camera + base.velocity + onnxruntime → yolo + gait + hosted LLM; not the arm VLA.
        self.assertEqual(sorted(m["id"] for m in dog["models"]), ["dog-gait", "echo-llm", "yolo-nano"])
        arm = self.ok(self.post("/catalog/models/compatible", {"manifest": arm_manifest()}, self.user))
        # Arm: tensorrt only, so the onnxruntime YOLO package is excluded.
        self.assertEqual(sorted(m["id"] for m in arm["models"]), ["echo-llm", "pi-vla"])
        self.error(self.post("/catalog/models/compatible", {"manifest": {"schema": "robot.adapter.v1"}}, self.user), 400,
                   "invalid_manifest")
        robot = self.ok(self.post("/robots", {"manifest": arm_manifest()}, self.user), 201)
        self.assertEqual(sorted(self.ids(f"?robot_id={robot['id']}")), ["echo-llm", "pi-vla"])

    def test_package_download_requires_entitlement(self):
        self.error(self.get("/catalog/models/dog-gait/package", self.user), 403, "entitlement_required")
        self.error(self.get("/catalog/models/echo-llm/package", self.user), 404, "no_package")
        self.fund(self.user, 200)
        self.entitle(self.user, "dog-gait")
        package = self.ok(self.get("/catalog/models/dog-gait/package", self.user))
        self.assertEqual(package["sha256"], "d" * 64)
        self.assertEqual(package["install"]["capability"], "model.install")
        capabilities.validate_payload("model.install", package["install"]["args"])
        # Free models still need an (instant, 0-credit) entitlement.
        self.error(self.get("/catalog/models/pi-vla/package", self.user), 403)
        self.entitle(self.user, "pi-vla")
        self.ok(self.get("/catalog/models/pi-vla/package", self.user))
        self.assertEqual(self.wallet(self.user)["balance"], 0)
        listed = {m["id"]: m["entitled"] for m in self.ok(self.get("/catalog/models", self.user))["models"]}
        self.assertEqual(listed, {"dog-gait": True, "echo-llm": False, "pi-vla": True, "yolo-nano": False})

    def test_unlist_hides_model(self):
        self.ok(self.post("/admin/models/yolo-nano/status", {"status": "unlisted"}, self.operator()))
        self.assertNotIn("yolo-nano", self.ids())
        self.error(self.get("/catalog/models/yolo-nano", self.user), 404)
        self.error(self.post("/purchases", {"kind": "model_license", "model_id": "yolo-nano"}, self.user), 404)

    def test_price_change_does_not_touch_history(self):
        op = self.operator()
        self.fund(self.user, 10_000)
        self.entitle(self.user, "echo-llm")
        # 1) A settled call at the old price.
        old = self.ok(self.post("/gateway/infer", {"model": "echo-llm", "input": "a " * 200, "max_tokens": 300}, self.user))
        # 2) An open session that reserved at the old price.
        session = self.ok(self.post("/gateway/sessions", {"request_id": "s", "model": "echo-llm", "max_tokens": 1000},
                                    self.user), 201)
        ledger_before = self.ok(self.get("/wallet/ledger", self.user))["entries"]
        # 3) Price goes up 100x.
        updated = self.ok(self.client.patch("/api/v1/admin/models/echo-llm",
                                            json={"pricing": {"type": "per_1k_tokens", "credits": 1000}},
                                            headers=op["headers"]))
        self.assertEqual(updated["pricing"]["credits"], 1000)
        ledger_after = self.ok(self.get("/wallet/ledger", self.user))["entries"]
        self.assertEqual(ledger_before, ledger_after)
        reply = self.ok(self.client.post(f"/api/v1/gateway/sessions/{session['session_id']}/v1/chat/completions",
                                         json={"messages": [{"role": "user", "content": "one two three"}]},
                                         headers={"Authorization": f"Bearer {session['access_token']}"}))
        closed = self.ok(self.post(f"/gateway/sessions/{session['session_id']}/close", {"request_id": "c"}, self.user))
        # The session was settled at its snapshot price (10 credits / 1k), not the new one.
        self.assertEqual(closed["credits_charged"], -(-reply["usage"]["total_tokens"] * 10 // 1000))
        new = self.ok(self.post("/gateway/infer", {"model": "echo-llm", "input": "a " * 200, "max_tokens": 300}, self.user))
        self.assertEqual(new["usage"]["total_tokens"], old["usage"]["total_tokens"])
        # 401 tokens: ceil(401 * 10 / 1000) = 5 before, 401 * 1000 / 1000 = 401 after.
        self.assertEqual(old["usage"]["total_tokens"], 401)
        self.assertEqual(old["credits_charged"], 5)
        self.assertEqual(new["credits_charged"], 401)
        settled_old = [e for e in ledger_after if e["detail"].get("evidence_ref") == "echo:1"]
        self.assertEqual([e["delta"] for e in settled_old], [-5])
        audit = self.ok(self.get("/admin/audit?action=admin.model_update", op))["audit"][0]
        self.assertEqual(audit["detail"]["pricing_before"]["credits"], 10)
