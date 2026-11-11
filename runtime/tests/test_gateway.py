"""HTTP gateway: auth, v2 endpoints, idempotency, restart recovery, stop latency."""
import json
import socket
import threading
import time
import unittest
from unittest import mock

import _support
from _support import TOKEN, RuntimeFixture, custom_package, example, http, wait_until

from neuxmind_runtime.core import EdgeRuntime
from neuxmind_runtime.errors import UnknownOutcome
from neuxmind_runtime.gateway import GatewayServer, make_handler

SLEEPER = """
def run(ctx):
    ctx.sleep(10000.0)
"""


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.fx = RuntimeFixture(http=True)

    def tearDown(self):
        self.fx.close()

    def test_short_token_refused(self):
        with self.assertRaises(ValueError):
            make_handler(self.fx.runtime, "short-token")

    def test_requests_need_bearer_token(self):
        self.assertEqual(http(self.fx.url, "/v1/manifest", token=None)[0], 401)
        self.assertEqual(http(self.fx.url, "/v1/manifest", token="x" * 40)[0], 401)
        self.assertEqual(http(self.fx.url, "/v2/stop", {}, token="wrong-" + TOKEN)[0], 401)
        self.assertFalse(self.fx.runtime.supervisor.latched)   # unauthenticated stop ignored
        status, body = http(self.fx.url, "/v1/manifest")
        self.assertEqual(status, 200)
        self.assertEqual(body["schema"], "robot.adapter.v1")

    def test_errors_are_structured(self):
        status, body = http(self.fx.url, "/v2/nope")
        self.assertEqual((status, body["code"]), (404, "not_found"))
        status, body = http(self.fx.url, "/v2/skills/install", {"x": 1}, content_type="text/plain")
        self.assertEqual(status, 400)
        self.assertIn("error", body)


class V2EndpointTests(unittest.TestCase):
    def setUp(self):
        self.fx = RuntimeFixture(http=True, time_scale=20.0)
        self.url = self.fx.url

    def tearDown(self):
        self.fx.close()

    def test_manifest_registry_state(self):
        status, manifest = http(self.url, "/v2/manifest")
        self.assertEqual((status, manifest["schema"], manifest["simulated"]), (200, "robot.adapter.v2", True))
        status, registry = http(self.url, "/v2/capabilities/registry")
        self.assertEqual(registry["schema"], "neuxmind.capabilities.v1")
        status, state = http(self.url, "/v2/state?ids=system.health,sensor.battery")
        self.assertEqual(set(state), {"system.health", "sensor.battery"})
        status, everything = http(self.url, "/v2/state")
        self.assertIn("base.odometry", everything)
        self.assertEqual(http(self.url, "/v2/state?ids=base.velocity")[0], 400)

    def test_invoke_command_and_action(self):
        body = {"request_id": "vel-1", "args": {"vx": 3.0, "wz": 0.0}, "hardware_authorized": True}
        status, out = http(self.url, "/v2/capabilities/base.velocity/invoke", body)
        self.assertEqual((status, out), (200, {"accepted": True}))
        self.assertEqual(self.fx.adapter.cmd[0], 1.0)      # clamped
        status, _ = http(self.url, "/v2/capabilities/base.velocity/invoke",
                         {"request_id": "vel-2", "args": {"vx": 0.1, "wz": 0.0}})
        self.assertEqual(status, 403)
        status, out = http(self.url, "/v2/capabilities/base.navigate_to/invoke",
                           {"request_id": "nav-1", "args": {"goal": [1.0, 0, 0, 0, 0, 0, 1]},
                            "hardware_authorized": True})
        self.assertEqual(status, 200)
        record = wait_until(lambda: (r := http(self.url, f"/v2/runs/{out['run_id']}")[1])["status"]
                            not in ("queued", "running") and r)
        self.assertEqual(record["status"], "succeeded")
        self.assertTrue(record["result"]["reached"])
        self.assertEqual(http(self.url, "/v2/capabilities/teleport.now/invoke", {"request_id": "t"})[0], 404)

    def test_skill_install_list_run_cancel_telemetry(self):
        status, info = http(self.url, "/v2/skills/install", {"request_id": "i1", "package": example("patrol")})
        self.assertEqual((status, info["installed"]), (200, True))
        status, body = http(self.url, "/v2/skills/install", {"request_id": "i2", "package": example("wave")})
        self.assertEqual((status, body["code"]), (409, "incompatible"))
        self.assertIn("arm.joint_position", body["compatibility"]["missing"])
        skills = http(self.url, "/v2/skills")[1]["skills"]
        self.assertEqual([s["id"] for s in skills], ["patrol"])
        status, run = http(self.url, "/v2/skills/patrol/run", {"request_id": "r1", "hardware_authorized": True,
                                                              "parameters": {"waypoints": [[150, 0]]}})
        self.assertEqual(status, 200)
        wait_until(lambda: http(self.url, f"/v2/runs/{run['run_id']}")[1]["status"] == "running")
        status, cancel = http(self.url, f"/v2/runs/{run['run_id']}/cancel", {})
        self.assertTrue(cancel["accepted"])
        record = wait_until(lambda: (r := http(self.url, f"/v2/runs/{run['run_id']}")[1])["status"] == "stopped" and r)
        self.assertEqual(record["stop_reason"], "cancelled")
        telemetry = http(self.url, "/v2/telemetry?since=0")[1]
        types = {e["type"] for e in telemetry["events"]}
        self.assertTrue({"run.started", "run.finished"} <= types)
        later = http(self.url, f"/v2/telemetry?since={telemetry['last_seq']}")[1]
        self.assertTrue(all(e["seq"] > telemetry["last_seq"] for e in later["events"]))
        self.assertIn(run["run_id"], [r["id"] for r in http(self.url, "/v2/runs")[1]["runs"]])

    def test_stop_and_reset_over_http(self):
        status, body = http(self.url, "/v2/stop", {"reason": "operator"})
        self.assertEqual((status, body["latched"], body["accepted"]), (200, True, True))
        status, body = http(self.url, "/v2/capabilities/base.velocity/invoke",
                            {"request_id": "v-l", "args": {"vx": 0.1, "wz": 0.0}, "hardware_authorized": True})
        self.assertEqual((status, body["code"]), (409, "estop_latched"))
        self.assertEqual(http(self.url, "/v2/reset", {})[0], 403)
        self.assertEqual(http(self.url, "/v2/reset", {"hardware_authorized": True})[1], {"latched": False})
        # stop works with garbage body and wrong content type too
        self.assertEqual(http(self.url, "/v1/stop", {"x": 1}, content_type="text/plain")[0], 200)
        self.assertTrue(self.fx.runtime.supervisor.latched)
        # safety.stop through invoke needs no request_id or authorization
        http(self.url, "/v2/reset", {"hardware_authorized": True})
        self.assertEqual(http(self.url, "/v2/capabilities/safety.stop/invoke", {})[1], {"stopped": True})
        self.assertTrue(self.fx.runtime.supervisor.latched)


class StopLatencyTests(unittest.TestCase):
    def test_stop_during_long_navigation_is_immediate(self):
        fx = RuntimeFixture(http=True, time_scale=1.0)       # real time: the robot is mid-route
        try:
            fx.install(example("patrol"))
            run_id = fx.run("patrol", {"waypoints": [[100, 0]]})
            wait_until(lambda: fx.runtime.bus.read("base.odometry")["pose"][0] > 0.05, timeout=5)
            started = time.monotonic()
            status, body = http(fx.url, "/v2/stop", {})
            elapsed = time.monotonic() - started
            self.assertEqual(status, 200)
            self.assertLess(elapsed, 0.5)
            self.assertIn(run_id, body["stopped_runs"])
            record = wait_until(lambda: (r := fx.runtime.runner.get(run_id))["status"] == "stopped" and r, 1.0)
            self.assertEqual(record["stop_reason"], "stopped")
            first = fx.runtime.bus.read("base.odometry")
            time.sleep(0.3)
            second = fx.runtime.bus.read("base.odometry")
            self.assertEqual(first["pose"], second["pose"])
            self.assertEqual(second["velocity"], [0.0, 0.0, 0.0])
            self.assertLess(second["pose"][0], 5.0)
        finally:
            fx.close()


class IdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.fx = RuntimeFixture(http=True)
        self.url = self.fx.url

    def tearDown(self):
        self.fx.close()

    def test_same_id_same_content_returns_same_result(self):
        body = {"request_id": "dup-1", "package": example("status_report")}
        first = http(self.url, "/v2/skills/install", body)
        second = http(self.url, "/v2/skills/install", body)
        self.assertEqual(first, second)
        run_body = {"request_id": "dup-run", "hardware_authorized": True}
        a = http(self.url, "/v2/skills/status_report/run", run_body)[1]
        b = http(self.url, "/v2/skills/status_report/run", run_body)[1]
        self.assertEqual(a["run_id"], b["run_id"])               # not executed twice
        self.assertEqual(len([r for r in http(self.url, "/v2/runs")[1]["runs"]]), 1)

    def test_same_id_different_content_rejected(self):
        http(self.url, "/v2/skills/install", {"request_id": "dup-2", "package": example("status_report")})
        status, body = http(self.url, "/v2/skills/install", {"request_id": "dup-2", "package": example("patrol")})
        self.assertEqual((status, body["code"]), (409, "request_id_reused"))
        # Different operation with the same id is also refused.
        status, body = http(self.url, "/v2/skills/status_report/run", {"request_id": "dup-2",
                                                                      "hardware_authorized": True})
        self.assertEqual(body["code"], "request_id_reused")

    def test_rejection_is_replayed_not_reexecuted(self):
        body = {"request_id": "rej-1", "package": example("wave")}
        first = http(self.url, "/v2/skills/install", body)
        second = http(self.url, "/v2/skills/install", body)
        self.assertEqual(first, second)
        self.assertEqual(first[0], 409)

    def test_missing_request_id(self):
        status, body = http(self.url, "/v2/skills/install", {"package": example("status_report")})
        self.assertEqual((status, body["code"]), (400, "request_id_required"))


class RestartTests(unittest.TestCase):
    """A second EdgeRuntime on the same state dir simulates a process restart."""

    def test_running_run_and_request_become_unknown_and_are_not_replayed(self):
        fx = RuntimeFixture(time_scale=1.0)
        restarted = None
        try:
            fx.install(custom_package("sleeper", SLEEPER, timeout_s=86400))
            run_id = fx.run("sleeper")
            wait_until(lambda: fx.runtime.store.get("runs", run_id)["status"] == "running")
            # An in-flight v1 evaluate holds its request record in "running".
            app = {"schema": "robot.application.v1", "name": "eval", "steps": [
                {"entrypoint": "sleeper", "parameters": {}, "timeout_s": 86400}]}
            payload = {"request_id": "eval-1", "application": app, "hardware_authorized": True}
            threading.Thread(target=lambda: _swallow(fx.runtime.v1_evaluate, payload), daemon=True).start()
            wait_until(lambda: (fx.runtime.store.get("requests", "eval-1") or {}).get("status") == "running")

            restarted = EdgeRuntime(_support.SimQuadruped(), fx.state_dir, start_monitor=False)
            self.assertEqual(restarted.runner.get(run_id)["status"], "unknown")
            with self.assertRaises(UnknownOutcome):
                restarted.v1_evaluate(payload)
            self.assertEqual(restarted.runner.active_runs(), [])     # nothing replayed
            started = [e for e in restarted.events.since(0) if e["type"] == "runtime.started"][0]
            self.assertIn(run_id, started["unknown_runs"])
            self.assertIn("eval-1", started["unknown_requests"])
        finally:
            if restarted is not None:
                restarted.close()
            fx.close()

    def test_latch_survives_restart(self):
        fx = RuntimeFixture()
        try:
            fx.runtime.emergency_stop("before restart")
            wait_until(lambda: (fx.runtime.store.get("safety", "latch") or {}).get("latched"))
            restarted = EdgeRuntime(_support.SimQuadruped(), fx.state_dir, start_monitor=False)
            try:
                self.assertTrue(restarted.supervisor.latched)
                self.assertTrue(restarted.status()["estop_latched"])
            finally:
                restarted.close()
        finally:
            fx.close()


def _swallow(fn, *args):
    try:
        fn(*args)
    except Exception:
        pass


class GatewayServerTests(unittest.TestCase):
    def test_server_binds_ephemeral_port(self):
        fx = RuntimeFixture()
        server = GatewayServer(fx.runtime, TOKEN, port=0).start()
        try:
            self.assertEqual(http(server.url, "/v1/status")[1]["instance_id"], "sim-quadruped-01")
        finally:
            server.close()
            fx.close()


def _half_open(port: int, count: int, payload: bytes = b"POST /v2/stop HTTP/1.1\r\nHost: x\r\n") -> list:
    """Unauthenticated connections that never finish their headers (slowloris)."""
    socks = []
    for _ in range(count):
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        s.sendall(payload)
        socks.append(s)
    return socks


def _raw(port: int, request: bytes, timeout: float = 5.0) -> tuple[int, dict, float]:
    started = time.monotonic()
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        s.sendall(request)
        data = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    status = int(head.split(b" ")[1]) if head else 0
    return status, (json.loads(body) if body else {}), time.monotonic() - started


def _post(path: str, body: dict, token: str = TOKEN) -> bytes:
    raw = json.dumps(body).encode()
    return (f"POST {path} HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {token}\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(raw)}\r\n\r\n").encode() + raw


class ConnectionLimitTests(unittest.TestCase):
    """R-05: bounded connections, request deadlines, and a stop path that idle clients cannot starve."""

    def setUp(self):
        self.fx = RuntimeFixture()
        self.server = GatewayServer(self.fx.runtime, TOKEN, port=0, max_connections=8, stop_reserved=2,
                                    request_timeout=1.5).start()
        self.socks = []

    def tearDown(self):
        for s in self.socks:
            s.close()
        self.server.close()
        self.fx.close()

    def test_stop_within_one_second_under_many_half_open_connections(self):
        threads_before = threading.active_count()
        self.socks = _half_open(self.server.port, 300)
        time.sleep(0.2)
        # Bounded: idle connections cannot create unbounded threads.
        self.assertLessEqual(threading.active_count() - threads_before, 8 + 2 + 4)
        for path in ("/v2/stop", "/v1/stop"):
            status, body, elapsed = _raw(self.server.port, _post(path, {"reason": "flood"}))
            self.assertEqual((status, body.get("latched")), (200, True), body)
            self.assertLess(elapsed, 1.0)
            self.fx.runtime.supervisor.reset(authorized=True)
        # Ordinary requests beyond the limit get an immediate 503, not a hang.
        # Root cause of the iter02 1-in-15 flake (§5.0): an ordinary slot is freed
        # only when its connection ends -- response sent, client closed, or the
        # request deadline (here 1.5 s) drops an idle one; evicting from the stop-
        # reserved slots never frees an ordinary slot (ReservedEvictionTests).
        # Opening 300 sockets takes 1-2 s (listen backlog full, SYN retransmits), so
        # the first idle connections' deadline used to expire somewhere around the
        # final check. Check at a known full state instead: wait until the flood's
        # ordinary connections have been dropped, fill exactly the ordinary slots
        # with fresh idle connections, and ask well inside their deadline.
        counts = self.server.httpd.connection_counts
        wait_until(lambda: counts()["general"] == 0, 1.5 * 3 + 2)
        filled_at = time.monotonic()
        self.socks += _half_open(self.server.port, 8)
        wait_until(lambda: counts()["general"] == 8, 1.0)
        status, body, elapsed = _raw(self.server.port, b"GET /v1/status HTTP/1.1\r\nHost: x\r\n"
                                     b"Authorization: Bearer " + TOKEN.encode() + b"\r\n\r\n")
        self.assertEqual((status, body.get("code")), (503, "busy"))
        self.assertLess(elapsed, 1.0)
        self.assertLess(time.monotonic() - filled_at, 1.5)      # the slots really were all held
        # A stop still gets through at this full state.
        status, body, elapsed = _raw(self.server.port, _post("/v2/stop", {"reason": "full"}))
        self.assertEqual((status, body.get("latched")), (200, True), body)
        self.assertLess(elapsed, 1.0)

    def test_idle_ordinary_connection_frees_its_slot_only_at_its_deadline(self):
        """The documented counting rule: "full" is momentary -- an idle ordinary connection
        holds its slot until the request deadline drops it, then the next request is served."""
        counts = self.server.httpd.connection_counts
        self.socks = _half_open(self.server.port, 8, b"GET /v1/status HTTP/1.1\r\n")
        wait_until(lambda: counts()["general"] == 8, 2)
        get = b"GET /v1/status HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer " + TOKEN.encode() + b"\r\n\r\n"
        self.assertEqual(_raw(self.server.port, get)[0], 503)
        wait_until(lambda: counts()["general"] == 0, 1.5 + 2)    # request deadline (1.5 s here)
        self.assertEqual(_raw(self.server.port, get)[0], 200)

    def test_stop_with_reserved_slots_held_by_idle_clients(self):
        self.socks = _half_open(self.server.port, 8)                 # fill ordinary slots
        time.sleep(0.1)
        self.socks += _half_open(self.server.port, 2, b"PO")          # fill reserved slots, unidentified
        time.sleep(0.1)
        status, body, elapsed = _raw(self.server.port, _post("/v2/stop", {}))
        self.assertEqual((status, body.get("latched")), (200, True))
        self.assertLess(elapsed, 1.0)
        self.assertGreaterEqual(self.server.httpd.connection_counts()["evicted"], 1)

    def test_half_sent_request_is_dropped_after_deadline(self):
        s = socket.create_connection(("127.0.0.1", self.server.port), timeout=5)
        self.socks.append(s)
        started = time.monotonic()
        s.sendall(b"GET /v1/status HTTP/1.1\r\nHost: x\r\n")
        for _ in range(4):                       # drip bytes: per-recv timeout alone would never fire
            time.sleep(0.25)
            s.sendall(b"X-Drip: 1\r\n")
        s.settimeout(5)
        try:
            leftover = s.recv(100)
        except (ConnectionResetError, OSError):
            leftover = b""
        self.assertLess(time.monotonic() - started, 1.5 + 1.0)
        self.assertNotIn(b"200", leftover)
        wait_until(lambda: self.server.httpd.connection_counts()["general"] == 0, 3)

    def test_body_slower_than_deadline_is_dropped(self):
        s = socket.create_connection(("127.0.0.1", self.server.port), timeout=5)
        self.socks.append(s)
        s.sendall(b"POST /v2/reset HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer " + TOKEN.encode() +
                  b"\r\nContent-Type: application/json\r\nContent-Length: 1999999\r\n\r\n{")
        wait_until(lambda: self.server.httpd.connection_counts()["general"] == 0, 4)

    def test_nan_json_rejected(self):
        raw = b'{"request_id": "n1", "hardware_authorized": true, "args": {"vx": NaN, "wz": 0}}'
        status, body, _ = _raw(self.server.port, (
            "POST /v2/capabilities/base.velocity/invoke HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer "
            f"{TOKEN}\r\nContent-Type: application/json\r\nContent-Length: {len(raw)}\r\n\r\n").encode() + raw)
        self.assertEqual(status, 400, body)
        self.assertEqual(self.fx.adapter.cmd, (0.0, 0.0, 0.0))


class ReservedEvictionTests(unittest.TestCase):
    """§5.0: the slot bookkeeping itself is deterministic -- evicting an unidentified
    connection from a stop-reserved slot never frees (or takes) an ordinary slot."""

    def test_eviction_never_changes_the_ordinary_count(self):
        from neuxmind_runtime.gateway import BoundedHTTPServer
        from http.server import BaseHTTPRequestHandler
        server = BoundedHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler, max_connections=3, stop_reserved=2)
        pairs = []
        try:
            def sock():
                a, b = socket.socketpair()
                pairs.append((a, b))
                return a
            general = [server._acquire(sock()) for _ in range(3)]
            self.assertTrue(all(s is not None and not s.stop_only for s in general))
            reserved = [server._acquire(sock()) for _ in range(2)]
            self.assertTrue(all(s.stop_only and s.evictable for s in reserved))
            for n in range(1, 6):                       # every further connection evicts the oldest idle one
                newest = server._acquire(sock())
                self.assertTrue(newest.stop_only)
                self.assertEqual(server.connection_counts(), {"general": 3, "reserved": 2, "rejected": 0,
                                                              "evicted": n})
            victim = reserved[0]
            self.assertTrue(victim.evicted)
            server._release(victim)                     # its thread ends: the ordinary count is untouched
            self.assertEqual(server.connection_counts()["general"], 3)
            # Identified stop connections are never evicted; with both reserved slots
            # identified, a further connection is rejected outright.
            for slot in list(server._reserved):
                slot.identified()
            self.assertIsNone(server._acquire(sock()))
            self.assertEqual(server.connection_counts()["rejected"], 1)
            server._release(general[0])                 # only an ending ordinary connection frees a slot
            again = server._acquire(sock())
            self.assertFalse(again.stop_only)
            self.assertEqual(server.connection_counts()["general"], 3)
        finally:
            for a, b in pairs:
                a.close()
                b.close()
            server.server_close()


class LedgerClassificationTests(unittest.TestCase):
    """R-12: only failures after a possible side effect become ``unknown``."""

    def setUp(self):
        import tempfile
        from neuxmind_runtime.ledger import Ledger
        from neuxmind_runtime.store import Store
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(self._tmp.name)
        self.ledger = Ledger(self.store)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def test_failure_before_any_effect_is_a_rejection(self):
        from neuxmind_runtime.errors import AdapterFault, RuntimeApiError

        def boom():
            raise TypeError("bad input shape")
        for _ in range(2):
            with self.assertRaises(RuntimeApiError) as ctx:
                self.ledger.execute("op", {"request_id": "pre"}, boom)
            self.assertEqual((ctx.exception.status, ctx.exception.code), (500, "internal_error"))
        self.assertEqual(self.store.get("requests", "pre")["status"], "rejected")

        def read_failed():
            raise AdapterFault("read failed before dispatch")
        with self.assertRaises(AdapterFault):
            self.ledger.execute("op", {"request_id": "pre2"}, read_failed)
        self.assertEqual(self.store.get("requests", "pre2")["status"], "rejected")

    def test_failure_after_effect_is_unknown_and_never_replayed(self):
        from neuxmind_runtime import effects
        from neuxmind_runtime.errors import AdapterFault

        def dispatched_then_boom():
            effects.mark()
            raise TypeError("after dispatch")
        with self.assertRaises(AdapterFault):
            self.ledger.execute("op", {"request_id": "post"}, dispatched_then_boom)
        self.assertEqual(self.store.get("requests", "post")["status"], "unknown")
        with self.assertRaises(UnknownOutcome):
            self.ledger.execute("op", {"request_id": "post"}, lambda: {"ok": True})


class StopPathPersistenceTests(unittest.TestCase):
    """R-05/R-10: the stop path creates no thread; latch writes are ordered; abnormal shutdown latches."""

    def test_stop_works_when_threads_cannot_be_created(self):
        fx = RuntimeFixture()
        try:
            with mock.patch("threading.Thread.start", side_effect=RuntimeError("can't start new thread")):
                result = fx.runtime.emergency_stop("no threads")
            self.assertTrue(result["stopped"] and result["latched"])
            wait_until(lambda: (fx.runtime.store.get("safety", "latch") or {}).get("latched"))
            self.assertEqual(fx.runtime.store.get("safety", "latch")["reason"], "no threads")
        finally:
            fx.close()

    def test_stale_latch_write_cannot_overwrite_newer_state(self):
        fx = RuntimeFixture()
        try:
            rt = fx.runtime
            rt.emergency_stop("first")
            wait_until(lambda: (rt.store.get("safety", "latch") or {}).get("latched"))
            old_epoch = rt.supervisor.epoch
            rt.reset({"hardware_authorized": True})
            self.assertFalse(rt.store.get("safety", "latch")["latched"])
            # A delayed "latched" write from before the reset is ignored ...
            self.assertFalse(rt._write_latch(True, "first", old_epoch))
            self.assertFalse(rt.store.get("safety", "latch")["latched"])
            # ... but a newer stop always wins.
            rt.emergency_stop("second")
            wait_until(lambda: (rt.store.get("safety", "latch") or {}).get("latched"))
            self.assertFalse(rt._write_latch(False, None, old_epoch))
            self.assertTrue(rt.store.get("safety", "latch")["latched"])
        finally:
            fx.close()

    def test_stop_reason_from_body_is_persisted(self):
        fx = RuntimeFixture(http=True)
        try:
            http(fx.url, "/v2/stop", {"reason": "人员进入作业区"})
            wait_until(lambda: (fx.runtime.store.get("safety", "latch") or {}).get("reason") == "人员进入作业区")
            self.assertEqual(fx.runtime.supervisor.latch_reason, "人员进入作业区")
        finally:
            fx.close()

    def test_abnormal_shutdown_starts_latched_clean_shutdown_does_not(self):
        import tempfile
        with tempfile.TemporaryDirectory() as state:
            first = EdgeRuntime(_support.SimQuadruped(), state, start_monitor=False)
            first.close()                                         # clean
            second = EdgeRuntime(_support.SimQuadruped(), state, start_monitor=False)
            self.assertFalse(second.supervisor.latched)
            # "crash": no close(); simulate by not closing and opening a new runtime
            third = EdgeRuntime(_support.SimQuadruped(), state, start_monitor=False)
            try:
                self.assertTrue(third.supervisor.latched)
                self.assertIn("未正常关闭", third.supervisor.latch_reason)
                # Telemetry now persists across restarts (§5.2): look at this boot's events only.
                this_boot = third.events.since(third.events.first_seq - 1)
                started = [e for e in this_boot if e["type"] == "runtime.started"][0]
                self.assertTrue(started["abnormal_shutdown"])
            finally:
                third.close()
                second._closing.set()                   # the "crashed" instance: stop its threads only
                second.store.close()


class WatchdogIndependenceTests(unittest.TestCase):
    """R-09: the velocity watchdog has its own thread and no storage IO."""

    def test_watchdog_fires_while_monitor_is_stalled(self):
        fx = RuntimeFixture(time_scale=1.0)
        stalled = threading.Event()
        original_tick = fx.runtime.runner.tick

        def slow_tick():
            stalled.set()
            time.sleep(2.0)              # e.g. finalising a run while the SD card stalls on fsync
            original_tick()
        try:
            with mock.patch.object(fx.runtime.runner, "tick", slow_tick):
                self.assertTrue(stalled.wait(2))
                with fx.runtime.store._lock:          # storage is blocked as well
                    fx.runtime.bus.invoke("base.velocity", {"vx": 0.5, "wz": 0.0, "timeout_s": 0.2},
                                          authorized=True)
                    started = time.monotonic()
                    wait_until(lambda: fx.adapter.cmd == (0.0, 0.0, 0.0), 1.0)
                    self.assertLess(time.monotonic() - started, 0.6)
        finally:
            fx.close()


if __name__ == "__main__":
    unittest.main()
