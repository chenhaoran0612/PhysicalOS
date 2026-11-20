"""Issue #26: the alerts kept on the device (``alerts.AlertBook``), the text they may carry, and how the
e-stop latch is turned into alerts (``alerts.LatchWatch``): one per latch, from a consistent look."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import _support  # noqa: F401

from neuxmind_runtime import alerts
from neuxmind_runtime.alert_report import IMMEDIATE_MIN_GAP_S, INTERVAL_S, SendError

from alert_support import WALL_START, ReporterCase, book_rows


# ====================================================================== text that cannot be sent as it is
LONE_SURROGATE = "\ud83d"                 # half an emoji, as a client that cuts a string in the middle sends it


def as_utf8(report: dict) -> bytes:
    return json.dumps(report, ensure_ascii=False, allow_nan=False).encode("utf-8")


class UnusableText(ReporterCase):
    def test_a_stop_reason_that_cannot_be_encoded_does_not_silence_the_device(self):
        runtime = self.runtime()
        reporter = self.reporter(runtime)
        reporter.tick()
        runtime.emergency_stop("围栏" + LONE_SURROGATE + "内\x00有人\x1b[31m")
        self.step(reporter, ticks=3)
        (alert,) = self.sent()
        as_utf8(alert)                                        # does not raise
        self.assertIn("围栏", alert["reason"])
        self.assertIn("有人", alert["reason"])
        self.assertFalse([ch for ch in alert["reason"] if ord(ch) < 0x20 or 0xD800 <= ord(ch) <= 0xDFFF])
        view = reporter.describe()
        self.assertEqual((view["unsent"], view["sent"], view["storage_note"], view["last_error"]), (0, 1, None, None))
        self.step(reporter, INTERVAL_S)                       # and the "still here" reports keep going out
        self.assertEqual(self.transport.attempts[-1]["trigger"], "interval")
        self.assertEqual(book_rows(runtime, "SELECT state FROM alerts"), [(alerts.SENT,)])

    def test_alert_text_from_anywhere_is_made_sendable(self):
        reporter = self.reporter(self.runtime())
        alert = reporter.raise_alert("disk_low", alerts.WARNING, "原因" + LONE_SURROGATE + "\x00", "建议\r\n第二行\x07",
                                     detail={"note" + LONE_SURROGATE: ["a\x00b", LONE_SURROGATE, float("nan"), 3],
                                             "deep": {"a": {"b": {"c": {"d": {"e": 1}}}}}, "obj": object()})
        as_utf8(alert)
        self.assertEqual(alert["reason"], "原因?")
        self.assertNotIn("\x07", alert["advice"])
        self.assertIn("第二行", alert["advice"])
        self.assertNotIn("\x00", json.dumps(alert["detail"], ensure_ascii=False))
        self.assertNotIn("\\u0000", json.dumps(alert["detail"]))
        with self.assertRaises(ValueError):
            reporter.raise_alert("disk_low", alerts.WARNING, "\x00\x07 ", "建议")      # nothing is left of it
        for bad_ts in (float("nan"), float("inf"), -1.0, "now"):
            with self.assertRaises(ValueError):
                alerts.make_alert("disk_low", alerts.WARNING, "原因", "建议", {"instance_id": "x"}, ts=bad_ts)

    def test_rows_someone_else_wrote_into_the_book_are_checked_before_they_are_sent(self):
        runtime = self.runtime()
        first = self.reporter(runtime)
        good = first.raise_alert("disk_low", alerts.WARNING, "磁盘占用 92%", "清理日志或扩容")
        self.transport.fail = SendError("网络不可达")
        self.step(first)
        first.close()
        db = sqlite3.connect(Path(runtime.state_dir) / alerts.BOOK_NAME)
        huge = {**good, "id": "planted-huge", "reason": "x" * 2_000_000}
        long = {**good, "id": "planted-long", "reason": "x" * 20_000}
        rows = [("planted-huge", 50, alerts.UNSENT, json.dumps(huge)),
                ("planted-long", 49, alerts.UNSENT, json.dumps(long)),
                ("planted-list", 51, alerts.UNSENT, json.dumps([1, 2])),
                ("planted-level", 52, alerts.UNSENT, json.dumps({**good, "id": "planted-level", "level": "urgent"})),
                ("planted-id", 53, alerts.UNSENT, json.dumps({**good, "id": "another-id"})),
                ("planted-text", 54, alerts.UNSENT, "{not json"),
                ("planted-ctl", 55, alerts.UNSENT, json.dumps({**good, "id": "planted-ctl", "reason": "a\x00b" + LONE_SURROGATE}))]
        db.executemany("INSERT INTO alerts (id, seq, state, payload) VALUES (?, ?, ?, ?)", rows)
        db.commit()
        db.close()
        self.transport.fail = None
        again = self.reporter(runtime)
        again.tick()
        sent = {a["id"]: a for a in self.sent()}
        self.assertEqual(sorted(sent), sorted([good["id"], "planted-long", "planted-ctl"]))
        self.assertLessEqual(len(sent["planted-long"]["reason"]), alerts.MAX_TEXT)
        self.assertEqual(sent["planted-ctl"]["reason"], "a b?")
        self.assertEqual(book_rows(runtime, "SELECT COUNT(*) FROM alerts WHERE id LIKE 'planted-%'"), [(2,)])
        for report in self.transport.attempts:
            self.assertLessEqual(len(as_utf8(report)), 120_000)

    def test_a_body_whose_identity_cannot_be_reported_switches_reporting_off(self):
        import types
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        for instance_id in ("", "a\nb", "x" * 300, None, LONE_SURROGATE):
            body = types.SimpleNamespace(
                manifest={"instance_id": instance_id, "name": "狗", "family": "quadruped", "simulated": True},
                state_dir=state.name, latch_guard=None,
                supervisor=types.SimpleNamespace(latched=False, latch_reason=None, stop_count=0, reset_count=0))
            reporter = self.reporter(body)
            self.step(reporter, INTERVAL_S)
            view = reporter.describe()
            self.assertIs(view["enabled"], False, instance_id)
            self.assertIn("instance_id", view["reason"])
            reporter.close()
        self.assertEqual(self.transport.attempts, [])

# ====================================================================== a book file that is not what it should be
class DamagedBook(ReporterCase):
    def plant(self, runtime, rows: list) -> None:
        first = self.reporter(runtime)
        first.tick()                                          # creates the file
        first.close()
        db = sqlite3.connect(Path(runtime.state_dir) / alerts.BOOK_NAME)
        db.executemany("INSERT INTO alerts (id, seq, state, payload) VALUES (?, ?, ?, ?)", rows)
        db.commit()
        db.close()
        self.transport.attempts.clear()

    def payload(self, alert_id: str) -> str:
        return json.dumps({**alerts.make_alert("disk_low", alerts.WARNING, "磁盘占用 92%", "清理日志或扩容",
                                               {"instance_id": "sim-quadruped-01"}, ts=WALL_START), "id": alert_id})

    def test_rows_with_a_sequence_that_is_not_one_do_not_stop_the_watch(self):
        runtime = self.runtime()
        self.plant(runtime, [("p-text", "abc", alerts.UNSENT, self.payload("p-text")),
                             ("p-inf", float("inf"), alerts.UNSENT, self.payload("p-inf")),
                             ("p-max", 2 ** 63 - 1, alerts.UNSENT, self.payload("p-max")),
                             ("p-neg", -5, alerts.UNSENT, self.payload("p-neg")),
                             ("p-none", 7, "nonsense", self.payload("p-none"))])
        reporter = self.reporter(runtime)
        early = reporter.raise_alert("disk_low", alerts.WARNING, "打开之前记的", "建议")     # before the first round
        runtime.emergency_stop("操作员停止")
        self.step(reporter, ticks=4)
        sent = {a["id"]: a for a in self.sent()}
        self.assertIn(early["id"], sent, "what was raised before the file was read is not lost")
        self.assertIn(alerts.ESTOP_LATCHED, [a["kind"] for a in sent.values()])
        self.assertEqual({"p-text", "p-inf", "p-max", "p-neg"} - set(sent), set(), "usable alerts are still sent")
        view = reporter.describe()
        self.assertEqual((view["unsent"], view["storage"], view["storage_note"], view["last_error"]),
                         (0, "disk", None, None))
        after = reporter.raise_alert("disk_low", alerts.WARNING, "之后记的", "建议")
        self.step(reporter, INTERVAL_S)
        self.assertIn(after["id"], self.transport.stored)
        self.assertEqual(dict(book_rows(runtime, "SELECT id, state FROM alerts")).get(after["id"]), alerts.SENT)
        self.assertEqual(book_rows(runtime, "SELECT COUNT(*) FROM alerts WHERE typeof(seq) != 'integer'"), [(0,)])

    def test_a_file_stuffed_with_rows_is_cut_down_to_what_a_book_holds(self):
        runtime = self.runtime()
        self.plant(runtime, [(f"s-{i}", i + 10, alerts.SENT, self.payload(f"s-{i}")) for i in range(600)]
                   + [("big", 5, alerts.UNSENT, "x" * 200_000)])
        reporter = self.reporter(runtime, capacity=100)
        self.step(reporter, ticks=2)
        view = reporter.describe()
        self.assertLessEqual(view["sent"], 100)
        self.assertLessEqual(book_rows(runtime, "SELECT COUNT(*) FROM alerts")[0][0], 100)
        self.assertEqual(book_rows(runtime, "SELECT COUNT(*) FROM alerts WHERE id='big'"), [(0,)])

    def test_each_step_of_a_round_stands_on_its_own(self):
        runtime = self.runtime()
        reporter = self.reporter(runtime)
        reporter.tick()
        runtime.emergency_stop("操作员停止")
        with mock.patch.object(reporter._book, "open", side_effect=RuntimeError("意外")), \
                mock.patch.object(reporter._book, "sync", side_effect=OverflowError("意外")):
            self.step(reporter, ticks=3)
        self.assertEqual(self.transport.kinds(), [alerts.ESTOP_LATCHED], "the stop is still seen and reported")

    def test_a_write_that_fails_in_an_unexpected_way_is_only_a_write_that_failed(self):
        runtime = self.runtime()
        reporter = self.reporter(runtime)
        reporter.tick()
        with mock.patch.object(reporter._book, "_write", side_effect=OverflowError("Python int too large")):
            alert = reporter.raise_alert("disk_low", alerts.WARNING, "一", "建议一")
            self.step(reporter, INTERVAL_S)
            self.assertIn(alert["id"], self.transport.stored)
            self.assertTrue(reporter.describe()["storage_note"])
        self.step(reporter)
        self.assertIsNone(reporter.describe()["storage_note"])
        self.assertEqual(dict(book_rows(runtime, "SELECT id, state FROM alerts"))[alert["id"]], alerts.SENT)

# ====================================================================== the book, on its own
class Book(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / alerts.BOOK_NAME

    def alert(self, level: str = alerts.WARNING, reason: str = "原因") -> dict:
        return alerts.make_alert("disk_low", level, reason, "建议", {"instance_id": "x"}, ts=WALL_START)

    def file_rows(self) -> dict:
        db = sqlite3.connect(self.path)
        try:
            return dict(db.execute("SELECT id, state FROM alerts").fetchall())
        finally:
            db.close()

    def test_alerts_the_cloud_has_make_room_before_any_that_it_has_not(self):
        book = alerts.AlertBook(self.path, capacity=4)
        book.open()
        sent = [book.add(self.alert(reason=f"已发 {i}")) for i in range(2)]
        waiting = [book.add(self.alert(reason=f"未发 {i}")) for i in range(2)]
        book.settle([a["id"] for a in sent], ())
        newer = [book.add(self.alert(reason=f"新的 {i}")) for i in range(2)]
        self.assertTrue(book.sync())
        self.assertEqual(book.counts(), {"unsent": 4, "sent": 0, "rejected": 0, "dropped": 0})
        self.assertEqual(sorted(self.file_rows()), sorted(a["id"] for a in waiting + newer))
        book.close()

    def test_what_was_set_before_the_file_was_read_survives_reading_it(self):
        first = alerts.AlertBook(self.path, capacity=1)
        first.open()
        first.add(self.alert())
        first.add(self.alert())                                # one over: the older one is dropped, and counted
        first.set_flag(alerts.EPISODE_FLAG, "1")
        first.set_flag("somebody-elses", "x" * 100)
        first.close()
        book = alerts.AlertBook(self.path)
        book.set_flag(alerts.EPISODE_FLAG, "0")                # before open(): the newer word
        early = book.add(self.alert())
        book.open()
        self.assertEqual((book.counts()["dropped"], book.flag(alerts.EPISODE_FLAG)), (1, "0"))
        self.assertIsNone(book.flag("somebody-elses"), "only the flags the book knows are read back")
        self.assertTrue(book.sync())
        book.close()
        again = alerts.AlertBook(self.path)
        again.open()
        self.assertEqual((again.counts()["dropped"], again.flag(alerts.EPISODE_FLAG)), (1, "0"))
        self.assertEqual([a["id"] for a in again.unsent(10, 100_000)][-1], early["id"])
        again.close()

    def test_a_file_that_cannot_be_read_right_now_is_left_alone_and_tried_again(self):
        seeded = alerts.AlertBook(self.path)
        seeded.open()
        kept = [seeded.add(self.alert(reason=f"还没发的 {i}")) for i in range(3)]
        seeded.close()
        clock = [1000.0]
        for error in (sqlite3.OperationalError("disk I/O error"), sqlite3.OperationalError("database is locked"),
                      sqlite3.OperationalError("unable to open database file"), OSError(24, "Too many open files")):
            with self.subTest(error=str(error)):
                book = alerts.AlertBook(self.path, clock=lambda: clock[0])
                with mock.patch.object(book, "_read_rows", side_effect=error):
                    book.open()
                    during = book.add(self.alert(reason="读不到文件时记的"))
                    self.assertEqual((book.storage, book.counts()["unsent"]), ("memory", 1))
                    self.assertTrue(book.note)
                    book.open()                                # at once: not tried again yet
                self.assertEqual(book.storage, "memory")
                self.assertEqual([p.name for p in self.path.parent.iterdir() if ".corrupt-" in p.name], [],
                                 "a file that may be perfectly good is never renamed away")
                clock[0] += alerts.OPEN_RETRY_S
                book.open()                                    # the file can be read again
                self.assertEqual((book.storage, book.note), ("disk", None))
                waiting = [a["id"] for a in book.unsent(50, 1_000_000)]
                self.assertEqual(waiting[:3], [a["id"] for a in kept], "what it held is taken in, in its order")
                self.assertIn(during["id"], waiting)
                book.settle([during["id"]], ())                # keep the file as it was for the next round
                self.assertTrue(book.sync())
                book.close()

    def test_rows_that_are_not_rows_of_ours_are_taken_out_of_the_file(self):
        book = alerts.AlertBook(self.path)
        book.open()
        good = book.add(self.alert())
        book.close()
        db = sqlite3.connect(self.path)
        payload = json.dumps(good)
        db.execute("INSERT INTO alerts (id, seq, state, payload) VALUES (?, 5, 'unsent', ?)", (b"\x00blob-id", payload))
        db.execute("INSERT INTO alerts (id, seq, state, payload) VALUES ('nul-first', 6, 'unsent', ?)",
                   ("{}\x00" + "x" * 200_000,))
        db.execute("INSERT INTO alerts (id, seq, state, payload) VALUES ('blob-payload', 7, 'unsent', ?)", (b"\x01\x02",))
        db.commit()
        db.close()
        again = alerts.AlertBook(self.path)
        again.open()
        self.assertEqual([a["id"] for a in again.unsent(10, 100_000)], [good["id"]])
        again.close()
        self.assertEqual(list(self.file_rows()), [good["id"]])

    def test_without_a_file_nothing_is_remembered_for_the_file(self):
        book = alerts.AlertBook(None, capacity=3)
        for index in range(500):
            book.add(self.alert(reason=f"第 {index} 条"))
        self.assertEqual((book.counts()["unsent"], book.counts()["dropped"]), (3, 497))
        self.assertEqual(len(book._removed), 0)

    def test_memory_and_file_agree_after_everything_happened_at_once(self):
        for round_number in range(6):
            path = Path(self.tmp.name) / f"book-{round_number}.sqlite3"
            book = alerts.AlertBook(path, capacity=150)
            book.open()
            added: list[str] = []
            done = threading.Event()

            def adder(n: int) -> None:
                for index in range(120):
                    added.append(book.add(self.alert(alerts.CRITICAL if index % 7 == 0 else alerts.WARNING))["id"])

            def settler() -> None:
                while not done.is_set():
                    batch = [a["id"] for a in book.unsent(20, 100_000)]
                    book.settle(batch[::2], batch[1::4])

            def syncer() -> None:
                while not done.is_set():
                    book.sync()
            workers = [threading.Thread(target=adder, args=(n,)) for n in range(3)]
            helpers = [threading.Thread(target=settler), threading.Thread(target=syncer)]
            for thread in workers + helpers:
                thread.start()
            for thread in workers:
                thread.join(30)
            done.set()
            for thread in helpers:
                thread.join(30)
            self.assertTrue(book.sync())
            memory = {i: r["state"] for i, r in book._records.items()}
            counts = book.counts()
            book.close()
            db = sqlite3.connect(path)
            on_disk = dict(db.execute("SELECT id, state FROM alerts").fetchall())
            flags = dict(db.execute("SELECT name, value FROM flags").fetchall())
            db.close()
            self.assertEqual(on_disk, memory, round_number)
            self.assertEqual(len(memory), 150)
            self.assertEqual({s: list(memory.values()).count(s) for s in ("unsent", "sent", "rejected")},
                             {k: counts[k] for k in ("unsent", "sent", "rejected")})
            self.assertEqual(int(flags.get("dropped", 0)), counts["dropped"])
            self.assertEqual(len(set(added)), 360)

    def test_a_time_beyond_what_the_cloud_takes_is_not_a_time(self):
        for bad_ts in (1e11, 10 ** 400, float("inf")):
            with self.assertRaises(ValueError):
                alerts.make_alert("disk_low", alerts.WARNING, "原因", "建议", {"instance_id": "x"}, ts=bad_ts)

# ====================================================================== reading the safety state in one piece
class TornSupervisor:
    """A supervisor whose state changes between two attribute reads, as a stop or a reset landing in
    the middle of a look would make it. ``script`` maps the number of a read to a change."""

    def __init__(self, script: dict, **state):
        self._state = {"latched": False, "latch_reason": None, "stop_count": 0, "reset_count": 0, **state}
        self._script = dict(script)
        self._reads = 0

    def _read(self, name: str):
        self._reads += 1
        change = self._script.pop(self._reads, None)
        value = self._state[name]
        if change is not None:
            self._state.update(change)                         # lands right after this read
        return value

    latched = property(lambda self: self._read("latched"))
    latch_reason = property(lambda self: self._read("latch_reason"))
    stop_count = property(lambda self: self._read("stop_count"))
    reset_count = property(lambda self: self._read("reset_count"))


class OnePieceReads(unittest.TestCase):
    def watch(self, supervisor) -> tuple:
        import types
        raised: list[tuple] = []
        book = alerts.AlertBook(None)
        watch = alerts.LatchWatch(types.SimpleNamespace(supervisor=supervisor, latch_guard=None), book,
                                  lambda kind, level, reason, advice, detail=None, ts=None: raised.append((kind, reason)))
        return watch, raised

    def looks(self, states: list[dict]) -> list:
        """Alerts raised when successive looks see ``states`` (each one a whole look)."""
        import types
        supervisor = types.SimpleNamespace(latched=False, latch_reason=None, stop_count=0, reset_count=0)
        raised: list[tuple] = []
        watch = alerts.LatchWatch(types.SimpleNamespace(supervisor=supervisor, latch_guard=None), alerts.AlertBook(None),
                                  lambda kind, level, reason, advice, detail=None, ts=None: raised.append(reason))
        for state in states:
            vars(supervisor).update(state)
            watch.poll()
        return raised

    def test_a_look_between_the_count_of_a_stop_and_its_latch_raises_nothing_of_its_own(self):
        # the supervisor counts the stop first and latches next: a look in between sees "stopped, not latched"
        counted = {"stop_count": 1}
        latched = {"latched": True, "latch_reason": "操作员停止", "stop_count": 1}
        raised = self.looks([{}, {}, counted, latched, latched, latched])
        self.assertEqual(len(raised), 1, raised)
        self.assertIn("操作员停止", raised[0])

    def test_a_look_between_the_count_of_a_reset_and_the_unlatch_raises_nothing(self):
        # a reset counts first and clears the latch next: a look in between sees "latched, already reset"
        latched = {"latched": True, "latch_reason": "操作员停止", "stop_count": 1}
        counted = {"reset_count": 1}
        cleared = {"latched": False, "latch_reason": None, "reset_count": 1}
        raised = self.looks([latched, latched, counted, cleared, cleared, cleared])
        self.assertEqual(len(raised), 1, raised)

    def test_a_reset_of_nothing_is_not_a_latch(self):
        raised = self.looks([{}, {}, {"reset_count": 1}, {"reset_count": 1}, {"reset_count": 2}, {"reset_count": 2}])
        self.assertEqual(raised, [])

    def test_a_stop_landing_in_the_middle_of_a_look_is_one_alert(self):
        for read in range(1, 8):
            with self.subTest(read=read):
                supervisor = TornSupervisor({read: {"latched": True, "latch_reason": "操作员停止", "stop_count": 1}})
                watch, raised = self.watch(supervisor)
                for _ in range(4):
                    watch.poll()
                self.assertEqual(len(raised), 1, raised)
                self.assertIn("操作员停止", raised[0][1])

    def test_a_reset_landing_in_the_middle_of_a_look_is_no_alert(self):
        for read in range(1, 8):
            with self.subTest(read=read):
                supervisor = TornSupervisor({}, latched=True, latch_reason="操作员停止", stop_count=1)
                watch, raised = self.watch(supervisor)
                watch.poll()
                watch.poll()
                self.assertEqual(len(raised), 1)
                supervisor._reads = 0
                supervisor._script = {read: {"latched": False, "latch_reason": None, "reset_count": 1}}
                for _ in range(4):
                    watch.poll()
                self.assertEqual(len(raised), 1, raised)

    def test_a_latch_the_runtime_started_with_that_is_reset_before_the_second_look(self):
        supervisor = TornSupervisor({}, latched=True, latch_reason="上次运行时未正常关闭（崩溃或断电）")
        watch, raised = self.watch(supervisor)
        watch.poll()                                          # seen once; restored latches count no stop
        supervisor._state.update(latched=False, latch_reason=None, reset_count=1)
        watch.poll()
        watch.poll()
        self.assertEqual(len(raised), 1, raised)
        self.assertIn("已解除", raised[0][1])

    def test_a_note_of_a_latch_that_is_no_longer_on_is_cleared_at_the_first_look(self):
        import types
        book = alerts.AlertBook(None)
        book.set_flag(alerts.EPISODE_FLAG, "1")                # an earlier run reported a latch ...
        supervisor = TornSupervisor({})                        # ... and this one starts without one
        watch = alerts.LatchWatch(types.SimpleNamespace(supervisor=supervisor, latch_guard=None), book,
                                  lambda *args, **kw: None)
        watch.poll()
        self.assertEqual(book.flag(alerts.EPISODE_FLAG), "0")

# ====================================================================== which stops are one latch
class LatchEpisodes(ReporterCase):
    def kinds_and_reasons(self) -> list[str]:
        return [a["reason"] for a in self.sent()]

    def test_a_second_stop_and_the_reset_between_two_looks_are_still_the_latch_already_reported(self):
        runtime = self.runtime()
        reporter = self.reporter(runtime)
        reporter.tick()
        runtime.emergency_stop("第一次")
        self.step(reporter, ticks=3)
        runtime.emergency_stop("又按了一次")                    # same latch
        runtime.reset({"hardware_authorized": True})
        self.step(reporter, IMMEDIATE_MIN_GAP_S, ticks=3)
        self.assertEqual(len(self.sent()), 1, self.kinds_and_reasons())

    def test_two_latches_within_two_looks_are_reported_as_the_one_that_is_on(self):
        runtime = self.runtime()
        reporter = self.reporter(runtime)
        reporter.tick()
        runtime.emergency_stop("第一次")
        self.step(reporter)                                   # seen once
        runtime.reset({"hardware_authorized": True})
        runtime.emergency_stop("第二次")
        self.step(reporter, IMMEDIATE_MIN_GAP_S, ticks=3)
        reasons = self.kinds_and_reasons()
        self.assertEqual(len(reasons), 1, reasons)
        self.assertIn("第二次", reasons[0])

    def test_a_latch_reported_then_reset_then_a_full_stop_and_reset_unseen_is_one_more_alert(self):
        runtime = self.runtime()
        reporter = self.reporter(runtime)
        reporter.tick()
        runtime.emergency_stop("第一次")
        self.step(reporter, ticks=3)
        runtime.reset({"hardware_authorized": True})
        runtime.emergency_stop("没人看见的一次")
        runtime.reset({"hardware_authorized": True})
        self.step(reporter, IMMEDIATE_MIN_GAP_S, ticks=3)
        reasons = self.kinds_and_reasons()
        self.assertEqual(len(reasons), 2, reasons)
        self.assertIn("已解除", reasons[1])


if __name__ == "__main__":
    unittest.main()
