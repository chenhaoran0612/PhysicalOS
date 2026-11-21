"""``device_install.sh`` and alert reports (issue #26): the installed runtime reports to the cloud only
when the operator put ``cloud-report.json`` into the install directory, and starts whatever that file
looks like. Linux only, like ``test_device_install`` whose device fixture this reuses.
"""
from __future__ import annotations

import json
import os
import secrets
import tempfile
import unittest
from pathlib import Path

from _support import http, wait_until

from fake_cloud import OPENSSL_SKIP_REASON, FakeCloud, make_certificates
from test_device_install import SKIP_REASON, arguments, installed_device

CLOUD_KEY = "nxk_" + secrets.token_hex(20)
REASON = SKIP_REASON or OPENSSL_SKIP_REASON


@unittest.skipIf(REASON, REASON)
class CloudReportFile(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.device = installed_device()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.certs = make_certificates(Path(cls.tmp.name) / "certs")

    @classmethod
    def tearDownClass(cls):
        cls.device.close()
        cls.tmp.cleanup()

    def setUp(self):
        self.cloud = FakeCloud(self.certs)
        self.addCleanup(self.cloud.close)
        self.file = self.device.root / "cloud-report.json"

    def tearDown(self):
        self.device.run("stop")
        self.file.unlink(missing_ok=True)

    def write(self, path: Path, mode: int = 0o600) -> None:
        path.write_text(json.dumps({"url": self.cloud.url, "key": CLOUD_KEY, "ca_file": str(self.certs["ca"])}),
                        encoding="utf-8")
        os.chmod(path, mode)

    def report_view(self) -> dict:
        status, body = http(self.device.url, "/v1/status", token=self.device.token)
        self.assertEqual(status, 200)
        return body["alert_report"]

    def test_without_the_file_nothing_is_reported(self):
        started = self.device.start()

        view = self.report_view()
        summary = self.device.run("status")

        self.assertNotIn("alert reports", started.stdout)
        self.assertIs(view["enabled"], False)
        self.assertIn("未配置", view["reason"])
        self.assertIs(json.loads(summary.stdout.splitlines()[-1])["alert_report"]["enabled"], False)
        self.assertEqual(self.cloud.requests, [])

    def test_with_the_file_the_runtime_reports_to_the_cloud_named_in_it(self):
        self.write(self.file)

        started = self.device.start()
        wait_until(lambda: len(self.cloud.reports()) >= 1, timeout=30)
        summary = self.device.run("status")

        self.assertIn("alert reports", started.stdout)
        self.assertEqual(self.cloud.requests[0]["headers"]["authorization"], "Bearer " + CLOUD_KEY)
        self.assertEqual(json.loads(summary.stdout.splitlines()[-1])["alert_report"]["url"], self.cloud.url)
        everything = (started.stdout + started.stderr + summary.stdout + summary.stderr
                      + " ".join(arguments(self.device.pid())) + (self.device.root / "runtime.log").read_text())
        self.assertNotIn(CLOUD_KEY, everything)
        self.assertNotIn(self.device.token, json.dumps([r["headers"] for r in self.cloud.requests])
                         + b"".join(r["body"] for r in self.cloud.requests).decode())

    def test_a_file_others_can_read_is_not_used_and_the_runtime_still_starts(self):
        self.write(self.file, mode=0o644)

        self.device.start()
        view = self.report_view()
        http(self.device.url, "/v1/stop", {"reason": "现场触发"}, token=self.device.token)
        wait_until(lambda: self.report_view()["unsent"] == 1, timeout=30)

        self.assertIs(view["enabled"], False)
        self.assertIn("权限", view["reason"])
        self.assertEqual(self.cloud.requests, [])

    def test_a_link_in_place_of_the_file_is_noticed_and_refused(self):
        for target in (self.device.root / "nowhere.json", Path(self.tmp.name) / "elsewhere.json"):
            self.write(Path(self.tmp.name) / "elsewhere.json")
            self.file.unlink(missing_ok=True)
            self.file.symlink_to(target)                      # dangling, then pointing at a usable file elsewhere

            started = self.device.start()
            view = self.report_view()
            self.device.run("stop")

            self.assertIn("alert reports", started.stdout, "the operator is told the file was seen")
            self.assertIs(view["enabled"], False)
            self.assertIn("上报配置", view["reason"])
        self.assertEqual(self.cloud.requests, [])

    def test_a_file_named_in_the_callers_environment_is_ignored(self):
        elsewhere = Path(self.tmp.name) / "elsewhere.json"
        self.write(elsewhere)

        self.device.start(NEUXMIND_CLOUD_CONFIG=str(elsewhere))

        self.assertIs(self.report_view()["enabled"], False)
        self.assertEqual(self.cloud.requests, [])


if __name__ == "__main__":
    unittest.main()
