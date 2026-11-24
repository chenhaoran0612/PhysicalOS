"""Issue #26: what really leaves the device when it reports alerts (``alert_report.HttpsTransport``).

The cloud here is ``fake_cloud.FakeCloud``: a real HTTPS server on loopback with a certificate made
for this run. Checked: the report goes to the configured address only, over verified TLS, with the
cloud key and nothing else from the device; a certificate that cannot be verified, a redirect, a
hanging or misbehaving cloud are failures that leave the alerts on the device; and ``serve`` started
with ``--cloud-config`` reports by itself with no console attached.
"""
from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import _support  # noqa: F401
from _support import RUNTIME_DIR, TOKEN, http, readline_within, wait_until

from fake_cloud import OPENSSL_SKIP_REASON, REPORT_PATH, FakeCloud, make_certificates
from neuxmind_runtime import alerts
from neuxmind_runtime.adapters.sim_quadruped import SimQuadruped
from neuxmind_runtime.alert_report import (INTERVAL_S, TOTAL_TIMEOUT_FACTOR, AlertReporter, ConfigError,
                                           HttpsTransport, ReportConfig, SendError, load_config)

from alert_support import FakeClock
from test_iter05_runtime import make_runtime

CLOUD_KEY = "nxk_" + secrets.token_hex(20)
_TMP = None
CERTS: dict = {}


def setUpModule():
    global _TMP, CERTS
    if OPENSSL_SKIP_REASON:
        raise unittest.SkipTest(OPENSSL_SKIP_REASON)
    _TMP = tempfile.TemporaryDirectory()
    CERTS = make_certificates(Path(_TMP.name) / "certs")


def tearDownModule():
    if _TMP is not None:
        _TMP.cleanup()


def report(*items: dict) -> dict:
    return {"schema": "neuxmind.device_report.v1", "device": {"instance_id": "dog-1"}, "sent_at": time.time(),
            "interval_s": INTERVAL_S, "trigger": "interval", "alerts": list(items), "pending": 0}


def alert(alert_id: str = "a-1") -> dict:
    return {"schema": "neuxmind.alert.v1", "id": alert_id, "kind": "disk_low", "level": "warning", "ts": time.time(),
            "device": {"instance_id": "dog-1"}, "reason": "磁盘占用 92%", "advice": "清理日志或扩容", "detail": {}}


class HttpsCase(unittest.TestCase):
    def cloud(self, tls: bool = True) -> FakeCloud:
        cloud = FakeCloud(CERTS if tls else None)
        self.addCleanup(cloud.close)
        return cloud

    def config(self, cloud: FakeCloud, **kw) -> ReportConfig:
        kw.setdefault("ca_file", str(CERTS["ca"]))
        kw.setdefault("timeout_s", 5.0)
        return ReportConfig(url=cloud.url, key=CLOUD_KEY, **kw)


class Transport(HttpsCase):
    def test_the_report_goes_over_https_with_the_cloud_key_to_the_configured_address(self):
        cloud = self.cloud()
        answer = HttpsTransport(self.config(cloud)).send(report(alert("a-1"), alert("a-2")))
        self.assertEqual(answer, {"accepted": ["a-1", "a-2"], "duplicates": [], "rejected": []})
        (request,) = cloud.requests
        self.assertEqual((request["method"], request["path"]), ("POST", REPORT_PATH))
        self.assertEqual(request["headers"]["authorization"], "Bearer " + CLOUD_KEY)
        self.assertTrue(request["headers"]["content-type"].startswith("application/json"))
        self.assertEqual(request["json"]["alerts"][0]["reason"], "磁盘占用 92%")
        self.assertEqual(HttpsTransport(self.config(cloud)).send(report(alert("a-1")))["duplicates"], ["a-1"])

    def test_an_address_with_a_path_keeps_the_path(self):
        cloud = self.cloud()
        config = ReportConfig(url=cloud.url + "/neuxmind/", key=CLOUD_KEY, ca_file=str(CERTS["ca"]))
        HttpsTransport(config).send(report())
        self.assertEqual(cloud.requests[0]["path"], "/neuxmind" + REPORT_PATH)

    def test_a_certificate_that_cannot_be_verified_ends_it_before_anything_is_sent(self):
        cloud = self.cloud()
        with self.assertRaises(SendError) as caught:
            HttpsTransport(self.config(cloud, ca_file=None)).send(report(alert()))
        self.assertNotIn(CLOUD_KEY, str(caught.exception))
        self.assertEqual(cloud.requests, [], "no request, so no key and no alert left the device")

    def test_a_certificate_for_another_name_is_refused(self):
        cloud = self.cloud()                                  # its certificate names 127.0.0.1 only
        port = cloud.server.server_address[1]
        config = ReportConfig(url=f"https://localhost:{port}", key=CLOUD_KEY, ca_file=str(CERTS["ca"]), timeout_s=5.0)
        with self.assertRaises(SendError):
            HttpsTransport(config).send(report(alert()))
        self.assertEqual(cloud.requests, [])

    def test_a_cloud_that_does_not_speak_tls_gets_nothing(self):
        plain = self.cloud(tls=False)
        port = plain.server.server_address[1]
        config = ReportConfig(url=f"https://127.0.0.1:{port}", key=CLOUD_KEY, ca_file=str(CERTS["ca"]), timeout_s=5.0)
        with self.assertRaises(SendError):
            HttpsTransport(config).send(report(alert()))
        self.assertEqual([r for r in plain.requests if r["body"]], [])

    def test_a_redirect_is_not_followed(self):
        cloud, elsewhere = self.cloud(), self.cloud()
        cloud.mode, cloud.redirect_to = "redirect", elsewhere.url
        with self.assertRaises(SendError) as caught:
            HttpsTransport(self.config(cloud)).send(report(alert()))
        self.assertIn("307", str(caught.exception))
        self.assertEqual(elsewhere.requests, [], "the key is never sent to an address that was not configured")

    def test_a_proxy_from_the_environment_is_not_used(self):
        cloud, proxy = self.cloud(), self.cloud(tls=False)
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": proxy.url, "https_proxy": proxy.url, "ALL_PROXY": proxy.url}):
            HttpsTransport(self.config(cloud)).send(report(alert()))
        self.assertEqual(len(cloud.requests), 1)
        self.assertEqual(proxy.requests, [])

    def test_error_answers_are_failures_that_do_not_show_the_key(self):
        cloud = self.cloud()
        for mode, expected in (("error", "500"), ("denied", "401"), ("garbage", "JSON"), ("huge", "过大")):
            cloud.mode = mode
            with self.subTest(mode=mode), self.assertRaises(SendError) as caught:
                HttpsTransport(self.config(cloud)).send(report(alert()))
            self.assertIn(expected, str(caught.exception))
            self.assertNotIn(CLOUD_KEY, str(caught.exception))

    def test_a_cloud_that_does_not_answer_times_out(self):
        cloud = self.cloud()
        cloud.mode = "hang"
        started = time.monotonic()
        with self.assertRaises(SendError):
            HttpsTransport(self.config(cloud, timeout_s=0.5)).send(report(alert()))
        self.assertLess(time.monotonic() - started, 5.0)

    def test_a_cloud_that_drips_its_answer_cannot_hold_the_reporter(self):
        cloud = self.cloud()
        cloud.mode = "trickle"                                # a byte every 0.1 s: the 0.5 s socket timeout never fires
        started = time.monotonic()
        with self.assertRaises(SendError) as caught:
            HttpsTransport(self.config(cloud, timeout_s=0.5)).send(report(alert()))
        took = time.monotonic() - started
        self.assertIn("秒内没有答复完", str(caught.exception))
        self.assertGreaterEqual(took, 0.5 * TOTAL_TIMEOUT_FACTOR - 0.2)
        self.assertLess(took, 10.0)

    def test_a_report_that_cannot_be_encoded_is_a_failure_not_a_crash(self):
        cloud = self.cloud()
        with self.assertRaises(SendError):
            HttpsTransport(self.config(cloud)).send(report(alert(), {"reason": "\ud83d"}))
        with self.assertRaises(SendError):
            HttpsTransport(self.config(cloud)).send(report({"ts": float("nan")}))
        self.assertEqual(cloud.requests, [])

    def test_session_secrets_are_not_written_where_the_environment_says(self):
        cloud = self.cloud()
        with tempfile.TemporaryDirectory() as tmp:
            keylog = Path(tmp) / "keys.log"
            with mock.patch.dict(os.environ, {"SSLKEYLOGFILE": str(keylog)}):
                HttpsTransport(self.config(cloud)).send(report(alert()))
            self.assertFalse(keylog.exists() and keylog.read_bytes(),
                             "with the session secrets anyone could read the key out of a capture")
        self.assertEqual(len(cloud.requests), 1)

    def test_a_ca_file_someone_else_can_change_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            ca = Path(tmp) / "ca.pem"
            ca.write_bytes(Path(CERTS["ca"]).read_bytes())
            for mode in (0o666, 0o664, 0o646):
                os.chmod(ca, mode)
                with self.subTest(mode=oct(mode)), self.assertRaises(ConfigError) as caught:
                    ReportConfig(url="https://cloud.example.com", key=CLOUD_KEY, ca_file=str(ca))
                self.assertIn("ca_file", str(caught.exception))
            os.chmod(ca, 0o644)
            self.assertEqual(ReportConfig(url="https://cloud.example.com", key=CLOUD_KEY, ca_file=str(ca)).ca_file, str(ca))
        with self.assertRaises(ConfigError):
            ReportConfig(url="https://cloud.example.com", key=CLOUD_KEY, ca_file=tmp)

    def test_a_handshake_that_drags_on_is_under_the_deadline_too(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        self.addCleanup(listener.close)
        held = []
        threading.Thread(target=lambda: held.append(listener.accept()), daemon=True).start()   # accepts, says nothing
        self.addCleanup(lambda: [conn.close() for conn, _address in held])
        config = ReportConfig(url=f"https://127.0.0.1:{listener.getsockname()[1]}", key=CLOUD_KEY,
                              ca_file=str(CERTS["ca"]), timeout_s=20.0)
        transport = HttpsTransport(config)
        transport._deadline = 0.5                             # far below the 20 s a single read may take
        started = time.monotonic()
        with self.assertRaises(SendError) as caught:
            transport.send(report(alert()))
        self.assertIn("秒内没有答复完", str(caught.exception))
        self.assertLess(time.monotonic() - started, 10.0)

    def test_host_names_and_ports_that_are_not_ones_are_refused(self):
        for url in ("https://good.example.com\\.evil.example", "https://cloud.example.com:0", "https://exa_mple.com",
                    "https://-bad.example.com", "https://cloud..example.com", "https://[::1%25eth0]:8443",
                    "https://[::1", "https://.", "https://a..b", "https://[not:an:address]", "https://:8443"):
            with self.subTest(url=url), self.assertRaises(ConfigError):
                ReportConfig(url=url, key=CLOUD_KEY)
        for url in ("https://cloud.example.com", "https://cloud.example.com:8443/base", "https://192.0.2.10:8443",
                    "https://[2001:db8::1]:8443", "https://xn--fsq.example"):
            with self.subTest(url=url):
                self.assertEqual(ReportConfig(url=url, key=CLOUD_KEY).url, url)

    def test_nobody_listening_is_a_failure(self):
        cloud = self.cloud()
        config = self.config(cloud, timeout_s=2.0)
        cloud.close()
        with self.assertRaises(SendError):
            HttpsTransport(config).send(report(alert()))


class OverTheWire(HttpsCase):
    """The reporter with the real transport, on a clock the test moves."""

    def setUp(self):
        self.clock, self.wall = FakeClock(), FakeClock(1_800_000_000.0)

    def reporter(self, runtime, cloud, **kw) -> AlertReporter:
        reporter = AlertReporter(runtime, self.config(cloud, **kw), clock=self.clock, wall=self.wall)
        self.addCleanup(reporter.close)
        return reporter

    def step(self, reporter, seconds: float = 1.0, ticks: int = 1) -> None:
        for _ in range(ticks):
            self.clock.advance(seconds)
            self.wall.advance(seconds)
            reporter.tick()

    def test_acceptance_no_console_alert_on_the_device_cloud_has_it_within_five_minutes(self):
        cloud = self.cloud()
        runtime = make_runtime(self, SimQuadruped(time_scale=20.0))      # no gateway: no console can be connected
        reporter = self.reporter(runtime, cloud)
        reporter.tick()
        self.step(reporter, 5.0)
        raised = reporter.raise_alert("disk_low", alerts.WARNING, "磁盘占用 92%", "清理日志或扩容")
        raised_at = self.clock()
        self.step(reporter, 1.0, ticks=int(INTERVAL_S))
        self.assertLessEqual(self.clock() - raised_at, INTERVAL_S)
        (stored,) = cloud.alerts()
        self.assertEqual(stored["id"], raised["id"])
        self.assertEqual({"level", "ts", "device", "reason", "advice"} - set(stored), set())
        self.assertEqual(stored["device"]["instance_id"], runtime.manifest["instance_id"])
        runtime.emergency_stop("操作员停止")
        self.step(reporter, ticks=3)                                     # the highest level does not wait
        self.assertEqual(sorted(a["kind"] for a in cloud.alerts()), ["disk_low", alerts.ESTOP_LATCHED])

    def test_what_the_cloud_missed_arrives_with_the_next_report(self):
        cloud = self.cloud()
        runtime = make_runtime(self, SimQuadruped(time_scale=20.0))
        reporter = self.reporter(runtime, cloud, timeout_s=1.0)
        reporter.tick()
        raised = reporter.raise_alert("disk_low", alerts.WARNING, "磁盘占用 92%", "清理日志或扩容")
        cloud.mode = "error"
        self.step(reporter, INTERVAL_S)
        self.assertEqual(cloud.alerts(), [])
        self.assertEqual(reporter.describe()["unsent"], 1)
        cloud.mode = "lost"                                              # stored, but the answer never arrives
        self.step(reporter, INTERVAL_S)
        self.assertEqual(reporter.describe()["unsent"], 1)
        cloud.mode = "ok"
        self.step(reporter, INTERVAL_S)
        self.assertEqual([a["id"] for a in cloud.alerts()], [raised["id"]])
        self.assertEqual(reporter.describe()["unsent"], 0)
        sent = [a["id"] for r in cloud.reports() for a in r["alerts"]]
        self.assertEqual(sent, [raised["id"]] * 3, "the same alert under the same id until the cloud has it")

    def test_a_cloud_that_cannot_be_reached_never_stops_a_stop(self):
        cloud = self.cloud()
        cloud.mode = "hang"
        runtime = make_runtime(self, SimQuadruped(time_scale=20.0))
        reporter = AlertReporter(runtime, self.config(cloud, timeout_s=3.0), poll_s=0.01)
        self.addCleanup(reporter.close)
        self.addCleanup(cloud.release)
        runtime.alert_report = reporter
        reporter.start()
        wait_until(lambda: len(cloud.requests) >= 1, timeout=10)         # the reporter is stuck in a request
        started = time.monotonic()
        result = runtime.emergency_stop("操作员停止")
        self.assertTrue(result["stopped"] and result["latched"])
        self.assertTrue(runtime.status()["estop_latched"])
        self.assertLess(time.monotonic() - started, 2.0, "the request hangs for three times as long")
        self.assertEqual(len(cloud.requests), 1, "and is still hanging: the stop did not wait for it")


class Serve(HttpsCase):
    """``neuxmind-runtime serve --cloud-config``: the whole thing as it runs on a robot."""

    def serve(self, state: str, *extra: str):
        proc = subprocess.Popen([sys.executable, "-m", "neuxmind_runtime", "serve", "--adapter", "sim_quadruped",
                                 "--port", "0", "--state-dir", state, "--sandbox", "inprocess", *extra],
                                cwd=RUNTIME_DIR, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                env={**os.environ, "BRAIN_EDGE_TOKEN": TOKEN})

        def stop():
            proc.terminate()
            try:
                proc.wait(15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(5)
            proc.stdout.close()
            proc.stderr.close()
        self.addCleanup(stop)
        url = readline_within(proc.stdout, 60).strip().split()[-1]
        return proc, url

    def write_config(self, directory: str, content: dict, mode: int = 0o600) -> str:
        path = Path(directory) / "cloud-report.json"
        path.write_text(json.dumps(content), encoding="utf-8")
        os.chmod(path, mode)
        return str(path)

    def test_serve_reports_by_itself_and_sends_nothing_but_the_report(self):
        cloud = self.cloud()
        with tempfile.TemporaryDirectory() as home:
            config = self.write_config(home, {"url": cloud.url, "key": CLOUD_KEY, "ca_file": str(CERTS["ca"])})
            self.assertEqual(load_config(config).url, cloud.url)
            # Issue #43: the same file also turns the cloud link on; this test is about the reports alone.
            config = self.write_config(home, {"url": cloud.url, "key": CLOUD_KEY, "ca_file": str(CERTS["ca"]),
                                              "link": False})
            _proc, url = self.serve(str(Path(home) / "state"), "--cloud-config", config)
            wait_until(lambda: len(cloud.reports()) >= 1, timeout=30)    # nobody asked: the runtime started it
            first = cloud.reports()[0]
            self.assertEqual((first["trigger"], first["alerts"], first["interval_s"]), ("start", [], INTERVAL_S))
            status, _body = http(url, "/v1/stop", {"reason": "现场触发"})   # an alert raised on the device
            self.assertEqual(status, 200)
            wait_until(lambda: any(a["kind"] == alerts.ESTOP_LATCHED for a in cloud.alerts()), timeout=30)
            (stored,) = cloud.alerts()
            self.assertEqual(stored["level"], alerts.CRITICAL)
            self.assertIn("现场触发", stored["reason"])
            self.assertEqual(stored["device"]["instance_id"], "sim-quadruped-01")
            wait_until(lambda: http(url, "/v1/status")[1]["alert_report"]["unsent"] == 0, timeout=30)
            status, body = http(url, "/v1/status")
            view = body["alert_report"]
            self.assertEqual((view["enabled"], view["url"], view["sent"]), (True, cloud.url, 1))
            for request in cloud.requests:
                everything = json.dumps(request["headers"]) + request["body"].decode()
                self.assertNotIn(TOKEN, everything, "the device's own token never leaves it")
                self.assertEqual(request["path"], REPORT_PATH)
            self.assertNotIn(CLOUD_KEY, json.dumps(body, ensure_ascii=False))

    def test_serve_without_a_cloud_config_reports_nothing_and_says_so(self):
        with tempfile.TemporaryDirectory() as home:
            proc, url = self.serve(str(Path(home) / "state"))
            status, body = http(url, "/v1/status")
            self.assertEqual(status, 200)
            self.assertIs(body["alert_report"]["enabled"], False)
            self.assertIn("未配置", body["alert_report"]["reason"])
            http(url, "/v1/stop", {"reason": "现场触发"})
            wait_until(lambda: http(url, "/v1/status")[1]["alert_report"]["unsent"] == 1, timeout=30)
            self.assertIsNone(proc.poll())

    def test_serve_starts_with_a_config_it_cannot_use(self):
        cloud = self.cloud()
        with tempfile.TemporaryDirectory() as home:
            config = self.write_config(home, {"url": cloud.url, "key": CLOUD_KEY, "ca_file": str(CERTS["ca"])},
                                       mode=0o644)
            proc, url = self.serve(str(Path(home) / "state"), "--cloud-config", config)
            status, body = http(url, "/v1/status")
            self.assertEqual(status, 200)
            self.assertIs(body["alert_report"]["enabled"], False)
            self.assertIn("权限", body["alert_report"]["reason"])
            self.assertNotIn(CLOUD_KEY, json.dumps(body, ensure_ascii=False))
            http(url, "/v1/stop", {"reason": "现场触发"})
            # the alert is noticed and kept on the device (so the reporter ran) and still nothing went out
            wait_until(lambda: http(url, "/v1/status")[1]["alert_report"]["unsent"] == 1, timeout=30)
            self.assertEqual(cloud.requests, [], "nothing is sent with a config that was refused")
            self.assertIsNone(proc.poll())


if __name__ == "__main__":
    unittest.main()
