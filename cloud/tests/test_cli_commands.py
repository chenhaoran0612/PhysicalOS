"""Management commands as the server operator runs them (rereview N-01 item 3)."""
import contextlib
import io
import os
import re
from unittest import mock

import support  # noqa: F401
from support import PASSWORD, CloudTestCase

from neuxmind_cloud.__main__ import main


class CliCommandsTest(CloudTestCase):
    env = "prod"

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        env = {"DATABASE_URL": self.settings.database_url, "NEUXMIND_ENV": "prod"}
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_create_operator_grant_and_verify(self):
        code, out, _ = self.run_cli("create-operator", "chief@example.com", "--name", "Chief")
        self.assertEqual(code, 0)
        token = re.search(r"(nxv_\S+)", out).group(1)
        op = self.ok(self.post("/auth/password-reset", {"token": token, "password": PASSWORD}))
        self.assertTrue(self.ok(self.get("/me", headers={"Authorization": f"Bearer {op['token']}"}))["platform_operator"])
        self.assertEqual(self.run_cli("create-operator", "chief@example.com")[0], 1)          # exists

        self.register("squat@example.com", verify=False)
        code, _, err = self.run_cli("grant-operator", "squat@example.com")
        self.assertEqual(code, 1)
        self.assertIn("未验证", err)
        code, _, err = self.run_cli("verify-email", "squat@example.com")
        self.assertEqual(code, 1)                                                           # has a session
        self.assertIn("--force", err)
        code, out, _ = self.run_cli("verify-email", "squat@example.com", "--force")
        self.assertEqual(code, 0)
        self.assertIn("revoked 1 sessions", out)
        self.assertRegex(out, r"nxv_\S+")
        code, out, _ = self.run_cli("grant-operator", "squat@example.com")
        self.assertEqual(code, 0)
        self.assertIn("registered publicly", out)
