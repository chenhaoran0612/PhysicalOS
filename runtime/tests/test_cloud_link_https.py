"""Issue #43: what really goes over the wire between the robot and the cloud (``cloud_link.py``).

The cloud here is a real HTTPS server on loopback with a certificate made for this run
(``fake_cloud.make_certificates``); behind it sits ``link_support.FakeCloud``. Checked: the sync
goes to the configured address only, over verified TLS, with the cloud key and nothing else from
the device; a call travels out and its result travels back with the link's own thread and pace; a
key without the scope, an answer that is too large or not JSON, a redirect and a cloud that hangs
all end as "not connected" -- without holding up a stop or the alert reports; and ``serve`` with
``--cloud-config`` connects by itself.
"""
from __future__ import annotations

import json
import os
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import _support  # noqa: F401
from _support import RUNTIME_DIR, TOKEN, example, http, readline_within, wait_until

from fake_cloud import OPENSSL_SKIP_REASON, REPORT_PATH, make_certificates
from link_support import CLOUD_KEY, FakeCloud
from neuxmind_runtime import cloud_link
from neuxmind_runtime.adapters.sim_quadruped import SimQuadruped
from neuxmind_runtime.alert_report import AlertReporter, ReportConfig, SendError
from neuxmind_runtime.cloud_link import SYNC_PATH, CloudLink, LinkTransport

from test_iter05_runtime import make_runtime

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


class HttpsCloud:
    """``mode``: ``ok`` (the honest cloud of ``link_support``) | ``scope`` (403 ``scope_missing``) |
    ``huge`` | ``garbage`` | ``redirect`` | ``hang``. Alert reports are always taken."""

    def __init__(self, tls: bool = True):
        self.logic = FakeCloud()
        self.logic.poll_after_s = 1
        self.requests: list[dict] = []
        self.mode = "ok"
        self.redirect_to = ""
        self._gate = threading.Event()
        self._lock = threading.Lock()
        cloud = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def _answer(self, status: int, body: bytes, headers: dict | None = None) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass
                self.close_connection = True

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                with cloud._lock:
                    cloud.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                                           "body": raw})
                if self.path.endswith(REPORT_PATH):
                    return self._answer(200, b'{"accepted": [], "duplicates": [], "rejected": []}')
                mode = cloud.mode
                if mode == "scope":
                    return self._answer(403, json.dumps({"error": "API Key 没有这个权限范围", "code": "scope_missing"}).encode())
                if mode == "huge":
                    return self._answer(200, b'{"accepted": true, "calls": [], "pad": "' + b"x" * 1_000_000 + b'"}')
                if mode == "garbage":
                    return self._answer(200, b"<html>not json</html>")
                if mode == "redirect":
                    return self._answer(307, b"{}", {"Location": cloud.redirect_to + self.path})
                if mode == "hang":
                    cloud._gate.wait(60)
                    self.close_connection = True
                    return None
                with cloud._lock:
                    answer = cloud.logic.send(json.loads(raw))
                return self._answer(200, json.dumps(answer).encode())

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.server.handle_error = lambda *_args: None
        self.scheme = "http"
        if tls:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(str(CERTS["cert"]), str(CERTS["key"]))
            self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
            self.scheme = "https"
        threading.Thread(target=self.server.serve_forever, name="link-cloud", daemon=True).start()

    @property
    def url(self) -> str:
        return f"{self.scheme}://127.0.0.1:{self.server.server_address[1]}"

    def paths(self) -> list[str]:
        with self._lock:
            return [r["path"] for r in self.requests]

    def close(self) -> None:
        self._gate.set()
        self.server.shutdown()
        self.server.server_close()


class HttpsCase(unittest.TestCase):
    def cloud(self, tls: bool = True) -> HttpsCloud:
        cloud = HttpsCloud(tls)
        self.addCleanup(cloud.close)
        return cloud

    def config(self, cloud: HttpsCloud, **kw) -> ReportConfig:
        kw.setdefault("ca_file", str(CERTS["ca"]))
        kw.setdefault("timeout_s", 5.0)
        return ReportConfig(url=cloud.url, key=CLOUD_KEY, **kw)

    def runtime(self):
        runtime = make_runtime(self, SimQuadruped(time_scale=40.0))
        self.addCleanup(runtime.runner.stop_all, "test teardown")
        return runtime

    def link(self, runtime, cloud: HttpsCloud, **kw) -> CloudLink:
        link = CloudLink(runtime, self.config(cloud), **kw)
        runtime.remote_access.link = link
        self.addCleanup(link.close)
        return link


def allow(runtime, skill_id: str) -> None:
    digest = runtime.remote_access.installed()[skill_id]["digest"]
    runtime.remote_access.set({"skill_id": skill_id, "allowed": True, "set_by": "王工", "hardware_authorized": True,
                               "digest": digest})


REQUEST = {"schema": "neuxmind.device_link.v1", "device": {"instance_id": "dog-1"},
           "state": {"estop_latched": False, "busy": False, "motion_blocked": False},
           "skills": [], "calls": [], "stops": []}


class Transport(HttpsCase):
    def test_the_sync_goes_over_https_with_the_cloud_key_to_the_sync_address(self):
        cloud = self.cloud()
        answer = LinkTransport(self.config(cloud)).send(REQUEST)
        self.assertEqual((answer["accepted"], answer["calls"], answer["stop"]), (True, [], None))
        (request,) = cloud.requests
        self.assertEqual(request["path"], "/api/v1/device-link/sync")
        self.assertEqual(request["headers"]["authorization"], "Bearer " + CLOUD_KEY)
        self.assertTrue(request["headers"]["content-type"].startswith("application/json"))
        self.assertIn("cloud-link", request["headers"]["user-agent"])
        self.assertEqual(json.loads(request["body"]), REQUEST)

    def test_an_address_with_a_path_keeps_the_path(self):
        cloud = self.cloud()
        LinkTransport(ReportConfig(url=cloud.url + "/neuxmind/", key=CLOUD_KEY, ca_file=str(CERTS["ca"]))).send(REQUEST)
        self.assertEqual(cloud.paths(), ["/neuxmind" + SYNC_PATH])

    def test_a_certificate_that_cannot_be_verified_ends_it_before_anything_is_sent(self):
        cloud = self.cloud()
        with self.assertRaises(SendError) as caught:
            LinkTransport(self.config(cloud, ca_file=None)).send(REQUEST)
        self.assertNotIn(CLOUD_KEY, str(caught.exception))
        self.assertEqual(cloud.requests, [])

    def test_a_redirect_is_not_followed_and_a_proxy_is_not_used(self):
        cloud, elsewhere, proxy = self.cloud(), self.cloud(), self.cloud(tls=False)
        cloud.mode, cloud.redirect_to = "redirect", elsewhere.url
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": proxy.url, "https_proxy": proxy.url, "ALL_PROXY": proxy.url}):
            with self.assertRaises(SendError):
                LinkTransport(self.config(cloud)).send(REQUEST)
        self.assertEqual((len(cloud.requests), elsewhere.requests, proxy.requests), (1, [], []))

    def test_answers_that_cannot_be_used_are_failures(self):
        cloud = self.cloud()
        for mode, expected in (("scope", "scope_missing"), ("garbage", "JSON"), ("huge", "过大")):
            cloud.mode = mode
            with self.subTest(mode=mode), self.assertRaises(SendError) as caught:
                LinkTransport(self.config(cloud)).send(REQUEST)
            self.assertIn(expected, str(caught.exception))
            self.assertNotIn(CLOUD_KEY, str(caught.exception))


class OverTheWire(HttpsCase):
    def test_a_call_goes_out_and_its_result_comes_back_on_the_links_own_thread(self):
        cloud, runtime = self.cloud(), self.runtime()
        runtime.install_skill({"request_id": "install-1", "package": example("goto_place")})
        allow(runtime, "goto_place")
        link = self.link(runtime, cloud).start()
        wait_until(lambda: link.describe()["state"] == "online", 15)
        call_id = cloud.logic.call("goto_place", {"place": "二楼"})
        wait_until(lambda: cloud.logic.status(call_id) == "succeeded", 30, interval=0.05)
        self.assertEqual(cloud.logic.states[call_id]["result"]["place"], "二楼")
        wait_until(lambda: json.loads(cloud.requests[-1]["body"])["calls"] == [], 15, interval=0.05)
        self.assertEqual(set(cloud.paths()), {SYNC_PATH})
        sent = b"".join(r["body"] for r in cloud.requests).decode() + json.dumps([r["headers"] for r in cloud.requests])
        self.assertNotIn(str(runtime.state_dir), sent)
        view = runtime.status()["cloud_link"]
        self.assertEqual((view["state"], view["device_id"], view["host"]),
                         ("online", "dlk_test0001", f"127.0.0.1:{cloud.server.server_address[1]}"))
        self.assertNotIn(CLOUD_KEY, json.dumps(runtime.status(), ensure_ascii=False))

    def test_a_key_without_the_scope_is_an_error_and_alert_reports_go_on(self):
        cloud, runtime = self.cloud(), self.runtime()
        cloud.mode = "scope"
        reporter = AlertReporter(runtime, self.config(cloud), poll_s=0.05)
        self.addCleanup(reporter.close)
        runtime.alert_report = reporter
        reporter.start()
        link = self.link(runtime, cloud).start()
        wait_until(lambda: link.describe()["state"] == "error", 15)
        wait_until(lambda: runtime.status()["alert_report"]["reports_sent"] >= 1, 15)
        status = runtime.status()
        self.assertIn("device:link", status["cloud_link"]["last_error"])
        self.assertIsNone(status["alert_report"]["last_error"])
        self.assertEqual(cloud.paths().count(SYNC_PATH), 1, "not asked again for 30 s")
        self.assertFalse(runtime.supervisor.latched)

    def test_answers_that_cannot_be_used_leave_the_link_not_connected(self):
        cloud, runtime = self.cloud(), self.runtime()
        runtime.install_skill({"request_id": "install-1", "package": example("status_report")})
        allow(runtime, "status_report")
        cloud.logic.call("status_report")
        for mode in ("huge", "garbage", "redirect"):
            with self.subTest(mode=mode):
                cloud.mode = mode
                link = self.link(runtime, cloud)
                link.tick()
                self.assertEqual(link.describe()["state"], "error")
                self.assertEqual(runtime.runner.list(), [])

    def test_a_hanging_cloud_holds_up_neither_a_stop_nor_the_runtime(self):
        cloud, runtime = self.cloud(), self.runtime()
        cloud.mode = "hang"
        with mock.patch.object(cloud_link, "TIMEOUT_S", 0.5):
            link = self.link(runtime, cloud)
        link.start()
        wait_until(lambda: len(cloud.requests) >= 1, 10)             # the link is stuck in a request
        started = time.monotonic()
        result = runtime.emergency_stop("操作员停止")
        self.assertTrue(result["stopped"] and result["latched"])
        self.assertIn("cloud_link", runtime.status())
        self.assertLess(time.monotonic() - started, 1.0)
        wait_until(lambda: link.describe()["state"] == "error", 10)   # the request was given up (0.5 s per read)
        self.assertIn("云平台", link.describe()["last_error"])


class Serve(HttpsCase):
    """``neuxmind-runtime serve --cloud-config``: the whole thing as it runs on a robot."""

    def serve(self, state: str, *extra: str):
        proc = subprocess.Popen([sys.executable, "-m", "neuxmind_runtime", "serve", "--adapter", "sim_quadruped",
                                 "--port", "0", "--state-dir", state, "--sandbox", "inprocess", "--time-scale", "40",
                                 *extra],
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

    def write_config(self, directory: str, content: dict) -> str:
        path = Path(directory) / "cloud-report.json"
        path.write_text(json.dumps(content), encoding="utf-8")
        os.chmod(path, 0o600)
        return str(path)

    def test_serve_connects_by_itself_and_a_call_runs_once_the_owner_allows_it(self):
        cloud = self.cloud()
        with tempfile.TemporaryDirectory() as home:
            config = self.write_config(home, {"url": cloud.url, "key": CLOUD_KEY, "ca_file": str(CERTS["ca"])})
            _proc, url = self.serve(str(Path(home) / "state"), "--cloud-config", config)
            wait_until(lambda: http(url, "/v1/status")[1]["cloud_link"]["state"] == "online", 30, interval=0.1)
            self.assertEqual(http(url, "/v2/skills/install", {"request_id": "i-1", "package": example("goto_place")})[0], 200)
            refused = cloud.logic.call("goto_place", {"place": "会议室"})
            wait_until(lambda: cloud.logic.status(refused) == "rejected", 30, interval=0.1)
            self.assertEqual(cloud.logic.states[refused]["error"]["code"], "remote_not_allowed")
            shown = http(url, "/v2/remote")[1]["skills"]["goto_place"]["digest"]
            status, body = http(url, "/v2/remote/skills", {"skill_id": "goto_place", "allowed": True, "set_by": "王工",
                                                           "hardware_authorized": True, "request_id": "allow-1",
                                                           "digest": shown})
            self.assertEqual((status, body["skills"]["goto_place"]["allowed"]), (200, True))
            call_id = cloud.logic.call("goto_place", {"place": "会议室"})
            wait_until(lambda: cloud.logic.status(call_id) == "succeeded", 30, interval=0.1)
            status, body = http(url, "/v2/remote")
            self.assertEqual((status, body["link"]["state"], body["calls"][0]["call_id"], body["calls"][0]["status"]),
                             (200, "online", call_id, "succeeded"))
            for request in cloud.requests:
                everything = json.dumps(request["headers"]) + request["body"].decode()
                self.assertNotIn(TOKEN, everything, "the device's own token never leaves it")
            self.assertNotIn(CLOUD_KEY, json.dumps(http(url, "/v1/status")[1], ensure_ascii=False))

    def test_link_false_sends_alert_reports_only(self):
        cloud = self.cloud()
        with tempfile.TemporaryDirectory() as home:
            config = self.write_config(home, {"url": cloud.url, "key": CLOUD_KEY, "ca_file": str(CERTS["ca"]),
                                              "link": False})
            _proc, url = self.serve(str(Path(home) / "state"), "--cloud-config", config)
            wait_until(lambda: REPORT_PATH in cloud.paths(), 30, interval=0.1)
            view = http(url, "/v1/status")[1]["cloud_link"]
            self.assertEqual((view["configured"], view["enabled"], view["state"]), (True, False, "off"))
            time.sleep(1.0)
            self.assertEqual(set(cloud.paths()), {REPORT_PATH})

    def test_serve_without_a_cloud_config_connects_nowhere(self):
        with tempfile.TemporaryDirectory() as home:
            proc, url = self.serve(str(Path(home) / "state"))
            status, body = http(url, "/v2/remote")
            self.assertEqual((status, body["link"]["configured"], body["link"]["state"]), (200, False, "off"))
            self.assertIn("未配置", http(url, "/v1/status")[1]["cloud_link"]["reason"])
            self.assertIsNone(proc.poll())


if __name__ == "__main__":
    unittest.main()
