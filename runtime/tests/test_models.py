"""§5.1: runtime-native model.install, /v2/models, ctx.models (read-only in the sandbox).

Model packages come from a local HTTP server (plain http is allowed for
127.0.0.1 by configuration only) and from a configured file:// root.
"""
import hashlib
import json
import os
import shutil
import stat
import tempfile
import threading
import time
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import _support
from _support import RuntimeFixture, custom_package, http, wait_until

from neuxmind_runtime.adapters.sim_quadruped import SimQuadruped
from neuxmind_runtime.core import EdgeRuntime
from neuxmind_runtime.gateway import GatewayServer
from neuxmind_runtime.models import ModelConfig, ModelStore

PAYLOAD = b"ONNX-test-model-" + bytes(range(256)) * 300          # ~77 KB
SHA = hashlib.sha256(PAYLOAD).hexdigest()
OTHER = b"another model" * 1000
OTHER_SHA = hashlib.sha256(OTHER).hexdigest()


class PackageServer:
    """Local package mirror with misbehaving routes."""

    def __init__(self, file_target: str):
        self.slow_started = threading.Event()
        self.release_slow = threading.Event()
        self.requests: dict[str, int] = {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _body(self, data: bytes, length: bool = True):
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                if length:
                    self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                path = self.path.split("?")[0]
                outer.requests[path] = outer.requests.get(path, 0) + 1
                if path == "/pkg/model.onnx":
                    return self._body(PAYLOAD)
                if path == "/pkg/other.onnx":
                    return self._body(OTHER)
                if path == "/nolength":          # streamed, no Content-Length, connection close
                    self.close_connection = True
                    return self._body(PAYLOAD, length=False)
                if path == "/big":               # announced too large
                    self.send_response(200)
                    self.send_header("Content-Length", str(10 ** 9))
                    self.end_headers()
                    self.wfile.write(b"x" * 1024)
                    return
                if path == "/bigstream":         # no length, grows beyond the cap
                    self.close_connection = True
                    self.send_response(200)
                    self.end_headers()
                    try:
                        for _ in range(64):
                            self.wfile.write(b"y" * 65536)
                    except OSError:
                        pass
                    return
                if path == "/to-file":
                    self.send_response(302)
                    self.send_header("Location", "file://" + file_target)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if path == "/to-https":
                    self.send_response(302)
                    self.send_header("Location", "https://127.0.0.1:1/pkg/model.onnx")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if path == "/to-same":
                    self.send_response(302)
                    self.send_header("Location", "/pkg/model.onnx?sig=secret")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if path == "/short":             # announces more than it sends, then closes
                    self.close_connection = True
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(PAYLOAD)))
                    self.end_headers()
                    self.wfile.write(PAYLOAD[:1000])
                    return
                if path == "/drip":              # the whole payload in ~1 s
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(PAYLOAD)))
                    self.end_headers()
                    step = len(PAYLOAD) // 20 + 1
                    for i in range(0, len(PAYLOAD), step):
                        self.wfile.write(PAYLOAD[i:i + step])
                        self.wfile.flush()
                        time.sleep(0.05)
                    return
                if path.startswith("/slow"):
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(PAYLOAD)))
                    self.end_headers()
                    outer.slow_started.set()
                    try:
                        for i in range(0, 4096, 1024):           # a little, then the mirror stalls
                            self.wfile.write(PAYLOAD[i:i + 1024])
                            self.wfile.flush()
                            time.sleep(0.05)
                        outer.release_slow.wait(30)
                    except OSError:
                        pass
                    return
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.release_slow.set()
        self.httpd.shutdown()
        self.httpd.server_close()


def public_dir(prefix: str) -> str:
    """A directory the (privilege-dropped) skill user can traverse (whole path, see _support)."""
    return _support.public_tempdir(prefix)


class ModelFixture(unittest.TestCase):
    def setUp(self):
        self.models_dir = Path(public_dir("nx-models-")) / "models"
        self.file_root = Path(tempfile.mkdtemp(prefix="nx-pkgs-"))
        (self.file_root / "model.onnx").write_bytes(PAYLOAD)
        self.outside = Path(tempfile.mkdtemp(prefix="nx-outside-"))
        (self.outside / "model.onnx").write_bytes(PAYLOAD)
        self.server = PackageServer(str(self.file_root / "model.onnx"))
        self.config = ModelConfig(models_dir=str(self.models_dir), runtimes=("onnxruntime", "tensorrt"),
                                  max_bytes=1 << 20, timeout_s=5.0, file_roots=(str(self.file_root),),
                                  allow_http_hosts=("127.0.0.1",))
        self.fx = self.make_fixture()

    def make_fixture(self, state_dir=None, **kw) -> RuntimeFixture:
        fx = RuntimeFixture.__new__(RuntimeFixture)
        fx._tmp = None
        if state_dir is None:
            fx._tmp = tempfile.TemporaryDirectory()
            state_dir = fx._tmp.name
        fx.state_dir = state_dir
        fx.adapter = kw.pop("adapter", None) or SimQuadruped(time_scale=20.0)
        sandbox = kw.pop("sandbox", "process")
        if sandbox == "process" and _support.SANDBOX_SKIP_REASON:
            raise unittest.SkipTest(_support.SANDBOX_SKIP_REASON)
        if sandbox == "process" and _support.SANDBOX_PYTHON:
            from neuxmind_runtime.sandbox import SandboxConfig
            sandbox = SandboxConfig(python=_support.SANDBOX_PYTHON)
        fx.runtime = EdgeRuntime(fx.adapter, state_dir, sandbox=sandbox, models=kw.pop("models", self.config))
        fx.server = GatewayServer(fx.runtime, _support.TOKEN, port=0).start()
        return fx

    def tearDown(self):
        self.fx.close()
        self.server.close()
        for d in (self.models_dir.parent, self.file_root, self.outside):
            shutil.rmtree(d, ignore_errors=True)
            # read-only model trees
            if d.exists():
                for root, dirs, _files in os.walk(d):
                    os.chmod(root, 0o700)
                shutil.rmtree(d, ignore_errors=True)

    # helpers
    def install(self, ref: str, sha: str = SHA, runtime: str = "onnxruntime", authorized=True, rid=None):
        return http(self.fx.url, "/v2/capabilities/model.install/invoke", {
            "request_id": rid or f"mi-{time.monotonic_ns()}", "hardware_authorized": authorized,
            "args": {"package_ref": ref, "sha256": sha, "runtime": runtime}})

    def install_and_wait(self, ref: str, sha: str = SHA, timeout=15, **kw) -> dict:
        status, body = self.install(ref, sha, **kw)
        self.assertEqual(status, 200, body)
        return self.fx.wait(body["run_id"], timeout)

    def partial_is_empty(self) -> bool:
        return list((self.models_dir / ".partial").iterdir()) == []


class InstallTests(ModelFixture):
    def test_capability_is_native_and_visible(self):
        manifest = http(self.fx.url, "/v2/manifest")[1]
        cap = next(c for c in manifest["capabilities"] if c["id"] == "model.install")
        self.assertEqual(cap["params"]["runtimes"], ["onnxruntime", "tensorrt"])
        self.assertTrue(manifest["simulated"])                     # works on the simulated body too
        status = http(self.fx.url, "/v1/status")[1]
        self.assertTrue(status["models"]["enabled"])
        self.assertEqual(status["models"]["installed"], 0)
        # Read-only at rest from the start, not only after the first install.
        self.assertEqual(stat.S_IMODE(os.stat(self.models_dir).st_mode), 0o555)
        # The adapter's own manifest is unchanged: no false "manifest changed" stop.
        self.assertIsNone(self.fx.runtime.bus.check_manifest_drift())
        self.assertIsNone(self.fx.runtime.supervisor.manifest_drift)

    def test_install_verify_atomic_layout(self):
        record = self.install_and_wait(self.server.url + "/pkg/model.onnx")
        self.assertEqual(record["status"], "succeeded", record["error"])
        self.assertEqual(record["result"], {"installed": True, "model_id": SHA})
        target = self.models_dir / SHA
        self.assertEqual((target / "model.onnx").read_bytes(), PAYLOAD)
        meta = json.loads((target / "model.json").read_text())
        self.assertEqual((meta["sha256"], meta["model_id"], meta["runtime"], meta["size"], meta["file"]),
                         (SHA, None, "onnxruntime", len(PAYLOAD), "model.onnx"))
        self.assertIn("installed_at", meta)
        self.assertEqual(meta["source"], self.server.url + "/pkg/model.onnx")
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o555)
        self.assertEqual(stat.S_IMODE(os.stat(self.models_dir).st_mode), 0o555)   # read-only at rest
        self.assertEqual(stat.S_IMODE(os.stat(target / "model.onnx").st_mode), 0o444)
        self.assertTrue(self.partial_is_empty())
        listed = http(self.fx.url, "/v2/models")[1]["models"]
        self.assertEqual([(m["sha256"], m["model_id"], m["name"]) for m in listed], [(SHA, None, None)])
        events = [e["type"] for e in self.fx.runtime.events.since(0, 10 ** 6)]
        self.assertIn("model.installed", events)

    def test_same_sha_is_idempotent(self):
        first = self.install_and_wait(self.server.url + "/pkg/model.onnx")
        self.assertEqual(first["status"], "succeeded", first["error"])
        before = os.stat(self.models_dir / SHA / "model.json").st_mtime_ns
        again = self.install_and_wait(self.server.url + "/nolength")           # other URL, same content
        self.assertEqual((again["status"], again["result"]), ("succeeded", {"installed": True, "model_id": SHA}))
        self.assertEqual(os.stat(self.models_dir / SHA / "model.json").st_mtime_ns, before)
        self.assertEqual(len(http(self.fx.url, "/v2/models")[1]["models"]), 1)
        # request_id replay: same run, no second install
        status, a = self.install(self.server.url + "/pkg/model.onnx", rid="same")
        status2, b = self.install(self.server.url + "/pkg/model.onnx", rid="same")
        self.assertEqual((status, status2, a["run_id"]), (200, 200, b["run_id"]))

    def test_concurrent_installs_of_one_sha(self):
        runs = [self.install(self.server.url + "/pkg/model.onnx")[1]["run_id"] for _ in range(4)]
        for run_id in runs:
            record = self.fx.wait(run_id, 15)
            self.assertEqual((record["status"], record["result"]["model_id"]), ("succeeded", SHA), record)
        self.assertEqual(sorted(p.name for p in self.models_dir.iterdir()), [".partial", SHA])
        self.assertTrue(wait_until(self.partial_is_empty, 3))

    def test_no_content_length_stream(self):
        record = self.install_and_wait(self.server.url + "/nolength")
        self.assertEqual(record["status"], "succeeded", record["error"])

    def test_wrong_sha_is_deleted_and_fails(self):
        wrong = "0" * 64
        record = self.install_and_wait(self.server.url + "/pkg/model.onnx", sha=wrong)
        self.assertEqual(record["status"], "failed")
        self.assertIn("SHA-256", record["error"])
        self.assertFalse((self.models_dir / wrong).exists())
        self.assertFalse((self.models_dir / SHA).exists())
        self.assertTrue(self.partial_is_empty())
        self.assertEqual(http(self.fx.url, "/v2/models")[1]["models"], [])

    def test_oversized_package_is_refused(self):
        for path in ("/big", "/bigstream"):
            with self.subTest(path=path):
                record = self.install_and_wait(self.server.url + path, sha="1" * 64)
                self.assertEqual(record["status"], "failed")
                self.assertIn("上限", record["error"])
                self.assertTrue(self.partial_is_empty())
        self.assertEqual(http(self.fx.url, "/v2/models")[1]["models"], [])

    def test_redirects(self):
        # Across schemes: http -> file:// and http -> https are both refused.
        for path in ("/to-file", "/to-https"):
            with self.subTest(path=path):
                record = self.install_and_wait(self.server.url + path)
                self.assertEqual(record["status"], "failed")
                self.assertIn("跨协议重定向", record["error"])
                self.assertFalse((self.models_dir / SHA).exists())
        # Same scheme is followed; the signed query string is never stored.
        record = self.install_and_wait(self.server.url + "/to-same?token=signed-secret")
        self.assertEqual(record["status"], "succeeded", record["error"])
        meta = (self.models_dir / SHA / "model.json").read_text()
        self.assertNotIn("secret", meta)
        self.assertEqual(json.loads(meta)["source"], self.server.url + "/to-same")

    def test_synchronous_refusals(self):
        url = self.server.url + "/pkg/model.onnx"
        self.assertEqual(self.install(url, authorized=False)[0], 403)
        self.assertEqual(self.install(url, sha="xyz")[0], 400)
        self.assertEqual(self.install(url, runtime="tflite")[1].get("code"), "runtime_unsupported")
        for ref in ("ftp://127.0.0.1/model.onnx", "http://example.com/model.onnx", "file:///etc/passwd",
                    "file://" + str(self.outside / "model.onnx"), "data:,x", "/plain/path"):
            with self.subTest(ref=ref):
                status, body = self.install(ref)
                self.assertEqual((status, body.get("code")), (400, "package_ref_refused"), body)
        # Unknown fields are refused by the spec (inputs are package_ref, sha256, runtime).
        status, body = http(self.fx.url, "/v2/capabilities/model.install/invoke", {
            "request_id": "extra", "hardware_authorized": True,
            "args": {"package_ref": url, "sha256": SHA, "runtime": "onnxruntime", "evil": 1}})
        self.assertEqual(status, 400, body)
        self.assertEqual(http(self.fx.url, "/v2/models")[1]["models"], [])

    def test_file_root(self):
        record = self.install_and_wait("file://" + str(self.file_root / "model.onnx"))
        self.assertEqual(record["status"], "succeeded", record["error"])
        self.assertEqual((self.models_dir / SHA / "model.onnx").read_bytes(), PAYLOAD)
        # A symlink inside the root that points outside is judged by its real path.
        (self.file_root / "escape.onnx").symlink_to(self.outside / "model.onnx")
        status, body = self.install("file://" + str(self.file_root / "escape.onnx"))
        self.assertEqual((status, body.get("code")), (400, "package_ref_refused"))

    def test_install_while_latched_and_beside_motion(self):
        """safety: system -- never motion: allowed while latched; motion keeps working meanwhile."""
        self.fx.runtime.emergency_stop("test")
        record = self.install_and_wait(self.server.url + "/pkg/model.onnx")
        self.assertEqual(record["status"], "succeeded", record["error"])
        self.assertTrue(self.fx.runtime.supervisor.latched)
        self.fx.runtime.reset({"hardware_authorized": True})
        status, body = self.install(self.server.url + "/slow", sha=SHA[:-1] + ("0" if SHA[-1] != "0" else "1"))
        self.assertEqual(status, 200, body)
        self.assertTrue(self.server.slow_started.wait(5))
        status, out = http(self.fx.url, "/v2/capabilities/base.velocity/invoke", {
            "request_id": "v1", "hardware_authorized": True, "args": {"vx": 0.2, "wz": 0.0}})
        self.assertEqual(status, 200, out)
        self.assertGreater(self.fx.adapter.cmd[0], 0)
        self.assertIsNone(self.fx.runtime.runner.motion_owner())      # the install is not a motion run
        self.fx.runtime.runner.cancel(body["run_id"])

    def test_stop_during_slow_download(self):
        status, body = self.install(self.server.url + "/slow")
        self.assertEqual(status, 200, body)
        self.assertTrue(self.server.slow_started.wait(5))
        time.sleep(0.3)
        started = time.monotonic()
        status, out = http(self.fx.url, "/v2/stop", {"reason": "slow download"})
        elapsed = time.monotonic() - started
        self.assertEqual((status, out["latched"]), (200, True))
        self.assertLess(elapsed, 1.0)
        record = self.fx.wait(body["run_id"], 3)
        self.assertEqual(record["status"], "stopped", record)
        self.assertFalse((self.models_dir / SHA).exists())
        self.assertTrue(wait_until(self.partial_is_empty, 3))
        failed = [e for e in self.fx.runtime.events.since(0, 10 ** 6) if e["type"] == "model.install_failed"]
        self.assertTrue(failed and failed[-1]["cancelled"])

    def test_run_progress_follows_the_download(self):
        """iter03 leftover (SDK worked around it): /v2/runs/{id}.progress stayed 0 until the end.
        It now follows the download (bytes/Content-Length, < 1 until installed) and
        progress_detail.feedback carries bytes/total."""
        status, body = self.install(self.server.url + "/slow")
        self.assertEqual(status, 200, body)
        self.assertTrue(self.server.slow_started.wait(5))

        def partial():
            record = http(self.fx.url, "/v2/runs/" + body["run_id"])[1]
            return record if record["progress_detail"].get("feedback", {}).get("bytes") == 4096 else None
        record = wait_until(partial, 5)
        self.assertEqual(record["status"], "running")
        self.assertAlmostEqual(record["progress"], 4096 / len(PAYLOAD), places=4)
        self.assertEqual(record["progress_detail"]["feedback"]["total"], len(PAYLOAD))
        self.assertEqual(record["progress_detail"]["capability"], "model.install")
        self.server.release_slow.set()            # the mirror then closes early: the run fails, never 1.0
        done = self.fx.wait(body["run_id"], 10)
        self.assertEqual(done["status"], "failed", done)
        self.assertLess(done["progress"], 1.0)
        done = self.install_and_wait(self.server.url + "/drip")
        self.assertEqual((done["status"], done["progress"]), ("succeeded", 1.0))

    def test_stop_path_never_waits_for_an_install(self):
        """cancel() is only an Event + socket shutdown: a stuck worker cannot delay stop."""
        store = self.fx.runtime.models
        gate = threading.Event()
        original = store._fetch

        def stuck_fetch(ref, tmp, handle):
            gate.wait(10)                        # e.g. a TCP connect that never completes
            return original(ref, tmp, handle)
        store._fetch = stuck_fetch
        try:
            status, body = self.install(self.server.url + "/pkg/model.onnx")
            self.assertEqual(status, 200)
            time.sleep(0.2)
            started = time.monotonic()
            self.fx.runtime.emergency_stop("stuck install")
            self.assertLess(time.monotonic() - started, 0.2)
            record = self.fx.wait(body["run_id"], 4)
            self.assertEqual(record["status"], "stopped")
        finally:
            gate.set()
        time.sleep(0.5)
        self.assertFalse((self.models_dir / SHA).exists(), "被停止的安装仍然发布了模型")
        self.assertTrue(wait_until(self.partial_is_empty, 3))

    def test_restart_keeps_models_and_sweeps_partials(self):
        self.assertEqual(self.install_and_wait(self.server.url + "/pkg/model.onnx")["status"], "succeeded")
        state = self.fx.state_dir
        tmp = self.fx._tmp
        self.fx._tmp = None
        self.fx.close()
        (self.models_dir / ".partial" / "leftover.part").write_bytes(b"half")
        os.chmod(self.models_dir, 0o755)                      # read-only at rest; plant a stray entry
        (self.models_dir / "not-a-model").mkdir()
        self.fx = self.make_fixture(state_dir=state)
        self.fx._tmp = tmp
        self.assertEqual([m["sha256"] for m in http(self.fx.url, "/v2/models")[1]["models"]], [SHA])
        self.assertTrue(self.partial_is_empty())
        self.assertIn("not-a-model", http(self.fx.url, "/v1/status")[1]["models"]["invalid_entries"])

    def test_v1_install_compat(self):
        status, body = http(self.fx.url, "/v1/models/install", {
            "request_id": "v1-mi", "hardware_authorized": True, "model_id": "vision-small", "name": "视觉小模型",
            "package_ref": self.server.url + "/pkg/model.onnx", "sha256": SHA.upper(), "runtime": "onnxruntime"})
        self.assertEqual(status, 200, body)
        self.assertEqual((body["accepted"], body["model_id"], body["sha256"], body["catalog_model_id"]),
                         (True, SHA, SHA, "vision-small"))
        record = self.fx.wait(body["run_id"], 15)
        self.assertEqual(record["status"], "succeeded", record["error"])
        listed = http(self.fx.url, "/v2/models")[1]["models"][0]
        self.assertEqual((listed["model_id"], listed["name"], listed["sha256"]), ("vision-small", "视觉小模型", SHA))
        self.assertEqual(http(self.fx.url, "/v1/models/install", {
            "request_id": "v1-mi2", "package_ref": self.server.url + "/pkg/model.onnx", "sha256": SHA,
            "runtime": "onnxruntime"})[0], 403)

    def test_v2_install_records_catalog_id_and_name(self):
        """SDK finding: optional spec inputs model_id/name were dropped on the v2 path."""
        status, body = http(self.fx.url, "/v2/capabilities/model.install/invoke", {
            "request_id": "v2-meta", "hardware_authorized": True,
            "args": {"package_ref": self.server.url + "/pkg/model.onnx", "sha256": SHA, "runtime": "onnxruntime",
                     "model_id": "det-yolo-n", "name": "检测 YOLO-n"}})
        self.assertEqual(status, 200, body)
        record = self.fx.wait(body["run_id"], 15)
        self.assertEqual((record["status"], record["result"]), ("succeeded", {"installed": True, "model_id": SHA}))
        listed = http(self.fx.url, "/v2/models")[1]["models"]
        self.assertEqual([(m["model_id"], m["name"], m["sha256"]) for m in listed], [("det-yolo-n", "检测 YOLO-n", SHA)])
        meta = json.loads((self.models_dir / SHA / "model.json").read_text())
        self.assertEqual((meta["model_id"], meta["name"]), ("det-yolo-n", "检测 YOLO-n"))
        bad = http(self.fx.url, "/v2/capabilities/model.install/invoke", {
            "request_id": "v2-bad", "hardware_authorized": True,
            "args": {"package_ref": self.server.url + "/pkg/model.onnx", "sha256": SHA, "runtime": "onnxruntime",
                     "model_id": "x" * 201}})
        self.assertEqual(bad[0], 400, bad)
        # Delete by catalog id (unique) works too.
        status, body = http(self.fx.url, "/v2/models/det-yolo-n", {"hardware_authorized": True}, method="DELETE")
        self.assertEqual((status, body), (200, {"deleted": True, "model_id": SHA}))

    def test_install_events_carry_run_id(self):
        """SDK finding: /v2/runs/{id}/events must replay download progress and the failure reason."""
        ok = self.install_and_wait(self.server.url + "/pkg/model.onnx")
        self.assertEqual(ok["status"], "succeeded", ok["error"])
        bad = self.install_and_wait(self.server.url + "/pkg/model.onnx", sha="0" * 64)
        self.assertEqual(bad["status"], "failed")
        for record, expected in ((ok, {"model.install_started", "model.installed"}),
                                 (bad, {"model.install_started", "model.install_failed"})):
            events = http(self.fx.url, f"/v2/runs/{record['run_id']}/events")[1]["events"]
            types = {e["type"] for e in events}
            self.assertTrue(expected <= types, types)
            for e in events:
                if e["type"].startswith("model."):
                    self.assertEqual(e["run_id"], record["run_id"])
        failed = [e for e in http(self.fx.url, f"/v2/runs/{bad['run_id']}/events")[1]["events"]
                  if e["type"] == "model.install_failed"][0]
        self.assertIn("SHA-256", failed["error"])
        self.assertEqual(failed["sha256"], "0" * 64)
        # Download progress too (interval 1 s by default; 0 here so every chunk reports).
        object.__setattr__(self.fx.runtime.models.config, "progress_interval_s", 0.0)   # frozen dataclass
        record = self.install_and_wait(self.server.url + "/pkg/other.onnx", sha=OTHER_SHA)
        self.assertEqual(record["status"], "succeeded", record["error"])
        events = http(self.fx.url, f"/v2/runs/{record['run_id']}/events")[1]["events"]
        progress = [e for e in events if e["type"] == "model.install_progress"]
        self.assertTrue(progress, [e["type"] for e in events])
        self.assertTrue(all(e["run_id"] == record["run_id"] and e["sha256"] == OTHER_SHA for e in progress))

    def test_manifest_model_runtimes_match_the_capability(self):
        for view in ("/v2/manifest", "/v1/manifest"):
            manifest = http(self.fx.url, view)[1]
            self.assertEqual(manifest["model_runtimes"], ["onnxruntime", "tensorrt"], view)
        cap = next(c for c in http(self.fx.url, "/v2/manifest")[1]["capabilities"] if c["id"] == "model.install")
        self.assertEqual(cap["params"]["runtimes"], ["onnxruntime", "tensorrt"])
        self.assertIsNone(self.fx.runtime.bus.check_manifest_drift())

    def test_delete(self):
        self.assertEqual(self.install_and_wait(self.server.url + "/pkg/model.onnx")["status"], "succeeded")
        status, body = http(self.fx.url, f"/v2/models/{SHA}", {}, method="DELETE")
        self.assertEqual((status, body.get("code")), (403, "hardware_not_authorized"))
        status, body = http(self.fx.url, "/v2/models/" + "a" * 64, {"hardware_authorized": True}, method="DELETE")
        self.assertEqual((status, body.get("code")), (404, "model_not_found"))
        status, body = http(self.fx.url, f"/v2/models/{SHA}", {"hardware_authorized": True, "request_id": "d1"},
                            method="DELETE")
        self.assertEqual((status, body), (200, {"deleted": True, "model_id": SHA}))
        self.assertFalse((self.models_dir / SHA).exists())
        self.assertEqual(http(self.fx.url, "/v2/models")[1]["models"], [])
        self.assertTrue(wait_until(self.partial_is_empty, 3))

    def test_adapter_implementation_wins(self):
        class Vendor(SimQuadruped):
            def manifest(self):
                m = super().manifest()
                m["capabilities"].append({"id": "model.install", "params": {"runtimes": ["vendor-rt"]}})
                return m

            def install_model(self, request):
                return {"accepted": True, "vendor": True}
        fx = self.make_fixture(adapter=Vendor(time_scale=20.0))
        try:
            self.assertIsNone(fx.runtime.models)
            cap = next(c for c in fx.runtime.manifest["capabilities"] if c["id"] == "model.install")
            self.assertEqual(cap["params"]["runtimes"], ["vendor-rt"])
            self.assertEqual(http(fx.url, "/v1/models/install", {"request_id": "x"})[1], {"accepted": True,
                                                                                           "vendor": True})
            self.assertEqual(http(fx.url, "/v1/status")[1]["models"], {"enabled": True, "provider": "adapter"})
        finally:
            fx.close()

    def test_disabled_without_models_dir(self):
        fx = RuntimeFixture("quadruped", http=True)
        try:
            self.assertNotIn("model.install", fx.runtime.bus.capability_ids)
            self.assertEqual(http(fx.url, "/v2/models")[0], 404)
            self.assertEqual(http(fx.url, "/v1/models/install", {"request_id": "x"})[1]["code"],
                             "capability_not_provided")
        finally:
            fx.close()


MODEL_READER = '''
import hashlib, os
def run(ctx, sha):
    out = {"ids": sorted(ctx.models)}
    info = ctx.models[sha]
    out["name"], out["runtime"] = info["name"], info["runtime"]
    with open(info["path"], "rb") as fh:
        out["digest"] = hashlib.sha256(fh.read()).hexdigest()
    attempts = {}
    for name, action in (("write", lambda: open(info["path"], "ab")),
                         ("unlink", lambda: os.unlink(info["path"])),
                         ("create", lambda: open(os.path.join(os.path.dirname(info["path"]), "evil"), "w")),
                         ("rename", lambda: os.rename(os.path.dirname(info["path"]),
                                                      os.path.dirname(info["path"]) + "-x"))):
        try:
            action()
            attempts[name] = "allowed"
        except OSError as exc:
            attempts[name] = type(exc).__name__
    out["attempts"] = attempts
    ctx.models[sha]["path"] = "/tampered"          # a copy: never changes what the runtime holds
    out["after"] = ctx.models[sha]["path"]
    return out
'''

HOLDS_MODEL = '''
def run(ctx):
    while True:
        ctx.sleep(0.05)
'''


class ReviewFixTests(ModelFixture):
    """Iteration-03 review: Q3-06, Q3-09..Q3-13, R3."""

    def test_same_sha_in_flight_is_merged_into_one_run(self):
        first = self.install(self.server.url + "/slow")[1]
        self.assertTrue(self.server.slow_started.wait(5))
        others = [self.install(self.server.url + "/slow")[1] for _ in range(3)]
        self.assertTrue(all(o["run_id"] == first["run_id"] and o.get("merged") for o in others), others)
        self.assertEqual(self.server.requests["/slow"], 1)               # one download, not four
        # A skill (ctx.call path) installing the same sha follows the same download, too.
        handle = self.fx.runtime.models.start_install({"package_ref": self.server.url + "/slow", "sha256": SHA,
                                                       "runtime": "onnxruntime"}, run_id="run-other")
        self.assertEqual(type(handle).__name__, "_FollowerHandle")
        self.assertEqual(self.server.requests["/slow"], 1)
        self.fx.runtime.runner.cancel(first["run_id"])
        self.fx.wait(first["run_id"], 5)
        self.assertTrue(wait_until(lambda: handle.poll().done, 5))
        self.assertTrue(wait_until(lambda: http(self.fx.url, "/v1/status")[1]["models"]["claims"] == 0, 5))

    def test_global_concurrency_limit(self):
        shas = [hashlib.sha256(bytes([i])).hexdigest() for i in range(3)]
        runs = []
        for sha in shas[:2]:
            status, body = self.install(self.server.url + f"/slow-{sha[:4]}", sha=sha)
            self.assertEqual(status, 200, body)
            runs.append(body["run_id"])
        status, body = self.install(self.server.url + "/slow-x", sha=shas[2])
        self.assertEqual((status, body.get("code")), (409, "install_busy"), body)
        self.fx.runtime.runner.cancel(runs[0])
        self.fx.wait(runs[0], 5)
        self.assertTrue(wait_until(lambda: self.install(self.server.url + "/slow-x", sha=shas[2])[0] == 200, 5))
        self.fx.runtime.emergency_stop("cleanup")

    def test_disk_space_is_checked_before_and_while_downloading(self):
        from neuxmind_runtime import models as models_module
        usage = shutil.disk_usage(self.models_dir.parent)
        full = type(usage)(usage.total, usage.total - 1000, 1000)
        with mock.patch.object(models_module.shutil, "disk_usage", return_value=full):
            record = self.install_and_wait(self.server.url + "/pkg/model.onnx")
        self.assertEqual(record["status"], "failed")
        self.assertIn("磁盘可用空间不足", record["error"])
        self.assertFalse((self.models_dir / SHA).exists())
        self.assertTrue(self.partial_is_empty())

    def test_internal_addresses_and_digest_oracle(self):
        for ref in ("https://169.254.169.254/latest/meta-data", "https://10.255.255.1/m.onnx",
                    "https://127.0.0.2/m.onnx", "https://[::1]/m.onnx", "https://100.64.1.1/m.onnx"):
            with self.subTest(ref=ref):
                status, body = self.install(ref)
                self.assertEqual((status, body.get("code")), (400, "package_ref_refused"), body)
        store = ModelStore(ModelConfig(models_dir=str(self.models_dir.parent / "m2"),
                                       allow_private_hosts=("10.255.255.1",)))
        self.assertEqual(store.check_url("https://10.255.255.1/m.onnx"), "https")
        wrong = self.install_and_wait(self.server.url + "/pkg/model.onnx", sha="0" * 64)
        self.assertNotIn(SHA, wrong["error"])                         # never reveal the real digest
        events = http(self.fx.url, f"/v2/runs/{wrong['run_id']}/events")[1]["events"]
        self.assertNotIn(SHA, json.dumps(events))

    def test_total_download_time_limit(self):
        object.__setattr__(self.fx.runtime.models.config, "max_download_s", 0.3)
        record = self.install_and_wait(self.server.url + "/drip")
        self.assertEqual(record["status"], "failed")
        self.assertIn("总时限", record["error"])
        self.assertTrue(self.partial_is_empty())

    def test_install_timeout_is_wall_clock(self):
        """Q3-11: at time_scale 3600 the 1800 s limit used to be 0.5 s of wall time."""
        fx = self.make_fixture(adapter=SimQuadruped(time_scale=3600.0))
        try:
            status, body = http(fx.url, "/v2/capabilities/model.install/invoke", {
                "request_id": "ts", "hardware_authorized": True,
                "args": {"package_ref": self.server.url + "/drip", "sha256": SHA, "runtime": "onnxruntime"}})
            record = fx.wait(body["run_id"], 15)
            self.assertEqual(record["status"], "succeeded", record["error"])
        finally:
            fx.close()

    def test_input_validation_gaps(self):
        url = self.server.url + "/pkg/model.onnx"
        self.assertEqual(self.install(url, sha=SHA + "\n")[0], 400)
        for bad in ("x\u202egnp.exe", "x\x7f", "a\u200bb", "x\x85"):
            with self.subTest(name=repr(bad)):
                status, body = http(self.fx.url, "/v2/capabilities/model.install/invoke", {
                    "request_id": f"n-{time.monotonic_ns()}", "hardware_authorized": True,
                    "args": {"package_ref": url, "sha256": SHA, "runtime": "onnxruntime", "name": bad}})
                self.assertEqual(status, 400, body)
        ok = http(self.fx.url, "/v2/capabilities/model.install/invoke", {
            "request_id": "n-ok", "hardware_authorized": True,
            "args": {"package_ref": url, "sha256": SHA, "runtime": "onnxruntime", "name": "检测 模型-v2 (int8)"}})
        self.assertEqual(ok[0], 200, ok)

    def test_disk_errors_are_reported_as_failure_events(self):
        import errno
        from neuxmind_runtime import models as models_module
        real = models_module.os.replace

        def full(src, dst):
            raise OSError(errno.ENOSPC, "No space left on device")
        with mock.patch.object(models_module.os, "replace", full):
            record = self.install_and_wait(self.server.url + "/pkg/model.onnx")
        self.assertIs(models_module.os.replace, real)
        self.assertEqual(record["status"], "failed")
        self.assertIn("磁盘空间不足", record["error"])
        failed = [e for e in http(self.fx.url, f"/v2/runs/{record['run_id']}/events")[1]["events"]
                  if e["type"] == "model.install_failed"]
        self.assertTrue(failed and "磁盘空间不足" in failed[0]["error"])
        self.assertTrue(wait_until(self.partial_is_empty, 3))

    def test_server_closing_early_is_an_incomplete_download(self):
        record = self.install_and_wait(self.server.url + "/short")
        self.assertEqual(record["status"], "failed")
        self.assertIn("不完整", record["error"])
        self.assertFalse((self.models_dir / SHA).exists())
        self.assertTrue(self.partial_is_empty())


class SkillVisibilityTests(ModelFixture):
    def test_models_are_read_only_in_the_sandbox(self):
        self.assertEqual(self.install_and_wait(self.server.url + "/pkg/model.onnx")["status"], "succeeded")
        self.fx.install(custom_package("reader", MODEL_READER, parameters=[
            {"name": "sha", "type": "str", "required": True}]))
        record = self.fx.wait(self.fx.run("reader", {"sha": SHA}), 20)
        self.assertEqual(record["status"], "succeeded", record["error"])
        out = record["result"]
        self.assertEqual((out["ids"], out["digest"], out["runtime"]), ([SHA], SHA, "onnxruntime"))
        self.assertEqual(out["after"], str(self.models_dir / SHA / "model.onnx"))
        for name, result in out["attempts"].items():
            self.assertNotEqual(result, "allowed", f"技能可以 {name} 模型")
        self.assertEqual((self.models_dir / SHA / "model.onnx").read_bytes(), PAYLOAD)
        self.assertEqual(sorted(p.name for p in (self.models_dir / SHA).iterdir()), ["model.json", "model.onnx"])

    def test_inprocess_context_has_models_too(self):
        fx = self.make_fixture(sandbox="inprocess")
        try:
            record = None
            status, body = http(fx.url, "/v2/capabilities/model.install/invoke", {
                "request_id": "ip", "hardware_authorized": True,
                "args": {"package_ref": self.server.url + "/pkg/model.onnx", "sha256": SHA, "runtime": "onnxruntime"}})
            self.assertEqual(fx.wait(body["run_id"], 15)["status"], "succeeded")
            fx.install(custom_package("lister", "def run(ctx):\n    return {'ids': sorted(ctx.models)}\n"))
            record = fx.wait(fx.run("lister"))
            self.assertEqual(record["result"], {"ids": [SHA]})
        finally:
            fx.close()

    def test_delete_refused_while_a_running_skill_has_it(self):
        self.assertEqual(self.install_and_wait(self.server.url + "/pkg/model.onnx")["status"], "succeeded")
        self.fx.install(custom_package("holder", HOLDS_MODEL))
        run_id = self.fx.run("holder")
        wait_until(lambda: self.fx.runtime.runner.get(run_id)["status"] == "running", 10)
        status, body = http(self.fx.url, f"/v2/models/{SHA}", {"hardware_authorized": True}, method="DELETE")
        self.assertEqual((status, body.get("code")), (409, "model_in_use"), body)
        self.assertEqual(body["runs"], [run_id])
        self.assertTrue((self.models_dir / SHA / "model.onnx").exists())
        # A model installed *after* the skill started was never shown to it: deletable.
        self.assertEqual(self.install_and_wait(self.server.url + "/pkg/other.onnx", sha=OTHER_SHA)["status"],
                         "succeeded")
        self.assertEqual(http(self.fx.url, f"/v2/models/{OTHER_SHA}", {"hardware_authorized": True},
                              method="DELETE")[0], 200)
        self.fx.runtime.runner.cancel(run_id)
        self.fx.wait(run_id, 5)
        status, body = http(self.fx.url, f"/v2/models/{SHA}", {"hardware_authorized": True}, method="DELETE")
        self.assertEqual(status, 200, body)


class ModelStoreUnitTests(unittest.TestCase):
    def test_bus_requires_authorization_for_system_capabilities(self):
        from neuxmind_runtime.bus import CapabilityBus
        from neuxmind_runtime.errors import NotAuthorized
        with tempfile.TemporaryDirectory() as tmp:
            store = ModelStore(ModelConfig(models_dir=tmp + "/m"))
            bus = CapabilityBus(SimQuadruped(), native={"model.install": store})
            args = {"package_ref": "https://models.example.com/a.onnx", "sha256": SHA, "runtime": "onnxruntime"}
            with mock.patch.object(store, "start_install") as start:
                with self.assertRaises(NotAuthorized):
                    bus.start_action("model.install", args, authorized=False)
                start.assert_not_called()


    def test_config_validation(self):
        with self.assertRaises(ValueError):
            ModelConfig(models_dir="/tmp/x", runtimes=())
        with self.assertRaises(ValueError):
            ModelConfig(models_dir="/tmp/x", max_bytes=0)

    def test_redirect_policy_on_its_own(self):
        """Each redirect layer separately (the http_error_302 check normally masks redirect_request's)."""
        import urllib.request
        from neuxmind_runtime.models import ModelInstallError, _RedirectPolicy
        with tempfile.TemporaryDirectory() as tmp:
            store = ModelStore(ModelConfig(models_dir=tmp + "/m", allow_http_hosts=("127.0.0.1",)))
            policy = _RedirectPolicy(store)
            req = urllib.request.Request("http://127.0.0.1/a.onnx")
            for target in ("https://127.0.0.1/a.onnx", "file:///etc/passwd"):
                with self.subTest(target=target), self.assertRaises(ModelInstallError):
                    policy.redirect_request(req, None, 302, "Found", {}, target)
            with self.assertRaises(ModelInstallError):        # same scheme, but a host http is not allowed for
                policy.redirect_request(req, None, 302, "Found", {}, "http://example.com/a.onnx")
            self.assertIsNotNone(policy.redirect_request(req, None, 302, "Found", {}, "/b.onnx"))

    def test_cancel_just_before_publish_publishes_nothing(self):
        from neuxmind_runtime.models import InstallHandle, ModelInstallError
        with tempfile.TemporaryDirectory() as tmp:
            store = ModelStore(ModelConfig(models_dir=tmp + "/m"))
            handle = InstallHandle(store, {})
            handle.cancel()
            part = store.partial / "x.part"
            part.write_bytes(PAYLOAD)
            with self.assertRaises(ModelInstallError):
                store._publish(SHA, part, "model.onnx", len(PAYLOAD), "onnxruntime", "https://x/m", None, None, handle)
            self.assertFalse((store.dir / SHA).exists())
            self.assertEqual(store.list(), [])
            self.assertEqual([p.name for p in store.partial.iterdir() if p.name != "x.part"], [])

    def test_reads_the_first_0_3_0_layout(self):
        """996d40b wrote model_id = sha256 into model.json; it is read as "no catalog id"."""
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "m" / SHA
            d.mkdir(parents=True)
            (d / "model.onnx").write_bytes(PAYLOAD)
            (d / "model.json").write_text(json.dumps({"model_id": SHA, "sha256": SHA, "name": None, "runtime":
                                                      "onnxruntime", "file": "model.onnx", "size": len(PAYLOAD),
                                                      "installed_at": 1.0, "source": "https://x/m"}))
            store = ModelStore(ModelConfig(models_dir=tmp + "/m"))
            self.assertEqual([(m["sha256"], m["model_id"]) for m in store.list()], [(SHA, None)])
            for root, _dirs, _files in os.walk(tmp):
                os.chmod(root, 0o755)

    def test_http_only_for_configured_hosts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ModelStore(ModelConfig(models_dir=tmp + "/m"))
            from neuxmind_runtime.models import ModelInstallError
            self.assertEqual(store.check_url("https://models.example.com/a.onnx"), "https")
            for url in ("http://127.0.0.1/a", "file:///tmp/a", "gopher://x/a", "https:///nohost"):
                with self.subTest(url=url), self.assertRaises(ModelInstallError):
                    store.check_url(url)


if __name__ == "__main__":
    unittest.main()
