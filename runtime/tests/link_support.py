"""Shared pieces of the cloud-link tests (issue #43): the cloud as the link sees it (it queues
calls, sends each again until the device reports it, settles what is finished), and a test case with
a runtime on the simulated quadruped, a link on a clock the test moves, and the example skills."""
from __future__ import annotations

import json
import secrets
import time
import unittest

import _support
from _support import RuntimeFixture, custom_package, example

from neuxmind_runtime.alert_report import ReportConfig
from neuxmind_runtime.cloud_link import CloudLink
from neuxmind_runtime.sandbox import INPROCESS

from alert_support import WALL_START, FakeClock

CLOUD_KEY = "nxk_" + secrets.token_hex(20)          # made up per run; nothing accepts it
CONFIG = ReportConfig(url="https://cloud.invalid", key=CLOUD_KEY)
TERMINAL = ("succeeded", "failed", "cancelled", "stopped", "rejected")

# A skill that says how far it is and then waits (robot-clock seconds) until it is told to end.
LINGER = '''
def run(ctx, seconds=600.0):
    ctx.progress(0.4, "走到一半")
    ctx.sleep(seconds)
    return {"lingered": seconds}
'''


def linger(version: str = "0.1.0", code: str = LINGER) -> dict:
    package = custom_package("linger", code, parameters=[{"name": "seconds", "type": "float", "min": 0, "max": 5000}],
                             timeout_s=3600)
    return {**package, "version": version}


class FakeCloud:
    """``send(request) -> answer``, honest unless ``fail`` or ``reply`` is set."""

    def __init__(self):
        self.requests: list[dict] = []
        self.queue: list[dict] = []          # calls waiting to be picked up
        self.states: dict[str, dict] = {}    # call id -> what the device last reported
        self.cancel: list[str] = []
        self.stop: dict | None = None
        self.stops_done: list[str] = []
        self.poll_after_s = 2
        self.fail: Exception | None = None
        self.reply = None                    # callable(request, honest answer) -> answer

    def call(self, skill_id: str, parameters: dict | None = None, **fields) -> str:
        call = {"call_id": "cal_" + secrets.token_hex(8), "skill_id": skill_id, "version": None,
                "parameters": parameters or {}, "timeout_s": None, **fields}
        self.queue.append(call)
        return call["call_id"]

    def send(self, request: dict) -> dict:
        request = json.loads(json.dumps(request, allow_nan=False))      # must be plain JSON
        self.requests.append(request)
        if self.fail is not None:
            raise self.fail
        settled = []
        for report in request["calls"]:
            self.states[report["call_id"]] = report
            if report["status"] in TERMINAL:
                settled.append(report["call_id"])
        self.stops_done += [s for s in request["stops"] if s not in self.stops_done]
        stop = self.stop if self.stop is not None and self.stop["stop_id"] not in self.stops_done else None
        answer = {"accepted": True, "device_id": "dlk_test0001", "poll_after_s": self.poll_after_s,
                  "calls": [c for c in self.queue if c["call_id"] not in self.states],
                  "cancel": [c for c in self.cancel if self.states.get(c, {}).get("status") not in TERMINAL],
                  "settled": settled, "stop": stop}
        return self.reply(request, answer) if self.reply is not None else answer

    def status(self, call_id: str) -> str | None:
        return self.states.get(call_id, {}).get("status")


class LinkCase(unittest.TestCase):
    TIME_SCALE = 40.0
    SANDBOX = INPROCESS             # so the files also run on macOS; ``process`` is what a device uses

    def setUp(self):
        self.clock, self.wall = FakeClock(), FakeClock(WALL_START)
        self.cloud = FakeCloud()
        self.fx = None
        self.boot()

    def tearDown(self):
        self.shutdown()

    def boot(self, state_dir: str | None = None, **link_kw) -> None:
        """A runtime (a new one on ``state_dir`` is a restart) and its link."""
        self.fx = RuntimeFixture("quadruped", time_scale=self.TIME_SCALE, sandbox=self.SANDBOX, state_dir=state_dir)
        self.runtime = self.fx.runtime
        self.access = self.runtime.remote_access
        self.link = self.make_link(**link_kw)

    def make_link(self, config=CONFIG, transport="fake", **kw) -> CloudLink:
        link = CloudLink(self.runtime, config, clock=self.clock, wall=self.wall,
                         transport=self.cloud if transport == "fake" else transport, **kw)
        self.access.link = link
        return link

    def shutdown(self) -> None:
        if self.fx is not None:
            self.link.close()
            self.fx.close()
            self.fx = None

    def restart(self) -> None:
        """Close the runtime and start a new one on the same state directory."""
        state = self.fx.state_dir
        if self.fx._tmp is not None:                 # keep the directory; it goes when the test ends
            self.fx._tmp, tmp = None, self.fx._tmp
            self.addCleanup(tmp.cleanup)
        self.shutdown()
        self.boot(state)

    def install(self, *names: str) -> None:
        for name in names:
            self.fx.install(linger() if name == "linger" else example(name))

    def allow(self, skill_id: str, allowed: bool = True, **fields) -> dict:
        payload = {"skill_id": skill_id, "allowed": allowed, "set_by": "王工", "hardware_authorized": True, **fields}
        if allowed and "digest" not in fields:           # what the person was shown: the installed content
            payload["digest"] = self.digest(skill_id)
        return self.access.set(payload)

    def digest(self, skill_id: str) -> str | None:
        record = self.access.installed().get(skill_id)
        return record["digest"] if record else None

    def sync(self, seconds: float = 31.0) -> dict:
        """Move the clocks far enough for a sync to be due, run one round, return what was sent."""
        before = len(self.cloud.requests)
        self.clock.advance(seconds)
        self.wall.advance(seconds)
        self.link.tick()
        self.assertEqual(len(self.cloud.requests), before + 1, "one sync")
        return self.cloud.requests[-1]

    def sync_until(self, call_id: str, statuses, timeout: float = 20.0) -> dict:
        """Sync until the cloud has heard one of ``statuses`` for ``call_id``; the report."""
        statuses = (statuses,) if isinstance(statuses, str) else statuses
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.sync()
            if self.cloud.status(call_id) in statuses:
                return self.cloud.states[call_id]
            time.sleep(0.02)
        raise AssertionError(f"{call_id}: {self.cloud.states.get(call_id)} 没有在时限内变成 {statuses}")

    def operations(self, name: str) -> list[dict]:
        self.runtime.records.flush()
        page = self.runtime.records.query("operation", limit=5000)
        return [r for r in page["records"] if r.get("operation") == name]


__all__ = ["CLOUD_KEY", "CONFIG", "FakeCloud", "LinkCase", "TERMINAL", "linger", "_support"]
