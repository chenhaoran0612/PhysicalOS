"""Run skills locally on the runtime's simulated bodies, in-process.

Nothing here talks HTTP: :class:`SkillHarness` builds a real
``neuxmind_runtime.core.EdgeRuntime`` around a simulated adapter
(``SimQuadruped`` / ``SimArm``) in a temporary state directory, installs your
skill directory exactly as ``nx skill install`` would package it, and runs it
through the same runner, SkillContext and safety supervisor as on a robot.

Results on simulated bodies prove protocol and logic only — never on-site
behaviour. Every :class:`RunResult` carries ``simulated=True``.

Example (``tests/test_skill.py`` in a generated template)::

    from pathlib import Path
    from neuxmind.skill.testing import SkillTestCase

    class PatrolTest(SkillTestCase):
        skill_dir = Path(__file__).resolve().parents[1]

        def test_runs_on_quadruped(self):
            result = self.run_skill("quadruped", waypoints=[[1, 0]])
            self.assertSucceeded(result)

        def test_arm_is_incompatible(self):
            self.assertIncompatible("arm", missing=["base.navigate_to"])
"""
from __future__ import annotations

import tempfile
import time
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import _bootstrap
from ..errors import NeuxMindError
from ..package import explain_compatibility, format_compatibility, load_skill

BODIES = {"quadruped": "sim_quadruped", "arm": "sim_arm"}
DEFAULT_TIME_SCALE = 20.0


def adapter_name(body: str) -> str:
    """``quadruped`` / ``arm`` → runtime adapter name; other values pass through (``module:Class``)."""
    return BODIES.get(body, body)


def sim_manifest(body: str) -> dict:
    """The validated ``robot.adapter.v2`` manifest of a simulated body, without starting it."""
    _bootstrap.runtime()
    from neuxmind_runtime.adapters import load_adapter

    spec = _bootstrap.spec()
    adapter = load_adapter(adapter_name(body))
    try:
        return spec.manifest.validate_manifest_v2(adapter.manifest())
    finally:
        try:
            adapter.close()
        except Exception:
            pass


def _from_runtime_error(exc: Exception) -> NeuxMindError:
    return NeuxMindError(str(exc), code=getattr(exc, "code", "error"), status=getattr(exc, "status", None),
                         details=dict(getattr(exc, "extra", {}) or {}))


@dataclass
class RunResult:
    """Outcome of one local run."""

    run_id: str
    status: str
    result: Any
    error: str | None
    progress: float
    events: list[dict] = field(default_factory=list)
    simulated: bool = True
    raw: dict = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.status == "succeeded"

    @property
    def logs(self) -> list[str]:
        """Messages from ``ctx.log`` / ``ctx.progress`` in order."""
        return [e.get("message", "") for e in self.events if e.get("type") in ("log", "progress")]

    def describe(self) -> str:
        lines = [f"run {self.run_id}: {self.status}" + (f" — {self.error}" if self.error else "")]
        for event in self.events:
            if event.get("type") == "exception":
                lines.append(event.get("traceback", ""))
        return "\n".join(lines)


class SkillHarness:
    """One simulated body + one skill, running in this process.

    ``body`` is ``"quadruped"``, ``"arm"`` or any runtime adapter spec.
    ``time_scale`` speeds up simulated time (only simulated bodies accept it).
    Use as a context manager or call :meth:`close`.
    """

    def __init__(self, skill_dir: str | Path, body: str = "quadruped", *,
                 time_scale: float = DEFAULT_TIME_SCALE):
        _bootstrap.runtime()
        from neuxmind_runtime.adapters import load_adapter
        from neuxmind_runtime.core import EdgeRuntime

        self.skill_dir = Path(skill_dir)
        self.body = body
        self.package, self.skill = load_skill(self.skill_dir)
        self._tmp = tempfile.TemporaryDirectory(prefix="nx-skilltest-")
        self.adapter = load_adapter(adapter_name(body), time_scale=time_scale)
        self.runtime = EdgeRuntime(self.adapter, self._tmp.name)
        self.installed: dict | None = None
        self._counter = 0

    # ------------------------------------------------------------ info
    @property
    def manifest(self) -> dict:
        return self.runtime.manifest

    def compatibility(self) -> dict:
        """:func:`neuxmind.package.explain_compatibility` against this body."""
        return explain_compatibility(self.skill, self.manifest)

    def read(self, cap_id: str) -> dict:
        """Read a state capability of the simulated body (e.g. ``base.odometry``)."""
        return self.runtime.bus.read(cap_id)

    def _rid(self, kind: str) -> str:
        self._counter += 1
        return f"test-{kind}-{self._counter}-{time.monotonic_ns()}"

    # ------------------------------------------------------------ actions
    def install(self) -> dict:
        """Install the skill (once). Raises ``NeuxMindError`` (``incompatible`` ...) like the gateway."""
        if self.installed is None:
            try:
                self.installed = self.runtime.install_skill({"request_id": self._rid("install"),
                                                             "package": self.package})
            except Exception as exc:
                if hasattr(exc, "code"):
                    raise _from_runtime_error(exc) from None
                raise
        return self.installed

    def run(self, parameters: dict | None = None, *, authorize: bool = True, timeout: float = 30.0,
            **params: Any) -> RunResult:
        """Run the skill and wait (``timeout`` wall seconds) for it to finish.

        ``authorize=False`` reproduces the runtime's refusal (``hardware_not_authorized``).
        """
        self.install()
        merged = {**(parameters or {}), **params}
        payload = {"request_id": self._rid("run"), "parameters": merged, "hardware_authorized": authorize}
        try:
            run_id = self.runtime.run_skill(self.skill["id"], payload)["run_id"]
        except Exception as exc:
            if hasattr(exc, "code"):
                raise _from_runtime_error(exc) from None
            raise
        record = self.runtime.runner.wait(run_id, timeout)
        if record["status"] in ("queued", "running"):
            self.runtime.runner.cancel(run_id)
            record = self.runtime.runner.wait(run_id, 5.0)
            raise NeuxMindError(f"技能在 {timeout} 秒内没有结束，已取消（状态 {record['status']}）",
                                code="wait_timeout", details={"run": record})
        return RunResult(run_id=run_id, status=record["status"], result=record.get("result"),
                         error=record.get("error"), progress=record.get("progress", 0.0),
                         events=list(record.get("events") or []),
                         simulated=self.manifest.get("simulated") is True, raw=record)

    def stop(self, reason: str = "test stop") -> dict:
        """Global stop (latches the e-stop, like ``nx stop``)."""
        return self.runtime.emergency_stop(reason, source="test")

    def reset(self) -> dict:
        return self.runtime.reset({"hardware_authorized": True})

    def close(self) -> None:
        try:
            self.runtime.runner.stop_all("test teardown")
            self.runtime.supervisor.halt_motion("test teardown")
            self.runtime.close()
        finally:
            self._tmp.cleanup()

    def __enter__(self) -> "SkillHarness":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class SkillTestCase(unittest.TestCase):
    """``unittest.TestCase`` with helpers for a skill directory.

    Set ``skill_dir`` (class attribute). Optional: ``time_scale``.
    """

    skill_dir: str | Path | None = None
    time_scale: float = DEFAULT_TIME_SCALE

    def _skill_dir(self) -> Path:
        if self.skill_dir is None:
            raise AssertionError("SkillTestCase 需要设置 skill_dir")
        return Path(self.skill_dir)

    def harness(self, body: str = "quadruped") -> SkillHarness:
        """A fresh harness, closed automatically after the test."""
        h = SkillHarness(self._skill_dir(), body, time_scale=self.time_scale)
        self.addCleanup(h.close)
        return h

    def run_skill(self, body: str = "quadruped", *, timeout: float = 30.0, **parameters: Any) -> RunResult:
        """Install and run the skill on a fresh simulated ``body``."""
        return self.harness(body).run(parameters, timeout=timeout)

    def compatibility(self, body: str) -> dict:
        _package, skill = load_skill(self._skill_dir())
        return explain_compatibility(skill, sim_manifest(body))

    # ------------------------------------------------------------ assertions
    def assertSucceeded(self, result: RunResult, msg: str | None = None) -> None:
        if not result.succeeded:
            self.fail(msg or result.describe())

    def assertCompatible(self, body: str) -> dict:
        report = self.compatibility(body)
        if not report["compatible"]:
            self.fail(format_compatibility(report, body))
        return report

    def assertIncompatible(self, body: str, missing: list[str] | None = None) -> dict:
        report = self.compatibility(body)
        if report["compatible"]:
            self.fail(f"预期技能与 {body} 不兼容，但检查结果为兼容")
        if missing is not None:
            for cap in missing:
                self.assertIn(cap, report["missing"], format_compatibility(report, body))
        return report


__all__ = ["SkillHarness", "SkillTestCase", "RunResult", "sim_manifest", "adapter_name", "BODIES"]
