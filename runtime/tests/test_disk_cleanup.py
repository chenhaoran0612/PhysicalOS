"""Clean-up when the disk is nearly full (issue #25, R7-13).

Order: operation records, then task results, safety event records last; inside a class the
oldest go first; it stops as soon as enough space is free. Every clean-up leaves a record
saying which class and which period. Disk usage is faked: ``total`` bytes, of which other
software holds ``other`` and the records hold what their files really occupy.
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _support  # noqa: F401  (path bootstrap)

from neuxmind_runtime.adapters.sim_quadruped import SimQuadruped
from neuxmind_runtime.core import EdgeRuntime
from neuxmind_runtime.diskguard import CLEANUP_ORDER, DiskGuard
from neuxmind_runtime.events import EventBus
from neuxmind_runtime.records import CLEANUP, OPERATION, SAFETY, TASK, DeviceRecords, RecordsConfig

T0 = 1_800_000_000.0
TOTAL = 1_000_000
LINE = 0.90                                 # nearly full = 90 % used: at most 100 000 bytes may be free


class FakeDisk:
    """A disk of ``total`` bytes shared with other software."""

    def __init__(self, root: Path, total: int = TOTAL, other: int = 0):
        self.root, self.total, self.other = root, total, other
        self.calls = 0
        self.fail = False

    def records_bytes(self) -> int:
        total = 0
        for path in self.root.rglob("*"):
            try:
                total += path.stat().st_size if path.is_file() else 0
            except FileNotFoundError:                             # renamed by the writer meanwhile
                pass
        return total

    def __call__(self, _path):
        self.calls += 1
        if self.fail:
            raise OSError(5, "Input/output error")
        used = self.other + self.records_bytes()
        return self.total, used, max(0, self.total - used)

    def fill_until_free(self, free: int) -> None:
        """Other software grows until only ``free`` bytes are left."""
        self.other = self.total - self.records_bytes() - free


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "records"
        self.records = DeviceRecords(self.root, RecordsConfig(flush_period_s=3600.0, segment_bytes=2_000,
                                                              coalesce_s=0.0))
        self.records.start()
        self.addCleanup(self.records.close)
        self.bus = EventBus()
        self.disk = FakeDisk(self.root)
        self.now = T0 + 10_000
        self.guard = DiskGuard(self.records, self.bus, self.root, full_ratio=LINE, usage=self.disk,
                               wall=lambda: self.now)

    def tearDown(self):
        self._tmp.cleanup()

    def fill(self, per_class: int = 200) -> dict:
        """``per_class`` records in every class, one per second from T0."""
        for i in range(per_class):
            self.records.operation({"ts": T0 + i, "operation": "skill.run", "i": i, "pad": "x" * 60})
            self.records.task_result({"id": f"run-{i}", "status": "succeeded", "ended_at": T0 + i,
                                      "result": {"pad": "x" * 60}})
            self.records.offer_event({"seq": i + 1, "ts": T0 + i, "type": "safety.estop", "pad": "x" * 60})
        self.records.flush()
        return self.counts()

    def counts(self) -> dict:
        classes = self.records.describe()["classes"]
        return {name: classes[name]["count"] for name in (OPERATION, TASK, SAFETY)}

    def types(self) -> list[str]:
        return [e["type"] for e in self.bus.since(0, 1000)]

    def test_the_order_is_fixed(self):
        self.assertEqual(CLEANUP_ORDER, (OPERATION, TASK, SAFETY))

    def test_nothing_is_cleaned_below_the_line(self):
        before = self.fill()
        self.disk.fill_until_free(100_001)                        # 89.9999 % used
        self.assertEqual(self.guard.check(), [])
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.types(), [])
        view = self.guard.describe()
        self.assertFalse(view["low"])
        self.assertEqual((view["disk"]["total_bytes"], view["disk"]["free_bytes"]), (TOTAL, 100_001))
        self.assertEqual(view["full_percent"], 90.0)
        self.assertIsNone(view["last_cleanup"])

    def test_operation_records_go_first_oldest_first_and_it_stops_when_enough(self):
        before = self.fill()
        self.disk.fill_until_free(100_000 - 5_000)                # 5 000 bytes over the line
        done = self.guard.check()
        self.assertEqual([entry["class"] for entry in done], [OPERATION])
        entry = done[0]
        after = self.counts()
        self.assertEqual((after[TASK], after[SAFETY]), (before[TASK], before[SAFETY]))      # untouched
        self.assertEqual(after[OPERATION], before[OPERATION] - entry["records"])
        self.assertGreater(after[OPERATION], 0)                    # it stopped: not everything was removed
        self.assertGreaterEqual(entry["bytes"], 5_000)
        self.assertLess(entry["bytes"], 5_000 + 2 * 2_000 + 500)   # no more than it needed (segment-sized steps)
        # oldest first: what is left starts right after the cleaned period
        left = self.records.query(OPERATION, limit=1)["records"][0]
        self.assertEqual(entry["from_ts"], T0)
        self.assertEqual(left["ts"], entry["to_ts"] + 1)
        self.assertEqual(left["id"], entry["last_id"] + 1)
        self.assertTrue(entry["enough"])
        self.assertFalse(self.guard.describe()["low"])
        self.assertEqual(self.guard.check(), [])                   # enough space again: nothing more happens

    def test_task_results_follow_and_safety_events_go_last(self):
        before = self.fill()
        sizes = {name: self.records.describe()["classes"][name]["bytes"] for name in (OPERATION, TASK, SAFETY)}
        # more than all operation records hold, less than operation + task
        self.disk.fill_until_free(100_000 - sizes[OPERATION] - 3_000)
        done = self.guard.check()
        self.assertEqual([entry["class"] for entry in done], [OPERATION, TASK])
        after = self.counts()
        self.assertEqual(after[OPERATION], 0)
        self.assertGreater(after[TASK], 0)
        self.assertLess(after[TASK], before[TASK])
        self.assertEqual(after[SAFETY], before[SAFETY])
        self.assertEqual((done[0]["from_ts"], done[0]["to_ts"]), (T0, T0 + 199))
        self.assertEqual(done[0]["records"], 200)
        # now even the task results are not enough
        self.disk.fill_until_free(100_000 - sizes[TASK] - 3_000)
        done = self.guard.check()
        self.assertEqual([entry["class"] for entry in done], [TASK, SAFETY])
        after = self.counts()
        self.assertEqual((after[OPERATION], after[TASK]), (0, 0))
        self.assertGreater(after[SAFETY], 0)
        self.assertLess(after[SAFETY], before[SAFETY])
        newest = self.records.query(SAFETY, limit=5000)["records"][-1]
        self.assertEqual(newest["ts"], T0 + 199)                   # the newest safety events are the last to go

    def test_every_cleanup_leaves_a_record_and_an_event(self):
        self.fill()
        self.disk.fill_until_free(100_000 - 5_000)
        done = self.guard.check()
        log = self.records.query(CLEANUP)["records"]
        self.assertEqual(len(log), 1)
        entry = log[0]
        self.assertEqual((entry["class"], entry["from_ts"], entry["to_ts"], entry["records"]),
                         (OPERATION, done[0]["from_ts"], done[0]["to_ts"], done[0]["records"]))
        self.assertEqual(entry["ts"], self.now)
        self.assertEqual(entry["full_percent"], 90.0)
        self.assertLess(entry["free_bytes_before"], 100_000)
        self.assertGreater(done[0]["free_bytes_after"], 100_000)
        events = self.bus.since(0, 100)
        self.assertEqual([e["type"] for e in events],
                         ["storage.disk_low", "storage.records_cleaned", "storage.disk_ok"])
        cleaned = events[1]
        self.assertEqual((cleaned["class"], cleaned["from_ts"], cleaned["to_ts"], cleaned["records"]),
                         (OPERATION, entry["from_ts"], entry["to_ts"], entry["records"]))
        self.assertEqual((cleaned["enough"], cleaned["free_bytes_after"]), (True, done[0]["free_bytes_after"]))
        self.assertEqual((events[0]["cleanup_can_help"], events[0]["full_percent"]), (True, 90.0))
        view = self.guard.describe()
        self.assertEqual(view["last_cleanup"]["class"], OPERATION)
        self.assertEqual(view["cleanups"], 1)

    def test_the_cleanup_log_survives_a_restart(self):
        self.fill()
        self.disk.fill_until_free(100_000 - 5_000)
        self.guard.check()
        self.records.close()
        again = DeviceRecords(self.root, RecordsConfig(flush_period_s=3600.0, segment_bytes=2_000))
        again.start()
        self.addCleanup(again.close)
        guard = DiskGuard(again, EventBus(), self.root, full_ratio=LINE, usage=self.disk)
        self.assertEqual(guard.describe()["last_cleanup"]["class"], OPERATION)
        self.assertEqual(len(guard.history()), 1)

    def test_the_entry_is_on_record_before_anything_is_removed(self):
        self.fill()
        self.disk.fill_until_free(100_000 - 5_000)
        seen = []
        real_drop = self.records.drop_oldest

        def watched(name):
            seen.append((name, [e["class"] for e in self.records.query(CLEANUP)["records"]]))
            return real_drop(name)

        self.records.drop_oldest = watched
        self.guard.check()
        self.assertTrue(seen)
        self.assertEqual(seen[0], (OPERATION, [OPERATION]))        # logged first, removed second

    def test_when_the_records_cannot_free_enough_none_are_removed(self):
        """Something else filled the disk: deleting every record would not bring it under the line."""
        before = self.fill(per_class=20)
        self.disk.fill_until_free(10)
        self.assertEqual(self.guard.check(), [])
        self.assertEqual(self.counts(), before)
        view = self.guard.describe()
        self.assertEqual((view["low"], view["cleanup_can_help"], view["last_cleanup"]), (True, False, None))
        events = self.bus.since(0, 1000)
        self.assertEqual([e["type"] for e in events], ["storage.disk_low"])
        self.assertIs(events[0]["cleanup_can_help"], False)
        self.assertGreater(events[0]["records_bytes"], 0)
        calls_before = self.disk.calls
        for _ in range(5):
            self.assertEqual(self.guard.check(), [])
        self.assertEqual(len(self.bus.since(0, 1000)), 1)          # no event flood
        self.assertEqual(self.disk.calls, calls_before + 5)        # one measurement per check
        self.assertEqual(self.records.query(CLEANUP)["records"], [])
        self.disk.other = 0                                        # whoever filled it made room again
        self.guard.check()
        self.assertEqual([e["type"] for e in self.bus.since(0, 1000)], ["storage.disk_low", "storage.disk_ok"])
        self.assertEqual(self.counts(), before)

    def test_everything_can_go_when_that_is_what_it_takes(self):
        self.fill(per_class=40)
        held = sum(self.records.describe()["classes"][name]["bytes"] for name in (OPERATION, TASK, SAFETY))
        self.disk.fill_until_free(100_000 + 2_000 - held)          # only all of it gets back to the line
        done = self.guard.check()
        self.assertEqual([entry["class"] for entry in done], [OPERATION, TASK, SAFETY])
        self.assertEqual(self.counts(), {OPERATION: 0, TASK: 0, SAFETY: 0})
        self.assertEqual([entry["records"] for entry in done], [40, 40, 40])

    def test_records_written_after_a_cleanup_get_new_ids(self):
        self.fill(per_class=20)
        operations = self.records.describe()["classes"][OPERATION]["bytes"]
        self.disk.fill_until_free(100_000 - operations - 500)      # all operation records have to go
        done = self.guard.check()
        self.assertEqual((done[0]["class"], done[0]["records"]), (OPERATION, 20))
        self.disk.other = 0
        self.records.operation({"ts": T0 + 500, "operation": "reset"})
        self.records.flush()
        self.assertEqual(self.records.query(OPERATION)["records"][0]["id"], 21)

    def test_exactly_at_the_line_counts_as_nearly_full(self):
        before = self.fill()
        self.disk.fill_until_free(100_000)                         # 90.000 % used
        done = self.guard.check()
        self.assertEqual([entry["class"] for entry in done], [OPERATION])
        self.assertLess(self.counts()[OPERATION], before[OPERATION])
        self.assertFalse(self.guard.describe()["low"])

    def test_a_class_that_cannot_be_removed_is_reported_and_the_next_one_is_used(self):
        before = self.fill()
        self.disk.fill_until_free(100_000 - 5_000)
        real_drop = self.records.drop_oldest

        def stuck(name):
            if name == OPERATION:
                raise OSError(13, "Permission denied")
            return real_drop(name)

        self.records.drop_oldest = stuck
        done = self.guard.check()
        self.assertEqual([entry["class"] for entry in done], [TASK])
        self.assertEqual(self.counts()[OPERATION], before[OPERATION])
        self.assertIn("Permission denied", self.guard.describe()["cleanup_error"])
        log = self.records.query(CLEANUP)["records"]               # the entry written ahead is corrected
        self.assertEqual([(e["type"], e["class"]) for e in log][:2],
                         [("storage.records_cleaned", OPERATION), ("storage.cleanup_incomplete", OPERATION)])
        self.assertEqual((log[1]["removed_records"], log[1]["planned_records"]), (0, log[0]["records"]))

    def test_an_entry_the_disk_does_not_take_is_kept_and_written_later(self):
        self.fill()
        self.disk.fill_until_free(100_000 - 5_000)
        real_log = self.records.log_cleanup
        self.records.log_cleanup = mock.Mock(side_effect=OSError(28, "No space left on device"))
        done = self.guard.check()
        self.assertEqual([entry["class"] for entry in done], [OPERATION])       # the removal went ahead
        self.assertEqual(self.records.query(CLEANUP)["records"], [])
        view = self.guard.describe()
        self.assertEqual((view["unsaved_cleanups"], view["last_cleanup"]["class"]), (1, OPERATION))
        self.assertEqual([entry["class"] for entry in self.guard.history()], [OPERATION])
        self.records.log_cleanup = real_log
        self.guard.check()                                         # next period: the disk takes it
        self.assertEqual(self.guard.describe()["unsaved_cleanups"], 0)
        self.assertEqual([entry["class"] for entry in self.records.query(CLEANUP)["records"]], [OPERATION])

    def test_a_measurement_that_fails_in_the_middle_stops_the_cleaning(self):
        before = self.fill()
        sizes = {name: self.records.describe()["classes"][name]["bytes"] for name in (OPERATION, TASK, SAFETY)}
        self.disk.fill_until_free(100_000 - sizes[OPERATION] - 3_000)           # operation and task would go
        real_usage, calls = self.disk, {"n": 0}

        def once_then_fail(path):
            calls["n"] += 1
            if calls["n"] > 1:
                raise OSError(5, "Input/output error")
            return real_usage(path)

        self.guard.usage = once_then_fail
        done = self.guard.check()
        self.assertEqual([entry["class"] for entry in done], [OPERATION])       # never deletes blind
        self.assertEqual((self.counts()[TASK], self.counts()[SAFETY]), (before[TASK], before[SAFETY]))

    def test_a_failing_measurement_cleans_nothing(self):
        before = self.fill()
        self.disk.fail = True
        self.assertEqual(self.guard.check(), [])
        self.assertEqual(self.counts(), before)
        view = self.guard.describe()
        self.assertIn("Input/output error", view["error"])
        self.assertEqual(self.types(), ["storage.check_failed"])
        self.guard.check()
        self.assertEqual(self.types(), ["storage.check_failed"])   # reported once, not every period
        self.disk.fail = False
        self.guard.check()
        self.assertIsNone(self.guard.describe()["error"])

    def test_a_nonsense_measurement_cleans_nothing(self):
        before = self.fill()
        for bogus in ((0, 0, 0), (-5, 1, 1), (100, 0, 500), None, ("a", "b", "c")):
            guard = DiskGuard(self.records, self.bus, self.root, full_ratio=LINE, usage=lambda _p, b=bogus: b)
            self.assertEqual(guard.check(), [], bogus)
        self.assertEqual(self.counts(), before)

    def test_the_line_must_be_sane(self):
        for bad in (0.0, 0.3, 1.0, 1.5, float("nan"), "0.9", None, True):
            with self.assertRaises(ValueError, msg=repr(bad)):
                DiskGuard(self.records, self.bus, self.root, full_ratio=bad, usage=self.disk)


class RuntimeCleanupTests(unittest.TestCase):
    def test_housekeeping_runs_the_guard_and_status_shows_it(self):
        with tempfile.TemporaryDirectory() as state:
            runtime = EdgeRuntime(SimQuadruped(), state, start_monitor=False, sandbox="inprocess",
                                  records=RecordsConfig(segment_bytes=2_000, disk_full_ratio=0.80))
            try:
                root = Path(state) / "records"
                disk = FakeDisk(root)
                runtime.disk_guard.usage = disk
                for i in range(100):
                    runtime.records.operation({"ts": T0 + i, "operation": "skill.run", "pad": "x" * 60})
                runtime.records.flush()
                disk.fill_until_free(200_000 - 3_000)              # the line is 80 % here
                runtime.housekeeping_tick()
                status = runtime.status()
                self.assertEqual(status["storage"]["full_percent"], 80.0)
                self.assertEqual(status["storage"]["last_cleanup"]["class"], OPERATION)
                self.assertLess(status["records"]["classes"][OPERATION]["count"], 100)
                self.assertTrue(any(e["type"] == "storage.records_cleaned" for e in runtime.events.since(0, 1000)))
            finally:
                runtime.close()

    def test_a_broken_guard_does_not_stop_housekeeping(self):
        with tempfile.TemporaryDirectory() as state:
            runtime = EdgeRuntime(SimQuadruped(), state, start_monitor=False, sandbox="inprocess")
            try:
                calls = []
                runtime.disk_guard.check = lambda: 1 / 0
                runtime.housekeeping.append(lambda: calls.append("log"))
                runtime.housekeeping_tick()
                self.assertEqual(calls, ["log"])
                self.assertTrue(any(e["type"] == "runtime.monitor_error" and "housekeeping" in e["error"]
                                    for e in runtime.events.since(0, 1000)))
            finally:
                runtime.close()


if __name__ == "__main__":
    unittest.main()
