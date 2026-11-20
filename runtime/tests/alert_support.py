"""Shared pieces of the alert-report tests (issue #26): a clock the test moves, a recording stand-in
for the cloud as the reporter sees it, and a test case that builds runtimes without a gateway -- so
that no console can be connected to them -- and reporters on that clock."""
from __future__ import annotations

import json
import secrets
import sqlite3
import unittest
from pathlib import Path

import _support  # noqa: F401

from neuxmind_runtime import alerts
from neuxmind_runtime.adapters.sim_quadruped import SimQuadruped
from neuxmind_runtime.alert_report import AlertReporter, ReportConfig
from neuxmind_runtime.core import EdgeRuntime

from test_iter05_runtime import make_runtime

CLOUD_KEY = "nxk_" + secrets.token_hex(20)          # made up per run; nothing accepts it
CONFIG = ReportConfig(url="https://cloud.invalid", key=CLOUD_KEY)
WALL_START = 1_800_000_000.0
STOP_BOUND_S = 3.0          # generous for a loaded machine; what it rules out is waiting for a 30 s hang


def book_rows(runtime, sql: str) -> list:
    """What the alert book's file holds, read through a connection of the test's own."""
    db = sqlite3.connect(Path(runtime.state_dir) / alerts.BOOK_NAME)
    try:
        return db.execute(sql).fetchall()
    finally:
        db.close()


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.t = float(start)

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeTransport:
    """The cloud as the reporter sees it: takes a report, answers which alerts it now has."""

    def __init__(self):
        self.attempts: list[dict] = []
        self.stored: dict[str, dict] = {}
        self.fail: Exception | None = None
        self.reply = None                     # callable(report) -> answer, instead of the honest one

    def send(self, report: dict) -> dict:
        report = json.loads(json.dumps(report, allow_nan=False))     # must be plain JSON
        self.attempts.append(report)
        if self.fail is not None:
            raise self.fail
        if self.reply is not None:
            return self.reply(report)
        accepted, duplicates = [], []
        for alert in report["alerts"]:
            (duplicates if alert["id"] in self.stored else accepted).append(alert["id"])
            self.stored.setdefault(alert["id"], alert)
        return {"accepted": accepted, "duplicates": duplicates, "rejected": []}

    def kinds(self) -> list[str]:
        return [a["kind"] for a in self.stored.values()]


class ReporterCase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.wall = FakeClock(WALL_START)
        self.transport = FakeTransport()

    def runtime(self, adapter=None, **kw) -> EdgeRuntime:
        return make_runtime(self, adapter or SimQuadruped(time_scale=20.0), **kw)

    def reporter(self, runtime, config=CONFIG, transport="fake", **kw) -> AlertReporter:
        reporter = AlertReporter(runtime, config, clock=self.clock, wall=self.wall,
                                 transport=self.transport if transport == "fake" else transport, **kw)
        self.addCleanup(reporter.close)
        return reporter

    def step(self, reporter, seconds: float = 1.0, ticks: int = 1) -> None:
        """Move both clocks and let the reporter run, as its thread would once a second."""
        for _ in range(ticks):
            self.clock.advance(seconds)
            self.wall.advance(seconds)
            reporter.tick()

    def sent(self) -> list[dict]:
        return [a for report in self.transport.attempts for a in report["alerts"]]
