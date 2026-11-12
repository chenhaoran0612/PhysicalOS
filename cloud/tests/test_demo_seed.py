"""scripts/demo.py seeds the demo cloud; the Docker cloud keeps its database between
starts, so seeding an already seeded platform must change nothing and must not fail."""
import importlib.util
import shutil
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import support  # noqa: F401
from support import API, REPO, CloudTestCase

BASE = "http://cloud.test"


def load_demo():
    spec = importlib.util.spec_from_file_location("nx_demo_script", REPO / "scripts" / "demo.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DemoSeedTest(CloudTestCase):
    def setUp(self):
        self.demo = load_demo()
        self.settings_overrides = {"operator_emails": [self.demo.OPS_EMAIL]}
        super().setUp()
        self.demo.http = self.http_through_test_client

    def http_through_test_client(self, method, url, body=None, token="", timeout=10.0):
        headers = {"Authorization": "Bearer " + token} if token else {}
        response = self.client.request(method, url.removeprefix(BASE), json=body, headers=headers)
        return response.status_code, (response.json() if response.content else {})

    def demo_user(self) -> dict:
        response = self.client.post(f"{API}/auth/login",
                                    json={"email": self.demo.DEMO_EMAIL, "password": self.demo.DEMO_PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        return {"headers": {"Authorization": f"Bearer {response.json()['token']}"}}

    def seeded_state(self) -> dict:
        user = self.demo_user()
        wallet = self.client.get(f"{API}/wallet", headers=user["headers"]).json()
        models = self.client.get(f"{API}/catalog/models", headers=user["headers"]).json()["models"]
        ledger = self.client.get(f"{API}/wallet/ledger", headers=user["headers"]).json()["entries"]
        return {"balance": wallet["balance"], "models": sorted(m["id"] for m in models), "ledger_entries": len(ledger)}

    def test_first_seed_creates_accounts_catalog_and_credits(self):
        info = self.demo.seed_cloud(BASE)

        self.assertEqual(info["user"], self.demo.DEMO_EMAIL)
        self.assertEqual(self.seeded_state(), {"balance": 50000, "ledger_entries": 1,
                                               "models": sorted(m["id"] for m in self.demo.DEMO_MODELS)})

    def test_seeding_again_changes_nothing(self):
        self.demo.seed_cloud(BASE)
        before = self.seeded_state()

        info = self.demo.seed_cloud(BASE)

        self.assertEqual(info["credits"], 50000)
        self.assertEqual(self.seeded_state(), before)

    def test_seed_finishes_an_interrupted_earlier_seed(self):
        # Registered but never verified (the earlier run died in between): platform
        # operators need a verified e-mail, so the seed has to finish that step.
        response = self.client.post(f"{API}/auth/register",
                                    json={"email": self.demo.OPS_EMAIL, "password": self.demo.DEMO_PASSWORD})
        self.assertEqual(response.status_code, 201, response.text)

        self.demo.seed_cloud(BASE)

        self.assertEqual(self.seeded_state()["balance"], 50000)

    def test_account_that_exists_with_another_password_is_explained(self):
        self.register(self.demo.DEMO_EMAIL)        # support.PASSWORD, not the demo password

        with self.assertRaisesRegex(RuntimeError, "down -v"):
            self.demo.seed_cloud(BASE)


class DemoDockerModeTest(unittest.TestCase):
    def setUp(self):
        self.demo = load_demo()
        root = Path(tempfile.mkdtemp(prefix="nxdemo-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        self.demo.DEMO, self.demo.PIDS, self.demo.INFO = root, root / "pids.json", root / "demo.json"
        self.demo.DOCKER_CLOUD_MARK = root / "cloud.docker"

    def test_down_is_not_blocked_when_docker_cannot_stop_the_cloud(self):
        failures = {"docker not installed": FileNotFoundError("docker"),
                    "daemon not running": subprocess.CalledProcessError(1, ["docker", "compose", "down"])}
        for case, failure in failures.items():
            with self.subTest(case):
                self.demo.DOCKER_CLOUD_MARK.touch()

                with mock.patch.object(self.demo.subprocess, "run", side_effect=failure), mock.patch("builtins.print"):
                    self.demo.down()

                self.assertFalse(self.demo.DOCKER_CLOUD_MARK.exists())     # the next up / down starts clean

    def test_local_cloud_refuses_a_port_another_cloud_already_answers_on(self):
        # e.g. the Docker cloud started by hand: seeding it would silently replace the
        # "SQLite, recreated on every up" cloud the local mode promises.
        self.demo.http = lambda *args, **kwargs: (200, {"ok": True})

        with self.assertRaisesRegex(SystemExit, "NX_DEMO_CLOUD=docker"):
            self.demo.require_free_cloud_port("http://127.0.0.1:8800")

    def test_local_cloud_starts_when_the_port_is_free(self):
        def refused(*args, **kwargs):
            raise urllib.error.URLError("connection refused")
        self.demo.http = refused

        self.demo.require_free_cloud_port("http://127.0.0.1:8800")
