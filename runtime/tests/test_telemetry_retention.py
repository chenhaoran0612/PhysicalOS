"""State and run data are kept for one hour (issue #25, R1-09 / R7-12).

The telemetry ring used to be bounded by count only. It now also has an age limit: an event
older than ``retention_s`` is dropped and never served, and what is left can be fetched by time
range after a reconnect. The clock is injected, so an hour passes instantly here.
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import _support  # noqa: F401  (path bootstrap)

from neuxmind_runtime.adapters.sim_quadruped import SimQuadruped
from neuxmind_runtime.core import TELEMETRY_RETENTION_S, EdgeRuntime
from neuxmind_runtime.events import EventBus, RingBusy

HOUR = 3600.0
T0 = 1_800_000_000.0


class FakeWall:
    def __init__(self, start: float = T0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def stored(path) -> list[tuple]:
    db = sqlite3.connect(path)
    try:
        return db.execute("SELECT seq, ts FROM events ORDER BY seq").fetchall()
    finally:
        db.close()


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "telemetry.sqlite3"
        self.wall = FakeWall()

    def tearDown(self):
        self._tmp.cleanup()

    def bus(self, retention_s: float | None = HOUR, capacity: int = 100_000, maxlen: int = 5000) -> EventBus:
        bus = EventBus(maxlen=maxlen, wall=self.wall)
        bus.retention_s = retention_s
        bus.persist(self.path, capacity=capacity)
        self.addCleanup(bus.close)
        return bus

    def test_events_carry_the_injected_time(self):
        bus = self.bus()
        self.assertEqual(bus.emit("state.changed")["ts"], T0)
        self.wall.advance(5)
        self.assertEqual(bus.emit("state.changed")["ts"], T0 + 5)

    def test_older_than_one_hour_is_dropped(self):
        bus = self.bus()
        for i in range(10):
            bus.emit("state.changed", i=i)                       # at T0
        self.wall.advance(30 * 60)
        for i in range(10, 20):
            bus.emit("state.changed", i=i)                       # at T0 + 30 min
        bus.flush()
        self.wall.advance(29 * 60)                               # T0 + 59 min: everything is younger than 1 h
        self.assertEqual(bus.prune(), 0)
        self.assertEqual(len(stored(self.path)), 20)
        self.assertEqual(len(bus.since(0)), 20)
        self.wall.advance(2 * 60)                                # T0 + 61 min: the first ten are older than 1 h
        self.assertEqual([e["i"] for e in bus.since(0)], list(range(10, 20)))   # not served even before pruning
        self.assertEqual(bus.prune(), 10)
        self.assertEqual([ts for _seq, ts in stored(self.path)], [T0 + 30 * 60] * 10)
        view = bus.describe()
        self.assertEqual((view["retention_s"], view["oldest_ts"], view["evicted_by_age"]), (HOUR, T0 + 30 * 60, 10))
        self.assertEqual(view["oldest_seq"], 11)

    def test_nothing_is_missing_within_the_hour(self):
        bus = self.bus(maxlen=50)                                # far more events than the memory buffer
        for minute in range(59):
            for i in range(20):
                bus.emit("state.changed", minute=minute, i=i)
            bus.flush()
            bus.prune()
            self.wall.advance(60)
        got = bus.since(0, limit=10_000)
        self.assertEqual(len(got), 59 * 20)
        self.assertEqual([e["seq"] for e in got], list(range(1, 59 * 20 + 1)))   # no gap

    def test_fetch_by_time_range_after_a_reconnect(self):
        bus = self.bus(maxlen=10)
        for minute in range(90):                                 # 90 minutes without a connection
            bus.emit("state.changed", minute=minute)
            bus.flush()
            bus.prune()
            self.wall.advance(60)
        now = self.wall()
        offline = bus.window(T0, now, limit=1000)
        minutes = [e["minute"] for e in offline]
        self.assertEqual(minutes, list(range(30, 90)))           # only the last hour is still there
        bus.prune()
        self.assertEqual(bus.retention()["oldest_ts"], T0 + 30 * 60)   # ... and the device says from when
        part = bus.window(T0 + 40 * 60, T0 + 50 * 60)
        self.assertEqual([e["minute"] for e in part], list(range(40, 50)))       # [since, until)
        page, after, got = None, 0, []
        while page is None or len(page) == 4:
            page = bus.window(T0, now, after=after, limit=4)
            got += page
            after = page[-1]["seq"] if page else after
        self.assertEqual(got, offline)

    def test_window_includes_events_not_yet_on_disk(self):
        bus = self.bus()
        bus.emit("state.changed", i=0)
        bus.flush()
        self.wall.advance(1)
        bus.emit("state.changed", i=1)                           # still in memory
        self.assertEqual([e["i"] for e in bus.window(T0, T0 + 10)], [0, 1])

    def test_pruning_happens_without_new_events(self):
        bus = self.bus()
        bus.emit("state.changed")
        bus.flush()
        self.wall.advance(HOUR + 1)
        bus._next_prune = 0.0                                    # the writer's own timer is due
        bus._prune_if_due()
        self.assertEqual(bus.since(0), [])
        self.assertEqual(bus.window(0, self.wall() + 1), [])
        self.assertEqual(bus.retention(), {"oldest_seq": None, "oldest_ts": None})
        self.assertEqual(len(stored(self.path)), 1)              # the newest row stays as the seq anchor

    def test_a_clean_restart_after_an_idle_hour_has_no_seq_gap(self):
        """Pruning must not make a clean shutdown look like a crash (seq would jump by ~1.8e15)."""
        bus = EventBus(wall=self.wall)
        bus.retention_s = HOUR
        bus.persist(self.path)
        for i in range(5):
            bus.emit("state.changed", i=i)
        bus.flush()
        self.wall.advance(8 * HOUR)                              # parked overnight, nothing happens
        bus.prune()
        last = bus.last_seq
        bus.close()
        again = self.bus()
        self.assertEqual(again.start_kind, "clean")
        self.assertEqual(again.emit("state.changed")["seq"], last + 1)
        self.assertEqual([e["seq"] for e in again.since(0)], [last + 1])

    def test_the_oldest_time_never_names_an_expired_event(self):
        bus = EventBus(wall=self.wall)                           # memory only
        bus.retention_s = HOUR
        bus.emit("state.changed", i=0)
        self.wall.advance(2 * HOUR)
        self.assertEqual(bus.retention(), {"oldest_seq": None, "oldest_ts": None})
        fresh = bus.emit("state.changed", i=1)
        self.assertEqual(bus.retention(), {"oldest_seq": fresh["seq"], "oldest_ts": fresh["ts"]})

    def test_a_busy_ring_is_not_answered_as_if_memory_were_everything(self):
        bus = self.bus(maxlen=3)
        for i in range(10):
            bus.emit("state.changed", i=i)
        bus.flush()
        self.assertTrue(bus._read_lock.acquire(timeout=1))       # another reader holds the connection
        try:
            with self.assertRaises(RingBusy):
                bus.window(T0, T0 + 10)
        finally:
            bus._read_lock.release()
        self.assertEqual(len(bus.window(T0, T0 + 10)), 10)

    def test_a_clock_set_back_keeps_time_range_reads_consistent(self):
        bus = self.bus()
        bus.emit("state.changed", i=0)                           # T0
        self.wall.advance(100)
        bus.emit("state.changed", i=1)                           # T0 + 100
        self.wall.advance(-90)
        bus.emit("state.changed", i=2)                           # T0 + 10: the clock was corrected backwards
        bus.flush()
        self.assertEqual([e["i"] for e in bus.window(T0, T0 + 50)], [0, 2])      # by time, in seq order
        self.assertEqual([e["i"] for e in bus.window(T0, T0 + 200)], [0, 1, 2])

    def test_memory_only_bus_applies_the_same_limit(self):
        bus = EventBus(wall=self.wall)
        bus.retention_s = HOUR
        bus.emit("state.changed", i=0)
        self.wall.advance(HOUR + 1)
        bus.emit("state.changed", i=1)
        self.assertEqual([e["i"] for e in bus.since(0)], [1])
        self.assertEqual([e["i"] for e in bus.window(0, self.wall() + 1)], [1])

    def test_without_an_age_limit_nothing_changes(self):
        bus = self.bus(retention_s=None)
        bus.emit("state.changed", i=0)
        bus.flush()
        self.wall.advance(10 * HOUR)
        self.assertEqual(bus.prune(), 0)
        self.assertEqual(len(bus.since(0)), 1)
        self.assertIsNone(bus.describe()["retention_s"])

    def test_run_replay_follows_the_same_limit(self):
        bus = self.bus(maxlen=2)
        bus.emit("run.started", run_id="run-1")
        bus.flush()
        self.wall.advance(HOUR + 1)
        bus.emit("run.finished", run_id="run-1")
        bus.flush()
        self.assertEqual([e["type"] for e in bus.run_events("run-1")], ["run.finished"])

    def test_the_count_limit_still_bounds_the_ring(self):
        bus = self.bus(capacity=50)
        for i in range(200):
            bus.emit("state.changed", i=i)
        bus.flush()
        self.assertEqual(len(stored(self.path)), 50)
        self.assertEqual(bus.describe()["evicted_by_capacity"], 150)

    def test_a_ring_from_before_the_upgrade_is_migrated_in_place(self):
        db = sqlite3.connect(self.path)
        db.execute("CREATE TABLE events (seq INTEGER PRIMARY KEY, run_id TEXT, type TEXT, payload TEXT NOT NULL)")
        db.execute("CREATE INDEX events_run ON events (run_id, seq)")
        db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL)")
        for seq in range(1, 6):
            event = {"seq": seq, "ts": T0 - 600 + seq, "type": "state.changed", "i": seq}
            db.execute("INSERT INTO events (seq, run_id, type, payload) VALUES (?, NULL, ?, ?)",
                       (seq, event["type"], json.dumps(event)))
        db.execute("INSERT INTO events (seq, run_id, type, payload) VALUES (6, NULL, 'x', 'not json')")
        db.execute("INSERT INTO meta (key, value) VALUES ('reserved_seq', 6), ('clean_shutdown', 1)")
        db.commit()
        db.close()
        bus = self.bus()
        self.assertEqual(bus.start_kind, "clean")
        self.assertEqual(bus.emit("state.changed", i=7)["seq"], 7)       # seq continues: the ring was kept
        bus.flush()
        rows = stored(self.path)
        self.assertEqual([ts for _seq, ts in rows[:5]], [T0 - 600 + seq for seq in range(1, 6)])
        self.assertEqual(rows[5], (6, 0.0))                              # unreadable: counts as expired
        self.assertEqual([e["i"] for e in bus.window(T0 - 600, T0 + 1)], [1, 2, 3, 4, 5, 7])
        self.assertEqual(bus.prune(), 1)

    def test_a_tap_sees_every_event_and_cannot_break_emit(self):
        bus = EventBus(wall=self.wall)
        seen = []
        bus.add_tap(seen.append)
        bus.add_tap(lambda event: 1 / 0)                                 # a broken consumer
        event = bus.emit("safety.estop", reason="x")
        self.assertEqual(seen, [event])
        self.assertEqual(bus.since(0), [event])


class RuntimeRetentionTests(unittest.TestCase):
    def test_the_runtime_keeps_one_hour_by_default(self):
        self.assertEqual(TELEMETRY_RETENTION_S, 3600.0)
        with tempfile.TemporaryDirectory() as state:
            runtime = EdgeRuntime(SimQuadruped(), state, start_monitor=False, sandbox="inprocess")
            try:
                telemetry = runtime.status()["telemetry"]
                self.assertEqual(telemetry["retention_s"], 3600.0)
                self.assertIn("oldest_ts", telemetry)
                page = runtime.telemetry_window(0, 4_000_000_000, 0, 100)
                self.assertTrue(any(e["type"] == "runtime.started" for e in page["events"]))
                self.assertEqual(page["retention_s"], 3600.0)
                self.assertFalse(page["more"])
            finally:
                runtime.close()

    def test_the_age_limit_is_configurable(self):
        with tempfile.TemporaryDirectory() as state:
            runtime = EdgeRuntime(SimQuadruped(), state, start_monitor=False, sandbox="inprocess",
                                  telemetry_retention_s=120.0)
            try:
                self.assertEqual(runtime.status()["telemetry"]["retention_s"], 120.0)
            finally:
                runtime.close()
            with self.assertRaises(ValueError):
                EdgeRuntime(SimQuadruped(), state + "/bad", start_monitor=False, sandbox="inprocess",
                            telemetry_retention_s=-1)


if __name__ == "__main__":
    unittest.main()
