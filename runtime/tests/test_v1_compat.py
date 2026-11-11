"""v1 compatibility layer: raw HTTP fields, and the real brainos Platform driving the runtime."""
import tempfile
import unittest

import _support
from _support import RuntimeFixture, example, http, wait_until

from neuxmind_runtime.core import json_digest


def application(steps, name="demo"):
    app = {"schema": "robot.application.v1", "name": name, "version": "1.0", "project_id": "p", "robot_id": "r",
           "failure_policy": "stop_and_report", "steps": steps, "id": "app-1", "created_at": "now"}
    app["digest"] = json_digest(app)
    return app


def step(entrypoint, parameters=None, requires=("system.health",), timeout_s=30):
    return {"skill_id": "s", "skill_digest": "d", "entrypoint": entrypoint, "parameters": parameters or {},
            "timeout_s": timeout_s, "requires": list(requires), "success_condition": "ok",
            "stop_condition": "stop", "model_digests": {}}


class V1HttpTests(unittest.TestCase):
    def setUp(self):
        self.fx = RuntimeFixture(http=True, time_scale=20.0)
        self.url = self.fx.url
        self.fx.install(example("patrol"))
        self.fx.install(example("status_report"))

    def tearDown(self):
        self.fx.close()

    def test_manifest_and_status(self):
        manifest = http(self.url, "/v1/manifest")[1]
        status = http(self.url, "/v1/status")[1]
        self.assertEqual(status["instance_id"], manifest["instance_id"])
        self.assertTrue(status["simulated"])
        self.assertFalse(status["estop_latched"])

    def test_validate(self):
        ok = http(self.url, "/v1/validate", {"application": application([step("status_report")])})[1]
        self.assertTrue(ok["accepted"] and ok["passed"])
        bad = http(self.url, "/v1/validate", {"application": application(
            [step("not_installed"), step("patrol", {"waypoints": [[1, 0]]}, requires=("navigation.goal",))])})[1]
        self.assertFalse(bad["accepted"])
        failed = [c["name"] for c in bad["cases"] if not c["passed"]]
        self.assertTrue(any("not_installed" in n for n in failed))
        self.assertIn("设备能力覆盖应用需求", failed)
        tampered = application([step("status_report")])
        tampered["name"] = "changed"
        self.assertFalse(http(self.url, "/v1/validate", {"application": tampered})[1]["passed"])

    def test_evaluate_deploy_start_stop(self):
        app = application([step("status_report"),
                           step("patrol", {"waypoints": [[1, 0]]}, requires=("base.navigate_to",))])
        self.assertEqual(http(self.url, "/v1/evaluate", {"request_id": "e0", "application": app})[0], 403)
        status, report = http(self.url, "/v1/evaluate",
                              {"request_id": "e1", "application": app, "hardware_authorized": True})
        self.assertEqual(status, 200, report)
        self.assertTrue(report["passed"])
        self.assertEqual(report["application_digest"], app["digest"])
        self.assertEqual(report["robot_manifest_digest"], json_digest(http(self.url, "/v1/manifest")[1]))
        self.assertTrue(report["evidence_ref"].startswith("simulation://"))
        self.assertEqual((report["simulated"], report["evidence_kind"]), (True, "simulation"))
        self.assertEqual([c["passed"] for c in report["cases"]], [True, True])

        status, deployed = http(self.url, "/v1/deploy", {"request_id": "d1", "application": app, "report_id": "r1"})
        self.assertTrue(deployed["accepted"])
        self.assertEqual(deployed["pinned"], ["status_report@0.1.0", "patrol@0.1.0"])
        release_id = deployed["release_id"]
        self.assertEqual(http(self.url, "/v1/start", {"request_id": "s0", "release_id": release_id})[0], 403)
        status, started = http(self.url, "/v1/start",
                               {"request_id": "s1", "release_id": release_id, "hardware_authorized": True})
        self.assertTrue(started["accepted"])
        record = wait_until(lambda: (r := http(self.url, f"/v2/runs/{started['run_id']}")[1])["status"]
                            not in ("queued", "running") and r, 10)
        self.assertEqual(record["status"], "succeeded", record)
        self.assertEqual([s["status"] for s in record["result"]["steps"]], ["succeeded", "succeeded"])
        stopped = http(self.url, "/v1/stop", {"request_id": "x1", "release_id": release_id,
                                              "hardware_authorized": False})[1]
        self.assertTrue(stopped["accepted"])
        # After a stop the latch blocks a new start until reset.
        status, body = http(self.url, "/v1/start", {"request_id": "s2", "release_id": release_id,
                                                    "hardware_authorized": True})
        self.assertEqual((status, body["code"]), (409, "estop_latched"))

    def test_v1_stop_then_v1_reset(self):
        """/v1/reset: same semantics as /v2/reset, needs request_id + hardware_authorized, idempotent."""
        app = application([step("status_report")])
        self.assertTrue(http(self.url, "/v1/stop", {})[1]["latched"])
        check = http(self.url, "/v1/validate", {"application": app})[1]
        self.assertFalse(check["accepted"])
        latch_case = next(c for c in check["cases"] if c["name"] == "急停未锁存")
        self.assertIn("请在桌面端解除急停", latch_case["detail"])
        self.assertIn("/v1/reset", latch_case["detail"])

        status, body = http(self.url, "/v1/reset", {"hardware_authorized": True})
        self.assertEqual((status, body["code"]), (400, "request_id_required"))
        status, body = http(self.url, "/v1/reset", {"request_id": "rs-0"})
        self.assertEqual((status, body["code"]), (403, "hardware_not_authorized"))
        self.assertTrue(self.fx.runtime.supervisor.latched)
        status, body = http(self.url, "/v1/reset", {"request_id": "rs-1", "hardware_authorized": True})
        self.assertEqual((status, body), (200, {"latched": False}))
        self.assertTrue(http(self.url, "/v1/validate", {"application": app})[1]["accepted"])
        # Same request_id + same body: idempotent, and reports the *current* state.
        self.assertEqual(http(self.url, "/v1/reset", {"request_id": "rs-1", "hardware_authorized": True})[1],
                         {"latched": False})
        http(self.url, "/v1/stop", {})
        self.assertEqual(http(self.url, "/v1/reset", {"request_id": "rs-1", "hardware_authorized": True})[1],
                         {"latched": True})          # replay never claims "unlatched" (R-14)
        status, body = http(self.url, "/v1/reset", {"request_id": "rs-1", "hardware_authorized": True, "x": 1})
        self.assertEqual((status, body["code"]), (409, "request_id_reused"))

    def test_validate_reports_bad_step_inputs_as_failed_cases(self):
        app = application([step("status_report", parameters=None)])
        app["steps"][0]["parameters"] = [["speed", 0.2]]
        app["steps"][0]["requires"] = "abc"
        app["digest"] = json_digest({k: v for k, v in app.items() if k != "digest"})
        status, body = http(self.url, "/v1/validate", {"application": app})
        self.assertEqual(status, 200, body)
        failed = {c["name"] for c in body["cases"] if not c["passed"]}
        self.assertTrue(any("参数有效" in n for n in failed), failed)
        self.assertTrue(any("能力需求格式有效" in n for n in failed), failed)
        self.assertFalse(any("." == c for c in body["cases"]))

    def test_failed_step_stops_application(self):
        app = application([step("patrol", {"waypoints": [[1, 0]]}, timeout_s=0.01), step("status_report")])
        report = http(self.url, "/v1/evaluate", {"request_id": "e2", "application": app,
                                                 "hardware_authorized": True})[1]
        self.assertFalse(report["passed"])
        self.assertEqual(len(report["cases"]), 1)       # second step never ran

    def test_deploy_rejected_when_validation_fails(self):
        status, body = http(self.url, "/v1/deploy", {"request_id": "d2", "report_id": "r",
                                                     "application": application([step("nope")])})
        self.assertEqual((status, body["code"]), (409, "validation_failed"))

    def test_models_install_refused_without_capability(self):
        status, body = http(self.url, "/v1/models/install", {"request_id": "m1", "model": {}})
        self.assertEqual((status, body["code"]), (400, "capability_not_provided"))


class BrainosPlatformTests(unittest.TestCase):
    """The unmodified desktop control plane (brainos/service.py) talks to this runtime."""

    def setUp(self):
        try:
            from brainos.service import Platform
        except Exception as exc:          # pragma: no cover - depends on host
            self.skipTest(f"brainos 无法导入：{exc}")
        self.tmp = tempfile.TemporaryDirectory()
        self.platform = Platform(self.tmp.name)
        self.fx = RuntimeFixture(http=True, time_scale=20.0)
        self.fx.install(example("status_report"))
        self.fx.install(example("patrol"))

    def tearDown(self):
        self.platform.close()
        self.fx.close()
        self.tmp.cleanup()

    def test_simulated_body_flow(self):
        """Runtime side of the v1 delivery flow on a simulated body.

        Asserts what the runtime is responsible for: the desktop connects over
        v1, the on-site test runs on the robot and every result is labelled as
        simulation. Since iter01-fix the contract (INTERFACES_V1 §1.1, invariant
        4) forbids using simulation evidence for deploy/start; how the desktop
        records it is the desktop's responsibility, so this test only asserts
        the invariant (never accepted as on-site evidence), not the details.
        """
        p = self.platform
        project = p.add_project({"name": "跨本体", "objective": "在仿真四足上跑状态报告与巡逻"})
        robot = p.add_robot({"project_id": project["id"], "name": "SimQuadruped", "family": "quadruped",
                             "endpoint": self.fx.url, "token": _support.TOKEN})
        connected = p.connect_robot({"robot_id": robot["id"]})
        self.assertEqual(connected["status"], "connected", connected.get("error"))
        self.assertIn("base.navigate_to", connected["capabilities"])
        self.assertEqual(connected["manifest"]["instance_id"], "sim-quadruped-01")
        self.assertIs(connected["manifest"].get("simulated"), True)     # v1 view carries the flag

        report_skill = p.add_skill({"project_id": project["id"], "name": "状态报告", "version": "0.1.0",
                                    "requires": ["system.health"], "entrypoint": "status_report",
                                    "success_condition": "报告生成", "stop_condition": "超时", "model_ids": []})
        patrol_skill = p.add_skill({"project_id": project["id"], "name": "巡逻", "version": "0.1.0",
                                    "requires": ["base.navigate_to"], "entrypoint": "patrol@0.1.0",
                                    "success_condition": "到达全部航点", "stop_condition": "超时或停止",
                                    "model_ids": []})
        app = p.add_application({"project_id": project["id"], "name": "巡检", "version": "1.0",
                                 "robot_id": robot["id"], "steps": [
                                     {"skill_id": report_skill["id"], "parameters": {}, "timeout_s": 30},
                                     {"skill_id": patrol_skill["id"], "parameters": {"waypoints": [[1, 0], [1, 1]]},
                                      "timeout_s": 120}]})
        try:
            op = p.onsite_test({"application_id": app["id"], "hardware_authorized": True})
        except Exception as exc:              # a desktop may refuse simulated bodies up front
            op = {"status": "refused", "error": str(exc)}

        # Runtime side: if the desktop asked for an evaluation, the runtime ran it
        # and labelled the answer as simulation (never device_run).
        evaluations = [r for r in self.fx.runtime.store.list("requests") if r["operation"] == "v1.evaluate"]
        for record in evaluations:
            if record["status"] == "completed":
                response = record["response"]
                if "evaluation_id" in response:    # async on-site test (INTERFACES_V1 §4.1)
                    view = wait_until(lambda: (v := self.fx.runtime.get_evaluation(response["evaluation_id"]))
                                      ["status"] != "running" and v, 30)
                    self.assertEqual(view["status"], "completed", view)
                    response = view["result"]
                self.assertIs(response["simulated"], True)
                self.assertEqual(response["evidence_kind"], "simulation")
                self.assertTrue(response["evidence_ref"].startswith("simulation://"))

        # Invariant 4: a simulated result is never usable as on-site evidence.
        report = p.store.get("evaluations", op["report_id"]) if op.get("report_id") else None
        if report is not None:
            self.assertFalse(report.get("scope") == "onsite" and report.get("passed") is True
                             and not str(report.get("evidence_ref", "")).startswith("simulation://")
                             and not (report.get("details") or {}).get("simulated"),
                             f"仿真结果被记成了现场证据：{report}")
            try:
                release = p.deploy({"application_id": app["id"], "report_id": report["id"]})
            except Exception:
                release = {"status": "refused"}
            self.assertNotEqual(release.get("status"), "deployed", "仿真证据不得用于部署")
        self.assertEqual(self.fx.runtime.runner.active_runs(), [])


if __name__ == "__main__":
    unittest.main()
