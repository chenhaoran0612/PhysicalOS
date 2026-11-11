"""§5.2: persistent telemetry ring (SQLite), seq continuous across restarts, run replay."""
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import _support
from _support import RuntimeFixture, example, http, wait_until

from neuxmind_runtime import events as events_module
from neuxmind_runtime.adapters.sim_quadruped import SimQuadruped
from neuxmind_runtime.core import EdgeRuntime
from neuxmind_runtime.events import EventBus


def stored_seqs(path) -> list[int]:
    db = sqlite3.connect(path)
    try:
        return [r[0] for r in db.execute("SELECT seq FROM events ORDER BY seq")]
    finally:
        db.close()


class RingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "telemetry.sqlite3"

    def tearDown(self):
        self._tmp.cleanup()

    def test_bounded_ring_and_reads_across_it(self):
        bus = EventBus(maxlen=20)
        bus.persist(self.path, capacity=50)
        for i in range(200):
            bus.emit("tick", i=i)
        bus.flush()
        seqs = stored_seqs(self.path)
        self.assertEqual((len(seqs), seqs[0], seqs[-1]), (50, 151, 200))
        self.assertEqual(bus.retention()["oldest_seq"], 151)
        # since() older than memory reads the ring; the ring's oldest is the floor.
        got = bus.since(0, limit=1000)
        self.assertEqual([e["seq"] for e in got], list(range(151, 201)))
        self.assertEqual([e["seq"] for e in bus.since(160, limit=5)], [161, 162, 163, 164, 165])
        bus.close()

    def test_clean_restart_continues_without_gap(self):
        bus = EventBus()
        bus.persist(self.path, capacity=1000)
        for i in range(30):
            bus.emit("a", i=i)
        last = bus.last_seq
        bus.close()
        again = EventBus()
        again.persist(self.path, capacity=1000)
        event = again.emit("b")
        self.assertEqual(event["seq"], last + 1)
        self.assertEqual([e["seq"] for e in again.since(0, 1000)], list(range(1, last + 2)))
        again.close()

    def test_crash_never_reuses_a_seq_a_client_saw(self):
        bus = EventBus()
        bus.persist(self.path, capacity=1000)
        for i in range(10):
            bus.emit("flushed", i=i)
        bus.flush()
        seen = [bus.emit("lost", i=i)["seq"] for i in range(5)]    # seen by a client, never flushed
        bus._pending.clear()                                       # "crash": the unflushed events are lost ...
        bus._closing.set()
        bus._writer.join(2)
        bus._release_lock()                                        # ... and the process (its flock) is gone
        again = EventBus()
        again.persist(self.path, capacity=1000)
        first = again.emit("after-crash")["seq"]
        self.assertGreater(first, max(seen))                       # monotonic: a gap, never a reuse
        again.close()

    def test_emit_never_waits_for_the_disk(self):
        bus = EventBus()
        bus.persist(self.path, capacity=10_000)
        with bus._db_lock:                                         # the writer is stuck on the database
            started = time.monotonic()
            for i in range(2000):
                bus.emit("burst", i=i)
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertEqual(bus.since(bus.last_seq - 3, 10)[-1]["i"], 1999)   # readable from memory
        self.assertTrue(wait_until(lambda: len(stored_seqs(self.path)) == 2000, 5))
        bus.close()

    def test_concurrent_emitters_get_unique_seqs(self):
        bus = EventBus(maxlen=100_000)
        bus.persist(self.path, capacity=100_000)

        def emitter(n):
            for i in range(2000):
                bus.emit("t", n=n, i=i)
        threads = [threading.Thread(target=emitter, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        bus.close()
        seqs = stored_seqs(self.path)
        self.assertEqual(len(seqs), 8000)
        self.assertEqual(len(set(seqs)), 8000)

    def test_memory_only_bus_unchanged(self):
        bus = EventBus(maxlen=3)
        for i in range(5):
            bus.emit("x", i=i)
        self.assertEqual([e["seq"] for e in bus.since(0)], [3, 4, 5])
        self.assertFalse(bus.describe()["persistent"])
        bus.close()                                                # no-op


def hold_exclusive(path):
    """Another process-like connection holding the ring's write lock (e.g. the sqlite3 CLI)."""
    db = sqlite3.connect(path, timeout=0.1, isolation_level=None)
    db.execute("BEGIN EXCLUSIVE")
    return db


class SeqNeverReusedTests(unittest.TestCase):
    """Q3-02: a failing writer keeps its batch; a crash never makes the next process reuse a seq."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "telemetry.sqlite3"

    def tearDown(self):
        self._tmp.cleanup()

    def crash(self, bus):
        bus._closing.set()
        bus._writer.join(5)
        bus._release_lock()                    # the dead process's flock is gone

    def test_review_scenario_locked_writer_then_crash(self):
        bus = EventBus(maxlen=100_000)
        bus.persist(self.path, capacity=100_000)
        for i in range(10):
            bus.emit("before", i=i)
        bus.flush()
        lock = hold_exclusive(self.path)
        try:
            for i in range(15_000):
                bus.emit("during-lock", i=i)
            self.assertTrue(wait_until(lambda: bus.write_errors >= 1, 5))
            seen = bus.since(0, 10 ** 6)[-1]["seq"]                   # what a client saw
            self.assertGreaterEqual(seen, 15_010)
            self.crash(bus)                                           # the writer's last flush fails too
        finally:
            lock.close()
        again = EventBus()
        self.assertTrue(again.persist(self.path, capacity=100_000))
        self.assertEqual(again.start_kind, "after_unclean")
        self.assertGreater(again.first_seq, seen, "崩溃后 seq 被重复使用")
        self.assertTrue(any(e["type"] == "telemetry.seq_floor" for e in again.since(again.first_seq - 1)))
        again.close()

    def test_failed_batch_is_retried_not_dropped(self):
        bus = EventBus(maxlen=100_000)
        bus.persist(self.path, capacity=100_000)
        lock = hold_exclusive(self.path)
        try:
            for i in range(3000):
                bus.emit("x", i=i)
            self.assertTrue(wait_until(lambda: bus.write_errors >= 1, 5))
            self.assertGreaterEqual(bus.describe()["unflushed"], 3000)
        finally:
            lock.close()
        self.assertTrue(wait_until(lambda: len(stored_seqs(self.path)) == 3000, 5), "写失败的批次被丢弃")
        self.assertEqual(bus.dropped_unpersisted, 0)
        bus.close()

    def test_reservation_covers_unflushed_events_when_the_clock_is_wrong(self):
        """A Jetson without RTC may boot at 1970: then the on-disk reservation alone must
        cover what was handed out (kills R5: "flush does not advance the reservation")."""
        with mock.patch.object(events_module, "_now_us", lambda: 0):
            bus = EventBus(maxlen=100_000)
            bus.persist(self.path, capacity=100_000)
            for i in range(20_000):
                bus.emit("stored", i=i)
            bus._closing.set()
            bus._writer.join(5)                       # final flush stores all 20 000
            for i in range(500):
                bus.emit("never-stored", i=i)         # handed out to clients, then the process dies
            seen = bus.last_seq
            bus._release_lock()
            again = EventBus()
            again.persist(self.path, capacity=100_000)
            self.assertGreater(again.first_seq, seen)
            again.close()

    def test_clean_close_after_failures_does_not_claim_clean(self):
        bus = EventBus()
        bus.persist(self.path, capacity=1000)
        lock = hold_exclusive(self.path)
        try:
            bus.emit("x")
            self.assertTrue(wait_until(lambda: bus.write_errors >= 1, 5))
            bus.close()                               # the final flush fails as well
        finally:
            lock.close()
        again = EventBus()
        again.persist(self.path, capacity=1000)
        self.assertEqual(again.start_kind, "after_unclean")   # not "clean": the last seq never reached disk
        again.close()


class DegradedTelemetryTests(unittest.TestCase):
    """Q3-01 / Q3-08 / Q3-17: telemetry never stops the runtime from starting or answering."""

    def boot_and_stop(self, state) -> tuple:
        started = time.monotonic()
        fx = RuntimeFixture("quadruped", http=True, state_dir=state)
        boot = time.monotonic() - started
        try:
            t0 = time.monotonic()
            status, body = http(fx.url, "/v1/stop", {"reason": "telemetry broken"})
            stop = time.monotonic() - t0
            self.assertEqual((status, body["latched"]), (200, True))
            self.assertLess(stop, 1.0)
            t0 = time.monotonic()
            code, info = http(fx.url, "/v1/status")
            self.assertEqual(code, 200)
            self.assertLess(time.monotonic() - t0, 1.0)
            return boot, info, fx.runtime.events.since(fx.runtime.events.first_seq - 1, 10 ** 4)
        finally:
            fx.close()

    def test_corrupt_directory_and_bad_reservation_are_quarantined(self):
        def garbage(path):
            path.write_bytes(b"this is not a sqlite database" * 100)

        def directory(path):
            path.mkdir()
            (path / "keep").write_text("x")

        def reservation(path):
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL)")
            db.execute("INSERT INTO meta VALUES ('reserved_seq', ?)", (2 ** 63 - 2,))
            db.commit()
            db.close()
        for name, make in (("garbage", garbage), ("directory", directory), ("reservation", reservation)):
            with self.subTest(state=name), tempfile.TemporaryDirectory() as state:
                path = Path(state) / "telemetry.sqlite3"
                make(path)
                boot, info, events = self.boot_and_stop(state)
                self.assertLess(boot, 5.0)
                tel = info["telemetry"]
                self.assertTrue(tel["persistent"], tel)                  # recreated
                self.assertFalse(info["telemetry_degraded"])
                quarantined = Path(tel["quarantined"])
                self.assertTrue(quarantined.exists(), "损坏的遥测库被删除而不是隔离")
                self.assertTrue(quarantined.name.startswith("telemetry.sqlite3.corrupt-"))
                if name == "directory":
                    self.assertEqual((quarantined / "keep").read_text(), "x")
                self.assertTrue(any(e["type"] == "telemetry.quarantined" for e in events))

    def test_locked_ring_degrades_to_memory(self):
        with tempfile.TemporaryDirectory() as state:
            first = RuntimeFixture("quadruped", state_dir=state)
            first.close()                                   # a valid ring exists
            lock = hold_exclusive(Path(state) / "telemetry.sqlite3")
            try:
                boot, info, events = self.boot_and_stop(state)
            finally:
                lock.close()
            self.assertLess(boot, 2.0)
            self.assertTrue(info["telemetry_degraded"])
            self.assertFalse(info["telemetry"]["persistent"])
            self.assertIn("锁", info["telemetry"]["degraded_reason"])
            self.assertTrue(any(e["type"] == "telemetry.persist_failed" for e in events))
            self.assertTrue(any(e["type"] == "safety.estop" for e in events))   # memory telemetry still works

    def test_second_runtime_on_the_same_state_dir(self):
        with tempfile.TemporaryDirectory() as state:
            first = RuntimeFixture("quadruped", state_dir=state)
            try:
                second = EdgeRuntime(SimQuadruped(), state, start_monitor=False, sandbox="inprocess")
                try:
                    tel = second.status()["telemetry"]
                    self.assertTrue(tel["degraded"])
                    self.assertIn("另一个运行时实例", tel["degraded_reason"])
                    self.assertGreater(second.events.first_seq, first.runtime.events.last_seq)
                    self.assertTrue(first.runtime.status()["telemetry"]["persistent"])
                finally:
                    second.close()
            finally:
                first.close()

    def test_status_does_not_wait_for_a_locked_ring(self):
        """Q3-08: with the ring locked by another process /v1/status answers at once."""
        fx = RuntimeFixture("quadruped", http=True)
        try:
            fx.runtime.events.emit("x")
            lock = hold_exclusive(Path(fx.state_dir) / "telemetry.sqlite3")
            try:
                for _ in range(20):
                    fx.runtime.events.emit("y")
                time.sleep(0.3)                             # the writer is now waiting / failing
                t0 = time.monotonic()
                code, info = http(fx.url, "/v1/status")
                self.assertEqual(code, 200)
                self.assertLess(time.monotonic() - t0, 0.5)
                t0 = time.monotonic()
                self.assertEqual(http(fx.url, "/v2/telemetry?since=0")[0], 200)
                self.assertLess(time.monotonic() - t0, 1.5)
            finally:
                lock.close()
        finally:
            fx.close()


class RuntimeTelemetryTests(unittest.TestCase):
    def test_telemetry_and_run_replay_survive_restart(self):
        with tempfile.TemporaryDirectory() as state:
            fx = RuntimeFixture("quadruped", http=True, state_dir=state)
            fx.install(example("status_report"))
            run_id = fx.run("status_report")
            self.assertEqual(fx.wait(run_id, 20)["status"], "succeeded")
            before = http(fx.url, "/v2/telemetry?since=0")[1]
            last = before["last_seq"]
            replay = http(fx.url, f"/v2/runs/{run_id}/events")[1]
            fx.close()
            stored_last = stored_seqs(Path(state) / "telemetry.sqlite3")[-1]     # incl. shutdown events
            self.assertGreaterEqual(stored_last, last)

            fx = RuntimeFixture("quadruped", http=True, state_dir=state)
            try:
                status = http(fx.url, "/v1/status")[1]["telemetry"]
                self.assertTrue(status["persistent"])
                self.assertEqual(status["first_seq_this_boot"], stored_last + 1)  # clean shutdown: no gap
                after = http(fx.url, "/v2/telemetry?since=0")[1]
                seqs = [e["seq"] for e in after["events"]]
                self.assertEqual(seqs, sorted(seqs))
                self.assertEqual(seqs[:len(before["events"])], [e["seq"] for e in before["events"]])
                self.assertIn(stored_last + 1, seqs)                            # the new boot follows on
                self.assertEqual(seqs, list(range(1, seqs[-1] + 1)))            # continuous, no gap
                # Replay one run of the previous boot: every event, in order.
                again = http(fx.url, f"/v2/runs/{run_id}/events")[1]
                types = [e["type"] for e in again["events"]]
                self.assertEqual(again["events"], replay["events"])
                self.assertEqual(types[0], "run.started")
                self.assertIn("sandbox.started", types)
                self.assertEqual(types[-1], "run.finished")
                self.assertEqual(again["events"][-1].get("status"), "succeeded")
                self.assertTrue(all(e["run_id"] == run_id for e in again["events"]))
                self.assertEqual(http(fx.url, "/v2/runs/run-unknown/events")[0], 404)
                self.assertEqual(http(fx.url, f"/v2/runs/{run_id}/events?since=x")[0], 400)
                page = http(fx.url, f"/v2/runs/{run_id}/events?limit=2")[1]
                self.assertEqual((len(page["events"]), page["more"]), (2, True))
                rest = http(fx.url, f"/v2/runs/{run_id}/events?since={page['last_seq']}")[1]
                self.assertEqual(page["events"] + rest["events"], again["events"])
            finally:
                fx.close()

    def test_stop_is_not_delayed_by_a_stalled_telemetry_disk(self):
        fx = RuntimeFixture("quadruped", http=True)
        try:
            with fx.runtime.events._db_lock:                        # disk stalls (SD card fsync)
                started = time.monotonic()
                status, body = http(fx.url, "/v2/stop", {"reason": "disk stalled"})
                self.assertLess(time.monotonic() - started, 1.0)
                self.assertEqual((status, body["latched"]), (200, True))
                self.assertTrue(any(e["type"] == "safety.estop"
                                    for e in fx.runtime.events.since(fx.runtime.events.first_seq - 1, 10 ** 4)))
            # ... and the stop's events reach the disk once it recovers.
            path = Path(fx.state_dir) / "telemetry.sqlite3"
            self.assertTrue(wait_until(lambda: fx.runtime.events.run_events("none") == [] and
                                       len(stored_seqs(path)) >= fx.runtime.events.last_seq
                                       - fx.runtime.events.first_seq + 1, 5))
        finally:
            fx.close()

    def test_capacity_is_configurable_and_memory_only_is_possible(self):
        with tempfile.TemporaryDirectory() as state:
            runtime = EdgeRuntime(SimQuadruped(), state, start_monitor=False, sandbox="inprocess",
                                  telemetry_capacity=10)
            try:
                for i in range(40):
                    runtime.events.emit("x", i=i)
                runtime.events.flush()
                self.assertEqual(len(stored_seqs(Path(state) / "telemetry.sqlite3")), 10)
                self.assertEqual(runtime.status()["telemetry"]["capacity"], 10)
            finally:
                runtime.close()
            runtime = EdgeRuntime(SimQuadruped(), state + "/b", start_monitor=False, sandbox="inprocess",
                                  telemetry_capacity=None)
            try:
                self.assertFalse(runtime.status()["telemetry"]["persistent"])
                self.assertFalse((Path(state) / "b" / "telemetry.sqlite3").exists())
            finally:
                runtime.close()


if __name__ == "__main__":
    unittest.main()
