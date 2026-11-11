"""Shared fixtures for cloud tests. Import this first in every test module."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

CLOUD = Path(__file__).resolve().parents[1]
REPO = CLOUD.parent
for path in (str(CLOUD), str(REPO / "spec")):
    if path not in sys.path:
        sys.path.insert(0, path)

from fastapi.testclient import TestClient  # noqa: E402

from neuxmind_cloud.app import create_app  # noqa: E402
from neuxmind_cloud.config import Settings  # noqa: E402
from neuxmind_cloud.payments import sign_webhook  # noqa: E402

API = "/api/v1"
PASSWORD = "correct-horse-battery"
OPERATOR_EMAIL = "ops@neuxmind.test"
SANDBOX_SECRET = "whsec_test_only_secret"


def quadruped_manifest(instance_id: str = "dog-1", **extra) -> dict:
    manifest = {"schema": "robot.adapter.v2", "instance_id": instance_id, "name": "Dog", "family": "quadruped",
                "adapter_version": "0.1.0", "vendor": "acme", "model": "q1", "simulated": True,
                "capabilities": [{"id": "safety.stop", "params": {"hardware_estop": True}}, {"id": "system.health"},
                                 {"id": "base.velocity", "params": {"max_vx": 1.0}}, {"id": "legged.posture"},
                                 {"id": "sensor.camera.rgb"}],
                "model_runtimes": ["onnxruntime"]}
    manifest.update(extra)
    return manifest


def arm_manifest(instance_id: str = "arm-1") -> dict:
    return {"schema": "robot.adapter.v2", "instance_id": instance_id, "name": "Arm", "family": "manipulator",
            "adapter_version": "0.1.0", "capabilities": [{"id": "safety.stop"}, {"id": "system.health"},
                                                        {"id": "arm.joint_position"}, {"id": "gripper.position"},
                                                        {"id": "sensor.camera.rgb"}, {"id": "policy.action_chunk"}],
            "model_runtimes": ["tensorrt"]}


ECHO_LLM = {"id": "echo-llm", "name": "Echo LLM (test double)", "source": "closed", "kind": "llm",
            "license": "test only", "pricing": {"type": "per_1k_tokens", "credits": 10},
            "upstream": {"provider": "echo"}}


# Run the whole suite on PostgreSQL instead of per-test SQLite files:
#   NEUXMIND_TEST_DATABASE_URL=postgresql+psycopg://user@host/db python3 -m unittest discover -s cloud/tests
# Every test starts from an empty schema (all tables dropped first).
PG_URL = os.environ.get("NEUXMIND_TEST_DATABASE_URL", "").strip()


def _reset_schema(url: str) -> None:
    from sqlalchemy import create_engine, text
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    engine.dispose()


class CloudTestCase(unittest.TestCase):
    """Fresh app + SQLite file database per test."""

    env = "test"
    settings_overrides: dict = {}

    def provider_factory(self):
        """Override to replace the upstream (default: the real factory, i.e. echo / OpenAI-compatible)."""
        return None

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nxcloud-")
        self.settings = self.make_settings(**self.settings_overrides)
        if PG_URL:
            _reset_schema(PG_URL)
        self.app = create_app(self.settings, provider_factory=self.provider_factory())
        self.client = TestClient(self.app)
        self.db = self.app.state.ctx.db

    def make_settings(self, **overrides) -> Settings:
        # scrypt_log_n=14 only to keep the suite fast (allowed in dev/test only;
        # prod default 17 is covered by test_security_hardening).
        values = dict(env=self.env, database_url=PG_URL or f"sqlite:///{self.tmp}/cloud.sqlite3",
                      operator_emails=[OPERATOR_EMAIL], webhook_secrets={"sandbox": SANDBOX_SECRET},
                      login_max_failures=3, max_body_bytes=200_000, cors_origins=["https://console.neuxmind.test"],
                      register_ip_max_per_hour=10_000, scrypt_log_n=14 if self.env in ("dev", "test") else 17)
        values.update(overrides)
        return Settings(**values)

    def tearDown(self):
        self.client.close()
        self.db.engine.dispose()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ helpers
    def register(self, email: str, password: str = PASSWORD, *, verify: bool = True, **extra) -> dict:
        """Register; by default also prove the mailbox, since spending needs a verified e-mail.

        dev/test: through the real HTTP flow, with the token the dev/test log
        sender returns. prod: no sender exists, so the management-command
        function stands in for "the operator confirmed it out of band"."""
        response = self.client.post(f"{API}/auth/register", json={"email": email, "password": password, **extra})
        self.assertEqual(response.status_code, 201, response.text)
        data = response.json()
        data["headers"] = {"Authorization": f"Bearer {data['token']}"}
        self.assertFalse(data["user"]["email_verified"])
        if verify:
            self.verify(data)
        return data

    def verify(self, user: dict) -> None:
        """dev/test: the real flow — the token, submitted from the account's own session.
        prod (no sender): a test-only DB shortcut standing in for the mailbox step
        (the management command ``verify-email`` would also reset the password)."""
        token = user.get("dev_verification_token")
        if token:
            self.ok(self.post("/auth/verify-email", {"token": token}, user))
        else:
            from sqlalchemy import update as sql_update
            from neuxmind_cloud.models import User
            with self.db.transaction() as s:
                s.execute(sql_update(User).where(User.email == user["user"]["email"]).values(email_verified=True))
        user["user"]["email_verified"] = True

    def operator(self) -> dict:
        """A platform operator.

        dev/test: allow-listed address + e-mail verified from its own session.
        prod/staging (allow-list off by default): the recommended path —
        ``create-operator`` on the server, then the owner sets a password with
        the one-time token."""
        if not hasattr(self, "_operator"):
            if self.settings.operator_allowlist_active:
                self._operator = self.register(OPERATOR_EMAIL)
            else:
                self._operator = self.create_operator(OPERATOR_EMAIL)
        return self._operator

    def create_operator(self, email: str) -> dict:
        from neuxmind_cloud import recovery
        with self.db.transaction() as s:
            _user, token = recovery.create_operator(s, email, self.settings)
        data = self.ok(self.post("/auth/password-reset", {"token": token, "password": PASSWORD}))
        data["headers"] = {"Authorization": f"Bearer {data['token']}"}
        data["org_id"] = self.ok(self.get("/me", data))["current_org_id"]
        return data

    def get(self, path: str, user: dict | None = None, **kwargs):
        headers = kwargs.pop("headers", None) or (user["headers"] if user else {})
        return self.client.get(API + path, headers=headers, **kwargs)

    def post(self, path: str, body=None, user: dict | None = None, **kwargs):
        headers = kwargs.pop("headers", None) or (user["headers"] if user else {})
        return self.client.post(API + path, json=body if body is not None else {}, headers=headers, **kwargs)

    def ok(self, response, status: int = 200) -> dict:
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    def error(self, response, status: int, code: str | None = None) -> dict:
        self.assertEqual(response.status_code, status, response.text)
        body = response.json()
        self.assertIn("error", body)
        self.assertIn("code", body)
        if code:
            self.assertEqual(body["code"], code, body)
        return body

    def publish(self, card: dict) -> dict:
        return self.ok(self.post("/admin/models", card, self.operator()), 201)

    def fund(self, user: dict, credits: int, reason: str = "test funding") -> dict:
        return self.ok(self.post(f"/admin/wallets/{user['org_id']}/adjust", {"delta": credits, "reason": reason},
                                 self.operator()))

    def entitle(self, user: dict, model_id: str) -> dict:
        return self.ok(self.post("/purchases", {"kind": "model_license", "model_id": model_id}, user), 201)

    def wallet(self, user: dict) -> dict:
        return self.ok(self.get("/wallet", user))

    def api_key(self, user: dict, scopes: list[str]) -> dict:
        data = self.ok(self.post("/api-keys", {"name": "k", "scopes": scopes}, user), 201)
        data["headers"] = {"Authorization": f"Bearer {data['key']}"}
        return data

    def webhook(self, provider: str, event: dict, secret: str = SANDBOX_SECRET, signature: str | None = None):
        body = json.dumps(event).encode()
        header = signature if signature is not None else sign_webhook(secret, body)
        return self.client.post(f"{API}/payments/{provider}/webhook", content=body,
                                headers={"X-NeuxMind-Signature": header, "Content-Type": "application/json"})


class LiveServer:
    """The app on a real uvicorn server in a background thread.

    Needed where the in-process TestClient cannot help: it buffers a whole
    streamed response before returning, and cannot drop a connection midway.
    Use ``httpx.Client(trust_env=False)`` against ``self.url``."""

    def __init__(self, app):
        import threading
        import time
        import uvicorn
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off",
                                loop="asyncio", http="h11")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started:
            if time.time() > deadline or not self.thread.is_alive():
                raise RuntimeError("uvicorn did not start")
            time.sleep(0.01)
        port = self.server.servers[0].sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(10)


def sse_events(text: str) -> list[tuple[str | None, str]]:
    """Parse an SSE body into ``[(event, data)]`` (test helper)."""
    events, name, data = [], None, []
    for line in text.split("\n"):
        line = line.rstrip("\r")
        if line == "":
            if data:
                events.append((name, "\n".join(data)))
            name, data = None, []
        elif line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[6:] if line[5:6] == " " else line[5:])
    return events
