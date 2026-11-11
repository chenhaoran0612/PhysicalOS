"""Writing NeuxMind skills.

A skill package's ``entry`` is ``"<file>.py:<name>"`` (runtime/neuxmind_runtime/skills.py):

* a **function** ``run(ctx, **parameters)``, or
* a **class**; the runtime instantiates it with no arguments and calls
  ``instance.run(ctx, **parameters)``. Subclass :class:`Skill` for that.

``ctx`` is the runtime's ``SkillContext`` — the only supported, safety
supervised API. :class:`Context` below describes it for type checkers and
editors; it is a ``Protocol``, nothing is wrapped at run time.

Example::

    from neuxmind.skill import Skill, Context

    class Patrol(Skill):
        def run(self, ctx: Context, waypoints, loops=1):
            for wp in waypoints:
                ctx.check_stop()
                ctx.call("base.navigate_to", goal=[wp[0], wp[1], 0, 0, 0, 0, 1])
            return {"done": True}

Exceptions are the runtime's own classes when the runtime is importable (as it
always is inside the runtime process and in :mod:`neuxmind.skill.testing`), so
``except ActionFailed`` in a skill catches exactly what the runtime raises.
Stop exceptions derive from ``BaseException``: ``except Exception`` will not
swallow a stop — do not catch :class:`SkillInterrupted`.

Skills keep working without the SDK on the robot: generated templates fall
back to ``neuxmind_runtime.skills.Skill`` (or ``object``) if ``neuxmind`` is
not installed there.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .. import _bootstrap

_rt = _bootstrap.try_runtime()

if _rt is not None:
    from neuxmind_runtime.skills import (  # noqa: E402
        ActionFailed,
        SkillCancelled,
        SkillError,
        SkillInterrupted,
        SkillStopped,
        SkillTimeout,
    )
    from neuxmind_runtime.skills import Skill as _RuntimeSkill  # noqa: E402
else:  # Runtime not available (e.g. SDK installed alone): same names and semantics.
    class SkillInterrupted(BaseException):  # type: ignore[no-redef]
        """Raised inside a skill when it must stop (BaseException on purpose)."""

    class SkillStopped(SkillInterrupted):  # type: ignore[no-redef]
        """Global stop / e-stop."""

    class SkillCancelled(SkillInterrupted):  # type: ignore[no-redef]
        """This run was cancelled."""

    class SkillTimeout(SkillInterrupted):  # type: ignore[no-redef]
        """The run exceeded its timeout."""

    class ActionFailed(Exception):  # type: ignore[no-redef]
        """An action capability finished unsuccessfully. Skills may catch this."""

    class SkillError(Exception):  # type: ignore[no-redef]
        """Misuse of the context (e.g. calling an undeclared capability)."""

    class _RuntimeSkill:  # type: ignore[no-redef]
        def run(self, ctx: Any, **parameters: Any) -> Any:
            raise NotImplementedError


@runtime_checkable
class Context(Protocol):
    """Type of the ``ctx`` argument (the runtime's ``SkillContext``)."""

    params: dict
    run_id: str

    @property
    def manifest(self) -> dict:
        """A copy of the body's ``robot.adapter.v2`` manifest."""

    @property
    def simulated(self) -> bool:
        """True on simulated bodies — results are never on-site evidence."""

    @property
    def stopped(self) -> bool:
        """True once this run has been stopped, cancelled or timed out."""

    def has(self, cap_id: str) -> bool:
        """True if the body provides ``cap_id`` *and* the skill declared it (requires/optional)."""

    def param(self, cap_id: str, name: str, default: Any = None) -> Any:
        """A manifest parameter, e.g. ``ctx.param("arm.joint_position", "dof")``."""

    def time_left(self) -> float | None:
        """Seconds (robot time) until this run times out."""

    def check_stop(self) -> None:
        """Raise SkillStopped / SkillCancelled / SkillTimeout if the run must end."""

    def sleep(self, seconds: float) -> None:
        """Sleep in robot time; wakes immediately on stop/cancel/timeout."""

    def log(self, message: str, **data: Any) -> None:
        """Add a log event to the run (visible in run status and telemetry)."""

    def progress(self, fraction: float, message: str | None = None) -> None:
        """Report progress 0..1."""

    def read(self, cap_id: str) -> dict:
        """Read a state/stream capability → outputs dict."""

    def call(self, cap_id: str, **args: Any) -> dict:
        """Invoke a capability. Commands return outputs; actions block until done
        (raise :class:`ActionFailed` on failure). ``safety.stop`` latches the e-stop."""


class Skill(_RuntimeSkill):
    """Base class for class-style skills (``"entry": "skill.py:MySkill"``).

    Override :meth:`run`. The runtime creates the instance with no
    arguments for every run, so keep per-run state on ``self`` freely.
    """

    def run(self, ctx: Context, **parameters: Any) -> Any:  # pragma: no cover - abstract
        raise NotImplementedError("在子类中实现 run(self, ctx, **parameters)")


__all__ = ["Skill", "Context", "ActionFailed", "SkillError", "SkillInterrupted", "SkillStopped",
           "SkillCancelled", "SkillTimeout"]
