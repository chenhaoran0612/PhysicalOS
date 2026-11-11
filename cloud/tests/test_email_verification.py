"""E-mail verification and platform-operator eligibility.

Review C-01 (allow-listed address squatting) and rereview N-01 (pre-registration
hijack: the owner verifying an account an attacker registered).
"""
import sqlite3
from unittest import mock

import support  # noqa: F401
from support import API, ECHO_LLM, OPERATOR_EMAIL, PASSWORD, CloudTestCase

from sqlalchemy import select, update

from neuxmind_cloud import auth, recovery
from neuxmind_cloud.config import Settings
from neuxmind_cloud.errors import ApiError
from neuxmind_cloud.models import EmailVerificationToken, Entitlement, User
from neuxmind_cloud.security import token_hash

ATTACKER_PASSWORD = "attacker-chosen-pass"


def hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class PreRegistrationHijackTest(CloudTestCase):
    """Rereview N-01, the reproduced path: must now fail at every step."""

    def test_owner_clicking_link_does_not_verify_attackers_account(self):
        attacker = self.register(" OPS@NEUXMIND.TEST ", password=ATTACKER_PASSWORD, verify=False)
        attacker_key = self.api_key(attacker, ["catalog:read"])
        mailbox_token = attacker["dev_verification_token"]          # what lands in the owner's mailbox
        # The owner, without logging in, submits the token: refused.
        self.error(self.post("/auth/verify-email", {"token": mailbox_token}), 401)
        # The owner, logged into a different account, submits it: refused, token not consumed.
        other = self.register("someone@example.com")
        self.error(self.post("/auth/verify-email", {"token": mailbox_token}, other), 403, "verification_wrong_account")
        me = self.ok(self.get("/me", attacker))
        self.assertEqual((me["email_verified"], me["platform_operator"]), (False, False))
        self.error(self.post(f"/admin/wallets/{attacker['org_id']}/adjust", {"delta": 10 ** 6, "reason": "x"},
                             attacker), 403, "operator_required")
        # The owner re-registers: instead of 409, a claim token goes to the mailbox.
        response = self.post("/auth/register", {"email": OPERATOR_EMAIL, "password": PASSWORD})
        self.assertEqual(response.status_code, 202, response.text)
        claim = response.json()
        self.assertEqual(claim["status"], "claim_sent")
        self.assertNotIn("token", claim)                            # no session for the new registrant
        owner = self.ok(self.post("/auth/claim", {"token": claim["dev_claim_token"], "password": PASSWORD}))
        self.assertTrue(owner["user"]["email_verified"])
        self.assertEqual(owner["user"]["email"], OPERATOR_EMAIL)
        self.assertNotEqual(owner["user"]["id"], attacker["user"]["id"])
        # The attacker is locked out everywhere; the owner is the (allow-listed) operator.
        self.error(self.get("/me", attacker), 401, "session_expired")
        self.error(self.get("/catalog/models", headers=attacker_key["headers"]), 401, "invalid_api_key")
        self.error(self.post("/auth/login", {"email": OPERATOR_EMAIL, "password": ATTACKER_PASSWORD}), 401)
        self.assertTrue(self.ok(self.get("/me", headers=hdr(owner["token"])))["platform_operator"])
        # The stale mailbox token cannot be replayed onto anything.
        self.error(self.post("/auth/verify-email", {"token": mailbox_token}, headers=hdr(owner["token"])), 400)
        with self.db.transaction() as s:
            old = s.get(User, attacker["user"]["id"])
            self.assertTrue(old.email.startswith("claimed+"))
            self.assertFalse(old.is_platform_operator)
            old_email = old.email
        # The disabled account cannot be logged into with the attacker's password either.
        self.error(self.post("/auth/login", {"email": old_email, "password": ATTACKER_PASSWORD}), 401,
                   "invalid_credentials")

    def test_squatter_cannot_cancel_owners_claim_token(self):
        self.register("victim@example.com", password=ATTACKER_PASSWORD, verify=False)
        first = self.post("/auth/register", {"email": "victim@example.com", "password": PASSWORD}).json()
        self.post("/auth/register", {"email": "victim@example.com", "password": ATTACKER_PASSWORD})  # re-trigger
        self.ok(self.post("/auth/claim", {"token": first["dev_claim_token"], "password": PASSWORD}))

    def test_claim_invalid_once_the_real_holder_verified(self):
        owner = self.register("real@example.com", verify=False)
        claim = self.post("/auth/register", {"email": "real@example.com", "password": ATTACKER_PASSWORD}).json()
        self.verify(owner)   # the account really was the owner's: verified from its own session
        self.error(self.post("/auth/claim", {"token": claim["dev_claim_token"], "password": ATTACKER_PASSWORD}), 409,
                   "recovery_token_invalid")
        self.error(self.post("/auth/register", {"email": "real@example.com", "password": PASSWORD}), 409, "email_taken")

    def test_verification_revokes_other_sessions_and_earlier_keys(self):
        user = self.register("rev@example.com", verify=False)
        second = self.ok(self.post("/auth/login", {"email": "rev@example.com", "password": PASSWORD}))
        key = self.api_key(user, ["catalog:read"])
        result = self.ok(self.post("/auth/verify-email", {"token": user["dev_verification_token"]}, user))
        self.assertEqual((result["sessions_revoked"], result["api_keys_revoked"]), (1, 1))
        self.ok(self.get("/me", user))                                           # the verifying session survives
        self.error(self.get("/me", headers=hdr(second["token"])), 401)
        self.error(self.get("/catalog/models", headers=key["headers"]), 401, "invalid_api_key")


class PasswordResetTest(CloudTestCase):
    def test_reset_flow_revokes_all_sessions(self):
        user = self.register("forgot@example.com")
        accepted = self.ok(self.post("/auth/password-reset/request", {"email": "forgot@example.com"}), 202)
        token = accepted["dev_reset_token"]
        with self.db.transaction() as s:
            self.assertEqual(s.get(EmailVerificationToken, token_hash(token)).purpose, "reset")
        new = self.ok(self.post("/auth/password-reset", {"token": token, "password": "brand-new-password"}))
        self.assertEqual(new["purpose"], "reset")
        self.error(self.get("/me", user), 401)
        self.ok(self.get("/me", headers=hdr(new["token"])))
        self.error(self.post("/auth/login", {"email": "forgot@example.com", "password": PASSWORD}), 401)
        self.ok(self.post("/auth/login", {"email": "forgot@example.com", "password": "brand-new-password"}))
        self.error(self.post("/auth/password-reset", {"token": token, "password": "another-password"}), 400,
                   "recovery_token_invalid")

    def test_no_enumeration_and_unverified_gets_claim(self):
        unknown = self.ok(self.post("/auth/password-reset/request", {"email": "nobody@example.com"}), 202)
        self.assertEqual(set(unknown), {"status", "message"})
        self.register("half@example.com", verify=False)
        claim = self.ok(self.post("/auth/password-reset/request", {"email": "half@example.com"}), 202)
        self.assertIn("dev_claim_token", claim)
        self.assertNotIn("dev_reset_token", claim)
        # A verify token or a claim token is not a reset token and vice versa.
        user = self.register("mix@example.com", verify=False)
        self.error(self.post("/auth/password-reset", {"token": user["dev_verification_token"], "password": PASSWORD}),
                   400, "recovery_token_invalid")
        # ... and a claim token is not a verification token, even from the account's own session.
        claim = self.ok(self.post("/auth/password-reset/request", {"email": "mix@example.com"}), 202)["dev_claim_token"]
        self.error(self.post("/auth/verify-email", {"token": claim}, user), 400, "verification_token_invalid")
        self.assertFalse(self.ok(self.get("/me", user))["email_verified"])

    def test_expired_reset_token(self):
        self.register("late@example.com")
        token = self.ok(self.post("/auth/password-reset/request", {"email": "late@example.com"}), 202)["dev_reset_token"]
        with self.db.transaction() as s:
            s.execute(update(EmailVerificationToken).values(expires_at=0))
        self.error(self.post("/auth/password-reset", {"token": token, "password": PASSWORD}), 400,
                   "recovery_token_expired")


class OperatorCommandsProdTest(CloudTestCase):
    """prod: allow-list ignored by default; operators come from create-operator / grant-operator."""

    env = "prod"

    def test_allow_list_ignored_in_prod_unless_enabled(self):
        user = self.register("  OPS@neuxmind.test ", verify=False)
        self.assertEqual(user["verification"]["status"], "unavailable")
        self.verify(user)                       # even verified, the allow-list is off in prod
        self.assertFalse(self.ok(self.get("/me", user))["platform_operator"])
        self.assertTrue(Settings(env="prod", operator_emails=["a@b.c"], operator_allowlist_enabled=True)
                        .operator_allowlist_active)
        self.assertFalse(Settings(env="prod", operator_emails=["a@b.c"]).operator_allowlist_active)

    def test_create_operator_and_set_password(self):
        op = self.create_operator("boss@example.com")
        me = self.ok(self.get("/me", op))
        self.assertTrue(me["platform_operator"] and me["email_verified"])
        with self.assertRaises(ApiError) as cm, self.db.transaction() as s:
            recovery.create_operator(s, "boss@example.com", self.settings)
        self.assertEqual(cm.exception.code, "email_taken")

    def test_grant_operator_refuses_unverified(self):
        user = self.register("promote@example.com", verify=False)
        with self.assertRaises(ApiError) as cm, self.db.transaction() as s:
            auth.grant_operator(s, "promote@example.com")
        self.assertEqual(cm.exception.code, "email_unverified")
        self.error(self.get("/admin/orders", user), 403, "operator_required")
        self.verify(user)
        with self.db.transaction() as s:
            auth.grant_operator(s, "promote@example.com")
        self.ok(self.get("/admin/orders", user))
        with self.db.transaction() as s:
            auth.revoke_operator(s, "promote@example.com")
        self.error(self.get("/admin/orders", user), 403, "operator_required")

    def test_verify_email_command_takes_account_from_whoever_set_the_password(self):
        squatter = self.register("owner@example.com", password=ATTACKER_PASSWORD, verify=False)
        key = self.api_key(squatter, ["catalog:read"])
        with self.assertRaises(ApiError) as cm, self.db.transaction() as s:
            recovery.admin_verify(s, "owner@example.com", self.settings)
        self.assertEqual(cm.exception.code, "credentials_exist")
        with self.db.transaction() as s:
            _user, token, revoked = recovery.admin_verify(s, "owner@example.com", self.settings, force=True)
        self.assertEqual(revoked, {"sessions_revoked": 1, "api_keys_revoked": 1})
        self.error(self.get("/me", squatter), 401)
        self.error(self.get("/catalog/models", headers=key["headers"]), 401)
        self.error(self.post("/auth/login", {"email": "owner@example.com", "password": ATTACKER_PASSWORD}), 401)
        owner = self.ok(self.post("/auth/password-reset", {"token": token, "password": PASSWORD}))
        self.assertTrue(owner["user"]["email_verified"])
        # Without a sender, a reset request is an explicit error.
        self.error(self.post("/auth/password-reset/request", {"email": "owner@example.com"}), 503,
                   "email_sender_not_configured")

    def test_existing_unverified_without_sender(self):
        self.register("held@example.com", verify=False)
        self.error(self.post("/auth/register", {"email": "held@example.com", "password": PASSWORD}), 409,
                   "email_pending_verification")

    def test_log_sender_refused_outside_dev_test(self):
        with self.assertRaises(ValueError):
            Settings(env="prod", email_sender="log")
        with self.assertRaises(ValueError):
            Settings(env="staging", email_sender="log")


class VerificationFlowTest(CloudTestCase):
    def test_register_verify_single_use_and_hash_only(self):
        user = self.register("vera@example.com", verify=False)
        self.assertEqual(user["verification"], {**user["verification"], "status": "sent", "sender": "log",
                                                "simulated": True})
        token = user["dev_verification_token"]
        self.assertTrue(token.startswith("nxv_"))
        with self.db.transaction() as s:
            row = s.get(EmailVerificationToken, token_hash(token))
            self.assertEqual(row.purpose, "verify")
            self.assertNotIn(token, str(row.__dict__))
        outbox = self.app.state.ctx.email_sender.outbox
        self.assertEqual(outbox[-1]["to"], "vera@example.com")
        self.assertIn(token, outbox[-1]["body"])
        self.assertFalse(self.ok(self.get("/me", user))["email_verified"])
        verified = self.ok(self.post("/auth/verify-email", {"token": token}, user))
        self.assertTrue(verified["user"]["email_verified"])
        self.assertTrue(self.ok(self.get("/me", user))["email_verified"])
        self.error(self.post("/auth/verify-email", {"token": token}, user), 400, "verification_token_invalid")
        self.error(self.post("/auth/verify-email", {"token": "nxv_forged"}, user), 400, "verification_token_invalid")
        self.error(self.post("/auth/verify-email", {}, user), 400, "verification_token_invalid")
        self.error(self.post("/auth/verify-email/resend", {}, user), 409, "already_verified")

    def test_expired_and_superseded_tokens(self):
        user = self.register("exp@example.com", verify=False)
        first = user["dev_verification_token"]
        second = self.ok(self.post("/auth/verify-email/resend", {}, user))["dev_verification_token"]
        self.error(self.post("/auth/verify-email", {"token": first}, user), 400, "verification_token_invalid")
        with self.db.transaction() as s:
            s.execute(update(EmailVerificationToken).values(expires_at=0))
        self.error(self.post("/auth/verify-email", {"token": second}, user), 400, "verification_token_expired")
        self.assertFalse(self.ok(self.get("/me", user))["email_verified"])

    def test_resend_is_rate_limited(self):
        user = self.register("spam@example.com", verify=False)
        for _ in range(self.settings.email_verify_max_sends_per_hour):
            self.ok(self.post("/auth/verify-email/resend", {}, user))
        self.error(self.post("/auth/verify-email/resend", {}, user), 429, "rate_limited")

    def test_allow_listed_operator_only_after_verification(self):
        op = self.register(OPERATOR_EMAIL, verify=False)
        self.error(self.get("/admin/orders", op), 403, "operator_required")
        self.verify(op)
        self.ok(self.get("/admin/orders", op))

    def test_request_fields_cannot_mark_verified(self):
        user = self.register("liar@example.com", verify=False, email_verified=True, is_platform_operator=True)
        self.assertFalse(self.ok(self.get("/me", user))["email_verified"])


class UnverifiedSpendingTest(CloudTestCase):
    """Unverified users can register, log in and browse; buying and the gateway need verification."""

    def setUp(self):
        super().setUp()
        self.publish(ECHO_LLM)
        self.user = self.register("unverified@example.com", verify=False)
        self.fund(self.user, 1000)

    def test_browse_allowed_spend_refused(self):
        self.ok(self.post("/auth/login", {"email": "unverified@example.com", "password": PASSWORD}))
        self.ok(self.get("/me", self.user))
        self.ok(self.get("/catalog/models", self.user))
        self.ok(self.get("/wallet", self.user))
        self.error(self.post("/purchases", {"kind": "model_license", "model_id": "echo-llm"}, self.user), 403,
                   "email_unverified")
        self.error(self.post("/purchases", {"kind": "credit_pack", "pack_id": "p"}, self.user), 403, "email_unverified")
        self.error(self.post("/payments/manual/checkout", {"order_id": "x"}, self.user), 403, "email_unverified")
        with self.db.transaction() as s:
            s.add(Entitlement(org_id=self.user["org_id"], model_id="echo-llm"))
        self.error(self.post("/gateway/infer", {"model": "echo-llm", "input": "hi", "max_tokens": 5}, self.user), 403,
                   "email_unverified")
        self.error(self.post("/gateway/sessions", {"request_id": "s", "model": "echo-llm", "max_tokens": 100},
                             self.user), 403, "email_unverified")
        key = self.api_key(self.user, ["gateway"])     # keys can be created, but act as their creator
        self.error(self.post("/gateway/infer", {"model": "echo-llm", "input": "hi", "max_tokens": 5},
                             headers=key["headers"]), 403, "email_unverified")
        self.assertEqual(self.wallet(self.user)["reserved"], 0)
        self.verify(self.user)
        # Keys created before verification are revoked by it (rereview N-01); a new key works.
        self.error(self.post("/gateway/infer", {"model": "echo-llm", "input": "hi", "max_tokens": 5},
                             headers=key["headers"]), 401, "invalid_api_key")
        key = self.api_key(self.user, ["gateway"])
        self.ok(self.post("/gateway/infer", {"model": "echo-llm", "input": "hi", "max_tokens": 5},
                          headers=key["headers"]))


class SmtpSenderTest(CloudTestCase):
    env = "prod"
    settings_overrides = {"email_sender": "smtp", "smtp_host": "smtp.example.test", "smtp_port": 2525,
                          "smtp_user": "mailer", "smtp_password_ref": "NX_TEST_SMTP_PASSWORD",
                          "smtp_from": "no-reply@neuxmind.test"}

    def test_smtp_send_and_failure(self):
        """smtplib is mocked: this checks our use of it, not a real mail server."""
        with mock.patch("neuxmind_cloud.mailer.smtplib.SMTP") as smtp, \
                mock.patch.dict("os.environ", {"NX_TEST_SMTP_PASSWORD": "pw"}):
            user = self.register("mailme@example.com", verify=False)
            conn = smtp.return_value.__enter__.return_value
            smtp.assert_called_once_with("smtp.example.test", 2525, timeout=15)
            conn.starttls.assert_called_once()
            conn.login.assert_called_once_with("mailer", "pw")
            message = conn.send_message.call_args[0][0]
            self.assertEqual(message["To"], "mailme@example.com")
        self.assertEqual(user["verification"]["status"], "sent")
        self.assertNotIn("dev_verification_token", user)
        token = message.get_content().split("验证令牌：")[1].split()[0]
        self.ok(self.post("/auth/verify-email", {"token": token}, user))
        import smtplib
        with mock.patch("neuxmind_cloud.mailer.smtplib.SMTP", side_effect=smtplib.SMTPConnectError(421, "down")):
            failed = self.register("down@example.com", verify=False)
        self.assertEqual(failed["verification"]["status"], "failed")   # account exists; user can resend later


class SchemaUpgradeTest(CloudTestCase):
    def test_existing_database_gets_columns_and_operators_must_verify(self):
        path = f"{self.tmp}/old.sqlite3"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE users (id VARCHAR(40) PRIMARY KEY, email VARCHAR(200) UNIQUE, name VARCHAR(100), "
                     "password_hash VARCHAR(300), is_platform_operator BOOLEAN, created_at FLOAT)")
        conn.execute("INSERT INTO users VALUES ('usr_old', 'old-op@example.com', 'Old', 'x', 1, 0)")
        conn.execute("CREATE TABLE email_verification_tokens (token_hash VARCHAR(64) PRIMARY KEY, user_id VARCHAR(40), "
                     "email VARCHAR(200), expires_at FLOAT, used_at FLOAT, created_at FLOAT)")
        conn.execute("INSERT INTO email_verification_tokens VALUES ('h', 'usr_old', 'old-op@example.com', 9e12, NULL, 0)")
        conn.commit()
        conn.close()
        from neuxmind_cloud.db import Database
        db = Database(f"sqlite:///{path}")
        db.create_all()
        db.create_all()   # idempotent
        with db.transaction() as s:
            user = s.execute(select(User).where(User.email == "old-op@example.com")).scalar_one()
            self.assertFalse(user.email_verified)
            self.assertFalse(auth.is_platform_operator(user, self.settings))
            self.assertEqual(s.get(EmailVerificationToken, "h").purpose, "verify")
        db.engine.dispose()
