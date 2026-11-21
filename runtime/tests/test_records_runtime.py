"""Device records wired into the runtime and the gateway (issue #25).

* a stop, a reset, a skill run each leave their records, in the right class;
* the records outlive the telemetry ring's count limit and its one-hour limit;
* they are fetched by time range over HTTP (what an uploader does after a reconnect);
* the stop path is untouched: a hung or failing records disk cannot delay a stop or the latch.
"""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import _support  # noqa: F401  (path bootstrap)
from _support import TOKEN, RuntimeFixture, custom_package, example, http, wait_until

from neuxmind_runtime.adapters.sim_quadruped import SimQuadruped
from neuxmind_runtime.core import EdgeRuntime
from neuxmind_runtime.gateway import GatewayServer
from neuxmind_runtime.records import OPERATION, SAFETY, TASK, RecordLog, RecordsConfig

FAR_FUTURE = 4_000_000_000
ROOMY_DISK = (10 ** 12, 10 ** 11, 9 * 10 ** 11)     # total, used, free: the test machine's own disk never matters


def of_class(runtime, name: str, **query) -> list[dict]:
    runtime.records.flush()
    return runtime.records.query(name, limit=5000, **query)["records"]


class InProcessRuntime:
    """A runtime that needs no skill process (runs on any platform), optionally over HTTP."""

    def __init__(self, state_dir: str, http_server: bool = False, **kwargs):
        self.runtime = EdgeRuntime(SimQuadruped(time_scale=20.0), state_dir, sandbox="inprocess", **kwargs)
        self.runtime.disk_guard.usage = lambda _path: ROOMY_DISK
        self.server = GatewayServer(self.runtime, TOKEN, port=0).start() if http_server else None
        self.url = self.server.url if self.server else None

    def close(self) -> None:
        if self.server is not None:
            self.server.close()
        self.runtime.runner.stop_all("test teardown")
        self.runtime.supervisor.halt_motion("test teardown")
        self.runtime.close()


class RecordsInTheRuntimeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = self._tmp.name
        self.fx = None

    def tearDown(self):
        if self.fx is not None:
            self.fx.close()
        self._tmp.cleanup()

    def start(self, **kwargs) -> InProcessRuntime:
        self.fx = InProcessRuntime(self.state, **kwargs)
        return self.fx

    def restart(self, **kwargs) -> InProcessRuntime:
        self.fx.close()
        return self.start(**kwargs)

    def test_a_stop_and_a_reset_leave_safety_and_operation_records(self):
        fx = self.start(http_server=True)
        before = time.time()
        self.assertEqual(http(fx.url, "/v2/stop", {"reason": "operator saw a person", "request_id": "stop-1"})[0], 200)
        status, body = http(fx.url, "/v2/reset", {"hardware_authorized": True, "request_id": "reset-1"})
        self.assertEqual((status, body["latched"]), (200, False))
        wait_until(lambda: len(of_class(fx.runtime, OPERATION)) >= 2)
        safety = of_class(fx.runtime, SAFETY)
        self.assertEqual([r["type"] for r in safety if r["type"] in ("safety.estop", "safety.reset")],
                         ["safety.estop", "safety.reset"])
        stop, reset = of_class(fx.runtime, OPERATION)
        self.assertEqual((stop["operation"], stop["outcome"], stop["stopped"], stop["reason"]),
                         ("stop", "ok", True, "operator saw a person"))
        self.assertEqual((stop["request_id"], stop["stop_count"]), ("stop-1", 1))
        self.assertEqual((reset["operation"], reset["outcome"], reset["request_id"], reset["hardware_authorized"]),
                         ("reset", "ok", "reset-1", True))
        for record in (stop, reset):
            self.assertEqual(record["type"], "operation")
            self.assertGreaterEqual(record["ts"], before)
            self.assertEqual(record["http_status"], 200)

    def test_a_refused_operation_is_recorded_with_its_reason(self):
        fx = self.start(http_server=True)
        http(fx.url, "/v2/stop", {})
        status, _body = http(fx.url, "/v2/reset", {"hardware_authorized": False})
        self.assertEqual(status, 403)
        wait_until(lambda: len(of_class(fx.runtime, OPERATION)) >= 2)
        refused = of_class(fx.runtime, OPERATION)[-1]
        self.assertEqual((refused["operation"], refused["outcome"], refused["http_status"]), ("reset", "rejected", 403))
        self.assertTrue(refused["code"])
        self.assertTrue(refused["error"])
        self.assertFalse(refused["hardware_authorized"])

    def test_the_operator_is_recorded_when_the_caller_names_one(self):
        fx = self.start(http_server=True)
        import json
        import urllib.request
        request = urllib.request.Request(fx.url + "/v2/stop", data=json.dumps({}).encode(), method="POST", headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + TOKEN,
            "X-NeuxMind-Operator": "operator-a\x00\x1b[31m" + "x" * 500})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=15) as resp:
            self.assertEqual(resp.status, 200)
        http(fx.url, "/v2/stop", {})
        wait_until(lambda: len(of_class(fx.runtime, OPERATION)) >= 2)
        named, anonymous = of_class(fx.runtime, OPERATION)
        self.assertTrue(named["operator"].startswith("operator-a"))
        self.assertLessEqual(len(named["operator"]), 120)
        self.assertTrue(named["operator"].isprintable())
        self.assertIsNone(anonymous["operator"])

    def test_reads_checks_and_velocity_commands_are_not_operations(self):
        fx = self.start(http_server=True)
        http(fx.url, "/v1/status")
        http(fx.url, "/v2/records")
        http(fx.url, "/v1/validate", {"application": {}})
        http(fx.url, "/v2/nothing", {})
        http(fx.url, "/v2/stop", {}, token=TOKEN[::-1])                                  # not authenticated
        for i in range(5):                                         # a control stream, not an operation
            status, _ = http(fx.url, "/v2/capabilities/base.velocity/invoke",
                             {"request_id": f"v-{i}", "hardware_authorized": True, "args": {"vx": 0.1, "wz": 0.0}})
            self.assertEqual(status, 200)
        status, _ = http(fx.url, "/v2/capabilities/safety.stop/invoke", {"request_id": "s-1", "args": {"reason": "x"}})
        self.assertEqual(status, 200)
        wait_until(lambda: len(of_class(fx.runtime, OPERATION)) >= 1)     # the last request's record is there
        self.assertEqual([r["operation"] for r in of_class(fx.runtime, OPERATION)], ["stop"])

    def test_install_run_and_cancel_are_operations(self):
        fx = self.start(http_server=True)
        package = custom_package("sleeper", "def run(ctx):\n    ctx.sleep(1000.0)\n")
        self.assertEqual(http(fx.url, "/v2/skills/install", {"request_id": "i-1", "package": package})[0], 200)
        status, started = http(fx.url, "/v2/skills/sleeper/run",
                               {"request_id": "r-1", "hardware_authorized": True, "parameters": {}})
        self.assertEqual(status, 200)
        run_id = started["run_id"]
        self.assertEqual(http(fx.url, f"/v2/runs/{run_id}/cancel", {})[0], 200)
        self.assertEqual(http(fx.url, "/v2/skills/missing/run", {"request_id": "r-2", "hardware_authorized": True})[0], 404)
        wait_until(lambda: len(of_class(fx.runtime, OPERATION)) >= 4)
        by_kind = {(r["operation"], r["outcome"]): r for r in of_class(fx.runtime, OPERATION)}
        install, run = by_kind[("skill.install", "ok")], by_kind[("skill.run", "ok")]
        cancel, missing = by_kind[("run.cancel", "ok")], by_kind[("skill.run", "rejected")]
        self.assertEqual((install["operation"], install["request_id"], install["outcome"]), ("skill.install", "i-1", "ok"))
        self.assertEqual((run["operation"], run["target"], run["run_id"], run["hardware_authorized"]),
                         ("skill.run", "sleeper", run_id, True))
        self.assertEqual((cancel["operation"], cancel["target"], cancel["outcome"]), ("run.cancel", run_id, "ok"))
        self.assertEqual((missing["operation"], missing["outcome"], missing["http_status"]), ("skill.run", "rejected", 404))
        fx.runtime.runner.wait(run_id, 10)
        wait_until(lambda: len(of_class(fx.runtime, TASK)) >= 1)
        result = of_class(fx.runtime, TASK)[0]
        self.assertEqual((result["run_id"], result["status"], result["kind"]), (run_id, "stopped", "skill"))

    def test_the_task_result_survives_a_store_that_cannot_be_written(self):
        """A full disk is exactly when the result must not be lost with the failing store write."""
        fx = self.start()
        runtime = fx.runtime
        runtime.install_skill({"request_id": "i-1", "package": custom_package("quick", "def run(ctx):\n    return {'v': 1}\n")})
        real_persist = runtime.runner._persist

        def full_disk(run):
            if run.finalized:
                raise OSError(28, "No space left on device")
            return real_persist(run)

        hook = threading.excepthook
        threading.excepthook = lambda args: None                   # the run thread dies of the store error
        try:
            with mock.patch.object(runtime.runner, "_persist", full_disk):
                run_id = runtime.run_skill("quick", {"request_id": "r-1", "hardware_authorized": True})["run_id"]
                wait_until(lambda: any(r["run_id"] == run_id for r in of_class(runtime, TASK)))
        finally:
            threading.excepthook = hook
        result = next(r for r in of_class(runtime, TASK) if r["run_id"] == run_id)
        self.assertEqual((result["status"], result["result"]), ("succeeded", {"v": 1}))

    def test_records_outlive_the_telemetry_rings_count_limit(self):
        fx = self.start(telemetry_capacity=50)
        runtime = fx.runtime
        runtime.emergency_stop("first stop")
        estop_seq = next(e["seq"] for e in runtime.events.since(0, 1000) if e["type"] == "safety.estop")
        for i in range(6000):                                      # chatter pushes it out of the ring and of memory
            runtime.events.emit("state.changed", i=i)
        runtime.events.flush()
        self.assertGreater(runtime.events.retention()["oldest_seq"], estop_seq)
        self.assertNotIn("safety.estop", [e["type"] for e in runtime.events.since(0, 10_000)])
        kept = [r for r in of_class(runtime, SAFETY) if r["type"] == "safety.estop"]
        self.assertEqual([(r["seq"], r["reason"]) for r in kept], [(estop_seq, "first stop")])

    def test_after_more_than_an_hour_offline_every_record_is_still_fetched(self):
        """Acceptance: offline for more than one hour, then safety event records, task results and
        operation records are all fetched, none missing; state and run data only for the last hour."""
        fx = self.start()
        runtime = fx.runtime
        wall = {"t": 1_800_000_000.0}
        runtime.events._wall = lambda: wall["t"]
        runtime.records._wall = lambda: wall["t"]
        t0 = wall["t"]
        for minute in range(150):                                  # 2.5 hours without a connection
            runtime.events.emit("state.changed", minute=minute)
            if minute % 10 == 0:
                runtime.events.emit("safety.halt", minute=minute, reason="test")
                runtime.records.task_result({"id": f"run-{minute}", "status": "succeeded", "result": {"m": minute},
                                             "ended_at": wall["t"]})
                runtime.records.operation({"ts": wall["t"], "operation": "skill.run", "minute": minute})
            runtime.events.flush()
            runtime.events.prune()
            wall["t"] += 60
        now = wall["t"]
        expected = list(range(0, 150, 10))
        self.assertEqual([r["minute"] for r in of_class(runtime, SAFETY, since_ts=t0, until_ts=now)
                          if r["type"] == "safety.halt"], expected)
        self.assertEqual([r["result"]["m"] for r in of_class(runtime, TASK, since_ts=t0, until_ts=now)], expected)
        self.assertEqual([r["minute"] for r in of_class(runtime, OPERATION, since_ts=t0, until_ts=now)], expected)
        state = [e["minute"] for e in runtime.telemetry_window(t0, now, 0, 10_000)["events"]
                 if e["type"] == "state.changed"]
        self.assertEqual(state, list(range(90, 150)))              # the last hour, without a gap

    def test_records_survive_a_restart_and_an_unfinished_run_becomes_a_task_result(self):
        fx = self.start()
        fx.runtime.emergency_stop("before restart")
        fx.runtime.store.put("runs", {"id": "run-lost", "kind": "skill", "target": "patrol@1.0.0", "status": "running",
                                      "created_at": time.time(), "started_at": time.time(), "ended_at": None,
                                      "result": None, "error": None})
        fx = self.restart()
        safety = [r for r in of_class(fx.runtime, SAFETY) if r["type"] == "safety.estop"]
        self.assertEqual([r["reason"] for r in safety], ["before restart"])
        lost = of_class(fx.runtime, TASK)
        self.assertEqual([(r["run_id"], r["status"]) for r in lost], [("run-lost", "unknown")])
        fx = self.restart()                                        # ... and only once
        self.assertEqual(len(of_class(fx.runtime, TASK)), 1)

    def test_a_run_whose_result_was_recorded_before_a_crash_is_not_recorded_again_as_unknown(self):
        fx = self.start()
        created = time.time()
        run = {"id": "run-done", "kind": "skill", "target": "patrol@1.0.0", "status": "running",
               "created_at": created, "started_at": created, "ended_at": None, "result": None, "error": None}
        fx.runtime.store.put("runs", run)                          # the store write of the final state was lost
        fx.runtime.records.task_result({**run, "status": "succeeded", "ended_at": created + 1, "result": {"laps": 2}})
        fx = self.restart()
        self.assertEqual(fx.runtime.runner.get("run-done")["status"], "unknown")
        self.assertEqual([(r["run_id"], r["status"]) for r in of_class(fx.runtime, TASK)], [("run-done", "succeeded")])

    def test_status_reports_the_records_without_touching_the_disk(self):
        fx = self.start()
        fx.runtime.emergency_stop("x")
        fx.runtime.records.flush()
        with mock.patch.object(RecordLog, "query", side_effect=AssertionError("IO in status")), \
                mock.patch.object(RecordLog, "append", side_effect=AssertionError("IO in status")):
            status = fx.runtime.status()
        records = status["records"]
        self.assertGreaterEqual(records["classes"][SAFETY]["count"], 1)
        self.assertEqual(set(records["classes"]), {SAFETY, TASK, OPERATION})
        self.assertFalse(records["degraded"])
        self.assertIn("disk", status["storage"])
        self.assertEqual(status["storage"]["full_percent"], 90.0)
        self.assertIsNone(status["log"])                           # no --log-file here

    def test_fetch_over_http_by_time_range(self):
        fx = self.start(http_server=True)
        runtime = fx.runtime
        t0 = 1_800_000_000.0
        for i in range(30):
            runtime.records.operation({"ts": t0 + i, "operation": "skill.run", "i": i})
        runtime.records.flush()
        status, page = http(fx.url, f"/v2/records/operation?since_ts={t0 + 10}&until_ts={t0 + 20}&limit=4")
        self.assertEqual(status, 200)
        self.assertEqual([r["i"] for r in page["records"]], [10, 11, 12, 13])
        self.assertEqual((page["class"], page["more"], page["next_after"]), (OPERATION, True, page["records"][-1]["id"]))
        got = list(page["records"])
        while page["more"]:
            page = http(fx.url, f"/v2/records/operation?since_ts={t0 + 10}&until_ts={t0 + 20}&limit=4"
                                f"&after={page['next_after']}")[1]
            got += page["records"]
        self.assertEqual([r["i"] for r in got], list(range(10, 20)))
        self.assertEqual((page["first_id"], page["last_id"], page["count"]), (1, 30, 30))
        # summary of every class, the clean-ups and the disk
        status, summary = http(fx.url, "/v2/records")
        self.assertEqual(status, 200)
        self.assertEqual(summary["classes"][OPERATION]["count"], 30)
        self.assertEqual(summary["classes"][OPERATION]["oldest_ts"], t0)
        self.assertEqual(summary["cleanups"], [])
        self.assertIn("disk", summary["storage"])
        # the same for state and run data
        for i in range(5):
            runtime.events.emit("state.changed", i=i)
        status, window = http(fx.url, f"/v2/telemetry?since_ts=0&until_ts={FAR_FUTURE}&limit=2")
        self.assertEqual((status, len(window["events"]), window["more"]), (200, 2, True))
        self.assertEqual(window["next_after"], window["events"][-1]["seq"])
        self.assertEqual(window["retention_s"], 3600.0)
        status, old = http(fx.url, "/v2/telemetry?since=0")        # the existing form is unchanged
        self.assertEqual(status, 200)
        self.assertIn("last_seq", old)

    def test_bad_requests_for_records_are_refused(self):
        fx = self.start(http_server=True)
        status, body = http(fx.url, "/v2/records/telemetry")
        self.assertEqual((status, body["code"]), (404, "not_found"))
        self.assertEqual(http(fx.url, "/v2/records/..%2f..%2fruntime")[0], 404)
        for query in ("since_ts=abc", "until_ts=nan", "since_ts=inf", "after=x", "limit=x", "after=-1",
                      "since_ts=-1"):
            self.assertEqual(http(fx.url, "/v2/records/safety?" + query)[0], 400, query)
            window = query if "_ts=" in query else "since_ts=0&" + query
            self.assertEqual(http(fx.url, "/v2/telemetry?" + window)[0], 400, query)
        status, page = http(fx.url, "/v2/records/safety?limit=999999")
        self.assertEqual(status, 200)                              # clamped, not refused
        huge = "after=" + "9" * 30
        self.assertEqual(http(fx.url, "/v2/records/safety?" + huge)[0], 400)
        self.assertEqual(http(fx.url, "/v2/telemetry?since_ts=0&" + huge)[0], 400)
        self.assertEqual(http(fx.url, "/v2/records/safety", token=None)[0], 401)
        self.assertEqual(http(fx.url, "/v2/records", token=None)[0], 401)


class BusyTests(unittest.TestCase):
    """While the disk holds a reader up, the answer is "try again", never "there is nothing"."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = InProcessRuntime(self._tmp.name, http_server=True)

    def tearDown(self):
        self.fx.close()
        self._tmp.cleanup()

    def test_records_being_written_answer_503(self):
        runtime = self.fx.runtime
        runtime.records.operation({"operation": "reset"})
        runtime.records.flush()
        lock = runtime.records._logs[OPERATION]._lock
        self.assertTrue(lock.acquire(timeout=1))                   # the writer is inside a slow write
        try:
            status, body = http(self.fx.url, "/v2/records/operation")
            self.assertEqual((status, body["code"]), (503, "busy"))
        finally:
            lock.release()
        status, body = http(self.fx.url, "/v2/records/operation")
        self.assertEqual((status, len(body["records"])), (200, 1))

    def test_a_busy_telemetry_ring_answers_503(self):
        runtime = self.fx.runtime
        self.assertTrue(runtime.events._read_lock.acquire(timeout=1))
        try:
            status, body = http(self.fx.url, "/v2/telemetry?since_ts=0")
            self.assertEqual((status, body["code"]), (503, "busy"))
        finally:
            runtime.events._read_lock.release()
        self.assertEqual(http(self.fx.url, "/v2/telemetry?since_ts=0")[0], 200)


class StopPathTests(unittest.TestCase):
    """Writing records must never be able to delay a stop, the latch, or its persistence."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = None

    def tearDown(self):
        if self.fx is not None:
            self.fx.close()
        self._tmp.cleanup()

    def test_a_hung_records_disk_does_not_delay_a_stop(self):
        self.fx = fx = InProcessRuntime(self._tmp.name, http_server=True,
                                        records=RecordsConfig(flush_period_s=0.02))
        gate, entered = threading.Event(), threading.Event()

        def hung(self_log, batch):
            entered.set()
            gate.wait(30)
            raise OSError(5, "Input/output error")

        try:
            with mock.patch.object(RecordLog, "append", hung):
                fx.runtime.records.operation({"operation": "warm-up"})
                self.assertTrue(entered.wait(5))                   # the writer is now stuck in the disk
                started = time.monotonic()
                status, body = http(fx.url, "/v2/stop", {"reason": "disk hung"})
                elapsed = time.monotonic() - started
                self.assertEqual((status, body["stopped"], body["latched"]), (200, True, True))
                self.assertLess(elapsed, 1.0)
                self.assertTrue(fx.runtime.supervisor.latched)
                # the latch reaches the disk although the records cannot
                wait_until(lambda: (fx.runtime.store.get("safety", "latch") or {}).get("latched") is True, 5)
                # a second stop and the status are just as quick
                started = time.monotonic()
                self.assertEqual(http(fx.url, "/v1/stop", {})[0], 200)
                self.assertEqual(http(fx.url, "/v1/status")[0], 200)
                self.assertLess(time.monotonic() - started, 1.0)
        finally:
            gate.set()
        # once the disk answers again nothing was lost
        wait_until(lambda: [r["operation"] for r in of_class(fx.runtime, OPERATION)].count("stop") == 2, 10)
        self.assertEqual(len([r for r in of_class(fx.runtime, SAFETY) if r["type"] == "safety.estop"]), 2)

    def test_stop_and_latch_work_when_the_records_directory_is_unusable(self):
        state = Path(self._tmp.name)
        (state / "records").write_text("a file where the records directory should be")
        self.fx = fx = InProcessRuntime(str(state), http_server=True)
        self.assertTrue(fx.runtime.status()["records"]["degraded"])
        started = time.monotonic()
        status, body = http(fx.url, "/v2/stop", {"reason": "no records"})
        self.assertEqual((status, body["latched"]), (200, True))
        self.assertLess(time.monotonic() - started, 1.0)
        wait_until(lambda: (fx.runtime.store.get("safety", "latch") or {}).get("latched") is True, 5)
        self.assertEqual(http(fx.url, "/v2/records/safety")[0], 200)                 # empty, not an error
        self.assertEqual((state / "records").read_text(), "a file where the records directory should be")

    def test_a_stop_does_no_records_io_and_survives_a_broken_recorder(self):
        self.fx = fx = InProcessRuntime(self._tmp.name)
        runtime = fx.runtime
        runtime.records.flush()
        order = []
        real_stop = runtime.adapter.stop

        def adapter_stop(*args, **kwargs):
            order.append("adapter.stop")
            return real_stop(*args, **kwargs)

        def offer(event):
            order.append("record:" + event["type"])
            raise RuntimeError("recorder broken")

        runtime.adapter.stop = adapter_stop
        runtime.events._taps = (offer,)
        log_calls = []
        with mock.patch.object(RecordLog, "append", side_effect=lambda batch: log_calls.append("append")), \
                mock.patch.object(RecordLog, "query", side_effect=lambda *a, **k: log_calls.append("query")), \
                mock.patch.object(RecordLog, "drop_oldest", side_effect=lambda: log_calls.append("drop")):
            result = runtime.emergency_stop("broken recorder")
            self.assertEqual(log_calls, [])                        # the stop itself reached no record log
        self.assertEqual((result["stopped"], result["latched"]), (True, True))
        self.assertEqual(order[:2], ["adapter.stop", "record:safety.estop"])   # the body is told first
        self.assertTrue(runtime.supervisor.latched)

    def test_the_real_recorder_only_queues_during_a_stop(self):
        self.fx = fx = InProcessRuntime(self._tmp.name, records=RecordsConfig(flush_period_s=3600.0))
        runtime = fx.runtime
        runtime.records.flush()
        with mock.patch.object(RecordLog, "append") as append:
            runtime.emergency_stop("queued only")
            append.assert_not_called()                             # nothing was written inside the stop
        self.assertEqual(runtime.records.describe()["pending"], 1)
        runtime.records.flush()
        self.assertEqual([r["reason"] for r in of_class(runtime, SAFETY) if r["type"] == "safety.estop"],
                         ["queued only"])

    def test_the_operation_record_of_a_stop_cannot_break_the_answer(self):
        self.fx = fx = InProcessRuntime(self._tmp.name, http_server=True)
        with mock.patch.object(fx.runtime, "record_operation", side_effect=RuntimeError("recorder broken")):
            status, body = http(fx.url, "/v2/stop", {"reason": "x"})
            self.assertEqual((status, body["latched"]), (200, True))
            status, body = http(fx.url, "/v2/reset", {"hardware_authorized": True})
            self.assertEqual((status, body["latched"]), (200, False))


class TaskResultTests(unittest.TestCase):
    """Runs of real skills (skill processes: Linux)."""

    def setUp(self):
        self.fx = RuntimeFixture("quadruped", http=True)

    def tearDown(self):
        self.fx.close()

    def test_every_finished_run_leaves_one_task_result(self):
        fx = self.fx
        fx.install(example("status_report"))
        fx.install(custom_package("fails", "def run(ctx):\n    raise ValueError('boom')\n"))
        status, started = http(fx.url, "/v2/skills/status_report/run",
                               {"request_id": "ok-1", "hardware_authorized": True, "parameters": {}})
        self.assertEqual(status, 200)
        good = fx.wait(started["run_id"])
        bad = fx.wait(fx.run("fails"))
        self.assertEqual((good["status"], bad["status"]), ("succeeded", "failed"))
        wait_until(lambda: len(of_class(fx.runtime, TASK)) >= 2)
        by_run = {r["run_id"]: r for r in of_class(fx.runtime, TASK)}
        self.assertEqual(len(by_run), 2)
        ok = by_run[good["id"]]
        self.assertEqual((ok["type"], ok["status"], ok["kind"], ok["result"]),
                         ("task.result", "succeeded", "skill", good["result"]))
        self.assertEqual(ok["ended_at"], good["ended_at"])
        self.assertEqual(ok["ts"], good["ended_at"])
        failed = by_run[bad["id"]]
        self.assertEqual(failed["status"], "failed")
        self.assertIn("boom", failed["error"])
        # the operation that started it points at the run
        wait_until(lambda: any(r["operation"] == "skill.run" for r in of_class(fx.runtime, OPERATION)))
        run_op = next(r for r in of_class(fx.runtime, OPERATION) if r["operation"] == "skill.run")
        self.assertEqual((run_op["target"], run_op["run_id"], run_op["request_id"], run_op["outcome"]),
                         ("status_report", good["id"], "ok-1", "ok"))
        install_ops = [r for r in of_class(fx.runtime, OPERATION) if r["operation"] == "skill.install"]
        self.assertEqual(install_ops, [])                          # installed through the fixture, not the gateway

    def test_a_stopped_run_is_recorded_as_stopped(self):
        fx = self.fx
        fx.install(custom_package("sleeper", "def run(ctx):\n    ctx.sleep(1000.0)\n"))
        run_id = fx.run("sleeper")
        wait_until(lambda: fx.runtime.runner.get(run_id)["status"] == "running")
        self.assertEqual(http(fx.url, "/v2/stop", {"reason": "enough"})[0], 200)
        fx.wait(run_id)
        wait_until(lambda: any(r["run_id"] == run_id for r in of_class(fx.runtime, TASK)))
        record = next(r for r in of_class(fx.runtime, TASK) if r["run_id"] == run_id)
        self.assertEqual(record["status"], "stopped")


if __name__ == "__main__":
    unittest.main()
