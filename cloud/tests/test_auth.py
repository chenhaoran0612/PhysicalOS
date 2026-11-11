"""Registration, login, rate limiting, session expiry, operator privileges, API keys."""
import support  # noqa: F401  (sets sys.path)
from support import API, OPERATOR_EMAIL, PASSWORD, CloudTestCase

from sqlalchemy import select, update

from neuxmind_cloud import auth
from neuxmind_cloud.models import ApiKey, AuthSession, Membership, Organization, User
from neuxmind_cloud.security import token_hash


class AuthTest(CloudTestCase):
    def test_register_creates_personal_org_owner(self):
        user = self.register("alice@example.com")
        me = self.ok(self.get("/me", user))
        self.assertEqual(me["role"], "owner")
        self.assertEqual(me["current_org_id"], user["org_id"])
        self.assertTrue(me["organizations"][0]["personal"])
        self.assertFalse(me["platform_operator"])
        with self.db.transaction() as s:
            row = s.execute(select(User).where(User.email == "alice@example.com")).scalar_one()
            self.assertTrue(row.password_hash.startswith("scrypt$"))
            self.assertNotIn(PASSWORD, row.password_hash)
            # Session tokens are stored only as hashes.
            self.assertIsNotNone(s.get(AuthSession, token_hash(user["token"])))
            self.assertIsNone(s.get(AuthSession, user["token"]))

    def test_duplicate_and_invalid_registration(self):
        self.register("bob@example.com")
        self.error(self.post("/auth/register", {"email": "BOB@example.com", "password": PASSWORD}), 409, "email_taken")
        self.error(self.post("/auth/register", {"email": "short@example.com", "password": "123"}), 400)
        self.error(self.post("/auth/register", {"email": "not-an-email", "password": PASSWORD}), 400)

    def test_login_logout(self):
        self.register("carol@example.com")
        login = self.ok(self.post("/auth/login", {"email": "carol@example.com", "password": PASSWORD}))
        headers = {"Authorization": f"Bearer {login['token']}"}
        self.ok(self.client.get(f"{API}/me", headers=headers))
        self.ok(self.client.post(f"{API}/auth/logout", headers=headers))
        self.error(self.client.get(f"{API}/me", headers=headers), 401, "session_expired")
        self.error(self.post("/auth/login", {"email": "carol@example.com", "password": "wrong-password!"}), 401,
                   "invalid_credentials")
        self.error(self.get("/me"), 401, "unauthorized")

    def test_login_rate_limit(self):
        self.register("dave@example.com")
        for _ in range(3):  # login_max_failures = 3 in tests
            self.error(self.post("/auth/login", {"email": "dave@example.com", "password": "wrong-password!"}), 401)
        # Even the correct password is refused while limited.
        self.error(self.post("/auth/login", {"email": "dave@example.com", "password": PASSWORD}), 429, "rate_limited")
        # Unknown e-mails are limited the same way (no account enumeration by behaviour).
        for _ in range(3):
            self.error(self.post("/auth/login", {"email": "ghost@example.com", "password": "wrong-password!"}), 401)
        self.error(self.post("/auth/login", {"email": "ghost@example.com", "password": "wrong-password!"}), 429)

    def test_session_expiry(self):
        user = self.register("erin@example.com")
        with self.db.transaction() as s:
            s.execute(update(AuthSession).values(expires_at=0))
        self.error(self.get("/me", user), 401, "session_expired")


class OperatorPrivilegeTest(CloudTestCase):
    def test_cannot_self_escalate(self):
        user = self.register("mallory@example.com", is_platform_operator=True, role="operator", platform_operator=True)
        self.assertFalse(self.ok(self.get("/me", user))["platform_operator"])
        self.error(self.get("/admin/orders", user), 403, "operator_required")
        self.error(self.post(f"/admin/wallets/{user['org_id']}/adjust", {"delta": 1000, "reason": "free money"}, user),
                   403, "operator_required")
        # Owner role cannot be granted through the members API either.
        self.register("friend@example.com")
        self.error(self.post("/org/members", {"email": "friend@example.com", "role": "owner"}, user), 400)

    def test_env_operator_and_management_command(self):
        op = self.operator()
        self.assertTrue(self.ok(self.get("/me", op))["platform_operator"])
        self.ok(self.get("/admin/orders", op))
        user = self.register("promoted@example.com")
        self.error(self.get("/admin/orders", user), 403)
        with self.db.transaction() as s:
            auth.grant_operator(s, "promoted@example.com")
        self.ok(self.get("/admin/orders", user))

    def test_admin_requires_session_not_api_key(self):
        op = self.operator()
        key = self.api_key(op, ["gateway"])
        self.error(self.get("/admin/orders", headers=key["headers"]), 403, "session_required")


class ApiKeyTest(CloudTestCase):
    def test_plaintext_shown_once_and_hashed(self):
        user = self.register("frank@example.com")
        key = self.api_key(user, ["gateway", "catalog:read"])
        self.assertTrue(key["key"].startswith("nx_live_"))
        listing = self.ok(self.get("/api-keys", user))["api_keys"]
        self.assertEqual(len(listing), 1)
        self.assertNotIn("key", listing[0])
        self.assertNotIn(key["key"], str(listing))
        with self.db.transaction() as s:
            row = s.get(ApiKey, key["id"])
            self.assertEqual(row.key_hash, token_hash(key["key"]))
            self.assertNotIn(key["key"], str(row.__dict__))
        self.ok(self.get("/catalog/models", headers=key["headers"]))

    def test_revoke(self):
        user = self.register("grace@example.com")
        key = self.api_key(user, ["catalog:read"])
        self.ok(self.get("/catalog/models", headers=key["headers"]))
        self.assertTrue(self.ok(self.client.delete(f"{API}/api-keys/{key['id']}", headers=user["headers"]))["revoked"])
        self.error(self.get("/catalog/models", headers=key["headers"]), 401, "invalid_api_key")

    def test_scopes_enforced(self):
        user = self.register("heidi@example.com")
        key = self.api_key(user, ["catalog:read"])
        self.error(self.get("/robots", headers=key["headers"]), 403, "scope_missing")
        self.error(self.get("/wallet", headers=key["headers"]), 403, "scope_missing")
        self.error(self.post("/gateway/infer", {"model": "x", "input": "hi", "max_tokens": 5}, headers=key["headers"]),
                   403, "scope_missing")
        # Keys cannot manage keys or buy things.
        self.error(self.post("/api-keys", {"scopes": ["gateway"]}, headers=key["headers"]), 403, "session_required")
        self.error(self.post("/purchases", {"kind": "credit_pack", "pack_id": "p"}, headers=key["headers"]), 403)
        self.error(self.post("/api-keys", {"scopes": ["admin"]}, user), 400, "invalid_scope")

    def test_key_scope_cannot_exceed_role(self):
        owner = self.register("ivan@example.com")
        member = self.register("judy@example.com")
        self.ok(self.post("/org/members", {"email": "judy@example.com", "role": "operator"}, owner))
        headers = {**member["headers"], "X-NeuxMind-Org": owner["org_id"]}
        self.error(self.post("/api-keys", {"scopes": ["gateway"]}, headers=headers), 403)  # operator role: no keys:manage
        self.ok(self.post("/org/members", {"email": "judy@example.com", "role": "developer"}, owner))
        key = self.ok(self.post("/api-keys", {"scopes": ["robots"]}, headers=headers), 201)
        self.assertEqual(key["org_id"], owner["org_id"])
        # Demoting the creator narrows the key immediately.
        self.ok(self.post("/org/members", {"email": "judy@example.com", "role": "operator"}, owner))
        k = {"Authorization": f"Bearer {key['key']}"}
        self.ok(self.get("/robots", headers=k))
        with self.db.transaction() as s:
            s.query(Membership).filter(Membership.org_id == owner["org_id"],
                                       Membership.user_id == member["user"]["id"]).delete()
        self.error(self.get("/robots", headers=k), 401, "invalid_api_key")

    def test_demoting_creator_narrows_key_scopes(self):
        """Mutant C4 (key scopes no longer intersected with the creator's role) must fail here."""
        owner = self.register("olga@example.com")
        dev = self.register("dmitri@example.com")
        self.ok(self.post("/org/members", {"email": "dmitri@example.com", "role": "developer"}, owner))
        headers = {**dev["headers"], "X-NeuxMind-Org": owner["org_id"]}
        key = self.ok(self.post("/api-keys", {"scopes": ["gateway", "robots", "usage:read"]}, headers=headers), 201)
        k = {"Authorization": f"Bearer {key['key']}"}
        self.assertEqual(set(self.ok(self.get("/me", headers=k))["permissions"]), {"gateway", "robots", "usage:read"})
        probe = {"model": "nope", "input": "x", "max_tokens": 5}
        self.error(self.post("/gateway/infer", probe, headers=k), 404, "model_not_found")   # scope passes
        # Demote to the org-level field operator: no gateway permission any more.
        self.ok(self.post("/org/members", {"email": "dmitri@example.com", "role": "operator"}, owner))
        self.assertEqual(set(self.ok(self.get("/me", headers=k))["permissions"]), {"robots", "usage:read"})
        self.error(self.post("/gateway/infer", probe, headers=k), 403, "scope_missing")
        self.ok(self.get("/robots", headers=k))
        self.ok(self.get("/usage", headers=k))
        # Removing the member through the API revokes the key's access entirely (review C-10).
        self.ok(self.client.delete(f"{API}/org/members/{dev['user']['id']}", headers=owner["headers"]))
        self.error(self.get("/robots", headers=k), 401, "invalid_api_key")

    def test_unknown_token_types_rejected(self):
        self.error(self.get("/me", headers={"Authorization": "Bearer something-random"}), 401, "invalid_token")
        self.error(self.get("/me", headers={"Authorization": "Bearer nx_live_forged"}), 401, "invalid_api_key")
        with self.db.transaction() as s:
            self.assertEqual(s.query(Organization).count(), 0)
