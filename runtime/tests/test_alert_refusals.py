"""Issue #26: what the reporter does when the cloud answers but does not take a report, and when
several things happen at once. The rule under test: the device never gives up an alert on its own."""
from __future__ import annotations

import threading
import time
import unittest

import _support  # noqa: F401
from _support import wait_until

from neuxmind_runtime import alerts
from neuxmind_runtime.alert_report import HOLD_S, INTERVAL_S, AlertReporter, ReportConfig, SendError

from alert_support import CLOUD_KEY, CONFIG, STOP_BOUND_S, ReporterCase, book_rows


# ====================================================================== the cloud answers "no" to a report
class Refused(ReporterCase):
    """An HTTP answer that is not 200: the cloud is there but does not take this report.

    The device never gives up an alert on its own. What it does: say "still here" with a report
    that carries no alerts, and -- only when that one is accepted, which shows the refusal was about
    the alerts -- send them one per report so that the one the cloud will not take can be told apart
    and left for later."""

    def refuse(self, poison=(), status: int = 400, everything: bool = False, more_than: int | None = None):
        poison = set(poison)

        def reply(report):
            ids = [a["id"] for a in report["alerts"]]
            if everything or poison & set(ids) or (more_than is not None and len(ids) > more_than):
                raise SendError(f"云平台答复 {status}（invalid_report）", status=status)
            for item in report["alerts"]:
                self.transport.stored[item["id"]] = item
            return {"accepted": ids, "duplicates": [], "rejected": []}
        self.transport.reply = reply

    def rounds(self, reporter, count: int) -> None:
        for _ in range(count):
            self.step(reporter, INTERVAL_S)
            self.step(reporter, ticks=5)

    def test_a_refused_report_is_followed_by_one_without_alerts_so_the_device_stays_online(self):
        reporter = self.reporter(self.runtime())
        reporter.tick()
        bad = reporter.raise_alert("disk_low", alerts.WARNING, "一", "建议一")
        self.refuse({bad["id"]})
        self.step(reporter, INTERVAL_S)
        self.step(reporter)
        self.assertEqual([(r["trigger"], len(r["alerts"]), r["pending"]) for r in self.transport.attempts[1:]],
                         [("interval", 1, 0), ("heartbeat", 0, 1)])
        view = reporter.describe()
        self.assertEqual(view["unsent"], 1)
        self.assertIn("400", view["last_error"])
        self.step(reporter, ticks=30)
        self.assertEqual(len(self.transport.attempts), 3, "one heartbeat, not one a second")

    def test_one_refusal_does_not_put_an_alert_of_the_highest_level_away_for_an_hour(self):
        runtime = self.runtime()
        reporter = self.reporter(runtime)
        reporter.tick()
        refused_once = []

        def reply(report):
            ids = [a["id"] for a in report["alerts"]]
            if ids and not refused_once:
                refused_once.append(ids)
                raise SendError("云平台答复 400（invalid_report）", status=400)
            for item in report["alerts"]:
                self.transport.stored[item["id"]] = item
            return {"accepted": ids, "duplicates": [], "rejected": []}
        self.transport.reply = reply
        runtime.emergency_stop("操作员停止")
        self.step(reporter, ticks=4)                          # sent at once, refused; the empty report is taken
        view = reporter.describe()
        self.assertEqual((view["unsent"], view["held"], view["rejected"]), (1, 1, 0))
        self.step(reporter, INTERVAL_S)                       # the next five-minute report has it again
        self.assertEqual(self.transport.kinds(), [alerts.ESTOP_LATCHED])
        view = reporter.describe()
        self.assertEqual((view["unsent"], view["held"]), (0, 0))

    def test_a_wrong_address_or_an_older_cloud_never_costs_an_alert(self):
        runtime = self.runtime()
        reporter = self.reporter(runtime)
        self.refuse(everything=True, status=404)              # every report, the empty ones too
        reporter.tick()
        runtime.emergency_stop("操作员停止")
        for index in range(4):
            reporter.raise_alert("disk_low", alerts.WARNING, f"第 {index} 条", "建议")
        self.rounds(reporter, 30)                             # two and a half hours of 404
        view = reporter.describe()
        self.assertEqual((view["unsent"], view["rejected"], view["held"], view["dropped"]), (5, 0, 0, 0))
        self.transport.reply = None                           # the address is put right
        self.rounds(reporter, 1)
        self.assertEqual(len(self.transport.stored), 5)
        self.assertEqual(reporter.describe()["unsent"], 0)

    def test_one_alert_the_cloud_will_not_take_does_not_hold_back_the_others(self):
        for level in (alerts.WARNING, alerts.CRITICAL):
            with self.subTest(level=level):
                self.transport.__init__()
                reporter = self.reporter(self.runtime())
                reporter.tick()
                bad = reporter.raise_alert("odd_one", level, "坏的", "建议")
                good = [reporter.raise_alert("disk_low", alerts.WARNING, f"好的 {i}", "建议") for i in range(3)]
                self.refuse({bad["id"]})
                self.rounds(reporter, 5)
                self.assertEqual(sorted(self.transport.stored), sorted(a["id"] for a in good))
                view = reporter.describe()
                self.assertEqual((view["unsent"], view["sent"], view["rejected"], view["held"]), (1, 3, 0, 1),
                                 "kept and set to one side, never given up")
                before = len(self.transport.attempts)
                self.rounds(reporter, 3)
                tries = [r for r in self.transport.attempts[before:] if r["alerts"]]
                self.assertEqual(tries, [], "left alone for a while, not sent every five minutes")
                self.transport.reply = None                   # the cloud is repaired
                self.step(reporter, HOLD_S)
                self.step(reporter, ticks=5)
                self.assertIn(bad["id"], self.transport.stored, "and tried again later")
                self.assertEqual((reporter.describe()["unsent"], reporter.describe()["held"]), (0, 0))
                reporter.close()

    def test_the_one_it_will_not_take_is_found_wherever_it_is_in_a_long_queue(self):
        reporter = self.reporter(self.runtime())
        reporter.tick()
        raised = [reporter.raise_alert("disk_low", alerts.WARNING, f"第 {i} 条", "建议") for i in range(30)]
        bad = raised[24]
        self.refuse({bad["id"]})
        self.rounds(reporter, 3)                              # three refusals confirmed: one alert per report
        for _ in range(8):
            self.step(reporter, ticks=40)
        self.assertEqual(len(self.transport.stored), 29)
        view = reporter.describe()
        self.assertEqual((view["unsent"], view["held"], view["rejected"]), (1, 1, 0))
        self.assertLessEqual(len(self.transport.attempts), 60)

    def test_reports_too_big_for_something_on_the_way_get_through_one_alert_at_a_time(self):
        reporter = self.reporter(self.runtime())
        reporter.tick()
        raised = [reporter.raise_alert("disk_low", alerts.WARNING, f"第 {i} 条", "建议") for i in range(6)]
        self.refuse(more_than=1, status=413)                  # a proxy that takes small bodies only
        self.rounds(reporter, 4)
        self.assertEqual(sorted(self.transport.stored), sorted(a["id"] for a in raised))
        view = reporter.describe()
        self.assertEqual((view["unsent"], view["held"], view["rejected"]), (0, 0, 0))
        self.transport.reply = None
        for index in range(3):
            reporter.raise_alert("disk_low", alerts.WARNING, f"之后 {index}", "建议")
        self.rounds(reporter, 1)
        self.assertEqual(max(len(r["alerts"]) for r in self.transport.attempts[-3:]), 3,
                         "back to whole reports once the queue is empty")

    def test_a_cloud_that_takes_no_alert_at_all_is_not_asked_every_second(self):
        reporter = self.reporter(self.runtime())
        reporter.tick()
        for index in range(300):
            reporter.raise_alert("disk_low", alerts.WARNING, f"第 {index} 条", "建议")

        def reply(report):                                    # empty reports are fine, any alert is refused
            if report["alerts"]:
                raise SendError("云平台答复 400（invalid_report）", status=400)
            return {"accepted": [], "duplicates": [], "rejected": []}
        self.transport.reply = reply
        self.step(reporter, INTERVAL_S)
        self.transport.attempts.clear()
        hours = 3
        for _ in range(hours * 12):
            self.step(reporter, 1.0, ticks=int(INTERVAL_S))
        per_interval = len(self.transport.attempts) / (hours * 12)
        self.assertLessEqual(per_interval, 10, "a handful of requests per five minutes, not one per second")
        view = reporter.describe()
        self.assertEqual((view["unsent"], view["rejected"], view["dropped"]), (300, 0, 0))

    def test_an_alert_of_the_highest_level_is_not_kept_waiting_behind_one_the_cloud_will_not_take(self):
        runtime = self.runtime()
        reporter = self.reporter(runtime)
        reporter.tick()
        bad = reporter.raise_alert("odd_one", alerts.WARNING, "坏的", "建议")
        self.refuse({bad["id"]})
        self.step(reporter, 5.0)
        runtime.emergency_stop("操作员停止")
        raised_at = self.clock()
        self.step(reporter, ticks=10)
        self.assertEqual(self.transport.kinds(), [alerts.ESTOP_LATCHED])
        self.assertLessEqual(self.clock() - raised_at, 10)
        for hour in range(3):                                 # and not when the one left out is tried again either
            self.step(reporter, HOLD_S)
            self.step(reporter, ticks=3)
            runtime.reset({"hardware_authorized": True})
            self.step(reporter, 40.0, ticks=2)
            runtime.emergency_stop(f"第 {hour} 小时")
            raised_at = self.clock()
            self.step(reporter, ticks=10)
            self.assertEqual(len(self.transport.stored), hour + 2, hour)
            self.assertLessEqual(self.clock() - raised_at, 10)
        self.assertEqual(reporter.describe()["rejected"], 0)

    def test_an_alert_left_out_gets_its_turn_even_when_others_keep_coming(self):
        reporter = self.reporter(self.runtime())
        reporter.tick()
        bad = reporter.raise_alert("odd_one", alerts.WARNING, "坏的", "建议")
        self.refuse({bad["id"]})
        self.rounds(reporter, 1)                              # refused on its own once: left out until the next report
        tries_before = sum(1 for r in self.transport.attempts if bad["id"] in [a["id"] for a in r["alerts"]])
        for index in range(2):                                # and every five minutes something else is waiting
            reporter.raise_alert("disk_low", alerts.WARNING, f"别的 {index}", "建议")
            self.rounds(reporter, 1)
        alone = [r for r in self.transport.attempts if [a["id"] for a in r["alerts"]] == [bad["id"]]]
        self.assertGreater(len(alone), tries_before, "tried again, alone, after the others went")
        self.assertEqual(len(self.transport.stored), 2)
        self.transport.reply = None                           # the cloud takes it now
        reporter.raise_alert("disk_low", alerts.WARNING, "又一条", "建议")
        self.step(reporter, HOLD_S)
        self.step(reporter, ticks=5)
        self.assertIn(bad["id"], self.transport.stored)
        self.assertEqual(reporter.describe()["unsent"], 0)

    def test_a_server_error_never_sets_an_alert_aside(self):
        reporter = self.reporter(self.runtime())
        reporter.tick()
        alert = reporter.raise_alert("disk_low", alerts.WARNING, "一", "建议一")
        self.refuse({alert["id"]}, status=500)
        self.rounds(reporter, 10)
        view = reporter.describe()
        self.assertEqual((view["unsent"], view["rejected"], view["held"]), (1, 0, 0))
        self.assertIn("heartbeat", {r["trigger"] for r in self.transport.attempts})
        self.transport.reply = None
        self.step(reporter, INTERVAL_S)
        self.assertEqual(list(self.transport.stored), [alert["id"]])

    def test_a_key_the_cloud_does_not_accept_gets_no_second_request(self):
        reporter = self.reporter(self.runtime())
        reporter.tick()
        reporter.raise_alert("disk_low", alerts.WARNING, "一", "建议一")
        for status in (401, 403, 429):
            self.refuse(everything=True, status=status)
            before = len(self.transport.attempts)
            self.step(reporter, INTERVAL_S)
            self.step(reporter, ticks=5)
            self.assertEqual(len(self.transport.attempts), before + 1, status)
        view = reporter.describe()
        self.assertEqual((view["unsent"], view["rejected"], view["held"]), (1, 0, 0))

# ====================================================================== more than one thing at a time
class Together(ReporterCase):
    def test_alerts_raised_from_many_threads_all_arrive_once(self):
        runtime = self.runtime()
        reporter = AlertReporter(runtime, ReportConfig(url="https://cloud.invalid", key=CLOUD_KEY, interval_s=0.05),
                                 transport=self.transport, poll_s=0.005)
        self.addCleanup(reporter.close)
        reporter.start()
        raised: list[str] = []

        def raise_some(n: int) -> None:
            for index in range(40):
                level = alerts.CRITICAL if index % 10 == 0 else alerts.WARNING
                raised.append(reporter.raise_alert("disk_low", level, f"线程 {n} 第 {index} 条", "建议")["id"])
        threads = [threading.Thread(target=raise_some, args=(n,)) for n in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        wait_until(lambda: len(self.transport.stored) == 320, timeout=30)
        self.assertEqual(sorted(self.transport.stored), sorted(raised))
        wait_until(lambda: reporter.describe()["unsent"] == 0, timeout=10)
        view = reporter.describe()
        self.assertEqual((view["sent"], view["dropped"], view["rejected"]), (320, 0, 0))
        reporter.close()
        self.assertEqual(book_rows(runtime, "SELECT state, COUNT(*) FROM alerts GROUP BY state"), [(alerts.SENT, 320)])

    def test_close_does_not_wait_for_a_request_in_flight_and_the_alert_is_kept(self):
        runtime = self.runtime()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        stored = self.transport.stored

        class Slow:
            def send(self, report):
                for item in report["alerts"]:
                    stored[item["id"]] = item                 # the cloud has it; the answer never comes back
                if report["alerts"]:
                    entered.set()
                    release.wait(30)
                    raise SendError("超时")
                return {"accepted": [], "duplicates": [], "rejected": []}
        reporter = AlertReporter(runtime, CONFIG, transport=Slow(), poll_s=0.01)
        reporter.start()
        alert = reporter.raise_alert("upgrade_failed", alerts.CRITICAL, "升级失败", "到现场处理")
        self.assertTrue(entered.wait(10))
        started = time.monotonic()
        reporter.close()
        self.assertLess(time.monotonic() - started, STOP_BOUND_S + 2)
        release.set()
        again = self.reporter(runtime)                        # the next run sends it again; the cloud knows the id
        again.tick()
        self.assertEqual(again.describe()["unsent"], 0)
        self.assertEqual(list(stored), [alert["id"]])

    def test_close_does_not_wait_for_a_disk_that_never_answers(self):
        runtime = self.runtime()
        reporter = AlertReporter(runtime, CONFIG, transport=self.transport, poll_s=0.01)
        reporter.tick()
        book = reporter._book
        stuck, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        real = book._write

        def never_returns(*args):
            stuck.set()
            release.wait(30)
            return real(*args)
        book._write = never_returns
        reporter.start()
        reporter.raise_alert("disk_low", alerts.WARNING, "一", "建议一")
        self.assertTrue(stuck.wait(10))
        started = time.monotonic()
        self.assertEqual(reporter.describe()["unsent"], 1)
        reporter.raise_alert("disk_low", alerts.WARNING, "二", "建议二")       # still does not wait
        runtime.emergency_stop("操作员停止")
        reporter.close()
        self.assertLess(time.monotonic() - started, STOP_BOUND_S + 2)
        release.set()                                         # the disk answers after all: nothing is left open
        wait_until(lambda: book._io.acquire(blocking=False), timeout=10)
        book._io.release()
        reporter.close()
        self.assertEqual(len(book_rows(runtime, "SELECT id FROM alerts")), 2, "and both alerts are in the file")


if __name__ == "__main__":
    unittest.main()
