"""Asynchronous on-site tests (INTERFACES_V1 §4.1)."""
import time
import unittest
from unittest import mock

import _support
from _support import RuntimeFixture, custom_package, example, http, wait_until
from test_v1_compat import application, step

from neuxmind_runtime.core import EdgeRuntime
from neuxmind_runtime.errors import Conflict, EStopLatched, NotAuthorized, NotFound, RuntimeApiError
from neuxmind_runtime.evaluations import STOP_CASE

SYNC_KEYS = {"passed", "status", "error", "application_digest", "robot_manifest_digest", "evidence_ref",
             "evidence_kind", "simulated", "run_id", "cases", "precheck"}

SLEEPER = """
def run(ctx, seconds=1000.0):
    ctx.sleep(seconds)
    return {"slept": seconds}
"""


def finished(fx, evaluation_id, timeout=20.0):
    return wait_until(lambda: (v := fx.runtime.get_evaluation(evaluation_id))["status"] != "running" and v,
                      timeout)


class AsyncEvaluateTests(unittest.TestCase):
    def setUp(self):
        self.fx = RuntimeFixture("quadruped", time_scale=20.0)
        self.fx.install(example("status_report"))
        self.fx.install(example("patrol"))
        self.fx.install(custom_package("sleeper", SLEEPER, timeout_s=86400))
        self.app = application([step("status_report"),
                                step("patrol", {"waypoints": [[1, 0]]}, requires=("base.navigate_to",))])
        self.long_app = application([step("patrol", {"waypoints": [[150, 0]]}, requires=("base.navigate_to",),
                                          timeout_s=3600)], name="long")

    def tearDown(self):
        self.fx.close()

    def evaluate(self, app, request_id, **extra):
        return self.fx.runtime.v1_evaluate({"request_id": request_id, "application": app,
                                            "hardware_authorized": True, "async": True, **extra})

    def test_returns_at_once_and_result_matches_sync_shape(self):
        started = time.monotonic()
        accepted = self.evaluate(self.long_app, "a-long")
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual((accepted["accepted"], accepted["status"]), (True, "running"))
        self.assertTrue(accepted["evaluation_id"].startswith("evaluation-"))
        self.assertEqual(self.fx.runtime.get_evaluation(accepted["evaluation_id"])["status"], "running")
        self.fx.runtime.runner.cancel(accepted["run_id"])
        self.fx.runtime.runner.wait(accepted["run_id"], 5)

        accepted = self.evaluate(self.app, "a-1")
        view = finished(self.fx, accepted["evaluation_id"])
        self.assertEqual(view["status"], "completed")
        result = view["result"]
        sync = self.fx.runtime.v1_evaluate({"request_id": "s-1", "application": self.app,
                                            "hardware_authorized": True})
        self.assertEqual(set(result), SYNC_KEYS)
        self.assertEqual(set(result), set(sync))
        self.assertTrue(result["passed"])
        self.assertEqual((result["simulated"], result["evidence_kind"]), (True, "simulation"))
        self.assertTrue(result["evidence_ref"].startswith("simulation://"))
        self.assertEqual(result["application_digest"], self.app["digest"])
        self.assertEqual(result["robot_manifest_digest"], sync["robot_manifest_digest"])
        self.assertEqual([c["passed"] for c in result["cases"]], [True, True])
        self.assertEqual(result["run_id"], accepted["run_id"])

    def test_idempotent(self):
        first = self.evaluate(self.long_app, "idem")
        again = self.evaluate(self.long_app, "idem")
        self.assertEqual(first["evaluation_id"], again["evaluation_id"])
        with self.assertRaises(RuntimeApiError) as ctx:
            self.evaluate(self.app, "idem")                       # same id, other content
        self.assertEqual(ctx.exception.code, "request_id_reused")
        with self.assertRaises(RuntimeApiError) as ctx:          # sync with the same id is other content too
            self.fx.runtime.v1_evaluate({"request_id": "idem", "application": self.long_app,
                                         "hardware_authorized": True})
        self.assertEqual(ctx.exception.code, "request_id_reused")
        self.assertEqual(len(self.fx.runtime.store.list("evaluations")), 1)
        self.fx.runtime.runner.cancel(first["run_id"])

    def test_stop_ends_evaluation_immediately_as_failed(self):
        stubborn = custom_package("stubborn", "import time\ndef run(ctx):\n    while True:\n        try:\n"
                                  "            time.sleep(0.01)\n        except BaseException:\n            pass\n",
                                  requires=("base.velocity",))
        self.fx.install(stubborn)
        app = application([step("stubborn", requires=("base.velocity",), timeout_s=3600)], name="stubborn")
        accepted = self.evaluate(app, "stop-me")
        run = self.fx.runtime.runner.live_run(accepted["run_id"])
        wait_until(lambda: run.sandbox is not None and run.sandbox.pid is not None, 10)
        self.fx.runtime.emergency_stop("operator stop")
        view = self.fx.runtime.get_evaluation(accepted["evaluation_id"])     # no waiting
        self.assertEqual(view["status"], "completed")
        result = view["result"]
        self.assertFalse(result["passed"])
        self.assertEqual(result["status"], "stopped")
        stop_cases = [c for c in result["cases"] if c["name"] == STOP_CASE]
        self.assertEqual(len(stop_cases), 1)
        self.assertFalse(stop_cases[0]["passed"])
        self.assertIn("operator stop", stop_cases[0]["detail"])
        self.assertFalse(run.finalized)          # the skill process is still in its grace period
        # Written once: the later finish hook does not change it.
        self.fx.runtime.runner.wait(accepted["run_id"], 5)
        self.assertTrue(run.finalized)
        self.assertEqual(self.fx.runtime.get_evaluation(accepted["evaluation_id"])["result"], result)

    def test_stop_without_polling_is_recorded_by_finish_hook(self):
        accepted = self.evaluate(self.long_app, "hook")
        wait_until(lambda: self.fx.adapter.goal is not None, 10)
        self.fx.runtime.emergency_stop("x")
        self.fx.runtime.runner.wait(accepted["run_id"], 5)
        record = self.fx.runtime.store.get("evaluations", accepted["evaluation_id"])
        self.assertEqual(record["status"], "completed")
        self.assertFalse(record["result"]["passed"])

    def test_cancel_and_timeout_fail(self):
        accepted = self.evaluate(self.long_app, "cancel")
        wait_until(lambda: self.fx.adapter.goal is not None, 10)
        self.fx.runtime.runner.cancel(accepted["run_id"])
        result = self.fx.runtime.get_evaluation(accepted["evaluation_id"])["result"]
        self.assertFalse(result["passed"])
        self.assertIn("取消", [c for c in result["cases"] if c["name"] == STOP_CASE][0]["detail"])
        self.fx.runtime.runner.wait(accepted["run_id"], 5)

        short = application([step("sleeper", timeout_s=1)], name="short")
        accepted = self.evaluate(short, "timeout")
        view = finished(self.fx, accepted["evaluation_id"], 10)
        self.assertFalse(view["result"]["passed"])

    def test_latched_at_submission_completes_failed_without_running(self):
        self.fx.runtime.emergency_stop("latched before")
        accepted = self.evaluate(self.app, "latched")
        self.assertEqual((accepted["accepted"], accepted["status"], accepted["run_id"]), (True, "completed", None))
        result = self.fx.runtime.get_evaluation(accepted["evaluation_id"])["result"]
        self.assertFalse(result["passed"])
        self.assertIn(STOP_CASE, [c["name"] for c in result["cases"]])
        self.assertTrue(result["evidence_ref"].startswith("simulation://"))
        self.assertEqual(self.fx.runtime.runner.active_runs(), [])

    def test_stop_between_check_and_admission(self):
        with mock.patch.object(self.fx.runtime.runner, "submit_application",
                               side_effect=EStopLatched("x", code="stale_after_stop")):
            accepted = self.evaluate(self.app, "race")
        self.assertEqual(accepted["status"], "completed")
        self.assertFalse(self.fx.runtime.get_evaluation(accepted["evaluation_id"])["result"]["passed"])

    def test_other_precheck_failures_are_rejected_like_sync(self):
        bad = application([step("not_installed")])
        for _ in range(2):                                     # replayed, not re-executed
            with self.assertRaises(RuntimeApiError) as ctx:
                self.evaluate(bad, "bad")
            self.assertEqual((ctx.exception.status, ctx.exception.code), (409, "validation_failed"))
        with self.assertRaises(NotAuthorized):
            self.fx.runtime.v1_evaluate({"request_id": "na", "application": self.app, "async": True})
        self.assertEqual(self.fx.runtime.store.list("evaluations"), [])

    def test_unexpected_admission_error_leaves_no_running_record(self):
        with mock.patch.object(self.fx.runtime.runner, "submit_application", side_effect=Conflict("busy")):
            with self.assertRaises(Conflict):
                self.evaluate(self.app, "busy")
        statuses = [r["status"] for r in self.fx.runtime.store.list("evaluations")]
        self.assertEqual(statuses, ["rejected"])

    def test_async_flag_must_be_boolean_and_sync_is_default(self):
        with self.assertRaises(RuntimeApiError) as ctx:
            self.evaluate(self.app, "flag", **{"async": "yes"})
        self.assertEqual(ctx.exception.code, "bad_request")
        sync = self.fx.runtime.v1_evaluate({"request_id": "plain", "application": self.app,
                                            "hardware_authorized": True})
        self.assertNotIn("evaluation_id", sync)
        self.assertTrue(sync["passed"])

    def test_unknown_evaluation(self):
        with self.assertRaises(NotFound):
            self.fx.runtime.get_evaluation("evaluation-nope")

    def test_internal_rejected_status_is_not_exposed(self):
        """Q2-05: a discarded (internal "rejected") evaluation maps to 404, not a 4th public state."""
        ev = self.fx.runtime.evaluations.create("evaluation-xyz", "rid", "digest", [])
        self.fx.runtime.evaluations.discard("evaluation-xyz")
        self.assertEqual(self.fx.runtime.store.get("evaluations", "evaluation-xyz")["status"], "rejected")
        with self.assertRaises(NotFound) as ctx:
            self.fx.runtime.get_evaluation("evaluation-xyz")
        self.assertEqual(ctx.exception.code, "evaluation_not_found")


class AsyncEvaluateRestartTests(unittest.TestCase):
    def test_unfinished_becomes_unknown_and_is_not_replayed(self):
        fx = RuntimeFixture("quadruped", time_scale=1.0)
        restarted = None
        try:
            fx.install(custom_package("sleeper", SLEEPER, timeout_s=86400))
            fx.install(example("status_report"))
            done = fx.runtime.v1_evaluate({"request_id": "done", "hardware_authorized": True, "async": True,
                                           "application": application([step("status_report")])})
            finished(fx, done["evaluation_id"])
            payload = {"request_id": "pending", "hardware_authorized": True, "async": True,
                       "application": application([step("sleeper", timeout_s=86400)], name="pending")}
            pending = fx.runtime.v1_evaluate(payload)

            restarted = EdgeRuntime(_support.SimQuadruped(), fx.state_dir, start_monitor=False)
            view = restarted.get_evaluation(pending["evaluation_id"])
            self.assertEqual(view["status"], "unknown")
            self.assertIn("不会自动重放", view["error"])
            self.assertNotIn("result", view)
            again = restarted.v1_evaluate(payload)                 # same request → same evaluation
            self.assertEqual(again["evaluation_id"], pending["evaluation_id"])
            self.assertEqual(restarted.runner.active_runs(), [])  # nothing replayed
            self.assertEqual(restarted.get_evaluation(done["evaluation_id"])["status"], "completed")
            self.assertTrue(restarted.get_evaluation(done["evaluation_id"])["result"]["passed"])
            # Telemetry persists across restarts (§5.2): only this boot's events (review G).
            this_boot = restarted.events.since(restarted.events.first_seq - 1, 10 ** 4)
            started = [e for e in this_boot if e["type"] == "runtime.started"][0]
            self.assertEqual(started["unknown_evaluations"], [pending["evaluation_id"]])
        finally:
            if restarted is not None:
                restarted.close()
            fx.close()


class AsyncEvaluateHttpTests(unittest.TestCase):
    def test_http_flow(self):
        fx = RuntimeFixture("quadruped", http=True)
        try:
            fx.install(example("status_report"))
            app = application([step("status_report")])
            status, accepted = http(fx.url, "/v1/evaluate", {"request_id": "h1", "application": app,
                                                             "hardware_authorized": True, "async": True})
            self.assertEqual(status, 200, accepted)
            path = f"/v1/evaluations/{accepted['evaluation_id']}"
            view = wait_until(lambda: (v := http(fx.url, path)[1])["status"] != "running" and v, 20)
            self.assertEqual(view["status"], "completed")
            self.assertTrue(view["result"]["passed"])
            self.assertEqual(http(fx.url, "/v1/evaluations/evaluation-missing")[0], 404)
            self.assertEqual(http(fx.url, path, token="wrong-token-0123456789abcdef")[0], 401)
            # Same request again → same evaluation id (idempotent over HTTP).
            self.assertEqual(http(fx.url, "/v1/evaluate", {"request_id": "h1", "application": app,
                                                           "hardware_authorized": True, "async": True})[1],
                             accepted)
        finally:
            fx.close()


if __name__ == "__main__":
    unittest.main()
