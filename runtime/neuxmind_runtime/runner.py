"""Run manager: executes skills, v1 applications and v2 action invocations.

Every run executes in its own daemon thread. Run status:
``queued → running → succeeded | failed | stopped``; runs found
``queued``/``running`` after a restart become ``unknown`` and are never
replayed.

Stopping
--------
``stop_all`` (global stop) and ``cancel`` only set per-run events and cancel
action handles; they never join threads or wait on locks held by skills.
A skill that ignores stops is detected by :meth:`tick`: after a grace
period its run is finalised anyway and its SkillContext is revoked, so it
can no longer reach the capability bus.

Skills run in a child process by default (``sandbox.py``). The stop path
then additionally sends the child one non-blocking SIGTERM (after the robot
has been halted); :meth:`tick` SIGKILLs the child's process group once the
sandbox's ``kill_grace_s`` has passed, and finalising a run always kills it.

Every run records the safety supervisor's stop *epoch* at admission. Any
stop increments the epoch, so a run admitted before a stop can never reach
the adapter again -- not even after the latch is reset -- including calls
that were already in flight (blocked in adapter IO) when the stop happened.
"""
from __future__ import annotations

import itertools
import json
import math
import threading
import time
import traceback
import uuid
from collections import deque
from typing import Any, Callable

from neuxmind_spec import capabilities as spec_caps
from . import effects
from .bus import CapabilityBus
from .errors import BadRequest, Conflict, EStopLatched, NotAuthorized, NotFound
from .safety import is_motion
from .sandbox import INPROCESS, SandboxConfig, Sandboxes, SandboxHost, SkillFailure
from .skills import (ActionFailed, SkillContext, SkillInterrupted, SkillStore,
                     check_parameters, split_entry)
from .store import Store

QUEUED, RUNNING, SUCCEEDED, FAILED, STOPPED, UNKNOWN = (
    "queued", "running", "succeeded", "failed", "stopped", "unknown")
ACTIVE = (QUEUED, RUNNING)
STOP_GRACE_S = 2.0          # wall seconds a skill thread gets to exit after a stop
MAX_RUN_EVENTS = 300

# Default timeout (robot-clock seconds) for an action invoked directly via
# POST /v2/capabilities/{id}/invoke. A request may shorten it with
# ``timeout_s``, never extend it.
ACTION_DEFAULT_TIMEOUT_S = {
    "base.navigate_to": 600.0,
    "arm.joint_position": 60.0,
    "arm.ee_pose": 60.0,
    "gripper.position": 30.0,
    "model.install": 1800.0,
}
ACTION_FALLBACK_TIMEOUT_S = 120.0
ACTION_CHUNK_MARGIN_S = 5.0


class RunState:
    """Mutable state of one run (kept in memory, persisted on transitions)."""

    _counter = itertools.count()

    def __init__(self, kind: str, target: str, authorized: bool, uses_motion: bool, params: dict):
        self.id = f"run-{uuid.uuid4().hex[:12]}"
        self.kind = kind                    # skill | application | capability
        self.target = target                # skill ref, release id or capability id
        self.authorized = authorized
        self.uses_motion = uses_motion
        self.params = params
        self.status = QUEUED
        self.progress = 0.0
        self.progress_detail: dict = {}
        self.result: Any = None
        self.error: str | None = None
        self.events: deque[dict] = deque(maxlen=MAX_RUN_EVENTS)
        self.created_at = time.time()
        self.started_at: float | None = None
        self.ended_at: float | None = None
        self.deadline: float | None = None  # clock time
        self.stop_event = threading.Event()
        self.stop_reason: str | None = None
        self.stop_requested_at: float | None = None   # wall time
        self.handles: set = set()
        self.used_motion = False
        self.log_events = 0                  # sandbox log/progress budget, accumulated across all steps
        self.log_dropped = 0
        self.revoked = False
        self.finalized = False
        self.epoch: int | None = None       # supervisor stop epoch at admission
        self.thread: threading.Thread | None = None
        self.sandbox: SandboxHost | None = None     # the current skill process (sandbox="process")
        self.sandbox_mode: str | None = None
        self.on_finish: Callable[["RunState"], None] | None = None
        self._bus_events = None
        self.models_exposed: set = set()            # model ids this run saw in ctx.models (§5.1)

    def add_event(self, type_: str, **data: Any) -> None:
        event = {"ts": time.time(), "type": type_, **data}
        self.events.append(event)
        if self._bus_events is not None:
            self._bus_events.emit("run." + type_, run_id=self.id, **data)

    def request_stop(self, reason: str, notify: bool = True) -> None:
        """Mark the run stopping and cancel its actions. Lock-free; used by the stop path.

        ``notify=False`` lets a caller halt the robot first and only then
        tell the skill process (:meth:`notify_sandbox`).
        """
        if self.stop_reason is None:
            self.stop_reason = reason
            self.stop_requested_at = time.time()
        self.stop_event.set()
        for handle in list(self.handles):
            try:
                handle.cancel()
            except Exception:
                pass
        if notify:
            self.notify_sandbox()

    def notify_sandbox(self) -> None:
        """Ask the skill process to exit (non-blocking SIGTERM); never waits for it."""
        sandbox = self.sandbox
        if sandbox is not None:
            sandbox.notify_stop()

    def to_dict(self) -> dict:
        return {"id": self.id, "run_id": self.id, "kind": self.kind, "target": self.target,
                "status": self.status, "progress": self.progress, "progress_detail": dict(self.progress_detail),
                "result": self.result, "error": self.error, "events": list(self.events),
                "parameters": self.params, "created_at": self.created_at, "started_at": self.started_at,
                "ended_at": self.ended_at, "stop_reason": self.stop_reason,
                "capability_access_revoked": self.revoked, "sandbox": self.sandbox_mode}


class Runner:
    def __init__(self, bus: CapabilityBus, skills: SkillStore, store: Store, stop_fn: Callable[[str], dict],
                 sandboxes: Sandboxes | None = None, models=None):
        self.models = models                # ModelStore or None (§5.1: ctx.models)
        self.bus = bus
        self.skills = skills
        self._store = store
        self._stop_fn = stop_fn
        self.sandboxes = sandboxes if sandboxes is not None else Sandboxes(SandboxConfig())
        self._runs: dict[str, RunState] = {}
        self._lock = threading.Lock()       # guards _runs only; never held while running code

    # ------------------------------------------------------------ restart
    def recover_after_restart(self) -> list[str]:
        marked = []
        for record in self._store.list("runs"):
            if record["status"] in ACTIVE:
                record.update(status=UNKNOWN, error="运行时重启时该运行未结束；结果未知，不会自动重放，请现场核对")
                self._store.put("runs", record)
                marked.append(record["id"])
        return marked

    # ------------------------------------------------------------ helpers
    def _persist(self, run: RunState) -> None:
        try:
            self._store.put("runs", run.to_dict())
        except Exception:
            # Unserialisable result: keep the run but drop the result payload.
            data = run.to_dict()
            data["result"] = None
            data["error"] = (data["error"] or "") + "（结果无法序列化）"
            self._store.put("runs", data)

    def _allowed_for(self, package: dict) -> set[str]:
        return {r["id"] for r in package["requires"]} | {r["id"] for r in package["optional"]}

    def _uses_motion(self, caps: set[str]) -> bool:
        return any(is_motion(c) for c in caps if c in self.bus.capability_ids and c != "safety.stop")

    def active_runs(self) -> list[RunState]:
        return [r for r in list(self._runs.values()) if r.status in ACTIVE and not r.finalized]

    def _admit(self, run: RunState, epoch: int | None = None) -> None:
        # ``epoch`` is the stop epoch recorded when the *request arrived* (before
        # the ledger wrote to disk). If a stop or a reset happened since, the
        # request is stale and refused (N-02). Without one, record it now --
        # before the latch check, so a stop landing in between voids the run.
        if epoch is not None:
            self.bus.supervisor.ensure_epoch(epoch, run.target)
        run.epoch = self.bus.supervisor.epoch if epoch is None else epoch
        if run.authorized is not True:
            raise NotAuthorized("技能运行可能产生机器人动作，需要 hardware_authorized=true")
        if run.uses_motion and self.bus.supervisor.latched:
            raise EStopLatched("急停已锁存；需经现场授权 reset 后才能运行运动类技能")
        with self._lock:
            if run.uses_motion:
                owner = next((r for r in self._runs.values()
                              if r.uses_motion and r.status in ACTIVE and not r.finalized), None)
                if owner:
                    raise Conflict(f"运动资源正被运行 {owner.id} 占用；请先等待或取消", code="busy")
            run._bus_events = self.bus.events
            self._runs[run.id] = run
        self._persist(run)

    def motion_owner(self) -> RunState | None:
        return next((r for r in self.active_runs() if r.uses_motion), None)

    # ------------------------------------------------------------ submit
    def submit_skill(self, ref: str, parameters: dict | None, authorized: bool,
                     timeout_s: float | None = None, epoch: int | None = None) -> RunState:
        record = self.skills.resolve(ref)
        package = record["package"]
        params = check_parameters(package["parameters"], parameters)
        timeout = float(package["timeout_s"])
        if timeout_s is not None:
            if (isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float))
                    or not math.isfinite(timeout_s) or timeout_s <= 0):
                raise BadRequest("timeout_s 须为正的有限数值")
            timeout = min(timeout, float(timeout_s))
        allowed = self._allowed_for(package)
        run = RunState("skill", record["id"], authorized, self._uses_motion(allowed), params)

        def body(run: RunState) -> Any:
            run.deadline = self.bus.clock.now() + timeout
            return self._call_skill(run, record, params, allowed)

        self._admit(run, epoch)
        self._start(run, body)
        return run

    def submit_application(self, release: dict, authorized: bool, epoch: int | None = None,
                           on_finish: Callable[[RunState], None] | None = None) -> RunState:
        steps = release["steps"]
        allowed_all = set()
        for index, step in enumerate(steps):
            package = self.skills.resolve(step["skill_ref"])["package"]
            allowed_all |= self._allowed_for(package)
            # Q5-05: every step's parameters are checked against its skill.json *before* the run is
            # admitted (a release deployed by an older runtime, or a step that is only reached
            # after earlier steps have moved the body, can never start with out-of-range values).
            try:
                check_parameters(package["parameters"], step.get("parameters"))
            except BadRequest as exc:
                message = f"第 {index + 1} 步：{exc}"
                raise BadRequest(message, code=exc.code,
                                 extra={**exc.extra, "message": message, "step": index}) from None
        run = RunState("application", release["id"], authorized, self._uses_motion(allowed_all), {})
        run.on_finish = on_finish

        def body(run: RunState) -> Any:
            results = []
            run.result = {"steps": results}
            for index, step in enumerate(steps):
                if run.stop_event.is_set():
                    break
                record = self.skills.resolve(step["skill_ref"])
                params = check_parameters(record["package"]["parameters"], step["parameters"])
                allowed = self._allowed_for(record["package"])
                entry = {"index": index, "entrypoint": step["entrypoint"], "skill": record["id"], "status": RUNNING}
                results.append(entry)
                run.add_event("step.started", index=index, skill=record["id"])
                run.deadline = self.bus.clock.now() + float(step["timeout_s"])
                try:
                    entry["result"] = self._call_skill(run, record, params, allowed)
                    entry["status"] = SUCCEEDED
                except BaseException as exc:
                    entry["status"] = FAILED
                    entry["error"] = f"{type(exc).__name__}: {exc}"
                    raise
                finally:
                    run.add_event("step.finished", index=index, status=entry["status"])
                run.progress = (index + 1) / len(steps)
            return {"steps": results}

        self._admit(run, epoch)
        self._start(run, body)
        return run

    @staticmethod
    def action_timeout(cap_id: str, args: dict, requested: Any = None) -> float:
        """Capability-level default timeout; ``requested`` may only shorten it."""
        if cap_id == "policy.action_chunk":
            actions, dt = args.get("actions"), args.get("dt")
            if (isinstance(actions, list) and not isinstance(dt, bool) and isinstance(dt, (int, float))
                    and math.isfinite(dt) and dt > 0):
                default = len(actions) * float(dt) + ACTION_CHUNK_MARGIN_S
            else:
                default = ACTION_FALLBACK_TIMEOUT_S
        else:
            default = ACTION_DEFAULT_TIMEOUT_S.get(cap_id, ACTION_FALLBACK_TIMEOUT_S)
        if requested is None:
            return default
        if (isinstance(requested, bool) or not isinstance(requested, (int, float))
                or not math.isfinite(requested) or requested <= 0):
            raise BadRequest("timeout_s 须为正的有限数值")
        return min(default, float(requested))

    def submit_capability(self, cap_id: str, args: dict, authorized: bool,
                          timeout_s: Any = None, epoch: int | None = None,
                          on_finish: Callable[[RunState], None] | None = None) -> RunState:
        timeout = self.action_timeout(cap_id, args, timeout_s)
        run = RunState("capability", cap_id, authorized, self._uses_motion({cap_id}),
                       {"args": args, "timeout_s": timeout})
        run.on_finish = on_finish
        # Q3-11: a system capability (model.install) runs on wall-clock time; the
        # simulated body's time scale must not shrink a download's time limit.
        system = not cap_id.startswith("x.") and spec_caps.get(cap_id).safety == "system"
        clock_timeout = timeout * float(getattr(self.bus.clock, "time_scale", 1.0) or 1.0) if system else timeout

        def body(run: RunState) -> Any:
            run.deadline = self.bus.clock.now() + clock_timeout
            ctx = SkillContext(run, self.bus, {cap_id}, {}, self._stop_fn)
            return ctx.call(cap_id, **args)

        self._admit(run, epoch)
        self._start(run, body)
        return run

    def _call_skill(self, run: RunState, record: dict, params: dict, allowed: set[str]) -> Any:
        models = self.models.skill_view(run) if self.models is not None else {}
        ctx = SkillContext(run, self.bus, allowed, params, self._stop_fn, models=models)
        run.sandbox_mode = self.sandboxes.mode
        if self.sandboxes.mode == INPROCESS:
            entry, cleanup = self.skills.load_entry(record, tag=run.id)
            try:
                return entry(ctx, **params)
            finally:
                cleanup()
        entry_file, entry_attr = split_entry(record["package"]["entry"])
        left = run.deadline - self.bus.clock.now() if run.deadline is not None else 3600.0
        host = SandboxHost(self.sandboxes, ctx, files_dir=self.skills.files_dir(record), entry_file=entry_file,
                           entry_attr=entry_attr, params=params,
                           timeout_wall_s=self.bus.clock.real_seconds(max(0.0, left)), events=self.bus.events)
        run.sandbox = host
        try:
            return host.run()
        finally:
            run.sandbox = None

    # ------------------------------------------------------------ execution
    def _start(self, run: RunState, body: Callable[[RunState], Any]) -> None:
        thread = threading.Thread(target=self._execute, args=(run, body), name=run.id, daemon=True)
        run.thread = thread
        effects.mark()
        thread.start()

    def _execute(self, run: RunState, body: Callable[[RunState], Any]) -> None:
        run.status = RUNNING
        run.started_at = time.time()
        run.add_event("started", kind=run.kind, target=run.target)
        self._persist(run)
        error: str | None = None
        result: Any = None
        try:
            result = body(run)
        except SkillInterrupted as exc:
            error = str(exc)
        except ActionFailed as exc:
            error = str(exc)
        except SkillFailure as exc:          # raised in (or by) the skill process
            error = str(exc)
            if exc.traceback:
                run.add_event("exception", traceback=exc.traceback)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            run.add_event("exception", traceback=traceback.format_exc(limit=8)[-4000:])
        if result is not None and not isinstance(result, dict):
            result = {"value": result}
        if result is not None:
            try:
                json.dumps(result, allow_nan=False)
            except (TypeError, ValueError):
                error = error or "技能返回值不能序列化为 JSON"
                result = None
        if run.kind == "application" and result is None and isinstance(run.result, dict):
            result = run.result      # keep partial step results on failure
        self._finalize(run, result, error)

    def _finalize(self, run: RunState, result: Any, error: str | None, note: str | None = None) -> None:
        with self._lock:
            if run.finalized:
                return
            run.finalized = True
            run.revoked = True
        sandbox = run.sandbox
        if sandbox is not None:
            sandbox.kill()                    # a finalised run has no process any more
        if run.stop_reason in ("stopped", "cancelled"):
            status = STOPPED
            error = error or ("已取消" if run.stop_reason == "cancelled" else "已停止")
        elif run.stop_reason == "timeout":
            status = FAILED
            error = "运行超时"
        elif error:
            status = FAILED
        else:
            status = SUCCEEDED
        if status != SUCCEEDED and run.used_motion and not self.bus.supervisor.latched:
            self.bus.supervisor.halt_motion(f"运行 {run.id} 以 {status} 结束")
        run.status = status
        run.result = result
        run.error = error
        if status == SUCCEEDED:
            run.progress = 1.0
        run.ended_at = time.time()
        run.add_event("finished", status=status, error=error, note=note)
        self._persist(run)
        if run.on_finish is not None:
            try:
                run.on_finish(run)
            except Exception as exc:
                self.bus.events.emit("runtime.monitor_error", error=f"on_finish: {exc}")

    # ------------------------------------------------------------ control
    def cancel(self, run_id: str) -> dict:
        run = self._runs.get(run_id)
        if run is None:
            record = self._store.get("runs", run_id)
            if not record:
                raise NotFound(f"找不到运行 {run_id}")
            return {"accepted": False, "status": record["status"], "reason": "运行已结束"}
        if run.finalized:
            return {"accepted": False, "status": run.status, "reason": "运行已结束"}
        run.request_stop("cancelled", notify=False)
        if run.used_motion or run.uses_motion:
            self.bus.supervisor.halt_motion(f"取消运行 {run.id}")
        run.notify_sandbox()                  # robot first, then the skill process
        run.add_event("cancel_requested")
        return {"accepted": True, "status": run.status}

    def stop_all(self, reason: str) -> list[str]:
        """Signal every active run to stop. Lock-free; called from the stop path."""
        stopped = []
        for run in list(self._runs.values()):
            if not run.finalized:
                run.request_stop("stopped")
                stopped.append(run.id)
        return stopped

    def tick(self) -> None:
        """Monitor: enforce deadlines and finalise runs that ignore stops.

        Deadlines (which halt motion) are enforced for every run first; only
        then are overdue runs finalised (which writes to storage), so a slow
        disk cannot delay another run's timeout halt.
        """
        now_clock = self.bus.clock.now()
        now_wall = time.time()
        runs = [r for r in list(self._runs.values()) if not r.finalized]
        for run in runs:
            if run.deadline is not None and now_clock >= run.deadline and not run.stop_event.is_set():
                run.request_stop("timeout", notify=False)
                if run.uses_motion:
                    self.bus.supervisor.halt_motion(f"运行 {run.id} 超时")
                run.notify_sandbox()
                run.add_event("timeout")
        kill_grace = self.sandboxes.config.kill_grace_s
        for run in runs:
            sandbox = run.sandbox
            if (sandbox is not None and run.stop_requested_at is not None
                    and now_wall - run.stop_requested_at > kill_grace):
                sandbox.kill()
        for run in runs:
            if (run.stop_requested_at is not None and now_wall - run.stop_requested_at > STOP_GRACE_S
                    and run.thread is not None and run.thread.is_alive()):
                self._finalize(run, None, None, note="技能线程未在宽限期内退出；其能力访问已撤销")

    def shutdown(self) -> None:
        """Runtime closing: kill every skill process (no grace)."""
        for run in list(self._runs.values()):
            sandbox = run.sandbox
            if sandbox is not None:
                sandbox.kill()

    # ------------------------------------------------------------ queries
    def live_run(self, run_id: str) -> RunState | None:
        """The in-memory run (this process), or None."""
        return self._runs.get(run_id)

    def get(self, run_id: str) -> dict:
        run = self._runs.get(run_id)
        if run is not None:
            return run.to_dict()
        record = self._store.get("runs", run_id)
        if not record:
            raise NotFound(f"找不到运行 {run_id}")
        return record

    def list(self, limit: int = 50) -> list[dict]:
        records = {r["id"]: r for r in self._store.list("runs")}
        for run in list(self._runs.values()):
            records[run.id] = run.to_dict()
        ordered = sorted(records.values(), key=lambda r: r["created_at"], reverse=True)
        return [{k: r[k] for k in ("id", "kind", "target", "status", "progress", "error", "created_at")}
                for r in ordered[:limit]]

    def wait(self, run_id: str, timeout: float) -> dict:
        run = self._runs.get(run_id)
        if run is not None and run.thread is not None:
            deadline = time.monotonic() + timeout
            while not run.finalized and time.monotonic() < deadline:
                run.thread.join(0.05)
        return self.get(run_id)
