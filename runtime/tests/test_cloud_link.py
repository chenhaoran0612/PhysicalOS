"""Issue #43: the robot fetches third-party calls from the cloud (``cloud_link.py``,
``remote_calls.py``), on the simulated quadruped with the cloud replaced by ``link_support.FakeCloud``.

From a call arriving to its result going back; every way a call is refused; cancel; stop and latch;
the same call delivered twice; a restart in the middle; the switch turned off; answers that are
wrong; a cloud that cannot be reached or does not take the key; a disk that does not take writes.
"""
from __future__ import annotations

import json
import math
import sys
import unittest
from unittest import mock

from link_support import CONFIG, CLOUD_KEY, LinkCase, linger

from _support import custom_package, wait_until
from neuxmind_runtime import remote_calls
from neuxmind_runtime.adapters import sim_quadruped
from neuxmind_runtime.alert_report import ReportConfig, SendError
from neuxmind_runtime.cloud_link import CloudLink, read_answer, read_call
from neuxmind_runtime.remote_access import STORE_KIND
from neuxmind_runtime.store import Store


class LifeCycle(LinkCase):
    def test_a_call_is_fetched_run_once_and_its_result_goes_back(self):
        self.install("goto_place")
        self.allow("goto_place")
        first = self.sync()
        self.assertEqual(first["schema"], "neuxmind.device_link.v1")
        self.assertEqual(first["device"]["instance_id"], "sim-quadruped-01")
        self.assertEqual(first["state"], {"estop_latched": False, "busy": False, "motion_blocked": False})
        (skill,) = first["skills"]
        self.assertEqual((skill["id"], skill["version"], skill["motion"], skill["timeout_s"]),
                         ("goto_place", "0.1.0", True, 1800))
        self.assertEqual(skill["requires"], ["base.navigate_to_place", "safety.stop"])
        self.assertEqual([p["name"] for p in skill["parameters"]], ["place", "max_speed"])
        self.assertEqual((skill["remote"]["allowed"], skill["remote"]["set_by"]), (True, "王工"))
        self.assertEqual((first["calls"], first["stops"]), ([], []))

        call_id = self.cloud.call("goto_place", {"place": "会议室"})
        self.sync()                                              # the answer to this one carries the call
        report = self.sync_until(call_id, ("running", "succeeded"))
        self.assertTrue(report["run_id"].startswith("run-"))
        report = self.sync_until(call_id, "succeeded")
        self.assertEqual((report["result"]["place"], report["error"], report["progress"]), ("会议室", None, 1.0))
        x, y = sim_quadruped.PLACES["会议室"]
        pose = self.runtime.bus.read("base.odometry")["pose"]
        self.assertLess(math.hypot(pose[0] - x, pose[1] - y), 0.2)
        self.assertEqual(self.sync()["calls"], [], "settled by the cloud: no longer reported")
        self.assertEqual(len(self.runtime.runner.list()), 1, "one run")

        view = self.access.view()
        self.assertEqual(view["link"]["state"], "online")
        self.assertEqual(view["link"]["device_id"], "dlk_test0001")
        (shown,) = view["calls"]
        self.assertEqual((shown["call_id"], shown["skill_id"], shown["status"], shown["run_id"], shown["error"]),
                         (call_id, "goto_place", "succeeded", report["run_id"], None))
        (record,) = self.operations("remote.call")
        self.assertEqual((record["call_id"], record["target"], record["switch_set_by"], record["status"],
                          record["outcome"], record["run_id"]),
                         (call_id, "goto_place", "王工", "succeeded", "ok", report["run_id"]))
        self.assertIn("会议室", record["parameters_summary"])
        self.assertEqual(len(record["parameters_digest"]), 64)

    def test_a_running_call_is_reported_with_its_progress(self):
        self.install("linger")
        self.allow("linger")
        call_id = self.cloud.call("linger")
        self.sync()
        report = self.sync_until(call_id, "running")
        wait_until(lambda: self.runtime.runner.get(report["run_id"])["progress"] == 0.4)
        report = self.sync()["calls"][0]
        self.assertEqual((report["status"], report["progress"], report["detail"], report["result"]),
                         ("running", 0.4, "走到一半", None))
        self.assertEqual(self.runtime.status()["cloud_link"]["active_call"], call_id)

    def test_the_call_id_is_on_disk_before_the_run_starts(self):
        self.install("linger")
        self.allow("linger")
        call_id = self.cloud.call("linger")
        seen_at_start = []
        real = self.runtime.run_skill

        def run_skill(skill, payload):
            stored = Store(self.fx.state_dir)                    # another connection: what a restart would find
            try:
                seen_at_start.append(call_id in stored.get(STORE_KIND, remote_calls.CALLS_ID)["seen"])
            finally:
                stored.close()
            self.assertIs(payload["hardware_authorized"], True)
            self.assertEqual(payload["request_id"], "remote-" + call_id)
            return real(skill, payload)

        with mock.patch.object(self.runtime, "run_skill", run_skill):
            self.sync()
        self.assertEqual(seen_at_start, [True])

    def test_the_same_call_delivered_again_and_again_runs_once(self):
        self.install("linger")
        self.allow("linger")
        call_id = self.cloud.call("linger")
        self.cloud.reply = lambda _request, answer: {**answer, "calls": list(self.cloud.queue), "settled": []}
        for _ in range(4):                                       # the cloud never hears us: it keeps sending
            self.sync()
        self.assertEqual(len(self.runtime.runner.list()), 1)
        self.runtime.runner.cancel(self.cloud.states[call_id]["run_id"])
        self.sync_until(call_id, "cancelled")
        for _ in range(3):                                       # and after it has ended as well
            self.sync()
        self.assertEqual(len(self.runtime.runner.list()), 1)
        self.assertEqual(self.cloud.status(call_id), "cancelled")

    def test_a_cancel_from_the_cloud_ends_the_run(self):
        self.install("linger")
        self.allow("linger")
        call_id = self.cloud.call("linger")
        self.sync()
        self.sync_until(call_id, "running")
        self.cloud.cancel.append(call_id)
        report = self.sync_until(call_id, "cancelled")
        self.assertEqual(report["error"]["code"], "cancelled")
        self.assertEqual(self.runtime.runner.get(report["run_id"])["status"], "stopped")
        self.assertFalse(self.runtime.supervisor.latched, "a cancel is not a stop")

    def test_a_call_cancelled_before_it_arrived_never_runs(self):
        self.install("linger")
        self.allow("linger")
        call_id = self.cloud.call("linger")
        self.cloud.cancel.append(call_id)                        # both in the same answer
        self.sync()
        self.assertEqual(self.sync_until(call_id, "cancelled")["run_id"], None)
        self.assertEqual(self.runtime.runner.list(), [])

    def test_one_remote_call_at_a_time(self):
        self.install("linger", "status_report")
        self.allow("linger")
        self.allow("status_report")
        first, second = self.cloud.call("linger"), self.cloud.call("status_report")
        self.sync()
        report = self.sync_until(second, "rejected")
        self.assertEqual(report["error"]["code"], "device_busy")
        self.assertIn(first, report["error"]["message"])
        self.assertEqual(self.cloud.status(first), "running")

    def test_a_failing_skill_is_reported_failed_with_its_error(self):
        self.fx.install(custom_package("broken", "def run(ctx):\n    raise RuntimeError('传感器没有数据')\n"))
        self.allow("broken")
        call_id = self.cloud.call("broken")
        self.sync()
        report = self.sync_until(call_id, "failed")
        self.assertEqual(report["error"]["code"], "run_failed")
        self.assertIn("传感器没有数据", report["error"]["message"])

    def test_a_result_that_is_too_large_is_not_sent(self):
        self.fx.install(custom_package("chatty", "def run(ctx):\n    return {'text': 'x' * 100000}\n"))
        self.allow("chatty")
        call_id = self.cloud.call("chatty")
        self.sync()
        report = self.sync_until(call_id, "succeeded")
        self.assertIs(report["result"]["omitted"], True)


@unittest.skipUnless(sys.platform == "linux", "技能进程沙箱只在 Linux 上可用")
class SandboxedLifeCycle(LifeCycle):
    """The same with every skill in its own process, as on a device (Linux only)."""
    SANDBOX = "process"


class Refusals(LinkCase):
    def rejected(self, call_id: str) -> dict:
        self.sync()
        report = self.sync_until(call_id, "rejected")
        self.assertIsNone(report["run_id"])
        self.assertEqual(self.runtime.runner.list(), [], "nothing ran")
        return report["error"]

    def test_a_skill_that_is_not_installed(self):
        self.assertEqual(self.rejected(self.cloud.call("goto_place", {"place": "会议室"}))["code"], "skill_not_installed")

    def test_a_skill_whose_switch_is_off(self):
        self.install("goto_place")
        call_id = self.cloud.call("goto_place", {"place": "会议室"})
        self.assertEqual(self.rejected(call_id)["code"], "remote_not_allowed")
        (record,) = self.operations("remote.call")
        self.assertEqual((record["outcome"], record["code"], record["hardware_authorized"]),
                         ("rejected", "remote_not_allowed", False))

    def test_another_version_than_the_installed_one(self):
        self.install("goto_place")
        self.allow("goto_place")
        call_id = self.cloud.call("goto_place", {"place": "会议室"}, version="9.9.9")
        self.assertEqual(self.rejected(call_id)["code"], "version_mismatch")

    def test_other_content_than_the_installed_one(self):
        self.install("goto_place")
        self.allow("goto_place")
        installed = self.sync()["skills"][0]["digest"]
        call_id = self.cloud.call("goto_place", {"place": "会议室"}, digest="0" * 64)
        self.assertEqual(self.rejected(call_id)["code"], "version_mismatch")
        call_id = self.cloud.call("goto_place", {"place": "会议室"}, digest=installed, version="0.1.0")
        self.sync()
        self.sync_until(call_id, "succeeded")

    def test_parameters_the_skill_does_not_declare_that_way(self):
        self.install("goto_place")
        self.allow("goto_place")
        for bad in ({}, {"place": "天台"}, {"place": "会议室", "max_speed": 9}, {"place": "会议室", "extra": 1}):
            with self.subTest(bad=bad):
                self.assertEqual(self.rejected(self.cloud.call("goto_place", bad))["code"], "invalid_parameters")

    def test_a_call_whose_fields_are_not_what_they_should_be(self):
        self.install("goto_place")
        self.allow("goto_place")
        for fields in ({"parameters": ["会议室"]}, {"timeout_s": -1}, {"timeout_s": "soon"}, {"version": 3}, {"digest": "abc"},
                       {"skill_id": 7}, {"parameters": {"place": "x" * 20000}}):
            with self.subTest(fields=fields):
                call_id = self.cloud.call("goto_place", {"place": "会议室"})
                self.cloud.queue[-1].update(fields)
                self.assertEqual(self.rejected(call_id)["code"], "invalid_parameters")

    def test_while_the_emergency_stop_is_latched(self):
        self.install("goto_place", "status_report")
        self.allow("goto_place")
        self.allow("status_report")
        self.runtime.emergency_stop("现场停止")
        self.assertIs(self.sync()["state"]["estop_latched"], True)
        for skill, parameters in (("goto_place", {"place": "会议室"}), ("status_report", {})):
            with self.subTest(skill=skill):
                self.assertEqual(self.rejected(self.cloud.call(skill, parameters))["code"], "estop_latched")

    def test_an_entry_without_a_usable_id_is_ignored(self):
        self.install("status_report")
        self.allow("status_report")
        junk = [None, 7, {}, {"call_id": "x"}, {"call_id": "cal_" + "a" * 200}, {"call_id": "cal_../etc"},
                {"call_id": ["cal_1"], "skill_id": "status_report"}]
        self.cloud.reply = lambda _request, answer: {**answer, "calls": junk}
        self.sync()
        self.assertEqual(self.sync()["calls"], [])
        self.assertEqual(self.runtime.runner.list(), [])
        self.assertEqual(self.link.describe()["state"], "online")


class BusyBody(LinkCase):
    TIME_SCALE = 1.0

    def test_while_a_local_run_holds_the_motion(self):
        self.install("goto_place", "patrol_places")
        self.allow("goto_place")
        local = self.fx.run("patrol_places")
        try:
            self.assertIs(self.sync()["state"]["busy"], True)
            call_id = self.cloud.call("goto_place", {"place": "会议室"})
            self.sync()
            report = self.sync_until(call_id, "rejected")
            self.assertEqual(report["error"]["code"], "device_busy")
            self.assertEqual(len(self.runtime.runner.list()), 1, "only the local run")
        finally:
            self.runtime.runner.cancel(local)
            self.fx.wait(local, 5)


class Switch(LinkCase):
    def test_turned_off_before_the_call_is_fetched(self):
        self.install("goto_place")
        self.allow("goto_place")
        self.sync()
        call_id = self.cloud.call("goto_place", {"place": "会议室"})     # queued in the cloud ...
        self.allow("goto_place", False, hardware_authorized=False, set_by=None)     # ... the owner turns it off
        self.sync()
        report = self.sync_until(call_id, "rejected")
        self.assertEqual(report["error"]["code"], "remote_not_allowed")
        self.assertEqual(self.runtime.runner.list(), [])

    def test_turned_off_while_the_call_runs_cancels_it(self):
        self.install("linger")
        self.allow("linger")
        call_id = self.cloud.call("linger")
        self.sync()
        report = self.sync_until(call_id, "running")
        self.allow("linger", False)
        wait_until(lambda: self.runtime.runner.get(report["run_id"])["status"] == "stopped")
        report = self.sync_until(call_id, "cancelled")
        self.assertEqual(report["error"]["code"], "remote_not_allowed")
        self.assertIs(self.sync()["skills"][0]["remote"]["allowed"], False)

    def test_turned_off_for_another_skill_leaves_the_call_alone(self):
        self.install("linger", "status_report")
        self.allow("linger")
        self.allow("status_report")
        call_id = self.cloud.call("linger")
        self.sync()
        self.sync_until(call_id, "running")
        self.allow("status_report", False)
        self.assertEqual(self.sync()["calls"][0]["status"], "running")

    def test_a_newer_version_of_the_skill_voids_the_switch(self):
        self.install("linger")
        self.allow("linger")
        self.fx.install(linger("0.2.0", "def run(ctx, seconds=1.0):\n    return {'other': True}\n"))
        report = self.sync()["skills"][0]
        self.assertEqual((report["version"], report["remote"]["allowed"], report["remote"]["set_by"]),
                         ("0.2.0", False, None))
        self.assertIs(self.access.view()["skills"]["linger"]["invalidated"], True)
        call_id = self.cloud.call("linger")
        self.sync()
        self.assertEqual(self.sync_until(call_id, "rejected")["error"]["code"], "remote_not_allowed")
        self.allow("linger")                                     # a person turns it on for the new content
        again = self.cloud.call("linger")
        self.sync()
        self.assertEqual(self.sync_until(again, "succeeded")["result"], {"other": True})


class Stop(LinkCase):
    TIME_SCALE = 1.0

    def stop_answer(self, stop_id: str, **extra):
        return lambda _request, answer: {**answer, "stop": {"stop_id": stop_id, "reason": "调度员叫停"}, **extra}

    def test_a_stop_from_the_cloud_stops_latches_and_is_acknowledged(self):
        self.install("goto_place")
        self.allow("goto_place")
        call_id = self.cloud.call("goto_place", {"place": "会议室"})
        self.sync()
        report = self.sync_until(call_id, "running")
        wait_until(lambda: any(abs(v) > 0.01 for v in self.runtime.bus.read("base.odometry")["velocity"]), 10)
        self.cloud.stop = {"stop_id": "stp_0001", "reason": "调度员叫停"}
        self.sync()
        self.assertTrue(self.runtime.supervisor.latched)
        self.assertIn("调度员叫停", self.runtime.supervisor.latch_reason)
        self.assertEqual(self.runtime.bus.read("base.odometry")["velocity"], [0.0, 0.0, 0.0])
        count = self.runtime.supervisor.stop_count
        request = self.sync()
        self.assertEqual(request["stops"], ["stp_0001"])
        self.assertIs(request["state"]["estop_latched"], True)
        self.assertEqual(self.sync_until(call_id, "stopped")["run_id"], report["run_id"])
        self.assertEqual(self.sync()["stops"], [], "acknowledged: no longer reported")
        self.assertEqual(self.runtime.supervisor.stop_count, count, "once")
        (record,) = self.operations("stop")
        self.assertEqual((record["operator"], record["stopped"]), ("cloud-link", True))
        later = self.cloud.call("goto_place", {"place": "二楼"})
        self.sync()
        self.assertEqual(self.sync_until(later, "rejected")["error"]["code"], "estop_latched")

    def test_a_stop_is_carried_out_once_however_often_it_is_sent_and_after_a_restart(self):
        self.cloud.reply = self.stop_answer("stp_0002")
        self.sync()
        count = self.runtime.supervisor.stop_count
        self.assertTrue(self.runtime.supervisor.latched)
        self.runtime.reset({"hardware_authorized": True})        # released on site
        for _ in range(3):
            self.assertEqual(self.sync()["stops"], ["stp_0002"])
        self.assertEqual(self.runtime.supervisor.stop_count, count)
        self.assertFalse(self.runtime.supervisor.latched, "the old stop did not latch it again")
        self.restart()
        self.sync()
        self.assertFalse(self.runtime.supervisor.latched)
        self.cloud.reply = self.stop_answer("stp_0003")          # a new one is a new stop
        self.sync()
        self.assertTrue(self.runtime.supervisor.latched)

    def test_a_stop_in_an_answer_that_is_otherwise_wrong_is_still_carried_out(self):
        for index, extra in enumerate(({"accepted": False}, {"calls": "none"}, {"cancel": 7},
                                       {"calls": [{}] * 50}, {"accepted": None, "poll_after_s": "x"})):
            with self.subTest(extra=extra):
                if self.runtime.supervisor.latched:
                    self.runtime.reset({"hardware_authorized": True})
                self.cloud.reply = self.stop_answer(f"stp_bad{index}", **extra)
                self.sync()
                self.assertTrue(self.runtime.supervisor.latched)
                self.assertEqual(self.link.describe()["state"], "error")
                self.cloud.reply = None
                self.assertIn(f"stp_bad{index}", self.sync()["stops"])

    def test_something_that_is_not_a_stop_stops_nothing(self):
        for stop in ("now", {"stop_id": "now"}, {"stop_id": 5}, {"reason": "x"}, ["stp_1"], {"stop_id": "stp_" + "a" * 99}):
            with self.subTest(stop=stop):
                self.cloud.reply = lambda _request, answer, stop=stop: {**answer, "stop": stop}
                self.sync()
                self.assertFalse(self.runtime.supervisor.latched)
                self.assertEqual(self.link.describe()["state"], "error")


class Restart(LinkCase):
    def test_a_call_that_was_under_way_is_reported_failed_and_never_rerun(self):
        self.install("linger")
        self.allow("linger")
        call_id = self.cloud.call("linger")
        self.sync()
        self.sync_until(call_id, "running")
        # A power cut: the book on disk still says "running", and so does the run's record.
        book = self.runtime.store.get(STORE_KIND, remote_calls.CALLS_ID)
        run = self.runtime.store.get("runs", book["recent"][0]["run_id"])
        self.assertEqual((book["recent"][0]["status"], run["status"]), ("running", "running"))
        self.restart()
        self.runtime.store.put(STORE_KIND, book)
        self.runtime.store.put("runs", {**run, "status": "unknown"})
        self.restart()                                           # this start finds what the power cut left
        self.cloud.states.clear()                                # the cloud never heard: it sends the call again
        self.sync()
        report = self.sync_until(call_id, "failed")
        self.assertEqual(report["error"]["code"], "runtime_restarted")
        self.sync()
        self.assertEqual([r for r in self.runtime.runner.list() if r["status"] in ("queued", "running")], [])
        self.assertEqual(len(self.runtime.runner.list()), 1, "no second run")
        self.assertEqual(self.operations("remote.call")[-1]["code"], "runtime_restarted")

    def test_a_finished_call_is_still_known_after_a_restart(self):
        self.install("status_report")
        self.allow("status_report")
        call_id = self.cloud.call("status_report")
        self.sync()
        self.sync_until(call_id, "succeeded")
        self.restart()
        self.assertEqual(self.access.view()["calls"][0]["call_id"], call_id)
        self.assertIs(self.access.view()["skills"]["status_report"]["allowed"], True, "the switch is kept")
        self.cloud.states.clear()
        self.sync()
        self.assertEqual(self.sync_until(call_id, "succeeded")["error"], None)
        self.assertEqual(len(self.runtime.runner.list()), 1)

    def test_a_damaged_book_still_keeps_its_calls_from_running_again(self):
        self.install("status_report")
        self.allow("status_report")
        self.runtime.store.put(STORE_KIND, {"id": remote_calls.CALLS_ID, "seen": {"cal_old0001": 5.0, "x": "y"},
                                            "recent": ["junk", {"call_id": 7}, {"call_id": "cal_old0002"},
                                                       {"call_id": "cal_old0003", "status": "launching"}]})
        self.restart()
        for call_id in ("cal_old0001", "cal_old0002", "cal_old0003"):
            self.cloud.queue.append({"call_id": call_id, "skill_id": "status_report", "parameters": {}})
        self.sync()
        reports = {r["call_id"]: r for r in self.sync()["calls"]}
        self.assertEqual({r["status"] for r in reports.values()}, {"failed"})
        self.assertEqual({r["error"]["code"] for r in reports.values()}, {"outcome_unknown"})
        self.assertEqual(self.runtime.runner.list(), [])

    def test_only_the_newest_call_ids_are_kept(self):
        book = self.access.calls
        with mock.patch.object(remote_calls, "SEEN_KEPT", 5), mock.patch.object(remote_calls, "RECENT_KEPT", 3):
            for index in range(8):
                book.deliver({"call_id": f"cal_{index:04d}", "skill_id": "nothing", "version": None,
                              "parameters": {}, "timeout_s": None, "problem": None})
            stored = self.runtime.store.get(STORE_KIND, remote_calls.CALLS_ID)
        self.assertEqual(sorted(stored["seen"]), [f"cal_{i:04d}" for i in range(3, 8)])
        self.assertEqual([r["call_id"] for r in stored["recent"]], ["cal_0005", "cal_0006", "cal_0007"])


class Failures(LinkCase):
    def test_a_cloud_that_cannot_be_reached_is_tried_again_more_and_more_slowly(self):
        self.cloud.fail = SendError("连不上云平台：ConnectionRefusedError")
        self.link.tick()
        view = self.link.describe()
        self.assertEqual((view["state"], view["consecutive_failures"]), ("error", 1))
        self.assertIn("连不上", view["last_error"])
        gaps = []
        for _ in range(6):
            sent, waited = len(self.cloud.requests), 0.0
            while len(self.cloud.requests) == sent:
                self.clock.advance(0.5)
                waited += 0.5
                self.link.tick()
            gaps.append(waited)
        self.assertEqual(gaps, [2.0, 4.0, 8.0, 16.0, 30.0, 30.0])
        self.cloud.fail = None
        self.sync()
        view = self.link.describe()
        self.assertEqual((view["state"], view["consecutive_failures"], view["last_error"]), ("online", 0, None))

    def test_the_pace_is_the_clouds_within_limits_and_one_second_while_a_call_runs(self):
        def gap() -> float:
            sent, waited = len(self.cloud.requests), 0.0
            while len(self.cloud.requests) == sent:
                self.clock.advance(0.25)
                waited += 0.25
                self.link.tick()
            return waited

        self.link.tick()
        self.assertEqual(gap(), 2.0)
        for asked, kept in ((0.01, 1.0), (500, 30.0), ("fast", 2.0), (None, 2.0), (float("inf"), 2.0), (True, 2.0)):
            with self.subTest(asked=asked):
                self.cloud.reply = lambda _r, answer, asked=asked: {**answer, "poll_after_s": asked}
                gap()
                self.assertEqual(gap(), kept)
        self.cloud.reply = None
        self.cloud.poll_after_s = 20
        self.install("linger")
        self.allow("linger")
        self.cloud.call("linger")
        gap()                                                    # the answer to this one carries the call
        gap()
        self.assertEqual(gap(), 1.0)

    def test_a_key_without_the_scope_shows_as_an_error_and_is_retried_slowly(self):
        self.cloud.fail = SendError("云平台答复 403（scope_missing）", status=403)
        self.link.tick()
        view = self.runtime.status()["cloud_link"]
        self.assertEqual((view["enabled"], view["state"]), (True, "error"))
        self.assertIn("device:link", view["last_error"])
        self.clock.advance(29.0)
        self.link.tick()
        self.assertEqual(len(self.cloud.requests), 1)
        self.clock.advance(1.0)
        self.link.tick()
        self.assertEqual(len(self.cloud.requests), 2)

    def test_answers_that_are_not_answers_change_nothing(self):
        self.install("status_report")
        self.allow("status_report")
        good = {"call_id": "cal_good0001", "skill_id": "status_report", "parameters": {}}
        for answer in (None, [], "ok", 5, {}, {"accepted": "yes"}, {"accepted": True, "calls": {"a": 1}},
                       {"accepted": True, "calls": [good] * 11}, {"accepted": True, "calls": [good], "cancel": "all"},
                       {"accepted": True, "calls": [good], "settled": list(range(500))},
                       {"accepted": False, "calls": [good]}):
            with self.subTest(answer=answer):
                self.cloud.reply = lambda _r, _a, answer=answer: answer
                self.sync()
                self.assertEqual(self.link.describe()["state"], "error")
                self.assertEqual(self.runtime.runner.list(), [])

    def test_whatever_the_transport_raises_tick_does_not(self):
        for exc in (RuntimeError("boom"), ValueError(CLOUD_KEY), OSError(28, "No space left"), KeyError("x"),
                    MemoryError()):
            with self.subTest(exc=exc):
                self.cloud.fail = exc
                self.sync()
                view = self.link.describe()
                self.assertEqual(view["state"], "error")
                self.assertNotIn(CLOUD_KEY, str(view))

    def test_a_disk_that_takes_no_writes_starts_nothing(self):
        self.install("linger")
        self.allow("linger")
        call_id = self.cloud.call("linger")
        with mock.patch.object(self.runtime.store, "put", side_effect=OSError(28, "No space left on device")):
            self.sync()
            self.assertEqual(self.runtime.runner.list(), [], "not started: its id could not be saved first")
            view = self.link.describe()
            self.assertEqual(view["state"], "error")
            self.assertIn("存储", view["last_error"])
            report = self.sync_until(call_id, "failed")
        self.assertEqual(report["error"]["code"], "state_not_saved")
        self.assertEqual(self.runtime.runner.list(), [])

    def test_a_book_that_cannot_be_read_starts_nothing_until_it_can(self):
        self.install("linger")
        self.allow("linger")
        real_get = Store.get

        def get(store, kind, key):
            if kind == STORE_KIND and key == remote_calls.CALLS_ID:
                raise OSError(5, "Input/output error")
            return real_get(store, kind, key)

        with mock.patch.object(Store, "get", get):
            self.restart()
            call_id = self.cloud.call("linger")
            self.sync()
            self.sync()
            self.assertEqual(self.runtime.runner.list(), [])
            self.assertEqual(self.link.describe()["state"], "error")
        self.sync()
        self.sync_until(call_id, "running")

    def test_a_stop_still_works_while_the_disk_takes_no_writes(self):
        self.cloud.reply = lambda _r, answer: {**answer, "stop": {"stop_id": "stp_disk01"}}
        with mock.patch.object(self.runtime.store, "put", side_effect=OSError(28, "No space left on device")):
            self.sync()
        self.assertTrue(self.runtime.supervisor.latched)
        self.assertEqual(self.sync()["stops"], ["stp_disk01"])


class OnAndOff(LinkCase):
    def test_without_a_configuration_nothing_is_sent(self):
        link = self.make_link(config=None)
        link.tick()
        view = link.describe()
        self.assertEqual((view["configured"], view["enabled"], view["state"]), (False, False, "off"))
        self.assertIn("未配置", view["reason"])
        self.assertEqual(self.cloud.requests, [])
        self.assertIsNone(link.start()._thread)

    def test_link_false_in_the_configuration_keeps_it_off(self):
        link = self.make_link(config=ReportConfig(url="https://cloud.invalid", key=CLOUD_KEY, link=False))
        link.tick()
        view = link.describe()
        self.assertEqual((view["configured"], view["enabled"], view["state"]), (True, False, "off"))
        self.assertIn("link", view["reason"])
        self.assertEqual(self.cloud.requests, [])

    def test_a_runtime_that_is_not_served_has_no_link(self):
        self.access.link = None
        view = self.runtime.status()["cloud_link"]
        self.assertEqual((view["configured"], view["enabled"], view["state"]), (False, False, "off"))

    def test_status_never_shows_the_key(self):
        self.sync()
        view = self.runtime.status()["cloud_link"]
        self.assertEqual((view["state"], view["host"], view["syncs"]), ("online", "cloud.invalid", 1))
        self.assertNotIn(CLOUD_KEY, str(self.runtime.status()))

    def test_a_link_whose_thread_is_stuck_is_not_online(self):
        self.sync()
        self.wall.advance(120.0)
        view = self.link.describe()
        self.assertEqual(view["state"], "error")
        self.assertIn("没有和云平台同步", view["last_error"])

    def test_the_thread_syncs_by_itself(self):
        import time as real_time
        link = CloudLink(self.runtime, CONFIG, transport=self.cloud, loop_s=0.01)
        self.addCleanup(link.close)
        link.start()
        wait_until(lambda: len(self.cloud.requests) >= 1)
        link.close()
        sent = len(self.cloud.requests)
        real_time.sleep(0.1)
        self.assertEqual(len(self.cloud.requests), sent, "closed: nothing more")


class SkillEntries(LinkCase):
    def test_a_skill_with_long_text_is_shortened_not_dropped_and_the_status_says_so(self):
        parameters = [{"name": f"p{i}", "type": "float", "min": 0, "max": 5, "title": "参数", "description": "说" * 400}
                      for i in range(12)]
        self.fx.install({**custom_package("wordy", "def run(ctx, **kw):\n    return {}\n", parameters=parameters),
                         "description": "长" * 1500, "title": "话多"})
        self.install("status_report")
        request = self.sync()
        wordy, other = request["skills"][1], request["skills"][0]
        self.assertLessEqual(len(json.dumps(wordy, ensure_ascii=False).encode()), 8 * 1024)
        self.assertEqual([(p["name"], p["type"], p["min"], p["max"]) for p in wordy["parameters"]],
                         [(f"p{i}", "float", 0, 5) for i in range(12)])
        self.assertEqual(other["id"], "status_report")
        self.assertIn("任何本体都能运行", other["description"], "a skill that fits is sent as it is")
        self.assertEqual(self.link.describe()["skills_trimmed"], ["wordy"])

    def test_skills_the_cloud_refuses_are_shown_and_nothing_breaks(self):
        self.install("status_report")
        self.cloud.reply = lambda _r, answer: {**answer, "server_time": 1.0, "something_new": {"a": 1},
                                               "rejected_skills": [{"index": 0, "id": "status_report", "error": "太大"},
                                                                   "junk", {"index": "x", "id": 5, "error": None}]}
        self.sync()
        view = self.link.describe()
        self.assertEqual(view["state"], "online")
        self.assertEqual(view["skills_rejected"], [{"id": "status_report", "error": "太大"}, {"id": None, "error": ""}])
        for junk in ("all", {"a": 1}, None):
            self.cloud.reply = lambda _r, answer, junk=junk: {**answer, "rejected_skills": junk}
            self.sync()
            self.assertEqual(self.link.describe()["skills_rejected"], [])

    def test_the_state_flags_are_booleans_and_lists_stay_within_the_clouds_bounds(self):
        book = self.access.calls
        for index in range(70):
            book.deliver({"call_id": f"cal_{index:04d}", "skill_id": "nothing", "version": None, "digest": None,
                          "parameters": {}, "timeout_s": None, "problem": None})
        for index in range(30):
            book._expect_ack(f"stp_{index:04d}")
        request = self.sync()
        self.assertTrue(all(type(v) is bool for v in request["state"].values()))
        self.assertEqual(sorted(request["state"]), ["busy", "estop_latched", "motion_blocked"])
        self.assertLessEqual(len(request["calls"]), 50)
        self.assertLessEqual(len(request["stops"]), 50)


class Parsing(unittest.TestCase):
    def test_read_call(self):
        self.assertIsNone(read_call({"call_id": "cal_a b"}))
        call = read_call({"call_id": "cal_1", "skill_id": "s", "version": "1.2.3", "parameters": None, "timeout_s": 5})
        self.assertEqual(call, {"call_id": "cal_1", "skill_id": "s", "version": "1.2.3", "digest": None,
                                "parameters": {}, "timeout_s": 5, "problem": None, "placed_at": None})
        self.assertIsNotNone(read_call({"call_id": "cal_1", "skill_id": "s", "parameters": {"a": float("nan")}})["problem"])

    def test_read_answer_keeps_what_fits(self):
        parts = read_answer({"accepted": True, "device_id": "<script>", "poll_after_s": 3, "cancel": ["cal_1", 5, "x"],
                             "settled": ["cal_2", None], "calls": [{"call_id": "cal_3", "skill_id": "s"}, "junk"]})
        self.assertEqual((parts["device_id"], parts["poll_after_s"], parts["cancel"], parts["settled"], parts["stop_id"]),
                         (None, 3.0, ["cal_1"], ["cal_2"], None))
        self.assertEqual([c["call_id"] for c in parts["calls"]], ["cal_3"])


if __name__ == "__main__":
    unittest.main()
