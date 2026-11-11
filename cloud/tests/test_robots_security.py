"""Fleet registry and security basics (body limit, CORS, error format, audit)."""
import support  # noqa: F401
from support import API, CloudTestCase, quadruped_manifest

from sqlalchemy import select

from neuxmind_cloud.models import AuditLog


class RobotsTest(CloudTestCase):
    def setUp(self):
        super().setUp()
        self.user = self.register("fleet@example.com")

    def test_register_validates_with_spec(self):
        robot = self.ok(self.post("/robots", {"manifest": quadruped_manifest()}, self.user), 201)
        self.assertTrue(robot["simulated"])
        self.assertEqual(len(robot["manifest_digest"]), 64)
        self.assertIn("base.velocity", robot["capabilities"])
        missing_stop = quadruped_manifest()
        missing_stop["capabilities"] = [c for c in missing_stop["capabilities"] if c["id"] != "safety.stop"]
        self.error(self.post("/robots", {"manifest": missing_stop}, self.user), 400, "invalid_manifest")
        unknown_param = quadruped_manifest()
        unknown_param["capabilities"][2]["params"]["warp"] = 9
        self.error(self.post("/robots", {"manifest": unknown_param}, self.user), 400, "invalid_manifest")
        self.error(self.post("/robots", {"manifest": {"schema": "robot.adapter.v1"}}, self.user), 400, "invalid_manifest")

    def test_reregister_updates_same_robot(self):
        first = self.ok(self.post("/robots", {"manifest": quadruped_manifest()}, self.user), 201)
        second = self.ok(self.post("/robots", {"manifest": quadruped_manifest(name="Dog v2", simulated=False)}, self.user),
                         201)
        self.assertEqual(first["id"], second["id"])
        self.assertNotEqual(first["manifest_digest"], second["manifest_digest"])
        self.assertFalse(second["simulated"])
        robots = self.ok(self.get("/robots", self.user))["robots"]
        self.assertEqual([(r["id"], r["name"]) for r in robots], [(first["id"], "Dog v2")])
        self.ok(self.client.delete(f"{API}/robots/{first['id']}", headers=self.user["headers"]))
        self.assertEqual(self.ok(self.get("/robots", self.user))["robots"], [])

    def test_robots_scope_for_api_key(self):
        key = self.api_key(self.user, ["robots"])
        self.ok(self.post("/robots", {"manifest": quadruped_manifest("k-1")}, headers=key["headers"]), 201)
        self.assertEqual(len(self.ok(self.get("/robots", headers=key["headers"]))["robots"]), 1)


class SecurityBasicsTest(CloudTestCase):
    def test_body_size_limit(self):
        user = self.register("big@example.com")
        huge = {"manifest": quadruped_manifest(name="x" * 300_000)}
        self.error(self.post("/robots", huge, user), 413, "payload_too_large")

        def chunks():  # no Content-Length: streamed body counted while reading
            for _ in range(50):
                yield b"x" * 10_000

        response = self.client.post(f"{API}/auth/login", content=chunks(), headers={"Content-Type": "application/json"})
        self.error(response, 413, "payload_too_large")

    def test_uniform_error_format(self):
        for response in (self.client.get(f"{API}/does-not-exist"), self.client.get(f"{API}/wallet"),
                         self.client.post(f"{API}/auth/login", content=b"not json",
                                          headers={"Content-Type": "application/json"}),
                         self.client.put(f"{API}/wallet")):
            body = response.json()
            self.assertEqual(set(body) >= {"error", "code"}, True, body)

    def test_cors_allow_list(self):
        allowed = self.client.options(f"{API}/me", headers={"Origin": "https://console.neuxmind.test",
                                                            "Access-Control-Request-Method": "GET"})
        self.assertEqual(allowed.headers.get("access-control-allow-origin"), "https://console.neuxmind.test")
        denied = self.client.options(f"{API}/me", headers={"Origin": "https://evil.example",
                                                           "Access-Control-Request-Method": "GET"})
        self.assertNotEqual(denied.headers.get("access-control-allow-origin"), "https://evil.example")

    def test_audit_records_key_actions(self):
        user = self.register("audited@example.com")
        key = self.api_key(user, ["gateway"])
        self.client.delete(f"{API}/api-keys/{key['id']}", headers=user["headers"])
        self.post("/auth/login", {"email": "audited@example.com", "password": "nope-nope-nope"})
        self.ok(self.post("/robots", {"manifest": quadruped_manifest()}, user), 201)
        with self.db.transaction() as s:
            actions = set(s.execute(select(AuditLog.action)).scalars())
        self.assertTrue({"auth.register", "api_key.create", "api_key.revoke", "auth.login_failed",
                         "robot.register"} <= actions, actions)
