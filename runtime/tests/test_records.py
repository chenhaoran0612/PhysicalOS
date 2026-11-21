"""Records kept on the device (issue #25): safety events, task results and operation records.

Each class has its own append-only log under ``<state-dir>/records/<class>/``. Unlike the
telemetry ring they are never evicted by count or age, and they can be fetched by time range
after a reconnect. Writing them must never be able to delay or break a stop.
"""
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import _support  # noqa: F401  (path bootstrap)
from _support import wait_until

from neuxmind_runtime import records as records_module
from neuxmind_runtime.events import EventBus
from neuxmind_runtime.records import (CLASSES, MAX_RECORD_BYTES, OPERATION, SAFETY, TASK, DeviceRecords, RecordLog,
                                      RecordsConfig, classify_operation)

T0 = 1_800_000_000.0          # an arbitrary wall-clock origin for records with explicit times


def rec(ts: float, type_: str = "safety.estop", **data) -> dict:
    return {"ts": ts, "type": type_, **data}


class RecordLogTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "safety"

    def tearDown(self):
        self._tmp.cleanup()

    def log(self, segment_bytes: int = 4096) -> RecordLog:
        log = RecordLog(self.dir, segment_bytes=segment_bytes)
        log.open()
        self.addCleanup(log.close)
        return log

    def test_ids_increase_and_everything_is_read_back_in_order(self):
        log = self.log()
        log.append([rec(T0 + i, i=i) for i in range(5)])
        log.append([rec(T0 + 5, i=5)])
        page = log.query()
        self.assertEqual([r["id"] for r in page["records"]], [1, 2, 3, 4, 5, 6])
        self.assertEqual([r["i"] for r in page["records"]], list(range(6)))
        self.assertEqual((page["more"], page["next_after"]), (False, 6))

    def test_nothing_is_evicted_however_many_records_arrive(self):
        """The telemetry ring keeps the newest 100k events; these logs keep every record."""
        log = self.log(segment_bytes=256 << 10)
        total = 100_000 + 5_000
        for start in range(0, total, 5_000):
            log.append([rec(T0 + i, i=i) for i in range(start, start + 5_000)])
        stats = log.stats()
        self.assertEqual((stats["count"], stats["first_id"], stats["last_id"]), (total, 1, total))
        first = log.query(limit=1)["records"][0]
        self.assertEqual((first["id"], first["i"], first["ts"]), (1, 0, T0))
        self.assertGreater(stats["segments"], 1)

    def test_fetch_by_time_range_with_a_cursor(self):
        log = self.log(segment_bytes=600)                       # several small segments
        log.append([rec(T0 + 60 * i, minute=i) for i in range(120)])
        hour = log.query(since_ts=T0 + 60 * 30, until_ts=T0 + 60 * 90, limit=1000)
        self.assertEqual([r["minute"] for r in hour["records"]], list(range(30, 90)))   # [since, until)
        self.assertFalse(hour["more"])
        got, after = [], 0
        while True:
            page = log.query(since_ts=T0 + 60 * 30, until_ts=T0 + 60 * 90, after=after, limit=7)
            got += page["records"]
            after = page["next_after"]
            if not page["more"]:
                break
        self.assertEqual(got, hour["records"])
        self.assertEqual(log.query(since_ts=T0 + 60 * 500)["records"], [])

    def test_records_and_ids_survive_a_restart(self):
        log = RecordLog(self.dir, segment_bytes=400)
        log.open()
        log.append([rec(T0 + i, i=i) for i in range(20)])
        log.close()
        again = self.log(segment_bytes=400)
        self.assertEqual(again.stats()["count"], 20)
        again.append([rec(T0 + 20, i=20)])
        ids = [r["id"] for r in again.query(limit=100)["records"]]
        self.assertEqual(ids, list(range(1, 22)))

    def test_a_torn_last_line_after_a_crash_is_skipped(self):
        log = RecordLog(self.dir, segment_bytes=1 << 20)
        log.open()
        log.append([rec(T0 + i, i=i) for i in range(3)])
        open_file = next(self.dir.glob("*.open"))
        log.close()
        with open(open_file, "ab") as fh:
            fh.write(b'{"id": 4, "ts": 18000')                    # power cut in the middle of a line
        again = self.log()
        self.assertEqual([r["id"] for r in again.query()["records"]], [1, 2, 3])
        again.append([rec(T0 + 9, i=9)])
        self.assertEqual([(r["id"], r["i"]) for r in again.query()["records"]][-1], (4, 9))

    def test_a_failed_write_leaves_no_partial_line_and_can_be_retried(self):
        log = self.log()
        log.append([rec(T0, i=0)])
        real_write = os.write
        calls = {"n": 0}

        def flaky(fd, data):
            calls["n"] += 1
            if calls["n"] == 1:
                real_write(fd, data[:10])                         # some bytes reach the disk, then it is full
                raise OSError(28, "No space left on device")
            return real_write(fd, data)

        with mock.patch.object(records_module.os, "write", flaky):
            with self.assertRaises(OSError):
                log.append([rec(T0 + 1, i=1)])
            log.append([rec(T0 + 1, i=1)])
        self.assertEqual([(r["id"], r["i"]) for r in log.query()["records"]], [(1, 0), (2, 1)])
        for path in self.dir.iterdir():
            if path.suffix in (".open", ".jsonl"):
                for line in path.read_bytes().splitlines():
                    json.loads(line)                              # every line on disk is a whole record

    def test_drop_oldest_reports_what_was_removed(self):
        log = self.log(segment_bytes=300)
        log.append([rec(T0 + i, i=i) for i in range(30)])
        before = log.stats()
        dropped = log.drop_oldest()
        self.assertEqual(dropped["first_id"], 1)
        self.assertEqual(dropped["from_ts"], T0)
        self.assertGreater(dropped["records"], 0)
        self.assertGreater(dropped["bytes"], 0)
        after = log.stats()
        self.assertEqual(after["count"], before["count"] - dropped["records"])
        self.assertEqual(after["first_id"], dropped["last_id"] + 1)
        self.assertEqual(after["oldest_ts"], dropped["to_ts"] + 1)
        self.assertEqual(log.query(limit=1)["records"][0]["id"], dropped["last_id"] + 1)

    def test_ids_are_never_reused_after_everything_was_dropped(self):
        log = RecordLog(self.dir, segment_bytes=300)
        log.open()
        log.append([rec(T0 + i, i=i) for i in range(10)])
        while log.drop_oldest() is not None:
            pass
        self.assertEqual(log.stats()["count"], 0)
        log.append([rec(T0 + 50)])
        self.assertEqual(log.query()["records"][0]["id"], 11)
        while log.drop_oldest() is not None:
            pass
        log.close()
        again = self.log()
        again.append([rec(T0 + 60)])
        self.assertEqual(again.query()["records"][0]["id"], 12)   # a cursor a client holds stays valid

    def test_files_it_did_not_create_are_left_alone(self):
        self.dir.mkdir(parents=True)
        stranger = self.dir / "notes.txt"
        stranger.write_text("not ours")
        log = self.log(segment_bytes=200)
        log.append([rec(T0 + i) for i in range(20)])
        while log.drop_oldest() is not None:
            pass
        self.assertEqual(stranger.read_text(), "not ours")

    def test_non_finite_numbers_do_not_lose_the_record(self):
        log = self.log()
        log.append([rec(T0, value=float("nan"), nested={"x": float("inf")})])
        got = log.query()["records"][0]
        self.assertEqual((got["id"], got["value"], got["nested"]["x"]), (1, None, None))

    def test_text_that_is_not_valid_unicode_does_not_lose_the_record(self):
        """A client can send a lone surrogate in a JSON string; the record must keep its fields."""
        log = self.log()
        log.append([rec(T0, type_="operation", operation="reset", request_id="id-\ud800", outcome="rejected")])
        got = log.query()["records"][0]
        self.assertEqual((got["operation"], got["outcome"], got["request_id"]), ("reset", "rejected", "id-\ud800"))

    def test_a_value_json_cannot_take_keeps_what_can_be_kept(self):
        class Broken:
            def __str__(self):
                raise RuntimeError("no text")

        log = self.log()
        log.append([rec(T0, type_="task.result", run_id="run-1", result=Broken())])
        got = log.query()["records"][0]
        self.assertEqual((got["id"], got["type"], got["data_dropped"]), (1, "task.result", "unserialisable"))

    def test_an_oversized_record_keeps_its_short_fields(self):
        log = self.log(segment_bytes=1 << 20)
        log.append([rec(T0, type_="task.result", run_id="run-1", status="succeeded",
                        result={"blob": "x" * (MAX_RECORD_BYTES + 10)})])
        got = log.query()["records"][0]
        self.assertEqual((got["run_id"], got["status"], got["truncated"]), ("run-1", "succeeded", True))
        self.assertNotIn("result", got)

    def test_a_time_out_of_range_is_filed_under_zero(self):
        log = self.log(segment_bytes=64)
        log.append([rec(1e300, i=0), rec(-5, i=1), rec(T0, i=2)])
        log.close()
        again = self.log()                                        # every segment name is still recognised
        self.assertEqual([(r["i"], r["ts"]) for r in again.query()["records"]], [(0, 0.0), (1, 0.0), (2, T0)])

    def test_a_clock_that_steps_back_does_not_hide_records(self):
        log = self.log(segment_bytes=300)
        stamps = [T0 + 100, T0 + 101, T0 + 5, T0 + 6, T0 + 102, T0 + 7] * 5      # the clock was set back and forth
        log.append([rec(ts, i=i) for i, ts in enumerate(stamps)])
        early = log.query(since_ts=T0, until_ts=T0 + 50, limit=1000)["records"]
        self.assertEqual([r["i"] for r in early], [i for i, ts in enumerate(stamps) if ts < T0 + 50])
        self.assertEqual([r["id"] for r in early], sorted(r["id"] for r in early))
        late = log.query(since_ts=T0 + 100, limit=1000)["records"]
        self.assertEqual(len(early) + len(late), len(stamps))
        self.assertEqual((log.stats()["oldest_ts"], log.stats()["newest_ts"]), (T0 + 5, T0 + 102))

    def test_a_batch_that_fails_half_way_says_how_much_is_stored(self):
        log = self.log(segment_bytes=300)                         # the batch needs several segments
        real_write = os.write
        calls = {"n": 0}

        def second_write_fails(fd, data):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError(28, "No space left on device")
            return real_write(fd, data)

        batch = [rec(T0 + i, i=i) for i in range(20)]
        with mock.patch.object(records_module.os, "write", second_write_fails):
            with self.assertRaises(OSError) as caught:
                log.append(batch)
        stored = caught.exception.stored
        self.assertGreater(stored, 0)
        self.assertLess(stored, 20)
        self.assertEqual([r["i"] for r in log.query()["records"]], list(range(stored)))
        log.append(batch[stored:])                                # the rest, as the writer does
        self.assertEqual([(r["id"], r["i"]) for r in log.query()["records"]], [(i + 1, i) for i in range(20)])

    def test_a_write_that_can_be_neither_finished_nor_undone_never_repeats_an_id(self):
        log = self.log()
        log.append([rec(T0, i=0)])
        with mock.patch.object(records_module.os, "fsync", side_effect=OSError(5, "Input/output error")), \
                mock.patch.object(records_module.os, "ftruncate", side_effect=OSError(5, "Input/output error")):
            with self.assertRaises(OSError):
                log.append([rec(T0 + 1, i=1)])                    # the line is in the file, but nobody knows
        log.append([rec(T0 + 1, i=1)])
        ids = [r["id"] for r in log.query()["records"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(ids, sorted(ids))

    def test_an_answer_is_limited_in_bytes_and_says_there_is_more(self):
        log = self.log(segment_bytes=1 << 20)
        log.append([rec(T0 + i, i=i, pad="x" * 1000) for i in range(50)])
        page = log.query(max_bytes=10_000)
        self.assertTrue(page["more"])
        self.assertLess(len(page["records"]), 12)
        rest = log.query(after=page["next_after"], limit=1000)
        self.assertEqual(len(page["records"]) + len(rest["records"]), 50)

    def test_a_leftover_that_cannot_be_read_is_kept_and_its_ids_are_not_reused(self):
        log = RecordLog(self.dir, segment_bytes=1 << 20)
        log.open()
        log.append([rec(T0 + i, i=i) for i in range(5)])
        leftover = next(self.dir.glob("*.open"))
        log.close()
        size = leftover.stat().st_size
        leftover.write_bytes(b"\x00" * size)                      # the flash block came back as zeros
        again = self.log()
        kept = list(self.dir.glob("*.unreadable"))
        self.assertEqual([p.stat().st_size for p in kept], [size])       # set aside, not deleted
        self.assertEqual(again.skipped_files, 1)
        again.append([rec(T0 + 9, i=9)])
        self.assertGreater(again.query()["records"][0]["id"], 5)          # a cursor at 5 still sees it

    def test_a_read_error_at_start_up_does_not_delete_the_leftover(self):
        log = RecordLog(self.dir, segment_bytes=1 << 20)
        log.open()
        log.append([rec(T0, i=0)])
        leftover = next(self.dir.glob("*.open"))
        log.close()
        content = leftover.read_bytes()
        real_open = open

        def failing_open(path, *args, **kwargs):
            if str(path) == str(leftover):
                raise OSError(5, "Input/output error")
            return real_open(path, *args, **kwargs)

        with mock.patch("builtins.open", failing_open):
            again = self.log()
        self.assertEqual([p.read_bytes() for p in self.dir.glob("*.unreadable")], [content])
        self.assertEqual(again.stats()["count"], 0)

    def test_a_segment_name_that_is_not_a_file_does_not_disable_the_log(self):
        log = RecordLog(self.dir, segment_bytes=200)
        log.open()
        log.append([rec(T0 + i, i=i) for i in range(10)])
        log.close()
        (self.dir / "seg-0000000000000500-0000000000000600-1-2-3.jsonl").mkdir()
        os.symlink(self.dir / "nowhere", self.dir / "seg-0000000000000700-0000000000000800-1-2-3.jsonl")
        again = self.log(segment_bytes=200)
        self.assertEqual(again.stats()["count"], 10)
        self.assertEqual(again.skipped_files, 2)
        again.append([rec(T0 + 50)])
        self.assertEqual(again.query(limit=100)["records"][-1]["id"], 801)       # beyond every id a name claims


class DeviceRecordsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "records"

    def tearDown(self):
        self._tmp.cleanup()

    def records(self, **config) -> DeviceRecords:
        config.setdefault("flush_period_s", 3600.0)               # tests flush by hand
        records = DeviceRecords(self.root, RecordsConfig(**config))
        records.start()
        self.addCleanup(records.close)
        return records

    def test_each_class_is_stored_separately(self):
        records = self.records()
        bus = EventBus()
        bus.add_tap(records.offer_event)
        bus.emit("safety.estop", reason="operator")
        bus.emit("run.started", run_id="run-1")                   # state / run data: not a record
        bus.emit("runtime.monitor_error", error="x")
        records.task_result({"id": "run-1", "kind": "skill", "target": "patrol@1.0.0", "status": "succeeded",
                             "result": {"laps": 2}, "error": None, "ended_at": T0 + 5, "events": [{"big": 1}]})
        records.operation({"ts": T0 + 6, "operation": "reset", "outcome": "ok"})
        records.flush()
        safety = records.query(SAFETY)["records"]
        self.assertEqual([(r["type"], r["reason"]) for r in safety], [("safety.estop", "operator")])
        task = records.query(TASK)["records"]
        self.assertEqual([(r["type"], r["run_id"], r["status"], r["result"]) for r in task],
                         [("task.result", "run-1", "succeeded", {"laps": 2})])
        self.assertNotIn("events", task[0])                       # the run's event list is run data
        self.assertEqual([r["operation"] for r in records.query(OPERATION)["records"]], ["reset"])
        for name in CLASSES:
            self.assertTrue((self.root / name).is_dir(), name)
            self.assertEqual(records.describe()["classes"][name]["count"], 1)

    def test_unknown_class_is_refused(self):
        records = self.records()
        with self.assertRaises(KeyError):
            records.query("telemetry")

    def test_offering_never_raises_and_never_touches_the_disk(self):
        records = self.records()
        with mock.patch.object(records_module.os, "write") as write, \
                mock.patch.object(records_module.os, "open") as opened, \
                mock.patch.object(records_module.os, "fsync") as fsync, \
                mock.patch.object(records_module.os, "listdir") as listed:
            records.offer_event({"seq": 1, "ts": T0, "type": "safety.estop"})
            records.offer_event({"seq": 2, "ts": T0})             # malformed: no type
            records.offer_event(None)
            records.operation({"operation": "stop"})
            records.task_result({"id": "run-1"})
            records.task_result(object())
            for io_call in (write, opened, fsync, listed):
                io_call.assert_not_called()
        self.assertEqual(records.describe()["pending"], 3)

    def test_a_failing_disk_keeps_the_records_and_writes_them_later(self):
        records = self.records()
        records.offer_event({"seq": 1, "ts": T0, "type": "safety.estop"})
        with mock.patch.object(RecordLog, "append", side_effect=OSError(28, "No space left on device")):
            self.assertEqual(records.flush(), 0)
            records.offer_event({"seq": 2, "ts": T0 + 1, "type": "safety.reset"})
            self.assertEqual(records.flush(), 0)
        view = records.describe()
        self.assertEqual((view["pending"], view["dropped"]), (2, 0))
        self.assertGreaterEqual(view["write_errors"], 2)
        self.assertIn("No space left", view["last_write_error"])
        self.assertEqual(records.flush(), 2)
        self.assertEqual([r["seq"] for r in records.query(SAFETY)["records"]], [1, 2])

    def test_an_unusable_directory_degrades_and_recovers(self):
        self.root.parent.mkdir(parents=True, exist_ok=True)
        self.root.write_text("a file where the directory should be")
        records = self.records()
        records.offer_event({"seq": 1, "ts": T0, "type": "safety.estop"})
        self.assertEqual(records.flush(), 0)
        view = records.describe()
        self.assertTrue(view["degraded"])
        self.assertEqual(view["pending"], 1)
        self.assertEqual(self.root.read_text(), "a file where the directory should be")   # never deleted
        os.rename(self.root, str(self.root) + ".moved")           # the operator clears the path
        records.reopen_after_s = 0.0
        self.assertEqual(records.flush(), 1)
        self.assertFalse(records.describe()["degraded"])
        self.assertEqual(len(records.query(SAFETY)["records"]), 1)

    def test_a_second_instance_on_the_same_directory_touches_nothing(self):
        first = self.records()
        first.operation({"ts": T0, "operation": "reset"})
        first.flush()
        before = sorted(p.name for p in (self.root / OPERATION).iterdir())
        second = DeviceRecords(self.root, RecordsConfig(flush_period_s=3600.0))
        self.assertFalse(second.start())
        self.addCleanup(second.close)
        second.operation({"ts": T0 + 1, "operation": "stop"})
        self.assertEqual(second.flush(), 0)
        self.assertTrue(second.describe()["degraded"])
        self.assertIn("另一个运行时实例", second.describe()["degraded_reason"])
        self.assertEqual(sorted(p.name for p in (self.root / OPERATION).iterdir()), before)   # nothing renamed
        first.operation({"ts": T0 + 2, "operation": "skill.run"})
        first.flush()
        self.assertEqual([r["operation"] for r in first.query(OPERATION)["records"]], ["reset", "skill.run"])
        first.close()                                             # the first runtime is gone
        second.reopen_after_s = 0.0
        self.assertEqual(second.flush(), 1)                       # the second takes over, with the next id
        self.assertEqual([(r["id"], r["operation"]) for r in second.query(OPERATION)["records"]],
                         [(1, "reset"), (2, "skill.run"), (3, "stop")])

    def test_bursts_of_limit_notes_are_merged_with_a_count(self):
        clock = {"t": T0}
        records = DeviceRecords(self.root, RecordsConfig(flush_period_s=3600.0, coalesce_s=10.0),
                                wall=lambda: clock["t"])
        records.start()
        self.addCleanup(records.close)
        for i in range(100):                                      # a client holding the stick beyond the limit
            records.offer_event({"seq": i + 1, "ts": T0 + i * 0.05, "type": "safety.limited",
                                 "capability": "base.velocity", "field": "vx"})
        records.offer_event({"seq": 200, "ts": T0 + 2, "type": "safety.estop", "reason": "a"})
        records.offer_event({"seq": 201, "ts": T0 + 2.1, "type": "safety.estop", "reason": "b"})
        clock["t"] = T0 + 5
        records.flush()
        got = records.query(SAFETY)["records"]
        self.assertEqual([r["type"] for r in got], ["safety.limited", "safety.estop", "safety.estop"])
        clock["t"] = T0 + 11                                      # the 10 s window has passed
        records.flush()
        summary = records.query(SAFETY)["records"][-1]
        self.assertEqual((summary["type"], summary["repeats"], summary["summary"]), ("safety.limited", 99, True))
        self.assertEqual((summary["from_ts"], summary["capability"]), (T0, "base.velocity"))
        self.assertAlmostEqual(summary["to_ts"], T0 + 99 * 0.05)

    def test_bursts_are_kept_apart_by_kind_and_capability_and_start_again_after_the_window(self):
        clock = {"t": T0 + 30}
        records = DeviceRecords(self.root, RecordsConfig(flush_period_s=3600.0, coalesce_s=10.0),
                                wall=lambda: clock["t"])
        records.start()
        self.addCleanup(records.close)
        events = [(0.0, "safety.limited", "base.velocity"), (0.1, "safety.limited", "arm.ee_pose"),
                  (0.2, "safety.rejected", "base.velocity"), (0.3, "safety.limited", "base.velocity"),
                  (0.4, "safety.limited", "arm.ee_pose"), (12.0, "safety.limited", "base.velocity"),
                  (12.5, "safety.limited", "base.velocity")]
        for seq, (offset, kind, capability) in enumerate(events, 1):
            records.offer_event({"seq": seq, "ts": T0 + offset, "type": kind, "capability": capability})
        records.flush()
        got = [(r["type"], r["capability"], r.get("repeats")) for r in records.query(SAFETY)["records"]]
        self.assertEqual(sorted(got, key=str), sorted([
            ("safety.limited", "base.velocity", None), ("safety.limited", "arm.ee_pose", None),
            ("safety.rejected", "base.velocity", None),
            ("safety.limited", "base.velocity", 1),               # the first window of base.velocity, closed
            ("safety.limited", "base.velocity", None),            # 12 s later: a new window starts with a full record
            ("safety.limited", "arm.ee_pose", 1),
            ("safety.limited", "base.velocity", 1)], key=str))

    def test_merging_can_be_switched_off(self):
        records = self.records(coalesce_s=0.0)
        for i in range(5):
            records.offer_event({"seq": i + 1, "ts": T0 + i * 0.01, "type": "safety.limited", "capability": "base.velocity"})
        records.flush()
        self.assertEqual(len(records.query(SAFETY)["records"]), 5)

    def test_close_writes_what_is_pending_including_open_bursts(self):
        records = DeviceRecords(self.root, RecordsConfig(flush_period_s=3600.0))
        records.start()
        for i in range(3):
            records.offer_event({"seq": i + 1, "ts": T0 + i, "type": "safety.rejected", "capability": "arm.ee_pose"})
        records.close()
        again = self.records()
        got = again.query(SAFETY)["records"]
        self.assertEqual([(r["type"], r.get("repeats")) for r in got],
                         [("safety.rejected", None), ("safety.rejected", 2)])

    def test_the_writer_thread_flushes_on_its_own(self):
        records = self.records(flush_period_s=0.02)
        records.operation({"ts": T0, "operation": "skill.run", "outcome": "ok"})
        wait_until(lambda: records.describe()["classes"][OPERATION]["count"] == 1, 5)

    def test_describe_reads_cached_values_only(self):
        records = self.records()
        records.operation({"ts": T0, "operation": "reset"})
        records.flush()
        with mock.patch.object(records_module.os, "listdir", side_effect=AssertionError("IO")), \
                mock.patch.object(records_module.os, "open", side_effect=AssertionError("IO")), \
                mock.patch("builtins.open", side_effect=AssertionError("IO")):
            view = records.describe()
        self.assertEqual(view["classes"][OPERATION]["count"], 1)
        self.assertEqual(view["classes"][OPERATION]["oldest_ts"], T0)

    def test_a_stalled_writer_does_not_block_new_records(self):
        records = self.records()
        gate, entered = threading.Event(), threading.Event()
        real_append = RecordLog.append

        def stuck(self, batch):
            entered.set()
            gate.wait(10)
            return real_append(self, batch)

        records.offer_event({"seq": 1, "ts": T0, "type": "safety.estop"})
        with mock.patch.object(RecordLog, "append", stuck):
            writer = threading.Thread(target=records.flush, daemon=True)
            writer.start()
            self.assertTrue(entered.wait(5))
            started = time.monotonic()
            for i in range(1000):
                records.offer_event({"seq": i + 2, "ts": T0 + i, "type": "safety.halt"})
            self.assertLess(time.monotonic() - started, 0.5)
            gate.set()
            writer.join(5)
        records.flush()
        self.assertEqual(len(records.query(SAFETY, limit=5000)["records"]), 1001)


class ClassifyOperationTests(unittest.TestCase):
    def test_state_changing_requests_have_a_name(self):
        cases = {
            ("POST", "/v1/stop"): ("stop", None),
            ("POST", "/v2/stop"): ("stop", None),
            ("POST", "/v2/reset"): ("reset", None),
            ("POST", "/v1/reset"): ("reset", None),
            ("POST", "/v2/skills/install"): ("skill.install", None),
            ("POST", "/v2/skills/patrol/run"): ("skill.run", "patrol"),
            ("POST", "/v2/runs/run-1/cancel"): ("run.cancel", "run-1"),
            ("POST", "/v2/capabilities/base.navigate_to/invoke"): ("capability.invoke", "base.navigate_to"),
            ("POST", "/v1/deploy"): ("app.deploy", None),
            ("POST", "/v1/start"): ("app.start", None),
            ("POST", "/v1/evaluate"): ("app.evaluate", None),
            ("POST", "/v1/models/install"): ("model.install", None),
            ("DELETE", "/v2/models/abc"): ("model.delete", "abc"),
            ("POST", "/v2/conformance"): ("conformance.start", None),
        }
        for (method, path), expected in cases.items():
            self.assertEqual(classify_operation(method, path), expected, path)

    def test_reads_and_unknown_paths_are_not_operations(self):
        for method, path in (("GET", "/v1/status"), ("GET", "/v2/runs"), ("POST", "/v1/validate"),
                             ("POST", "/v2/nothing"), ("DELETE", "/v2/runs/run-1"), ("PUT", "/v2/reset")):
            self.assertIsNone(classify_operation(method, path), path)


if __name__ == "__main__":
    unittest.main()
