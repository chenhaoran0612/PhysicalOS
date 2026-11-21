"""Issue #26 (R7-08): the device reports its alerts to the cloud by itself, every five minutes.

No console takes part anywhere in this file: the runtimes have no gateway, so nothing can connect to
them; the alerts are raised on the device; and the reporter runs on a clock the test moves. The cloud
is a recording stand-in (``FakeTransport`` here, a real HTTPS server in ``test_alert_report_https``).

What is covered: the acceptance case (an alert raised on the device is at the cloud within five
minutes), the two alerts of the highest level going out at once, resending what could not be sent, an
unconfigured device sending nothing, and the reporter never getting in the way of a stop.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import _support  # noqa: F401
from _support import TOKEN, wait_until

from neuxmind_runtime import alerts
from neuxmind_runtime.adapters.sim_quadruped import SimQuadruped
from neuxmind_runtime.alert_report import (IMMEDIATE_MIN_GAP_S, INTERVAL_S, MAX_ALERTS_PER_REPORT, AlertReporter,
                                           ConfigError, ReportConfig, SendError, for_serve, load_config)
from neuxmind_runtime.clock import ManualClock
from neuxmind_runtime.core import EdgeRuntime

from alert_support import CLOUD_KEY, CONFIG, STOP_BOUND_S, WALL_START, ReporterCase, book_rows
from test_iter05_runtime import make_runtime
from test_iter06_runtime import Creeper, tick_for


# ====================================================================== acceptance
class Acceptance(ReporterCase):
    def test_alert_raised_on_the_device_is_at_the_cloud_within_five_minutes_without_a_console(self):
        runtime = self.runtime()
        reporter = self.reporter(runtime)
        reporter.tick()                                       # the report every start begins with
        self.assertEqual([r["alerts"] for r in self.transport.attempts], [[]])
        self.step(reporter, 10.0)
        raised_at = self.clock()
        alert = reporter.raise_alert("disk_low", alerts.WARNING, "磁盘占用 92%", "清理日志或扩容")
        self.step(reporter, 1.0, ticks=int(INTERVAL_S) - 12)  # up to one second before the next report
        self.assertEqual(len(self.transport.attempts), 1, "an ordinary alert waits for the 5-minute report")
        self.step(reporter, 1.0, ticks=2)
        self.assertEqual(len(self.transport.attempts), 2)
        self.assertLessEqual(self.clock() - raised_at, INTERVAL_S)
        got = self.transport.stored[alert["id"]]
        self.assertEqual((got["kind"], got["level"], got["reason"], got["advice"]),
                         ("disk_low", "warning", "磁盘占用 92%", "清理日志或扩容"))
        self.assertEqual(got["device"]["instance_id"], runtime.manifest["instance_id"])
        self.assertAlmostEqual(got["ts"], WALL_START + 10.0, delta=1.0)
        self.assertEqual(reporter.describe()["unsent"], 0)

    def test_every_report_names_the_device_and_carries_no_credentials(self):
        runtime = self.runtime()
        reporter = self.reporter(runtime)
        runtime.emergency_stop("操作员停止")
        self.step(reporter, ticks=3)
        self.assertTrue(self.sent())
        for report in self.transport.attempts:
            self.assertEqual(report["schema"], "neuxmind.device_report.v1")
            self.assertEqual(report["device"]["instance_id"], runtime.manifest["instance_id"])
            self.assertEqual(report["device"]["simulated"], True)
            self.assertIn("runtime_version", report["device"])
            self.assertEqual(report["interval_s"], INTERVAL_S)
            text = json.dumps(report, ensure_ascii=False)
            self.assertNotIn(CLOUD_KEY, text)
            self.assertNotIn(TOKEN, text)
            self.assertNotIn(str(runtime.state_dir), text)

    def test_a_report_goes_out_every_five_minutes_even_with_nothing_to_report(self):
        reporter = self.reporter(self.runtime())
        reporter.tick()
        self.step(reporter, 1.0, ticks=int(INTERVAL_S) * 4)
        self.assertEqual(len(self.transport.attempts), 5)
        self.assertEqual([r["alerts"] for r in self.transport.attempts], [[]] * 5)
        self.assertEqual(self.transport.attempts[0]["trigger"], "start")
        self.assertEqual({r["trigger"] for r in self.transport.attempts[1:]}, {"interval"})

    def test_the_thread_reports_by_itself(self):
        runtime = self.runtime()
        reporter = AlertReporter(runtime, ReportConfig(url="https://cloud.invalid", key=CLOUD_KEY, interval_s=0.1),
                                 transport=self.transport, poll_s=0.01)
        self.addCleanup(reporter.close)
        reporter.start()
        wait_until(lambda: len(self.transport.attempts) >= 3, timeout=10)
        runtime.emergency_stop("测试")
        wait_until(lambda: alerts.ESTOP_LATCHED in self.transport.kinds(), timeout=10)
        reporter.close()
        seen = len(self.transport.attempts)
        time.sleep(0.3)
        self.assertEqual(len(self.transport.attempts), seen, "nothing is sent after close()")

# ====================================================================== highest level: at once
class HighestLevel(ReporterCase):
    def test_emergency_stop_is_reported_at_once(self):
        runtime = self.runtime()
        reporter = self.reporter(runtime)
        reporter.tick()
        runtime.emergency_stop("操作员停止")
        self.step(reporter, ticks=3)                          # three seconds, far from the next interval
        self.assertEqual(len(self.transport.attempts), 2)
        report = self.transport.attempts[1]
        self.assertEqual(report["trigger"], "immediate")
        (alert,) = report["alerts"]
        self.assertEqual((alert["kind"], alert["level"]), (alerts.ESTOP_LATCHED, alerts.CRITICAL))
        self.assertIn("操作员停止", alert["reason"])
        self.assertTrue(alert["advice"])
        self.assertEqual(alert["schema"], "neuxmind.alert.v1")

    def test_the_reason_written_after_the_stop_is_the_one_reported(self):
        runtime = self.runtime()
        reporter = self.reporter(runtime)
        reporter.tick()
        runtime.emergency_stop("stop")                       # the gateway's placeholder
        self.step(reporter)                                   # seen latched; the body is still being read
        runtime.annotate_stop_reason("围栏内有人")
        self.step(reporter, ticks=2)
        (alert,) = self.sent()
        self.assertIn("围栏内有人", alert["reason"])

    def test_one_alert_per_latch_however_many_stops(self):
        runtime = self.runtime()
        reporter = self.reporter(runtime)
        reporter.tick()
        for _ in range(3):
            runtime.emergency_stop("操作员停止")
            self.step(reporter, ticks=2)
        self.assertEqual([a["kind"] for a in self.sent()], [alerts.ESTOP_LATCHED])
        runtime.reset({"hardware_authorized": True})
        self.step(reporter, IMMEDIATE_MIN_GAP_S)
        runtime.emergency_stop("第二次")
        self.step(reporter, ticks=3)
        self.assertEqual(len(self.sent()), 2)
        self.assertIn("第二次", self.sent()[1]["reason"])

    def test_a_stop_reset_between_two_looks_still_raises_an_alert(self):
        runtime = self.runtime()
        reporter = self.reporter(runtime)
        reporter.tick()
        runtime.emergency_stop("很快就解除")
        runtime.reset({"hardware_authorized": True})
        self.step(reporter, ticks=2)
        (alert,) = self.sent()
        self.assertEqual((alert["kind"], alert["level"]), (alerts.ESTOP_LATCHED, alerts.CRITICAL))
        self.assertIn("已解除", alert["reason"])

    def test_stop_that_the_body_does_not_obey_is_reported_at_once(self):
        body_clock = ManualClock()
        adapter = Creeper(0.019, delay=2.0, clock=body_clock)   # resumes two seconds after every stop
        runtime = self.runtime(adapter, start_monitor=False)
        reporter = self.reporter(runtime)
        reporter.tick()
        runtime.emergency_stop("操作员停止")
        self.step(reporter, ticks=3)
        tick_for(runtime, adapter, body_clock, 60.0)            # the guard gives up on software stops
        self.assertIsNotNone(runtime.latch_guard.stop_not_obeyed)
        self.step(reporter, ticks=2)
        kinds = [a["kind"] for a in self.sent()]
        self.assertEqual(kinds, [alerts.ESTOP_LATCHED, alerts.STOP_NOT_OBEYED])
        alert = self.sent()[1]
        self.assertEqual(alert["level"], alerts.CRITICAL)
        self.assertIn("硬件急停", alert["advice"])
        self.assertEqual(self.transport.attempts[-1]["trigger"], "immediate")
        self.step(reporter, ticks=5)
        self.assertEqual(len(self.sent()), 2, "one alert per escalation")

    def test_the_same_kind_goes_out_at_once_at_most_every_30_seconds(self):
        runtime = self.runtime()
        reporter = self.reporter(runtime)
        reporter.tick()
        runtime.emergency_stop("第一次")
        self.step(reporter, ticks=2)
        self.assertEqual(len(self.sent()), 1)
        first_at = self.clock()
        runtime.reset({"hardware_authorized": True})
        self.step(reporter)
        runtime.emergency_stop("第二次")
        self.step(reporter, ticks=10)
        self.assertEqual(len(self.sent()), 1, "the second one waits")
        self.step(reporter, ticks=int(IMMEDIATE_MIN_GAP_S))
        self.assertEqual(len(self.sent()), 2)
        self.assertLessEqual(self.clock() - first_at, IMMEDIATE_MIN_GAP_S + 13)

    def test_latch_found_at_start_after_an_abnormal_shutdown_is_reported(self):
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        crashed = EdgeRuntime(SimQuadruped(time_scale=20.0), state.name, sandbox="inprocess",
                              telemetry_capacity=None, start_monitor=False)
        crashed._closing.set()                                # the process dies: nothing marks a clean close
        crashed.store.close()
        runtime = EdgeRuntime(SimQuadruped(time_scale=20.0), state.name, sandbox="inprocess",
                              telemetry_capacity=None, start_monitor=False)
        self.addCleanup(runtime.close)
        self.assertTrue(runtime.supervisor.latched)
        reporter = self.reporter(runtime)
        self.step(reporter, ticks=3)
        (alert,) = self.sent()
        self.assertEqual(alert["kind"], alerts.ESTOP_LATCHED)
        self.assertIn("未正常关闭", alert["reason"])

    def test_a_latch_carried_over_a_clean_restart_is_not_reported_twice(self):
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        first = EdgeRuntime(SimQuadruped(time_scale=20.0), state.name, sandbox="inprocess",
                            telemetry_capacity=None, start_monitor=False)
        reporter = self.reporter(first)
        first.emergency_stop("操作员停止")
        self.step(reporter, ticks=3)
        self.assertEqual(len(self.sent()), 1)
        reporter.close()
        first.close()
        second = EdgeRuntime(SimQuadruped(time_scale=20.0), state.name, sandbox="inprocess",
                             telemetry_capacity=None, start_monitor=False)
        self.addCleanup(second.close)
        self.assertTrue(second.supervisor.latched)
        again = self.reporter(second)
        self.step(again, ticks=5)
        self.assertEqual(len(self.sent()), 1, "still the same latch")
        second.reset({"hardware_authorized": True})
        self.step(again, IMMEDIATE_MIN_GAP_S)
        second.emergency_stop("重启后的新急停")
        self.step(again, ticks=3)
        self.assertEqual(len(self.sent()), 2)

# ====================================================================== not sent -> sent next time
class Resend(ReporterCase):
    def test_an_alert_that_could_not_be_sent_goes_out_with_the_next_report(self):
        reporter = self.reporter(self.runtime())
        reporter.tick()
        alert = reporter.raise_alert("disk_low", alerts.WARNING, "磁盘占用 92%", "清理日志或扩容")
        self.transport.fail = SendError("连接超时")
        self.step(reporter, INTERVAL_S)
        self.assertEqual(len(self.transport.attempts), 2)
        view = reporter.describe()
        self.assertEqual((view["unsent"], view["consecutive_failures"]), (1, 1))
        self.assertIn("连接超时", view["last_error"])
        self.step(reporter, 1.0, ticks=120)
        self.assertEqual(len(self.transport.attempts), 2, "no retry before the next 5-minute report")
        self.transport.fail = None
        self.step(reporter, INTERVAL_S - 120)
        self.assertEqual(len(self.transport.attempts), 3)
        self.assertEqual([a["id"] for a in self.transport.attempts[2]["alerts"]], [alert["id"]])
        view = reporter.describe()
        self.assertEqual((view["unsent"], view["sent"], view["consecutive_failures"], view["last_error"]),
                         (0, 1, 0, None))
        self.step(reporter, INTERVAL_S)
        self.assertEqual(self.transport.attempts[3]["alerts"], [], "sent once, not again")

    def test_a_highest_level_alert_that_fails_at_once_goes_out_with_the_next_report(self):
        runtime = self.runtime()
        reporter = self.reporter(runtime)
        reporter.tick()
        self.transport.fail = SendError("网络不可达")
        runtime.emergency_stop("操作员停止")
        self.step(reporter, ticks=3)
        self.assertEqual(len(self.transport.attempts), 2)
        self.step(reporter, ticks=100)
        self.assertEqual(len(self.transport.attempts), 2, "one immediate attempt, then the 5-minute report")
        self.transport.fail = None
        self.step(reporter, INTERVAL_S)
        self.assertEqual(self.transport.kinds(), [alerts.ESTOP_LATCHED])

    def test_unsent_alerts_survive_a_restart(self):
        runtime = self.runtime()
        reporter = self.reporter(runtime)
        self.transport.fail = SendError("网络不可达")
        alert = reporter.raise_alert("disk_low", alerts.WARNING, "磁盘占用 92%", "清理日志或扩容")
        self.step(reporter, ticks=2)
        reporter.close()
        self.transport.fail = None
        again = self.reporter(runtime)
        again.tick()
        self.assertEqual(list(self.transport.stored), [alert["id"]])
        self.assertEqual(again.describe()["unsent"], 0)

    def test_an_answer_lost_on_the_way_back_means_the_same_alert_is_sent_again(self):
        reporter = self.reporter(self.runtime())
        reporter.tick()
        alert = reporter.raise_alert("disk_low", alerts.WARNING, "磁盘占用 92%", "清理日志或扩容")

        def stores_then_breaks(report):
            for item in report["alerts"]:
                self.transport.stored[item["id"]] = item
            raise SendError("连接被重置")
        self.transport.reply = stores_then_breaks
        self.step(reporter, INTERVAL_S)
        self.assertEqual(reporter.describe()["unsent"], 1)
        self.transport.reply = None                           # now answers "duplicates": [id]
        self.step(reporter, INTERVAL_S)
        self.assertEqual([a["id"] for a in self.transport.attempts[2]["alerts"]], [alert["id"]])
        self.assertEqual(reporter.describe()["unsent"], 0)
        self.assertEqual(len(self.transport.stored), 1)

    def test_only_what_the_cloud_says_it_has_counts_as_sent(self):
        reporter = self.reporter(self.runtime())
        reporter.tick()
        first = reporter.raise_alert("disk_low", alerts.WARNING, "一", "建议一")
        second = reporter.raise_alert("disk_low", alerts.WARNING, "二", "建议二")
        self.transport.reply = lambda report: {"accepted": [first["id"], "not-ours"], "duplicates": [], "rejected": []}
        self.step(reporter, INTERVAL_S)
        self.assertEqual(reporter.describe()["unsent"], 1)
        self.transport.reply = None
        self.step(reporter, INTERVAL_S)
        self.assertEqual([a["id"] for a in self.transport.attempts[-1]["alerts"]], [second["id"]])

    def test_an_answer_that_is_not_an_answer_is_a_failure(self):
        reporter = self.reporter(self.runtime())
        reporter.tick()
        reporter.raise_alert("disk_low", alerts.WARNING, "一", "建议一")
        for bad in (None, [], "ok", {"accepted": "all"}, {"accepted": [1, 2]}, {}):
            self.transport.reply = lambda report, bad=bad: bad
            self.step(reporter, INTERVAL_S)
            self.assertEqual(reporter.describe()["unsent"], 1, bad)
        self.assertGreaterEqual(reporter.describe()["consecutive_failures"], 5)

    def test_an_alert_the_cloud_refuses_is_not_sent_for_ever(self):
        reporter = self.reporter(self.runtime())
        reporter.tick()
        alert = reporter.raise_alert("disk_low", alerts.WARNING, "一", "建议一")
        self.transport.reply = lambda report: {"accepted": [], "duplicates": [],
                                               "rejected": [{"id": alert["id"], "error": "级别无效"}]}
        self.step(reporter, INTERVAL_S)
        view = reporter.describe()
        self.assertEqual((view["unsent"], view["rejected"]), (0, 1))
        self.transport.reply = None
        self.step(reporter, INTERVAL_S)
        self.assertEqual(self.transport.attempts[-1]["alerts"], [])

    def test_a_backlog_goes_out_in_several_reports_the_highest_level_first(self):
        runtime = self.runtime()
        reporter = self.reporter(runtime)
        reporter.tick()
        self.transport.fail = SendError("网络不可达")
        total = MAX_ALERTS_PER_REPORT * 2 + 30
        for index in range(total):
            reporter.raise_alert("disk_low", alerts.WARNING, f"第 {index} 条", "清理日志或扩容")
        runtime.emergency_stop("操作员停止")
        self.step(reporter, ticks=3)                          # the immediate attempt fails
        self.transport.fail = None
        self.transport.attempts.clear()
        self.step(reporter, INTERVAL_S)
        first = self.transport.attempts[0]
        self.assertEqual(first["alerts"][0]["kind"], alerts.ESTOP_LATCHED)
        self.assertLessEqual(len(first["alerts"]), MAX_ALERTS_PER_REPORT)
        self.assertGreater(first["pending"], 0)
        self.step(reporter, ticks=10)                         # the rest follows without waiting five minutes
        self.assertEqual(len(self.transport.stored), total + 1)
        self.assertEqual(reporter.describe()["unsent"], 0)
        ids = [a["id"] for report in self.transport.attempts for a in report["alerts"]]
        self.assertEqual(len(ids), len(set(ids)), "nothing is sent twice")

    def test_a_report_stays_within_its_size_limit(self):
        reporter = self.reporter(self.runtime())
        reporter.tick()
        for index in range(60):
            reporter.raise_alert("disk_low", alerts.WARNING, "原因" * 1000, "建议" * 1000,
                                 detail={"note": "x" * 5000, "index": index})
        self.step(reporter, INTERVAL_S)
        self.step(reporter, ticks=10)
        self.assertEqual(len(self.transport.stored), 60)
        for report in self.transport.attempts:
            self.assertLessEqual(len(json.dumps(report, ensure_ascii=False).encode()), 120_000)
        one = next(iter(self.transport.stored.values()))
        self.assertLessEqual(len(one["reason"]), alerts.MAX_TEXT)
        self.assertLessEqual(len(one["advice"]), alerts.MAX_TEXT)

    def test_a_full_book_drops_the_oldest_ordinary_alerts_and_says_so(self):
        runtime = self.runtime()
        reporter = self.reporter(runtime, capacity=5)
        self.transport.fail = SendError("网络不可达")
        critical = reporter.raise_alert("upgrade_rollback_failed", alerts.CRITICAL, "回退失败", "到现场处理")
        for index in range(8):
            reporter.raise_alert("disk_low", alerts.WARNING, f"第 {index} 条", "清理日志或扩容")
        view = reporter.describe()
        self.assertEqual((view["unsent"], view["dropped"]), (5, 4))
        self.transport.fail = None
        self.step(reporter, INTERVAL_S + 1)
        self.assertIn(critical["id"], self.transport.stored, "the highest level is the last to go")
        self.assertEqual(sorted(a["reason"] for a in self.transport.stored.values() if a["kind"] == "disk_low"),
                         ["第 4 条", "第 5 条", "第 6 条", "第 7 条"])

# ====================================================================== no address: nothing leaves
class NotConfigured(ReporterCase):
    def test_without_a_cloud_address_nothing_is_sent_and_the_alerts_wait(self):
        runtime = self.runtime()
        idle = self.reporter(runtime, config=None, transport=None)
        idle.raise_alert("disk_low", alerts.WARNING, "磁盘占用 92%", "清理日志或扩容")
        runtime.emergency_stop("操作员停止")
        self.step(idle, 1.0, ticks=int(INTERVAL_S) * 2)
        view = idle.describe()
        self.assertIs(view["enabled"], False)
        self.assertIn("未配置", view["reason"])
        self.assertEqual((view["unsent"], view["reports_sent"], view["last_attempt_at"]), (2, 0, None))
        self.assertEqual(self.transport.attempts, [])
        idle.close()
        configured = self.reporter(runtime)                   # the address arrives later: they are sent then
        configured.tick()
        self.assertEqual(sorted(self.transport.kinds()), ["disk_low", alerts.ESTOP_LATCHED])

    def test_status_says_whether_reporting_is_on_and_never_shows_the_key(self):
        runtime = self.runtime()
        self.assertEqual(runtime.status()["alert_report"]["enabled"], False)
        runtime.alert_report = self.reporter(runtime)
        runtime.alert_report.tick()
        view = runtime.status()["alert_report"]
        self.assertEqual((view["enabled"], view["url"], view["interval_s"], view["reports_sent"]),
                         (True, "https://cloud.invalid", INTERVAL_S, 1))
        self.assertNotIn(CLOUD_KEY, json.dumps(runtime.status(), ensure_ascii=False, default=str))
        self.assertNotIn(CLOUD_KEY, repr(CONFIG))

    def test_the_runtime_has_one_entry_for_alerts_from_anywhere(self):
        runtime = self.runtime()
        self.assertIsNone(runtime.raise_alert("disk_low", alerts.WARNING, "一", "建议一"), "no reporter: no alert")
        runtime.alert_report = self.reporter(runtime)
        alert = runtime.raise_alert("skill_instance_failed", alerts.WARNING, "能力实例退出", "查看日志后重启",
                                    detail={"run_id": "run-1"})
        self.assertEqual(alert["detail"], {"run_id": "run-1"})
        with self.assertRaises(ValueError):
            runtime.raise_alert("x", "urgent", "一", "建议一")
        with self.assertRaises(ValueError):
            runtime.raise_alert("", alerts.INFO, "一", "建议一")
        with self.assertRaises(ValueError):
            runtime.raise_alert("x", alerts.INFO, "", "建议一")

# ====================================================================== never in the way of a stop
class RecordingQuad(SimQuadruped):
    """Notes which thread calls into the body."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.callers: list[tuple[str, str]] = []

    def _note(self, what: str) -> None:
        self.callers.append((threading.current_thread().name, what))

    def stop(self):
        self._note("stop")
        return super().stop()

    def read(self, cap_id):
        self._note("read")
        return super().read(cap_id)

    def invoke(self, cap_id, args):
        self._note("invoke")
        return super().invoke(cap_id, args)

    def reset(self):
        self._note("reset")
        return super().reset()

    def status(self):
        self._note("status")
        return super().status()


class Exploding:
    def __getattr__(self, name):
        raise RuntimeError("the reporter is broken")


class StopIsNeverHeldUp(ReporterCase):
    def test_stop_works_while_the_cloud_does_not_answer(self):
        runtime = self.runtime()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        class Hanging:
            def send(self, report):
                entered.set()
                release.wait(30)
                raise SendError("超时")
        reporter = AlertReporter(runtime, CONFIG, transport=Hanging(), poll_s=0.01)
        self.addCleanup(reporter.close)
        runtime.alert_report = reporter
        reporter.start()
        self.assertTrue(entered.wait(10))
        started = time.monotonic()
        result = runtime.emergency_stop("操作员停止")
        took = time.monotonic() - started
        self.assertTrue(result["stopped"] and result["latched"])
        self.assertLess(took, STOP_BOUND_S, "the stop does not wait for the request that hangs for 30 s")
        started = time.monotonic()
        self.assertTrue(runtime.status()["estop_latched"])
        runtime.reset({"hardware_authorized": True})
        self.assertLess(time.monotonic() - started, STOP_BOUND_S)
        release.set()

    def test_stop_works_while_the_alert_book_is_locked(self):
        runtime = self.runtime()
        reporter = self.reporter(runtime)
        runtime.alert_report = reporter
        reporter.tick()
        other = sqlite3.connect(Path(runtime.state_dir) / alerts.BOOK_NAME, timeout=0.1)
        self.addCleanup(other.close)
        other.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        result = runtime.emergency_stop("操作员停止")
        self.assertTrue(result["stopped"])
        self.assertLess(time.monotonic() - started, STOP_BOUND_S)
        self.assertEqual(runtime.status()["alert_report"]["enabled"], True)
        ordinary = reporter.raise_alert("disk_low", alerts.WARNING, "磁盘占用 92%", "清理日志或扩容")
        self.assertIsNone(reporter.describe()["storage_note"],
                          "raising an alert does not go to the disk: only the reporter's own round does")
        self.step(reporter, ticks=3)                          # cannot write: must not raise
        self.assertEqual(sorted(self.transport.kinds()), ["disk_low", alerts.ESTOP_LATCHED],
                         "sent from memory all the same")
        self.assertTrue(reporter.describe()["storage_note"])
        other.rollback()
        self.step(reporter, ticks=2)                          # the disk is back: what it refused is written now
        self.assertIsNone(reporter.describe()["storage_note"])
        other.close()
        stored = dict(book_rows(runtime, "SELECT id, state FROM alerts"))
        self.assertEqual(stored[ordinary["id"]], alerts.SENT)
        self.assertEqual(sorted(stored.values()), [alerts.SENT, alerts.SENT])

    def test_the_reporter_never_calls_into_the_body(self):
        adapter = RecordingQuad(time_scale=20.0)
        runtime = self.runtime(adapter)
        reporter = self.reporter(runtime)
        runtime.emergency_stop("操作员停止")

        def work():
            for _ in range(20):
                self.step(reporter)
        worker = threading.Thread(target=work, name="reporter-under-test")
        worker.start()
        worker.join(30)
        self.assertEqual(self.transport.kinds(), [alerts.ESTOP_LATCHED])
        self.assertEqual([c for c in adapter.callers if c[0] == "reporter-under-test"], [])

    def test_the_stop_path_does_not_touch_the_reporter(self):
        adapter = RecordingQuad(time_scale=20.0)
        runtime = self.runtime(adapter)
        runtime.alert_report = Exploding()
        result = runtime.emergency_stop("操作员停止")
        self.assertTrue(result["stopped"] and result["latched"])
        self.assertIn("stop", [what for _thread, what in adapter.callers])
        view = runtime.status()                               # status survives a broken reporter as well
        self.assertTrue(view["estop_latched"])
        self.assertEqual(view["alert_report"]["enabled"], False)
        self.assertIsNone(runtime.raise_alert("x", alerts.INFO, "一", "建议一"))
        runtime.alert_report = None

    def test_a_failing_reporter_breaks_nothing_else(self):
        runtime = self.runtime()
        reporter = self.reporter(runtime)
        for error in (RuntimeError("意外"), OSError("磁盘"), ValueError("坏数据"), MemoryError()):
            self.transport.fail = error
            self.step(reporter, INTERVAL_S)                   # must not raise
        self.assertEqual(reporter.describe()["consecutive_failures"], 4)
        self.assertTrue(runtime.status()["health"]["ok"])
        self.transport.fail = None
        runtime.emergency_stop("操作员停止")
        self.step(reporter, ticks=3)
        self.assertEqual(self.transport.kinds(), [alerts.ESTOP_LATCHED])

    def test_a_disk_that_cannot_hold_the_book_falls_back_to_memory(self):
        runtime = self.runtime()
        reporter = self.reporter(runtime)
        with mock.patch.object(alerts.sqlite3, "connect", side_effect=sqlite3.OperationalError("disk I/O error")):
            reporter.tick()                                   # the first round opens the book
        runtime.emergency_stop("操作员停止")
        self.step(reporter, ticks=3)
        self.assertEqual(self.transport.kinds(), [alerts.ESTOP_LATCHED])
        view = reporter.describe()
        self.assertEqual(view["storage"], "memory")
        self.assertTrue(view["storage_note"])

    def test_a_damaged_book_is_set_aside_not_deleted(self):
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        book = Path(state.name) / alerts.BOOK_NAME
        book.write_bytes(b"this is not a database" * 100)
        runtime = EdgeRuntime(SimQuadruped(time_scale=20.0), state.name, sandbox="inprocess",
                              telemetry_capacity=None, start_monitor=False)
        self.addCleanup(runtime.close)
        reporter = self.reporter(runtime)
        reporter.raise_alert("disk_low", alerts.WARNING, "一", "建议一")
        self.step(reporter)
        self.assertEqual(reporter.describe()["storage"], "disk")
        self.assertEqual(len(self.transport.stored), 1)
        aside = [p.name for p in Path(state.name).iterdir() if p.name.startswith(alerts.BOOK_NAME + ".corrupt-")]
        self.assertEqual(len(aside), 1)

    def test_the_book_is_private_to_the_runtime_user(self):
        runtime = self.runtime()
        reporter = self.reporter(runtime)
        book = Path(runtime.state_dir) / alerts.BOOK_NAME
        self.assertFalse(book.exists(), "building the reporter does not touch the disk; its first round does")
        reporter.tick()
        self.assertEqual(book.stat().st_mode & 0o777, 0o600)

# ====================================================================== the configuration file
class Configuration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "cloud-report.json"

    def write(self, content, mode: int = 0o600) -> Path:
        text = content if isinstance(content, str) else json.dumps(content)
        self.path.write_text(text, encoding="utf-8")
        os.chmod(self.path, mode)
        return self.path

    def test_a_private_file_with_an_https_address_and_a_key_is_accepted(self):
        config = load_config(self.write({"url": "https://cloud.example.com/", "key": CLOUD_KEY}))
        self.assertEqual((config.url, config.key, config.interval_s), ("https://cloud.example.com", CLOUD_KEY, INTERVAL_S))
        self.assertNotIn(CLOUD_KEY, repr(config))
        self.assertNotIn(CLOUD_KEY, str(config))
        behind_a_path = load_config(self.write({"url": "https://example.com:8443/neuxmind", "key": CLOUD_KEY}))
        self.assertEqual(behind_a_path.url, "https://example.com:8443/neuxmind")

    def test_a_file_others_can_read_is_refused(self):
        for mode in (0o644, 0o640, 0o604, 0o660):
            with self.assertRaises(ConfigError) as caught:
                load_config(self.write({"url": "https://cloud.example.com", "key": CLOUD_KEY}, mode))
            self.assertIn("权限", str(caught.exception))
            self.assertNotIn(CLOUD_KEY, str(caught.exception))

    def test_only_https_addresses_are_accepted(self):
        for url in ("http://cloud.example.com", "http://127.0.0.1:8800", "ftp://cloud.example.com", "cloud.example.com",
                    "https://", "https://user:pw@cloud.example.com", "https://cloud.example.com/?a=1",
                    "https://cloud.example.com/#x", "https://cloud.example.com:99999", "https://cloud .example.com",
                    "https://cloud.example.com/a\r\nX-Evil: 1", "", None, 5, "https://" + "a" * 600):
            with self.subTest(url=url), self.assertRaises(ConfigError):
                load_config(self.write({"url": url, "key": CLOUD_KEY}))
            with self.subTest(direct=url), self.assertRaises(ConfigError):
                ReportConfig(url=url, key=CLOUD_KEY)

    def test_a_key_that_cannot_be_a_header_value_is_refused(self):
        for key in ("", "short", None, 7, CLOUD_KEY + "\r\nX-Evil: 1", "有 空格" * 10, "k" * 600):
            with self.subTest(key=key), self.assertRaises(ConfigError) as caught:
                load_config(self.write({"url": "https://cloud.example.com", "key": key}))
            if isinstance(key, str) and len(key) > 8:
                self.assertNotIn(key, str(caught.exception))

    def test_anything_else_wrong_with_the_file_is_refused_with_a_reason(self):
        cases = {"不是 JSON": "url=https://cloud.example.com", "须为对象": ["https://cloud.example.com"],
                 "未知": {"url": "https://cloud.example.com", "key": CLOUD_KEY, "interval": 5},
                 "ca_file": {"url": "https://cloud.example.com", "key": CLOUD_KEY, "ca_file": "/no/such/ca.pem"}}
        for expected, content in cases.items():
            with self.subTest(expected=expected), self.assertRaises(ConfigError) as caught:
                load_config(self.write(content))
            self.assertIn(expected, str(caught.exception))
        with self.assertRaises(ConfigError):
            load_config(Path(self.tmp.name) / "missing.json")
        with self.assertRaises(ConfigError):
            load_config(Path(self.tmp.name))                  # a directory
        self.write("x" * 100_000)
        with self.assertRaises(ConfigError):
            load_config(self.path)

    def test_a_link_to_a_file_elsewhere_is_refused(self):
        real = Path(self.tmp.name) / "real.json"
        real.write_text(json.dumps({"url": "https://cloud.example.com", "key": CLOUD_KEY}), encoding="utf-8")
        os.chmod(real, 0o600)
        self.path.symlink_to(real)
        with self.assertRaises(ConfigError):
            load_config(self.path)

    def test_a_bad_file_switches_reporting_off_and_the_runtime_still_starts(self):
        runtime = make_runtime(self, SimQuadruped(time_scale=20.0), start_monitor=False)
        reporter = for_serve(runtime, self.write({"url": "http://cloud.example.com", "key": CLOUD_KEY}))
        self.addCleanup(reporter.close)
        self.assertIs(runtime.alert_report, reporter)
        view = reporter.describe()
        self.assertIs(view["enabled"], False)
        self.assertIn("https", view["reason"])
        reporter.tick()                                       # watches and records, sends nothing
        none = for_serve(runtime, None)
        self.addCleanup(none.close)
        self.assertIn("未配置", none.describe()["reason"])


if __name__ == "__main__":
    unittest.main()
