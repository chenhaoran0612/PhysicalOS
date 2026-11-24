"""Issue #43: the runtime's link against the real cloud application, both in this process.

The cloud is the FastAPI app the cloud's own tests build (``cloud/tests/skill_support.SkillCase``:
an organisation, a robot key with ``device:link``, a third-party key with ``skills:invoke``); the
robot is a runtime on the simulated quadruped whose link sends its syncs straight into that app. So
the two ends of ``POST /device-link/sync`` are checked against each other, not against a stand-in.
Skipped where the cloud's dependencies are not installed (the runtime itself needs none).
"""
from __future__ import annotations

import sys
import time
import unittest

import _support
from _support import REPO_ROOT, RuntimeFixture, example

# At the END of the path: cloud/tests has modules named like some of the runtime's (test_gateway,
# test_conformance); the runtime's own must keep winning for every module imported after this one.
CLOUD_TESTS = str(REPO_ROOT / "cloud" / "tests")
if CLOUD_TESTS not in sys.path:
    sys.path.append(CLOUD_TESTS)
try:
    import support as cloud_support
    from skill_support import SkillCase
    SKIP_REASON = None
except ImportError as exc:                       # no fastapi / sqlalchemy here
    cloud_support, SkillCase = None, unittest.TestCase
    sys.path.remove(CLOUD_TESTS)
    SKIP_REASON = f"云平台的依赖没有安装，跳过和真实云平台应用的对接测试：{exc}"

from neuxmind_runtime.alert_report import SendError  # noqa: E402
from neuxmind_runtime.cloud_link import SYNC_PATH, CloudLink  # noqa: E402
from neuxmind_runtime.sandbox import INPROCESS  # noqa: E402

from alert_support import FakeClock  # noqa: E402
from link_support import CONFIG, linger  # noqa: E402

assert cloud_support is None or SYNC_PATH == cloud_support.API + "/device-link/sync"


class AppTransport:
    """``HttpsTransport.send`` without the socket: the request goes into the cloud app."""

    def __init__(self, case, key: dict):
        self._case, self._key = case, key
        self.requests: list[dict] = []
        self.answers: list[dict] = []

    def send(self, request: dict) -> dict:
        self.requests.append(request)
        response = self._case.client.post(SYNC_PATH, json=request, headers=self._key["headers"])
        if response.status_code != 200:
            raise SendError(f"云平台答复 {response.status_code}（{response.json().get('code')}）",
                            status=response.status_code)
        self.answers.append(response.json())
        return self.answers[-1]


@unittest.skipIf(SKIP_REASON, SKIP_REASON)
class EndToEnd(SkillCase):
    def setUp(self):
        super().setUp()
        self.fx = RuntimeFixture("quadruped", time_scale=40.0, sandbox=INPROCESS)
        self.addCleanup(self.fx.close)
        self.runtime = self.fx.runtime
        self.fx.install(example("goto_place"))
        self.fx.install(linger())
        self.ticks = FakeClock()
        self.transport = AppTransport(self, self.robot_key)
        self.link = self.make_link(self.transport)

    def make_link(self, transport) -> CloudLink:
        link = CloudLink(self.runtime, CONFIG, clock=self.ticks, wall=self.clock, transport=transport)
        self.runtime.remote_access.link = link
        self.addCleanup(link.close)
        return link

    def robot_sync(self) -> dict:
        sent = len(self.transport.requests)
        self.ticks.advance(31.0)
        self.clock.advance(1.0)                  # the cloud's clock: a sync a second, as while a call runs
        self.link.tick()
        self.assertEqual(len(self.transport.requests), sent + 1)
        self.assertEqual(self.link.describe()["last_error"], None)
        return self.transport.answers[-1]

    def allow(self, skill_id: str, allowed: bool = True) -> None:
        digest = self.runtime.remote_access.installed()[skill_id]["digest"]
        self.runtime.remote_access.set({"skill_id": skill_id, "allowed": allowed, "set_by": "王工",
                                        "hardware_authorized": True, "digest": digest})

    def until(self, call_id: str, statuses, timeout: float = 20.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.robot_sync()
            call = self.seen(call_id)
            if call["status"] in statuses:
                return call
            time.sleep(0.02)
        raise AssertionError(f"{call_id}: {self.seen(call_id)}")

    def test_a_third_party_call_runs_on_the_robot_and_its_result_is_read_from_the_cloud(self):
        device_id = self.robot_sync()["device_id"]
        self.assertEqual(self.link.describe()["skills_rejected"], [], "the cloud takes every skill entry as sent")
        robots = self.ok(self.get("/open/robots", headers=self.caller["headers"]))["robots"]
        self.assertEqual([(r["device_id"], r["instance_id"], r["online"], r["estop_latched"]) for r in robots],
                         [(device_id, "sim-quadruped-01", True, False)])
        skills = self.ok(self.get(f"/open/robots/{device_id}/skills", headers=self.caller["headers"]))["skills"]
        self.assertEqual({s["id"]: (s["remote_allowed"], s["motion"]) for s in skills},
                         {"goto_place": (False, True), "linger": (False, False)})
        self.error(self.call_raw(device_id), 403, "remote_not_allowed")         # the owner has not allowed it

        self.allow("goto_place")
        self.robot_sync()
        self.error(self.call_raw(device_id, parameters={"place": "天台"}), 400, "invalid_parameters")
        call = self.call(device_id, parameters={"place": "二楼"})
        done = self.until(call["id"], ("succeeded", "failed", "rejected"))
        self.assertEqual((done["status"], done["error"], done["progress"]), ("succeeded", None, 1.0))
        self.assertEqual(done["result"]["place"], "二楼")
        run = self.runtime.runner.get(done["run_id"]) if done.get("run_id") else self.runtime.runner.list()[0]
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(len(self.runtime.runner.list()), 1)
        self.robot_sync()
        self.assertEqual(self.transport.requests[-1]["calls"], [], "settled by the cloud")
        self.assertEqual(self.link.describe()["device_id"], device_id)

    def test_a_cancel_and_a_stop_from_the_third_party_reach_the_robot(self):
        self.allow("linger")
        self.allow("goto_place")
        device_id = self.robot_sync()["device_id"]
        call = self.call(device_id, skill_id="linger", parameters={})
        self.until(call["id"], ("running",))
        self.ok(self.post(f"/open/calls/{call['id']}/cancel", headers=self.caller["headers"]))
        self.assertEqual(self.until(call["id"], ("cancelled", "succeeded", "failed"))["status"], "cancelled")

        stop = self.ok(self.post(f"/open/robots/{device_id}/stop", {"reason": "调度员叫停"},
                                 headers=self.caller["headers"]), 202)
        self.robot_sync()
        self.assertTrue(self.runtime.supervisor.latched)
        self.assertIn("调度员叫停", self.runtime.supervisor.latch_reason)
        count = self.runtime.supervisor.stop_count
        self.robot_sync()                                        # carries the acknowledgement
        self.assertEqual(self.transport.requests[-1]["stops"], [stop["stop_id"]])
        self.assertIsNone(self.transport.answers[-1]["stop"])
        self.robot_sync()
        self.assertEqual((self.transport.requests[-1]["stops"], self.runtime.supervisor.stop_count), ([], count))
        self.error(self.call_raw(device_id, request_id="req-2"), 409, "estop_latched")

    def test_a_switch_turned_off_on_the_robot_refuses_a_call_the_cloud_still_let_through(self):
        self.allow("goto_place")
        device_id = self.robot_sync()["device_id"]
        call = self.call(device_id)                              # the cloud still believes it is allowed
        self.allow("goto_place", False)
        done = self.until(call["id"], ("rejected", "succeeded", "failed"))
        self.assertEqual((done["status"], done["error"]["code"]), ("rejected", "remote_not_allowed"))
        self.assertEqual(self.runtime.runner.list(), [])

    def test_a_key_without_the_scope_leaves_the_link_in_error(self):
        link = self.make_link(AppTransport(self, self.caller))   # skills:invoke only
        link.tick()
        view = link.describe()
        self.assertEqual(view["state"], "error")
        self.assertIn("device:link", view["last_error"])


if __name__ == "__main__":
    unittest.main()
