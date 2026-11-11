"""Review P3 items: C-06 error text, C-07 scrypt cost, C-08 docs in prod, C-10 member removal / role rules."""
import json
import os

import support  # noqa: F401
from support import API, PASSWORD, CloudTestCase

from sqlalchemy import select

from neuxmind_cloud import security
from neuxmind_cloud.config import Settings
from neuxmind_cloud.models import User


class UpstreamErrorTextTest(CloudTestCase):
    def test_operator_details_never_reach_users(self):
        """C-06: the review saw "运营凭据 OPENAI_PROD_KEY_VAULT_PATH 未配置" in a user-facing error."""
        user = self.register("leak@example.com")
        self.publish({"id": "gpt", "name": "G", "source": "closed", "kind": "llm", "license": "t",
                      "pricing": {"type": "per_1k_tokens", "credits": 1},
                      "upstream": {"provider": "openai_compatible", "base_url": "http://127.0.0.1:9/v1", "model": "m",
                                   "credential_ref": "OPENAI_PROD_KEY_VAULT_PATH"}})
        self.entitle(user, "gpt")
        self.fund(user, 100)
        os.environ.pop("OPENAI_PROD_KEY_VAULT_PATH", None)
        body = self.error(self.post("/gateway/infer", {"model": "gpt", "input": "hi", "max_tokens": 5}, user), 502,
                          "upstream_error")
        visible = json.dumps([body, self.ok(self.get("/usage", user)), self.ok(self.get("/wallet/reservations", user))],
                             ensure_ascii=False)
        self.assertNotIn("OPENAI_PROD_KEY_VAULT_PATH", visible)
        self.assertNotIn("127.0.0.1", visible)
        audit = self.ok(self.get("/admin/audit?action=gateway.upstream_error", self.operator()))["audit"]
        self.assertIn("OPENAI_PROD_KEY_VAULT_PATH", audit[0]["detail"]["detail"])   # operators still see it


class ScryptCostTest(CloudTestCase):
    env = "prod"

    def test_prod_cost_and_transparent_upgrade(self):
        with self.assertRaises(ValueError):
            Settings(env="prod", scrypt_log_n=14)
        user = self.register("hash@example.com", verify=False)
        with self.db.transaction() as s:
            row = s.execute(select(User).where(User.email == "hash@example.com")).scalar_one()
            self.assertEqual(row.password_hash.split("$")[1], str(2 ** 17))
            # An iteration-01 hash (N=2^14) still verifies and is upgraded on login.
            security.configure(14)
            row.password_hash = security.hash_password(PASSWORD)
            security.configure(17)
        self.ok(self.post("/auth/login", {"email": "hash@example.com", "password": PASSWORD}))
        with self.db.transaction() as s:
            row = s.execute(select(User).where(User.email == "hash@example.com")).scalar_one()
            self.assertEqual(row.password_hash.split("$")[1], str(2 ** 17))
        self.assertTrue(user["token"])
        self.assertFalse(security.verify_password(PASSWORD, "scrypt$1099511627776$8$1$00$00"))  # absurd N refused


class DocsExposureTest(CloudTestCase):
    env = "prod"

    def test_docs_hidden_in_prod(self):
        self.assertEqual(self.client.get(f"{API}/docs").status_code, 404)
        self.assertEqual(self.client.get(f"{API}/openapi.json").status_code, 404)


class DocsInTestEnvTest(CloudTestCase):
    def test_docs_available_in_test_env(self):
        self.assertEqual(self.client.get(f"{API}/openapi.json").status_code, 200)


class MemberRulesTest(CloudTestCase):
    """C-10: member removal exists; owner is untouchable; admins are managed only by the owner."""

    def setUp(self):
        super().setUp()
        self.owner = self.register("owner@org.test")
        self.admin = self.register("admin@org.test")
        self.admin2 = self.register("admin2@org.test")
        self.dev = self.register("dev@org.test")
        for email, role in (("admin@org.test", "admin"), ("admin2@org.test", "admin"), ("dev@org.test", "developer")):
            self.ok(self.post("/org/members", {"email": email, "role": role}, self.owner))

    def in_org(self, user):
        return {**user["headers"], "X-NeuxMind-Org": self.owner["org_id"]}

    def delete(self, user_id, headers):
        return self.client.delete(f"{API}/org/members/{user_id}", headers=headers)

    def test_rules(self):
        members = {m["email"]: m["role"] for m in self.ok(self.get("/org/members", headers=self.in_org(self.admin)))["members"]}
        self.assertEqual(members, {"owner@org.test": "owner", "admin@org.test": "admin", "admin2@org.test": "admin",
                                   "dev@org.test": "developer"})
        # An admin can neither demote nor remove another admin, nor touch the owner.
        self.error(self.post("/org/members", {"email": "admin2@org.test", "role": "developer"},
                             headers=self.in_org(self.admin)), 403)
        self.error(self.delete(self.admin2["user"]["id"], self.in_org(self.admin)), 403)
        self.error(self.delete(self.owner["user"]["id"], self.in_org(self.admin)), 403)
        self.error(self.delete(self.owner["user"]["id"], self.owner["headers"]), 403)   # owner cannot leave either
        # A developer cannot remove others but can leave.
        self.error(self.delete(self.admin["user"]["id"], self.in_org(self.dev)), 403)
        key = self.ok(self.post("/api-keys", {"scopes": ["catalog:read"]}, headers=self.in_org(self.dev)), 201)
        self.ok(self.delete(self.dev["user"]["id"], self.in_org(self.dev)))
        self.error(self.get("/catalog/models", headers={"Authorization": f"Bearer {key['key']}"}), 401, "invalid_api_key")
        self.error(self.get("/me", headers=self.in_org(self.dev)), 403, "not_member")
        # The owner manages admins.
        self.ok(self.post("/org/members", {"email": "admin2@org.test", "role": "developer"}, self.owner))
        self.ok(self.delete(self.admin2["user"]["id"], self.owner["headers"]))
        self.error(self.delete(self.admin2["user"]["id"], self.owner["headers"]), 404)


class RereviewP3Test(CloudTestCase):
    def test_zero_credit_overage_can_only_be_waived(self):
        """Rereview N-07."""
        from neuxmind_cloud.models import OverageRecord
        user = self.register("tiny@example.com")
        self.fund(user, 10)
        with self.db.transaction() as s:
            s.add(OverageRecord(id="ovg_tiny", org_id=user["org_id"], reservation_id="rsv_x", model_id="m",
                                reported_tokens=101, charged_tokens=100, overage_tokens=1, overage_credits=0))
        self.error(self.post("/admin/overages/ovg_tiny/resolve", {"action": "charge", "credits": 1, "reason": "x"},
                             self.operator()), 400, "nothing_to_charge")
        self.assertEqual(self.wallet(user)["balance"], 10)
        self.ok(self.post("/admin/overages/ovg_tiny/resolve", {"action": "waive", "reason": "1 token"}, self.operator()))

    def test_lazy_sweep_needs_authentication(self):
        """Rereview N-11: unauthenticated gateway requests do not trigger a sweep."""
        from unittest import mock
        with mock.patch.object(self.app.state.ctx.maintenance, "maybe_sweep") as sweep:
            self.error(self.post("/gateway/infer", {"model": "x", "input": "x", "max_tokens": 1}), 401)
            self.error(self.post("/gateway/sessions", {"request_id": "r", "model": "x", "max_tokens": 1}), 401)
            sweep.assert_not_called()
            user = self.register("swept@example.com")
            self.post("/gateway/infer", {"model": "x", "input": "x", "max_tokens": 1}, user)
            sweep.assert_called_once()
