"""Edge model install (§5.1/§5.4) and telemetry tail (§5.2) against the real runtime and cloud.

Real: the runtime's native ``model.install`` (``--models-dir`` equivalent,
in-process ``EdgeRuntime`` + ``GatewayServer`` on a random port) downloading
from a local HTTP package server, and the cloud app (uvicorn, SQLite) where a
platform operator lists models with a ``package`` and a developer buys the
licence. Plain http is allowed for 127.0.0.1 only through the runtime's own
dev/test switch (``allow_http_hosts``). The robot body is the simulated
quadruped (``simulated=true``); model files are random bytes, not models.
"""
import json
import os
import threading
import time
import unittest
from unittest import mock

import _support
from _support import OPERATOR_EMAIL, PASSWORD, CliHome, CloudServer, PackageServer, RuntimeServer, make_skill

from neuxmind import NeuxMindError
from neuxmind import robot as robot_module
from neuxmind.cloud import CloudClient
from neuxmind.models import install_from_cloud, resolve_model
from neuxmind.robot import RobotClient

HOLDER = """
def run(ctx):
    ctx.log("holding models", models=sorted(ctx.models))
    while True:
        ctx.check_stop()
        ctx.sleep(0.2)
"""

TALKER = """
def run(ctx, n=3):
    for i in range(n):
        ctx.log(f"line {i}")
        ctx.progress((i + 1) / n, f"step {i}")
    return {"lines": n}
"""


def _wait_for(predicate, timeout=30.0, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


class ModelInstallTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pkgs = PackageServer()
        cls.dog = RuntimeServer("quadruped", models=True)
        cls.plain = RuntimeServer("quadruped")                  # no --models-dir
        cls.cloud = CloudServer()
        cls.ops = CloudClient(cls.cloud.url)
        reg = cls.ops.register(OPERATOR_EMAIL, PASSWORD)
        cls.ops.verify_email(reg["dev_verification_token"])
        assert cls.ops.me()["platform_operator"]
        cls.det_url, cls.det_sha = cls.pkgs.add("det.onnx")
        cls.seg_url, cls.seg_sha = cls.pkgs.add("seg.onnx", size=150_000)
        # ~1 MiB served in 64 KiB chunks 0.05 s apart: the install takes ~1 s and reports progress.
        cls.slow_url, cls.slow_sha = cls.pkgs.add("slow.onnx", size=1 << 20, slow=0.05)
        bad_url, _real = cls.pkgs.add("bad.onnx", size=50_000)
        cls.bad_path = "/bad.onnx"
        cards = [
            {"id": "edge-det", "name": "Edge detector (random bytes)", "source": "native", "kind": "vision",
             "license": "test only", "pricing": {"type": "license_once", "credits": 30},
             "package": {"ref": cls.det_url, "sha256": cls.det_sha, "runtime": "onnxruntime"}},
            {"id": "edge-seg", "name": "Edge segmenter (random bytes)", "source": "native", "kind": "vision",
             "license": "test only", "pricing": {"type": "free", "credits": 0},
             "package": {"ref": cls.seg_url, "sha256": cls.seg_sha, "runtime": "onnxruntime"}},
            # The catalog sha does not match the file the URL serves (corrupted / tampered package).
            {"id": "edge-bad", "name": "Mismatched package", "source": "native", "kind": "vision",
             "license": "test only", "pricing": {"type": "free", "credits": 0},
             "package": {"ref": bad_url, "sha256": "c" * 64, "runtime": "onnxruntime"}},
            {"id": "edge-trt", "name": "TensorRT only", "source": "native", "kind": "vision",
             "license": "test only", "pricing": {"type": "free", "credits": 0},
             "package": {"ref": cls.det_url, "sha256": cls.det_sha, "runtime": "tensorrt"}},
            {"id": "edge-slow", "name": "Slow link package", "source": "native", "kind": "vision",
             "license": "test only", "pricing": {"type": "free", "credits": 0},
             "package": {"ref": cls.slow_url, "sha256": cls.slow_sha, "runtime": "onnxruntime"}},
            {"id": "cloud-llm", "name": "Echo (test double)", "source": "closed", "kind": "llm",
             "license": "test only", "pricing": {"type": "per_1k_tokens", "credits": 1}, "upstream": {"provider": "echo"}},
        ]
        for card in cards:
            cls.ops._request("POST", "/admin/models", card)

    @classmethod
    def tearDownClass(cls):
        cls.dog.close()
        cls.plain.close()
        cls.cloud.close()
        cls.pkgs.close()

    def setUp(self):
        # Every test starts from an empty model store.
        robot = RobotClient(self.dog.url, self.dog.token)
        for model in robot.models():
            robot.remove_model(model["sha256"], authorize=True)

    def developer(self, email: str, credits: int = 100) -> CliHome:
        home = CliHome()
        self.addCleanup(home.close)
        home.json("config", "set", "cloud_url", self.cloud.url)
        reg = home.json("register", "--email", email, "--password", PASSWORD)
        home.json("verify-email", reg["dev_verification_token"])
        if credits:
            self.ops._request("POST", f"/admin/wallets/{reg['org_id']}/adjust", {"delta": credits, "reason": "sdk test"})
        home.json("robot", "add", "dog", self.dog.url, "--token", self.dog.token)
        return home

    # ------------------------------------------------------------------ RobotClient
    def test_robot_client_install_idempotent_mismatch_remove(self):
        robot = RobotClient(self.dog.url, self.dog.token)
        self.assertTrue(robot.supports_model_install())
        # No authorize → refused locally, nothing sent.
        calls = []
        real = robot_module.request_json
        with mock.patch.object(robot_module, "request_json", lambda *a, **k: calls.append(a) or real(*a, **k)):
            for call in (lambda: robot.install_model(self.det_url, self.det_sha, "onnxruntime"),
                         lambda: robot.remove_model(self.det_sha)):
                with self.assertRaises(NeuxMindError) as ctx:
                    call()
                self.assertEqual(ctx.exception.code, "authorization_required")
        self.assertEqual(calls, [])

        hits_before = self.pkgs.hits.get("/det.onnx", 0)
        started = robot.install_model(self.det_url, self.det_sha.upper(), "onnxruntime", model_id="edge-det",
                                      name="edge-det", authorize=True)
        record = robot.wait(started["run_id"], timeout=60)
        self.assertEqual(record["status"], "succeeded", record.get("error"))
        self.assertEqual(record["result"]["model_id"], self.det_sha)
        installed = robot.find_model(self.det_sha.upper())
        self.assertEqual(installed["sha256"], self.det_sha)
        self.assertEqual(installed["size"], 200_000)
        self.assertTrue(os.path.isfile(installed["path"]))
        self.assertEqual(self.pkgs.hits["/det.onnx"], hits_before + 1)

        # Same sha again: succeeds without downloading again (runtime idempotency).
        again = robot.wait(robot.install_model(self.det_url, self.det_sha, "onnxruntime", authorize=True)["run_id"])
        self.assertEqual(again["status"], "succeeded")
        self.assertEqual(self.pkgs.hits["/det.onnx"], hits_before + 1)
        self.assertEqual(len(robot.models()), 1)

        # sha mismatch: the run fails, nothing is published.
        bad = robot.wait(robot.install_model(self.det_url, "d" * 64, "onnxruntime", authorize=True)["run_id"])
        self.assertEqual(bad["status"], "failed")
        self.assertIn("SHA-256", bad["error"])
        self.assertIsNone(robot.find_model("d" * 64))

        # Synchronous refusals carry hints.
        with self.assertRaises(NeuxMindError) as ctx:
            robot.install_model(self.det_url, self.det_sha, "tensorrt", authorize=True)
        self.assertEqual(ctx.exception.code, "runtime_unsupported")
        self.assertIn("--model-runtimes", ctx.exception.details["hint"])
        with self.assertRaises(NeuxMindError) as ctx:
            robot.install_model("ftp://example.com/x.onnx", self.det_sha, "onnxruntime", authorize=True)
        self.assertEqual(ctx.exception.code, "package_ref_refused")

        removed = robot.remove_model(self.det_sha, authorize=True)
        self.assertEqual(removed, {"deleted": True, "model_id": self.det_sha})
        self.assertEqual(robot.models(), [])
        self.assertFalse(os.path.exists(installed["path"]))
        with self.assertRaises(NeuxMindError) as ctx:
            robot.remove_model(self.det_sha, authorize=True)
        self.assertEqual(ctx.exception.code, "model_not_found")
        self.assertIn("nx models installed", ctx.exception.details["hint"])

    def test_install_from_cloud_api(self):
        dev = CloudClient(self.cloud.url)
        reg = dev.register("models-api@sdk.test", PASSWORD)
        dev.verify_email(reg["dev_verification_token"])
        robot = RobotClient(self.dog.url, self.dog.token)
        with self.assertRaises(NeuxMindError) as ctx:          # local refusal first, before the cloud
            install_from_cloud(dev, robot, "edge-seg")
        self.assertEqual(ctx.exception.code, "authorization_required")
        with self.assertRaises(NeuxMindError) as ctx:
            install_from_cloud(dev, robot, "edge-seg", authorize=True)
        self.assertEqual(ctx.exception.code, "entitlement_required")
        self.assertEqual(ctx.exception.details["pricing"], {"type": "free", "credits": 0})
        self.assertIn("nx models buy edge-seg", str(ctx.exception))
        self.assertEqual(robot.models(), [])
        dev.purchase_model("edge-seg")                          # free model: 0-credit licence
        self.assertEqual(dev.model_install_args("edge-seg"),
                         {"package_ref": self.seg_url, "sha256": self.seg_sha, "runtime": "onnxruntime"})
        updates, started, sent = [], [], []
        real = robot_module.request_json

        def spy(method, url, **kw):
            sent.append((method, url, kw.get("body")))
            return real(method, url, **kw)

        with mock.patch.object(robot_module, "request_json", spy):
            result = install_from_cloud(dev, robot, "edge-seg", authorize=True, on_progress=updates.append,
                                        on_start=started.append)
        invokes = [body for method, url, body in sent if url.endswith("/v2/capabilities/model.install/invoke")]
        self.assertEqual(len(invokes), 1)                         # one v2 invoke, catalog id included
        self.assertEqual(invokes[0]["args"], {"package_ref": self.seg_url, "sha256": self.seg_sha,
                                              "runtime": "onnxruntime", "model_id": "edge-seg", "name": "edge-seg"})
        self.assertIs(invokes[0]["hardware_authorized"], True)
        self.assertEqual(started[0]["run_id"], result["run_id"])
        self.assertFalse(started[0]["already_installed"])
        self.assertFalse(result["already_installed"])
        self.assertTrue(result["simulated"])
        self.assertEqual(result["sha256"], self.seg_sha)
        self.assertEqual(result["model"]["sha256"], self.seg_sha)
        self.assertEqual(result["model"]["model_id"], "edge-seg")                  # catalog id recorded
        self.assertEqual(updates[-1]["status"], "succeeded")
        self.assertEqual(resolve_model(robot, "edge-seg"), "edge-seg")      # the runtime resolves catalog ids
        self.assertEqual(resolve_model(robot, self.seg_sha.upper()), self.seg_sha)
        self.assertEqual(robot.find_model("edge-seg")["sha256"], self.seg_sha)
        with self.assertRaises(NeuxMindError) as ctx:
            resolve_model(robot, "edge-det", dev)               # catalog id of a model that is not installed
        self.assertEqual(ctx.exception.code, "model_not_found")
        with self.assertRaises(NeuxMindError) as ctx:
            dev.model_install_args("cloud-llm")
        self.assertEqual(ctx.exception.code, "no_package")
        self.assertIn("nx infer", ctx.exception.details["hint"])
        plain = RobotClient(self.plain.url, self.plain.token)
        sent.clear()
        with mock.patch.object(robot_module, "request_json", spy), self.assertRaises(NeuxMindError) as ctx:
            install_from_cloud(dev, plain, "edge-seg", authorize=True)
        self.assertEqual(ctx.exception.code, "capability_not_provided")
        self.assertIn("--models-dir", str(ctx.exception))
        self.assertEqual([m for m, _u, _b in sent], ["GET"])      # only the manifest was read; no install sent

        # A package answer without the install fields is refused locally (never sent to a robot).
        with mock.patch.object(dev, "model_package", lambda _id: {"model_id": "x", "ref": self.seg_url}):
            with self.assertRaises(NeuxMindError) as ctx:
                dev.model_install_args("x")
        self.assertEqual(ctx.exception.code, "bad_response")
        with mock.patch.object(dev, "model_package", lambda _id: {
                "ref": "https://old.example/x.onnx", "sha256": "e" * 64, "runtime": "onnxruntime",
                "install": {"args": {"package_ref": "https://signed.example/x.onnx?sig=1", "sha256": "e" * 64,
                                     "runtime": "onnxruntime"}}}):
            self.assertEqual(dev.model_install_args("x")["package_ref"], "https://signed.example/x.onnx?sig=1")

    # ------------------------------------------------------------------ nx models install / installed / remove
    def test_cli_install_reinstall_mismatch_remove(self):
        home = self.developer("models-cli@sdk.test", credits=100)
        hits_before = self.pkgs.hits.get("/det.onnx", 0)

        failed = home.json("models", "install", "edge-det", "--robot", "dog", expect=1)
        self.assertEqual(failed["code"], "authorization_required")
        # Not entitled: purchase prompt with the price and wallet; nothing reaches the robot.
        code, out, err = home.run("models", "install", "edge-det", "--robot", "dog", "--authorize")
        self.assertEqual(code, 1)
        self.assertNotIn("正在把", out)
        self.assertIn("nx models buy edge-det", err)
        self.assertIn("一次性 30 点", err)
        self.assertIn("钱包可用 100 点", err)
        self.assertIn("entitlement_required", err)
        self.assertEqual(home.json("models", "installed", "--robot", "dog")["models"], [])
        self.assertEqual(self.pkgs.hits.get("/det.onnx", 0), hits_before)

        home.json("models", "buy", "edge-det")
        code, out, err = home.run("models", "install", "edge-det", "--robot", "dog", "--authorize")
        self.assertEqual(code, 0, err)
        self.assertIn("正在把 edge-det 安装到 dog【仿真】（运行 run-", out)
        self.assertIn("已安装 edge-det 到 dog", out)
        self.assertIn(self.det_sha, out)
        self.assertEqual(self.pkgs.hits["/det.onnx"], hits_before + 1)

        again = home.json("models", "install", "edge-det", "--robot", "dog", "--authorize")
        self.assertTrue(again["already_installed"])
        self.assertEqual(again["run"]["status"], "succeeded")
        self.assertEqual(self.pkgs.hits["/det.onnx"], hits_before + 1)            # no second download
        code, out, _ = home.run("models", "install", "edge-det", "--robot", "dog", "--authorize")
        self.assertIn("同一 SHA-256 已安装，未重复下载", out)

        listed = home.json("models", "installed", "--robot", "dog")["models"]
        self.assertEqual([m["sha256"] for m in listed], [self.det_sha])
        self.assertEqual(listed[0]["model_id"], "edge-det")
        code, out, _ = home.run("models", "installed", "--robot", "dog")
        self.assertRegex(out, rf"{self.det_sha[:12]}\s+edge-det\s+edge-det\s+onnxruntime")
        self.assertIn("onnxruntime", out)

        # Mismatched sha: install fails with the runtime's reason, nothing is installed, temp files are gone.
        home.json("models", "buy", "edge-bad")
        failed = home.json("models", "install", "edge-bad", "--robot", "dog", "--authorize", expect=1)
        self.assertEqual(failed["code"], "install_failed")
        self.assertIn("SHA-256", failed["error"])
        self.assertEqual(failed["details"]["run"]["status"], "failed")
        self.assertEqual(self.pkgs.hits[self.bad_path] >= 1, True)
        self.assertEqual([m["sha256"] for m in home.json("models", "installed", "--robot", "dog")["models"]],
                         [self.det_sha])
        partial = self.dog.models_dir / ".partial"
        self.assertEqual([p for p in os.listdir(partial)] if partial.exists() else [], [])

        # Unsupported runtime: refused synchronously with a hint.
        home.json("models", "buy", "edge-trt")
        code, _out, err = home.run("models", "install", "edge-trt", "--robot", "dog", "--authorize")
        self.assertEqual(code, 1)
        self.assertIn("runtime_unsupported", err)

        failed = home.json("models", "remove", "edge-det", "--robot", "dog", expect=1)
        self.assertEqual(failed["code"], "authorization_required")
        removed = home.json("models", "remove", "edge-det", "--robot", "dog", "--authorize")   # by catalog id
        self.assertEqual(removed["model_id"], self.det_sha)
        self.assertEqual(removed["sha256"], self.det_sha)
        self.assertEqual(home.json("models", "installed", "--robot", "dog")["models"], [])
        failed = home.json("models", "remove", "edge-det", "--robot", "dog", "--authorize", expect=1)
        self.assertEqual(failed["code"], "model_not_found")
        failed = home.json("models", "remove", self.det_sha, "--robot", "dog", "--authorize", expect=1)
        self.assertEqual(failed["code"], "model_not_found")
        code, _out, err = home.run("models", "install", "cloud-llm", "--robot", "dog", "--authorize")
        self.assertEqual(code, 1)
        self.assertIn("没有端侧部署包", err)

    def test_catalog_ids_without_cloud_ambiguity_and_compatible_list(self):
        robot = RobotClient(self.dog.url, self.dog.token)
        self.assertEqual(robot.manifest(refresh=True)["model_runtimes"], ["onnxruntime"])
        home = CliHome()                         # no cloud configured at all
        self.addCleanup(home.close)
        home.json("robot", "add", "dog", self.dog.url, "--token", self.dog.token)
        for url, sha in ((self.det_url, self.det_sha), (self.seg_url, self.seg_sha)):   # two versions, one id
            run = robot.wait(robot.install_model(url, sha, "onnxruntime", model_id="edge-multi", name="Multi",
                                                 authorize=True)["run_id"])
            self.assertEqual(run["status"], "succeeded", run.get("error"))
        self.assertIsNone(robot.find_model("edge-multi"))
        self.assertEqual(len(robot.find_models("edge-multi")), 2)
        failed = home.json("models", "remove", "edge-multi", "--robot", "dog", "--authorize", expect=1)
        self.assertEqual(failed["code"], "model_ambiguous")
        self.assertEqual(failed["details"]["sha256"], sorted([self.det_sha, self.seg_sha]))
        code, _out, err = home.run("models", "remove", "edge-multi", "--robot", "dog", "--authorize")
        self.assertEqual(code, 1)
        self.assertIn(f"nx models remove {sorted([self.det_sha, self.seg_sha])[0]} --robot dog --authorize", err)
        self.assertEqual(len(robot.models()), 2)                         # nothing deleted
        home.json("models", "remove", self.det_sha, "--robot", "dog", "--authorize")
        # Now unique: removed by catalog id straight through the runtime, no cloud login needed.
        removed = home.json("models", "remove", "edge-multi", "--robot", "dog", "--authorize")
        self.assertEqual(removed["sha256"], self.seg_sha)
        self.assertEqual(robot.models(), [])

        # The cloud's compatibility filter now sees the robot's model runtimes: onnxruntime edge
        # packages are listed for the dog, the tensorrt-only one is not.
        dev = self.developer("models-list@sdk.test", credits=0)
        listed = {m["id"] for m in dev.json("models", "list", "--robot", "dog")["models"]}
        self.assertTrue({"edge-det", "edge-seg", "edge-slow"} <= listed, listed)
        self.assertNotIn("edge-trt", listed)

    def test_install_progress_in_run_events(self):
        home = self.developer("models-progress@sdk.test", credits=0)
        home.json("models", "buy", "edge-slow")
        code, out, err = home.run("models", "install", "edge-slow", "--robot", "dog", "--authorize")
        self.assertEqual(code, 0, err)
        percents = [int(line.split("下载与校验中 ")[1].split("%")[0]) for line in out.splitlines() if "下载与校验中" in line]
        self.assertGreaterEqual(len(percents), 3, out)                   # progress shown while downloading
        self.assertEqual(percents, sorted(percents))
        self.assertIn(" / 1.0 MiB）", out)                               # bytes from model.install_progress
        run_id = out.split("（运行 ")[1].split("：")[0]
        events = home.json("telemetry", "tail", "--robot", "dog", "--run", run_id)["events"]
        types = [e["type"] for e in events]
        self.assertEqual(types[0], "run.started")
        self.assertIn("model.install_started", types)
        self.assertIn("model.installed", types)
        self.assertEqual(types[-1], "run.finished")
        progress = [e for e in events if e["type"] == "model.install_progress"]
        self.assertGreaterEqual(len(progress), 3)
        self.assertTrue(all(e["run_id"] == run_id and e["sha256"] == self.slow_sha and e["model_id"] == "edge-slow"
                            for e in progress))
        sizes = [e["bytes"] for e in progress]
        self.assertEqual(sizes, sorted(sizes))
        self.assertEqual(progress[0]["total"], 1 << 20)
        code, text, _ = home.run("telemetry", "tail", "--robot", "dog", "--run", run_id)
        self.assertIn("model.install_progress", text)
        # A failed install's reason is in its run log too.
        home.json("models", "buy", "edge-bad")
        failed = home.json("models", "install", "edge-bad", "--robot", "dog", "--authorize", expect=1)
        bad_events = home.json("telemetry", "tail", "--robot", "dog", "--run", failed["details"]["run_id"])["events"]
        reasons = [e["error"] for e in bad_events if e["type"] == "model.install_failed"]
        self.assertEqual(len(reasons), 1)
        self.assertIn("SHA-256", reasons[0])

    def test_cli_remove_model_in_use(self):
        home = self.developer("models-inuse@sdk.test", credits=0)
        home.json("models", "buy", "edge-seg")
        installed = home.json("models", "install", "edge-seg", "--robot", "dog", "--authorize")
        sha = installed["sha256"]
        skill = make_skill(home.path, "model-holder", HOLDER, timeout_s=120)
        home.json("skill", "install", str(skill), "--robot", "dog")
        run_id = home.json("skill", "run", "model-holder", "--robot", "dog", "--authorize")["run_id"]
        robot = RobotClient(self.dog.url, self.dog.token)
        def end_run():                           # leave no skill holding the model for the next test
            if robot.run(run_id)["status"] in ("queued", "running"):
                robot.cancel(run_id)
            robot.wait(run_id, timeout=60)
        self.addCleanup(end_run)
        held = _wait_for(lambda: [e for e in robot.run(run_id).get("events") or []
                                  if e.get("message") == "holding models"], what="skill to start")
        self.assertIn(sha, held[0]["models"])                   # the skill really sees the model

        failed = home.json("models", "remove", sha, "--robot", "dog", "--authorize", expect=1)
        self.assertEqual(failed["code"], "model_in_use")
        self.assertEqual(failed["details"]["runs"], [run_id])
        code, _out, err = home.run("models", "remove", "edge-seg", "--robot", "dog", "--authorize")
        self.assertEqual(code, 1)
        self.assertIn(f"模型 edge-seg 正被运行中的技能使用（{run_id}），没有删除", err)
        self.assertIn("nx skill cancel", err)
        self.assertEqual(len(home.json("models", "installed", "--robot", "dog")["models"]), 1)   # still there

        home.json("skill", "cancel", run_id, "--robot", "dog")
        _wait_for(lambda: robot.run(run_id)["status"] in ("stopped", "failed", "succeeded"), 60, "cancel")
        removed = home.json("models", "remove", "edge-seg", "--robot", "dog", "--authorize")
        self.assertTrue(removed["deleted"])
        self.assertEqual(home.json("models", "installed", "--robot", "dog")["models"], [])


class TelemetryAndStatusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dog = RuntimeServer("quadruped", models=True)
        cls.home = CliHome()
        cls.home.json("robot", "add", "dog", cls.dog.url, "--token", cls.dog.token)
        skill = make_skill(cls.home.path, "talker", TALKER, parameters=[{"name": "n", "type": "int", "default": 3}])
        cls.home.json("skill", "install", str(skill), "--robot", "dog")

    @classmethod
    def tearDownClass(cls):
        cls.home.close()
        cls.dog.close()

    def run_talker(self, n=3) -> str:
        run_id = self.home.json("skill", "run", "talker", "--robot", "dog", "--authorize", "--param", f"n={n}",
                                "--wait")["id"]
        return run_id

    def test_tail_since_lines_and_paging(self):
        robot = RobotClient(self.dog.url, self.dog.token)
        start = robot.latest_seq()
        for i in range(1200):                       # more than two pages of /v2/telemetry (500 each)
            self.dog.runtime.events.emit("test.marker", index=i)
        _wait_for(lambda: robot.latest_seq() >= start + 1200, what="events")
        data = self.home.json("telemetry", "tail", "--robot", "dog", "--since", str(start))
        markers = [e["index"] for e in data["events"] if e["type"] == "test.marker"]
        self.assertEqual(markers, list(range(1200)))
        seqs = [e["seq"] for e in data["events"]]
        self.assertEqual(seqs, sorted(set(seqs)))                       # ordered, no duplicates
        self.assertTrue(all(s > start for s in seqs))
        self.assertEqual(data["last_seq"], seqs[-1])
        # Incremental: nothing new after last_seq.
        self.assertEqual(self.home.json("telemetry", "tail", "--robot", "dog", "--since",
                                        str(data["last_seq"]))["events"], [])
        # Default: the last 20 events; -n 5: the last five.
        latest = self.home.json("telemetry", "tail", "--robot", "dog")["events"]
        self.assertEqual([e["index"] for e in latest], list(range(1180, 1200)))
        last5 = self.home.json("telemetry", "tail", "--robot", "dog", "-n", "5")["events"]
        self.assertEqual([e["index"] for e in last5], list(range(1195, 1200)))
        self.assertEqual(self.home.json("telemetry", "tail", "--robot", "dog", "-n", "0")["events"], [])
        code, out, _ = self.home.run("telemetry", "tail", "--robot", "dog", "-n", "2")
        self.assertEqual(code, 0)
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertRegex(lines[-1], r"^\[\d+\] \d\d:\d\d:\d\d test\.marker index=1199$")

    def test_tail_run_replay_and_follow(self):
        run_id = self.run_talker(3)
        data = self.home.json("telemetry", "tail", "--robot", "dog", "--run", run_id)
        types = [e["type"] for e in data["events"]]
        self.assertEqual(types[0], "run.started")
        self.assertEqual(types[-1], "run.finished")
        self.assertTrue(all(e.get("run_id") == run_id for e in data["events"]))
        self.assertEqual([e["message"] for e in data["events"] if e["type"] == "run.log"], ["line 0", "line 1", "line 2"])
        self.assertEqual(self.home.json("telemetry", "tail", "--robot", "dog", "--run", run_id, "-n", "1")
                         ["events"][0]["type"], "run.finished")
        # --since inside a run.
        cut = data["events"][1]["seq"]
        later = self.home.json("telemetry", "tail", "--robot", "dog", "--run", run_id, "--since", str(cut))["events"]
        self.assertEqual([e["seq"] for e in later], [e["seq"] for e in data["events"] if e["seq"] > cut])
        failed = self.home.json("telemetry", "tail", "--robot", "dog", "--run", "run-nope", expect=1)
        self.assertEqual(failed["status"], 404)

        # --follow --run on a run still going: prints events as they come and exits after run.finished.
        robot = RobotClient(self.dog.url, self.dog.token)
        skill = make_skill(self.home.path, "slow-talker", """
def run(ctx):
    for i in range(4):
        ctx.log(f"tick {i}")
        ctx.sleep(4.0)
    return {"ok": True}
""")
        self.home.json("skill", "install", str(skill), "--robot", "dog")
        run_id = self.home.json("skill", "run", "slow-talker", "--robot", "dog", "--authorize")["run_id"]
        t0 = time.monotonic()
        code, out, err = self.home.run("telemetry", "tail", "--robot", "dog", "--run", run_id, "--follow",
                                       "--interval", "0.1")
        self.assertEqual(code, 0, err)
        self.assertEqual(robot.run(run_id)["status"], "succeeded")
        self.assertLess(time.monotonic() - t0, 60)
        body = out.splitlines()
        self.assertIn(f"跟踪 运行 {run_id} 的遥测", body[0])
        self.assertTrue(body[1].split()[2] == "run.started", body)
        self.assertIn("run.finished", body[-1])
        self.assertEqual(sum("tick" in line for line in body), 4)

    def test_follow_robot_wide_sees_new_events(self):
        robot = RobotClient(self.dog.url, self.dog.token)
        stop = threading.Event()

        def emitter():
            i = 0
            while not stop.is_set():
                self.dog.runtime.events.emit("test.live", index=i)
                i += 1
                time.sleep(0.05)

        before = robot.latest_seq()
        thread = threading.Thread(target=emitter, daemon=True)
        thread.start()
        try:
            events = list(robot.tail(lines=0, follow=True, interval=0.1, duration=1.5))
        finally:
            stop.set()
            thread.join()
        live = [e["index"] for e in events if e["type"] == "test.live"]
        self.assertGreater(len(live), 5)
        self.assertEqual(live, list(range(live[0], live[0] + len(live))))          # contiguous, no repeats
        self.assertTrue(all(e["seq"] > before for e in events))
        # CLI: NDJSON with --follow --json; --duration ends it.
        self.dog.runtime.events.emit("test.ndjson", index=1)
        code, out, _ = self.home.run("telemetry", "tail", "--robot", "dog", "-n", "1", "--follow",
                                     "--duration", "0.3", "--interval", "0.1", "--json")
        self.assertEqual(code, 0)
        rows = [json.loads(line) for line in out.splitlines()]
        self.assertEqual(rows[0]["type"], "test.ndjson")

    def test_robot_status_shows_sandbox_models_telemetry(self):
        status = self.home.json("robot", "status", "dog")
        sandbox = status["sandbox"]
        for key in ("level", "best_effort_reasons", "python_usable", "privilege_drop", "runtime_user"):
            self.assertIn(key, sandbox)
        code, out, _ = self.home.run("robot", "status", "dog")
        self.assertEqual(code, 0)
        level = {"full": "完整隔离（full）", "best-effort": "尽力而为（best-effort）"}[sandbox["level"]]
        self.assertIn(f"隔离等级：{level}", out)
        self.assertIn("技能解释器：", out)
        self.assertIn("网络未隔离", out)
        self.assertIn("端侧模型：已安装", out)
        self.assertIn("遥测：最新 seq", out)
        for reason in sandbox["best_effort_reasons"]:
            self.assertIn(reason, out)
        if os.geteuid() == 0 and sandbox.get("privilege_drop"):
            self.assertIn("已降权", out)

    def test_status_text_for_degraded_sandbox(self):
        from neuxmind.cli import _sandbox_lines

        lines = "\n".join(_sandbox_lines({"mode": "process", "level": "best-effort", "runtime_user": "nx",
                                          "user": "nx", "privilege_drop": False, "python_usable": False,
                                          "python": "/root/.local/bin/python3.10", "pids_cgroup": False,
                                          "network_isolated": False,
                                          "best_effort_reasons": ["运行时不是 root：技能与运行时同用户"]}))
        self.assertIn("尽力而为（best-effort）", lines)
        self.assertIn("未降权（技能与运行时同用户）", lines)
        self.assertIn("技能用户无法执行，技能运行会失败", lines)
        self.assertIn("--sandbox-python", lines)
        self.assertIn("pids cgroup 未启用", lines)
        self.assertIn("尽力而为原因：运行时不是 root：技能与运行时同用户", lines)
        self.assertIn("未报告", "\n".join(_sandbox_lines({})))


if __name__ == "__main__":
    unittest.main()
