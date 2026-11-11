"""CloudClient and the cloud half of `nx` against the real cloud app (uvicorn, random port).

The only stand-in is the cloud's own EchoProvider upstream (a test double,
``simulated=true``); accounts, wallet ledger, catalog filter, entitlements and
the metered gateway are the real implementation.
"""
import json
import unittest

import _support
from _support import OPERATOR_EMAIL, PASSWORD, CliHome, CloudServer, RuntimeServer

from neuxmind import NeuxMindError
from neuxmind.cloud import CloudClient

ECHO = {"id": "echo-llm", "name": "Echo LLM (test double)", "source": "closed", "kind": "llm",
        "license": "test only", "pricing": {"type": "per_1k_tokens", "credits": 10}, "upstream": {"provider": "echo"}}
DOG_POLICY = {"id": "dog-gait", "name": "Gait policy (test double)", "source": "closed", "kind": "policy",
              "license": "test only", "pricing": {"type": "per_call", "credits": 2}, "upstream": {"provider": "echo"},
              "requires_capabilities": ["base.velocity", "legged.posture"], "families": ["quadruped"]}
ARM_VLA = {"id": "arm-vla", "name": "Arm VLA (test double)", "source": "closed", "kind": "vla",
           "license": "test only", "pricing": {"type": "per_call", "credits": 5}, "upstream": {"provider": "echo"},
           "requires_capabilities": ["arm.joint_position", "gripper.position"], "families": ["manipulator"]}
# Edge-only package for onnxruntime bodies: the simulated bodies declare no model runtime, so it is filtered out.
EDGE_PACK = {"id": "edge-detector", "name": "Edge detector", "source": "native", "kind": "vision",
             "license": "test only", "pricing": {"type": "license_once", "credits": 500},
             "package": {"ref": "nx://edge-detector:1", "sha256": "a" * 64, "runtime": "onnxruntime"}}


class CloudTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cloud = CloudServer()
        cls.dog = RuntimeServer("quadruped")
        cls.arm = RuntimeServer("arm")
        cls.ops = CloudClient(cls.cloud.url)
        reg = cls.ops.register(OPERATOR_EMAIL, PASSWORD)
        # Platform operator = whitelisted e-mail AND verified (dev/test server returns the token).
        cls.ops.verify_email(reg["dev_verification_token"])          # in the operator's own session
        assert cls.ops.me()["platform_operator"], "operator must be verified"
        for card in (ECHO, DOG_POLICY, ARM_VLA, EDGE_PACK):
            cls.ops._request("POST", "/admin/models", card)

    @classmethod
    def tearDownClass(cls):
        cls.dog.close()
        cls.arm.close()
        cls.cloud.close()

    def fund(self, org_id: str, credits: int):
        self.ops._request("POST", f"/admin/wallets/{org_id}/adjust", {"delta": credits, "reason": "sdk test funding"})

    def test_client_account_keys_catalog_gateway_fleet(self):
        client = CloudClient(self.cloud.url)
        reg = client.register("dev1@sdk.test", PASSWORD, name="Dev One")
        self.assertTrue(client.token)
        self.assertFalse(reg["user"]["email_verified"])
        self.assertTrue(reg["verification"]["simulated"])
        self.assertTrue(reg["dev_verification_token"].startswith("nxv_"))

        # Unverified: browsing works, spending does not — with a friendly hint.
        self.assertTrue(client.models())
        self.assertEqual(client.wallet()["balance"], 0)
        for call in (lambda: client.purchase_model("echo-llm"), lambda: client.infer("echo-llm", "hi", 10)):
            with self.assertRaises(NeuxMindError) as cm:
                call()
            self.assertEqual((cm.exception.code, cm.exception.status), ("email_unverified", 403))
            self.assertIn("nx verify-email", cm.exception.details["hint"])
            self.assertIn("nx verify-email", str(cm.exception))
        with self.assertRaises(NeuxMindError) as cm:
            client.verify_email("nxv_not-a-real-token")
        self.assertEqual(cm.exception.code, "verification_token_invalid")
        resent = client.resend_verification()
        self.assertFalse(resent["email_verified"])
        with self.assertRaises(NeuxMindError) as cm:
            client.verify_email(reg["dev_verification_token"])      # the resend invalidated it
        self.assertEqual(cm.exception.code, "verification_token_invalid")
        # Verification must happen in the account's own session (cloud N-01).
        with self.assertRaises(NeuxMindError) as cm:
            CloudClient(self.cloud.url).verify_email(resent["dev_verification_token"])
        self.assertEqual(cm.exception.code, "not_logged_in")
        with self.assertRaises(NeuxMindError) as cm:
            self.ops.verify_email(resent["dev_verification_token"])          # someone else's session
        self.assertEqual((cm.exception.code, cm.exception.status), ("verification_wrong_account", 403))
        self.assertIn("nx login", cm.exception.details["hint"])
        early_key = client.create_api_key(["catalog:read"], name="before-verify")
        other_session = CloudClient(self.cloud.url)
        other_session.login("dev1@sdk.test", PASSWORD)
        verified = client.verify_email(resent["dev_verification_token"])     # token was not consumed above
        self.assertTrue(verified["user"]["email_verified"])
        self.assertEqual(verified["api_keys_revoked"], 1)
        self.assertEqual(verified["sessions_revoked"], 1)                    # other_session
        for revoked in (CloudClient(self.cloud.url, early_key["key"]), other_session):
            with self.assertRaises(NeuxMindError) as cm:
                revoked.models()
            self.assertEqual(cm.exception.status, 401)
        self.assertTrue(client.me()["email_verified"])
        with self.assertRaises(NeuxMindError) as cm:
            client.resend_verification()
        self.assertEqual(cm.exception.code, "already_verified")
        relogged = CloudClient(self.cloud.url)
        relogged.login("dev1@sdk.test", PASSWORD)
        me = relogged.me()
        self.assertEqual(me["user"]["email"], "dev1@sdk.test")
        self.assertEqual(me["current_org_id"], reg["org_id"])
        with self.assertRaises(NeuxMindError) as cm:
            CloudClient(self.cloud.url).login("dev1@sdk.test", "wrong-password-xx")
        self.assertEqual((cm.exception.code, cm.exception.status), ("invalid_credentials", 401))

        self.fund(reg["org_id"], 500)
        self.assertEqual(client.wallet()["available"], 500)
        self.assertEqual(client.ledger()[0]["delta"], 500)

        # Server-side manifest filter: what each simulated body can use.
        dog_ids = {m["id"] for m in client.compatible_models(self._manifest(self.dog))["models"]}
        arm_ids = {m["id"] for m in client.compatible_models(self._manifest(self.arm))["models"]}
        self.assertEqual(dog_ids, {"echo-llm", "dog-gait"})
        self.assertEqual(arm_ids, {"echo-llm", "arm-vla"})
        self.assertEqual({m["id"] for m in client.models(kind="vla")}, {"arm-vla"})

        with self.assertRaises(NeuxMindError) as cm:
            client.gateway.infer("echo-llm", "hi", 20)
        self.assertEqual(cm.exception.code, "entitlement_required")
        bought = client.purchase_model("arm-vla")
        self.assertEqual(bought["order"]["credits"], 0)          # per-call models: 0-credit license
        self.assertEqual(client.wallet()["balance"], 500)
        with self.assertRaises(NeuxMindError) as cm:
            client.model_package("edge-detector")
        self.assertEqual(cm.exception.code, "entitlement_required")
        client.purchase_model("edge-detector")
        self.assertEqual(client.wallet()["balance"], 0)
        package = client.model_package("edge-detector")
        self.assertEqual(package["install"]["capability"], "model.install")
        client.purchase_model("echo-llm")
        self.assertEqual({e["model_id"] for e in client.entitlements()}, {"arm-vla", "echo-llm", "edge-detector"})
        with self.assertRaises(NeuxMindError) as cm:
            client.purchase_model("echo-llm")
        self.assertEqual(cm.exception.code, "already_entitled")

        with self.assertRaises(NeuxMindError) as cm:
            client.gateway.infer("echo-llm", "hello robot", 50)
        self.assertEqual(cm.exception.code, "insufficient_credits")
        self.fund(reg["org_id"], 100)
        # Generation options outside the allow-list are refused locally, before any request.
        with self.assertRaises(NeuxMindError) as cm:
            client.gateway.infer("echo-llm", "hello", 50, n=4)
        self.assertEqual(cm.exception.code, "n_unsupported")
        with self.assertRaises(NeuxMindError) as cm:
            client.gateway.infer("echo-llm", "hello", 50, temperature=0.3, max_completion_tokens=99999)
        self.assertEqual(cm.exception.code, "parameter_not_allowed")
        self.assertEqual(cm.exception.details["parameters"], ["max_completion_tokens", "temperature"])
        self.assertEqual(client.usage()["usage"], [])
        out = client.gateway.infer("echo-llm", "hello robot", 50, request_id="sdk-infer-1", n=1)
        self.assertEqual(out["output"]["text"], "echo: hello robot")
        self.assertTrue(out["simulated"])
        replay = client.infer("echo-llm", "hello robot", 50, request_id="sdk-infer-1")
        self.assertEqual(replay["id"], out["id"])
        self.assertEqual(client.usage()["usage"][0]["request_id"], "sdk-infer-1")

        # API key (scoped) can read the catalog but cannot buy.
        key = client.create_api_key(["catalog:read", "gateway"], name="ci")
        keyed = CloudClient(self.cloud.url, key["key"])
        self.assertTrue(keyed.models())
        with self.assertRaises(NeuxMindError):
            keyed.purchase_model("dog-gait")
        self.assertEqual([k["revoked"] for k in client.api_keys()].count(False), 1)   # the early key is revoked
        client.revoke_api_key(key["id"])
        with self.assertRaises(NeuxMindError) as cm:
            keyed.models()
        self.assertEqual(cm.exception.status, 401)

        robot = client.register_robot(self._manifest(self.dog))
        self.assertTrue(robot["simulated"])
        self.assertEqual([r["id"] for r in client.robots()], [robot["id"]])
        self.assertEqual(client.capability_registry()["schema"], "neuxmind.capabilities.v1")
        with self.assertRaises(NeuxMindError) as cm:
            client.checkout("stripe", bought["order"]["id"])
        client.logout()
        with self.assertRaises(NeuxMindError):
            client.me()

    def test_unverified_user_friendly_errors_and_cli_verify(self):
        home = CliHome()
        self.addCleanup(home.close)
        home.json("config", "set", "cloud_url", self.cloud.url)
        reg = home.json("register", "--email", "dev3@sdk.test", "--password", PASSWORD)
        self.assertFalse(reg["user"]["email_verified"])
        self.fund(reg["org_id"], 50)
        for argv in (("models", "buy", "echo-llm"), ("infer", "echo-llm", "hi")):
            failed = home.json(*argv, expect=1)
            self.assertEqual(failed["code"], "email_unverified")
            self.assertIn("nx verify-email", failed["details"]["hint"])
        self.assertEqual(home.json("wallet")["wallet"]["balance"], 50)      # browsing still works
        verified = home.json("verify-email", reg["dev_verification_token"])
        self.assertTrue(verified["email_verified"])
        self.assertTrue(home.json("models", "buy", "echo-llm")["entitlement"])
        self.assertEqual(home.json("infer", "echo-llm", "hi")["output"]["text"], "echo: hi")

    def test_claim_and_password_reset_client(self):
        squatter = CloudClient(self.cloud.url)
        squatter.register("owner1@sdk.test", "squatter-password-1")         # never verified
        owner = CloudClient(self.cloud.url)
        pending = owner.register("owner1@sdk.test", PASSWORD)
        self.assertEqual((pending["status"], pending["code"]), ("claim_sent", "email_pending_claim"))
        self.assertIsNone(owner.token)                                      # no session for a 202
        claimed = owner.claim(pending["dev_claim_token"], PASSWORD, name="Owner")
        self.assertTrue(owner.token)
        me = owner.me()
        self.assertEqual(me["user"]["email"], "owner1@sdk.test")
        self.assertTrue(me["email_verified"])
        self.assertEqual(claimed["org_id"], me["current_org_id"])
        with self.assertRaises(NeuxMindError) as cm:
            squatter.me()
        self.assertEqual(cm.exception.status, 401)
        with self.assertRaises(NeuxMindError) as cm:
            owner.claim(pending["dev_claim_token"], PASSWORD)
        self.assertEqual(cm.exception.code, "recovery_token_invalid")
        self.assertIn("password-reset", cm.exception.details["hint"])

        unknown = owner.request_password_reset("nobody@sdk.test")
        self.assertEqual(unknown["status"], "accepted")
        self.assertFalse(any(k.startswith("dev_") for k in unknown))
        issued = owner.request_password_reset("owner1@sdk.test")
        before = owner.token
        reset = CloudClient(self.cloud.url).reset_password(issued["dev_reset_token"], "brand-new-password-9")
        self.assertTrue(reset["token"])
        with self.assertRaises(NeuxMindError):
            CloudClient(self.cloud.url, before).me()                        # old sessions deleted
        with self.assertRaises(NeuxMindError) as cm:
            CloudClient(self.cloud.url).login("owner1@sdk.test", PASSWORD)
        self.assertEqual(cm.exception.code, "invalid_credentials")
        CloudClient(self.cloud.url).login("owner1@sdk.test", "brand-new-password-9")
        with self.assertRaises(NeuxMindError) as cm:
            CloudClient(self.cloud.url).register("owner1@sdk.test", PASSWORD)
        self.assertEqual(cm.exception.code, "email_taken")

    def test_claim_and_password_reset_cli(self):
        CloudClient(self.cloud.url).register("owner2@sdk.test", "squatter-password-2")
        home = CliHome()
        self.addCleanup(home.close)
        home.json("config", "set", "cloud_url", self.cloud.url)
        code, out, _ = home.run("register", "--email", "owner2@sdk.test", "--password", PASSWORD)
        self.assertEqual(code, 0, out)
        self.assertIn("已被一个未验证的账户占用，认领邮件已发送", out)
        self.assertIn("仅开发环境", out)
        claim_token = next(w for w in out.replace("：", " ").split() if w.startswith("nxv_"))
        self.assertNotIn("cloud_token", json.loads((home.path / "config.json").read_text()))
        code, out, err = home.run("claim", claim_token, "--password-stdin", stdin=PASSWORD + "\n")
        self.assertEqual(code, 0, err)
        self.assertIn("已认领邮箱 owner2@sdk.test", out)
        self.assertTrue(home.json("whoami")["email_verified"])

        requested = home.json("password-reset", "request", "--email", "owner2@sdk.test")
        code, out, _ = home.run("password-reset", "request", "--email", "owner2@sdk.test")
        self.assertIn("仅开发环境", out)
        reset_token = next(w for w in out.replace("：", " ").split() if w.startswith("nxv_"))
        self.assertNotEqual(reset_token, requested["dev_reset_token"])
        code, out, err = home.run("password-reset", "confirm", reset_token, "--password-stdin",
                                  stdin="another-new-password\n")
        self.assertEqual(code, 0, err)
        self.assertIn("API Key 未被吊销", out)
        self.assertEqual(home.json("whoami")["user"]["email"], "owner2@sdk.test")
        self.assertEqual(home.json("password-reset", "confirm", "nxv_bogus", "--password", "x" * 12,
                                   expect=1)["code"], "recovery_token_invalid")

    def _manifest(self, server):
        from neuxmind.robot import RobotClient
        return RobotClient(server.url, server.token).manifest()

    def test_cli_cloud_flow(self):
        home = CliHome()
        self.addCleanup(home.close)
        home.json("config", "set", "cloud_url", self.cloud.url)
        self.assertEqual(home.json("whoami", expect=1)["code"], "not_logged_in")
        code, out, _ = home.run("register", "--email", "dev2@sdk.test", "--password", PASSWORD)
        self.assertEqual(code, 0)
        self.assertIn("邮箱尚未验证", out)
        self.assertIn("仅开发环境", out)
        dev_token = next(w for w in out.split() if w.startswith("nxv_"))
        org_id = home.json("whoami")["current_org_id"]
        home.json("logout")
        code, out, err = home.run("login", "--email", "dev2@sdk.test", "--password-stdin", stdin=PASSWORD + "\n")
        self.assertEqual(code, 0, err)
        config = json.loads((home.path / "config.json").read_text())
        self.assertTrue(config["cloud_token"].startswith("nxs_"))
        self.assertEqual(home.json("whoami")["user"]["email"], "dev2@sdk.test")
        self.fund(org_id, 100)

        # Unverified: friendly refusal; then verify via the CLI.
        code, _out, err = home.run("models", "buy", "echo-llm")
        self.assertEqual(code, 1)
        self.assertIn("email_unverified", err)
        self.assertIn("nx verify-email", err)
        self.assertEqual(home.json("infer", "echo-llm", "hi", expect=1)["code"], "email_unverified")
        self.assertEqual(home.json("verify-email", "nxv_bogus", expect=1)["code"], "verification_token_invalid")
        self.assertEqual(home.json("verify-email", expect=1)["code"], "bad_request")
        resent = home.json("verify-email", "--resend")
        self.assertNotEqual(resent["dev_verification_token"], dev_token)
        self.assertEqual(home.json("verify-email", dev_token, expect=1)["code"], "verification_token_invalid")
        code, out, _ = home.run("verify-email", resent["dev_verification_token"])
        self.assertEqual(code, 0, out)
        self.assertIn("邮箱已验证", out)
        self.assertIn("邮箱：已验证", home.run("whoami")[1])
        self.assertEqual(home.json("verify-email", "--resend", expect=1)["code"], "already_verified")
        self.assertEqual(home.json("wallet")["wallet"]["available"], 100)
        home.json("robot", "add", "dog", self.dog.url, "--token", self.dog.token)
        home.json("robot", "add", "arm", self.arm.url, "--token", self.arm.token)

        dog_models = home.json("models", "list", "--robot", "dog")
        self.assertEqual({m["id"] for m in dog_models["models"]}, {"echo-llm", "dog-gait"})
        arm_models = home.json("models", "list", "--robot", "arm")
        self.assertEqual({m["id"] for m in arm_models["models"]}, {"echo-llm", "arm-vla"})
        code, out, _ = home.run("models", "list", "--robot", "dog")
        self.assertIn("适用于 dog", out)
        self.assertNotIn("arm-vla", out)
        self.assertEqual(len(home.json("models", "list")["models"]), 4)

        self.assertEqual(home.json("infer", "echo-llm", "hello", expect=1)["code"], "entitlement_required")
        home.json("models", "buy", "echo-llm")
        self.assertEqual(home.json("infer", "echo-llm", "x", "--option", "n=2", expect=1)["code"], "n_unsupported")
        code, _out, err = home.run("infer", "echo-llm", "x", "--option", "temperature=0.2")
        self.assertEqual(code, 1)
        self.assertIn("parameter_not_allowed", err)
        self.assertIn("temperature", err)
        result = home.json("infer", "echo-llm", "hello robot world", "--max-tokens", "40")
        self.assertEqual(result["output"]["text"], "echo: hello robot world")
        self.assertGreaterEqual(result["credits_charged"], 1)
        code, out, _ = home.run("infer", "echo-llm", "again")
        self.assertIn("echo: again", out)
        self.assertIn("测试替身", out)
        wallet = home.json("wallet", "--ledger")
        self.assertLess(wallet["wallet"]["balance"], 100)
        self.assertEqual(wallet["wallet"]["reserved"], 0)
        self.assertTrue(any(e["delta"] < 0 for e in wallet["ledger"]))
        self.assertEqual(home.json("models", "buy", "edge-detector", expect=1)["code"], "insufficient_credits")

        registered = home.json("robot", "register", "dog")
        self.assertEqual(registered["instance_id"], "sim-quadruped-01")
        self.assertTrue(registered["simulated"])


if __name__ == "__main__":
    unittest.main()
