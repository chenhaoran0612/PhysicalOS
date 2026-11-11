"""The edge runtime: wires adapter, safety, skills, runs and persistence.

``EdgeRuntime`` holds all behaviour behind the HTTP gateway, so it can be
tested and embedded without HTTP. ``gateway.py`` is only transport.
"""
from __future__ import annotations

import hashlib
import json
import math
import threading
import time
import uuid
from pathlib import Path

from neuxmind_spec import capabilities as spec_caps
from neuxmind_spec import manifest as spec_manifest

from . import __version__
from .bus import CapabilityBus
from .conformance import ConformanceRuns
from .errors import BadRequest, Conflict, NotAuthorized, NotFound, RuntimeApiError, SafetyRejected
from .evaluations import STOP_CASE, Evaluations, new_id as new_evaluation_id
from .events import EventBus
from .hal import RobotAdapter
from .latch_guard import DEFAULT_CONFIG as GUARD_DEFAULTS, GuardConfig, LatchedMotionGuard
from .ledger import Ledger
from .models import CAPABILITY as MODEL_INSTALL, ModelConfig, ModelStore
from .runner import ACTIVE, SUCCEEDED, Runner
from .safety import is_motion
from .sandbox import SandboxConfig, Sandboxes
from .skills import SkillStore, check_parameters
from .store import Store

MONITOR_PERIOD_S = 0.02
CLOSE_JOIN_S = 1.0                 # total time close() waits for the runtime's own threads (Q5-08)
WATCHDOG_PERIOD_S = 0.02
MANIFEST_CHECK_PERIOD_S = 1.0     # how often the adapter's manifest is compared with the start-up copy
LATCH_PERSIST_POLL_S = 0.2
ABNORMAL_SHUTDOWN_REASON = "上次运行时未正常关闭（崩溃或断电）；默认锁存，请现场确认后解除急停"
LATCH_CASE = "急停未锁存"          # name of the /v1/validate check for the e-stop latch
TELEMETRY_CAPACITY = 100_000      # persistent telemetry ring (§5.2)


RESET_EXPECTED_MAX_S = 25.0   # Q7c-07: SafetySupervisor.RESET_EXPIRE_S (30 s) - the desktop's 5 s margin (spec: (0, 25])


def reset_expected(manifest: dict) -> tuple[float | None, str | None]:
    """Q7b-06: ``safety.reset_expected_s`` -- how long ``adapter.reset()`` typically takes on this
    body (a legged stand-up ~10 s, an arm homing slowly ...), so the desktop can choose
    ``expire_s = max(8, reset_expected_s + margin)``. Advisory only (never changes what the
    runtime accepts): a value that is not a positive finite number is ignored, one above
    :data:`RESET_EXPECTED_MAX_S` is clamped; ``(value, note)``."""
    raw = (manifest.get("safety") or {}).get("reset_expected_s")
    if raw is None:
        return None, None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw) or raw <= 0:
        return None, f"safety.reset_expected_s={raw!r} 不是正的有限数值，已忽略"
    if raw > RESET_EXPECTED_MAX_S:
        return RESET_EXPECTED_MAX_S, (f"safety.reset_expected_s={raw!r} 超过 {RESET_EXPECTED_MAX_S:g} s，"
                                      f"按 {RESET_EXPECTED_MAX_S:g} 执行（更慢的复位无法在时限内完成）")
    return float(raw), None


def json_digest(value) -> str:
    """Same digest function as brainos/service.py ``digest``."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


class EdgeRuntime:
    def __init__(self, adapter: RobotAdapter, state_dir: str | Path, *, start_monitor: bool = True,
                 sandbox: str | SandboxConfig = "process", models: ModelConfig | None = None,
                 telemetry_capacity: int | None = TELEMETRY_CAPACITY, latch_guard: GuardConfig | None = GUARD_DEFAULTS,
                 latch_guard_overrides: dict | None = None):
        """``sandbox``: ``"process"`` (default; every skill run in its own child
        process) or ``"inprocess"`` (debug only, must be chosen explicitly), or a
        full :class:`~neuxmind_runtime.sandbox.SandboxConfig`.

        ``models``: enables the runtime-native ``model.install`` (§5.1).
        ``telemetry_capacity``: events kept in the persistent SQLite ring (§5.2);
        ``None`` keeps telemetry in memory only.
        ``latch_guard``: configuration of the latched-state guard (R4b-04; see latch_guard.py);
        ``None`` disables it (tests only).
        ``latch_guard_overrides``: field calibration of the guard (round 07, ``serve --latch-guard``),
        same keys and bounds as the manifest's ``safety.latch_guard`` (overrides it); invalid
        values raise ``ValueError``."""
        self.adapter = adapter
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.events = EventBus()
        if telemetry_capacity is not None:
            self.events.persist(self.state_dir / "telemetry.sqlite3", telemetry_capacity)
        self.models = ModelStore(models, self.events) if models is not None else None
        native = {MODEL_INSTALL: self.models} if self.models is not None else None
        self.bus = CapabilityBus(adapter, self.events, native=native)
        if self.models is not None and MODEL_INSTALL not in self.bus.native:
            self.models = None                       # the adapter brings its own model.install: it wins
        self.supervisor = self.bus.supervisor
        self.latch_guard = (LatchedMotionGuard(self.bus, self.events, self.bus.clock, latch_guard, adapter=adapter,
                                               overrides=latch_guard_overrides)
                            if latch_guard is not None else None)
        self.reset_expected_s, self.reset_expected_note = reset_expected(self.bus.manifest)
        self.store = Store(self.state_dir)
        self.skills = SkillStore(self.store, self.state_dir / "skills", self.bus.manifest)
        config = sandbox if isinstance(sandbox, SandboxConfig) else SandboxConfig(mode=sandbox)
        self.sandboxes = Sandboxes(config, self.state_dir)
        self.runner = Runner(self.bus, self.skills, self.store, self._skill_stop, self.sandboxes,
                             models=self.models)
        if self.models is not None:
            self.models.in_use = self._model_users
        self.ledger = Ledger(self.store)
        self.evaluations = Evaluations(self.store, self.runner, self._evaluation_result)
        self.conformance = ConformanceRuns(self)       # §6.1 POST /v2/conformance
        self.conformance_lease: str | None = None       # motion lease while a conformance run is active
        self.conformance_refused = 0
        self._install_lock = threading.Lock()         # model.install admission (Q3-06); never on the stop path
        self._latch_write_lock = threading.Lock()    # persister vs reset; never taken by the stop path
        self._latch_dirty = threading.Event()        # set by the stop path; the persister writes

        # Boot sequence: a monotonic counter across restarts, used (with the
        # stop epoch) to order latch writes. "running" stays true until a
        # clean close(); finding it true at boot means an abnormal shutdown.
        boot = self.store.get("safety", "boot") or {}
        abnormal = bool(boot.get("running"))
        self._boot_seq = int(boot.get("seq", 0)) + 1
        self.store.put("safety", {"id": "boot", "seq": self._boot_seq, "running": True, "ts": time.time()})

        # Restart recovery: never replay; keep a latched e-stop latched;
        # after an abnormal shutdown, start latched.
        unknown_requests = self.ledger.recover()
        unknown_runs = self.runner.recover_after_restart()
        unknown_evaluations = self.evaluations.recover_after_restart()
        self.conformance.recover_after_restart()
        latch = self.store.get("safety", "latch")
        if latch and latch.get("latched"):
            self.supervisor.restore_latch(latch.get("reason") or "重启前已锁存")
        elif abnormal:
            self.supervisor.restore_latch(ABNORMAL_SHUTDOWN_REASON)
        if self.supervisor.latched:
            self._write_latch(True, self.supervisor.latch_reason, self.supervisor.epoch)
        self.events.emit("runtime.started", instance_id=self.bus.manifest["instance_id"],
                         simulated=self.bus.manifest["simulated"], unknown_requests=unknown_requests,
                         unknown_runs=unknown_runs, unknown_evaluations=unknown_evaluations,
                         latched=self.supervisor.latched, abnormal_shutdown=abnormal,
                         sandbox=self.sandboxes.describe())

        self._closing = threading.Event()
        self._threads: list[threading.Thread] = []
        # The latch persister always runs (the stop path only sets an Event).
        self._spawn(self._latch_persist_loop, "nx-latch-persist")
        if start_monitor:
            # Watchdog has its own thread and does no storage IO, so a slow
            # disk (run finalisation, ledger writes) can never delay it.
            self._spawn(self._watchdog_loop, "nx-watchdog")
            self._spawn(self._monitor_loop, "nx-monitor")
            if self.latch_guard is not None:
                self._spawn(self._latch_guard_loop, "nx-latch-guard")

    # ------------------------------------------------------------ lifecycle
    def _spawn(self, target, name: str) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)
        self._threads.append(thread)
        thread.start()

    def _watchdog_loop(self) -> None:
        while not self._closing.wait(WATCHDOG_PERIOD_S):
            try:
                self.supervisor.watchdog_tick()
            except Exception as exc:     # must survive anything
                self.events.emit("runtime.watchdog_error", error=str(exc))

    def _latch_guard_loop(self) -> None:
        guard = self.latch_guard
        while not self._closing.wait(guard.config.period_s):
            try:
                guard.tick(self._closing)
            except Exception as exc:     # must survive anything
                self.events.emit("runtime.monitor_error", error=f"latch guard: {exc}")

    def _monitor_loop(self) -> None:
        next_drift_check = 0.0
        while not self._closing.wait(MONITOR_PERIOD_S):
            try:
                self.runner.tick()
            except Exception as exc:     # the monitor must survive anything
                self.events.emit("runtime.monitor_error", error=str(exc))
            if self.latch_guard is not None:
                try:
                    self.latch_guard.check_stalled_read()   # Q5-02: a hung feedback read while latched
                except Exception as exc:
                    self.events.emit("runtime.monitor_error", error=f"latch guard: {exc}")
            try:
                self.sandboxes.periodic_reap()      # setsid escapees when there is no cgroup
            except Exception as exc:
                self.events.emit("runtime.monitor_error", error=str(exc))
            if time.monotonic() >= next_drift_check:
                next_drift_check = time.monotonic() + MANIFEST_CHECK_PERIOD_S
                try:
                    self.bus.check_manifest_drift()
                except Exception as exc:
                    self.events.emit("runtime.monitor_error", error=str(exc))

    def monitor_tick(self) -> None:
        """One watchdog + monitor iteration (for tests running without threads)."""
        fns = [self.supervisor.watchdog_tick, self.runner.tick]
        if self.latch_guard is not None:
            fns += [self.latch_guard.tick, self.latch_guard.check_stalled_read]
        for fn in fns:
            try:
                fn()
            except Exception as exc:
                self.events.emit("runtime.monitor_error", error=str(exc))

    def _latch_persist_loop(self) -> None:
        while not self._closing.is_set():
            if not self._latch_dirty.wait(LATCH_PERSIST_POLL_S):
                continue
            self._latch_dirty.clear()
            self._persist_current_latch()

    def _persist_current_latch(self) -> None:
        epoch = self.supervisor.epoch
        self._write_latch(self.supervisor.latched, self.supervisor.latch_reason, epoch)

    def _write_latch(self, latched: bool, reason: str | None, epoch: int) -> bool:
        """Write the latch record only if it is newer than what is on disk.

        Order key = (boot sequence, stop epoch, phase) with phase 0 for a
        latched write and 1 for the reset of that same epoch. A delayed
        write can therefore never overwrite a newer state; an equal key
        (same state, e.g. the stop reason read from the body) overwrites.
        """
        key = [self._boot_seq, int(epoch), 0 if latched else 1]
        try:
            with self._latch_write_lock:
                stored = self.store.get("safety", "latch") or {}
                if list(stored.get("key") or [-1]) > key:
                    return False          # disk already holds a newer state
                self.store.put("safety", {"id": "latch", "latched": latched, "reason": reason,
                                          "key": key, "ts": time.time()})
                return True
        except Exception as exc:
            self.events.emit("safety.persist_error", error=str(exc))
            return False

    def close(self) -> None:
        self.runner.shutdown()               # kill skill processes
        self._closing.set()
        # Q5-08: bounded join. Every thread here is a daemon and checks _closing; one stuck in an
        # adapter call (e.g. the latch guard inside a feedback read that never returns) is left
        # behind, makes no further adapter call once that call returns, and dies with the process.
        deadline = time.monotonic() + CLOSE_JOIN_S
        for thread in self._threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        self.lingering_threads = [t.name for t in self._threads if t.is_alive()]
        try:
            self._persist_current_latch()
            self.store.put("safety", {"id": "boot", "seq": self._boot_seq, "running": False, "ts": time.time()})
        except Exception:
            pass
        if self.latch_guard is not None:
            self.latch_guard.flush()         # Q7-08: aggregated re-stop counts are not lost on close
        if self.models is not None:
            self.models.close()
        try:
            self.adapter.close()
        finally:
            self.store.close()
            self.sandboxes.close()
            self.events.close()              # flushes the telemetry ring (clean shutdown: no seq gap)

    # ------------------------------------------------------------ manifests
    @property
    def manifest(self) -> dict:
        return self.bus.manifest

    def manifest_v1(self) -> dict:
        return spec_manifest.manifest_v1_view(self.bus.manifest)

    def manifest_v1_digest(self) -> str:
        # brainos stores digest(validate_manifest(v1)); validate_manifest is
        # an identity on manifest_v1_view output (unique, trimmed strings).
        return json_digest(self.manifest_v1())

    def capabilities_view(self) -> dict:
        """``/v1/status.capabilities`` (round 07 second fix pass, Q7b-05/06):

        * ``reset_expire``: a reset whose ``adapter.reset()`` returns after the caller's
          ``expire_s`` (or the server limit) is void -- the latch stays on (Q7-10). Runtimes
          without this field ignore ``expire_s``: a late reset there may still unlatch.
        * ``reset_expire_max_s``: the server limit ``expire_s`` is clamped to (``[1, max]``).
        * ``reset_expected_s``: the manifest's typical reset duration (``safety.reset_expected_s``),
          ``null`` when not declared."""
        limit = self.supervisor.reset_expire_s
        return {"reset_expire": bool(limit), "reset_expire_max_s": limit if limit else None,
                "reset_expire_min_s": 1.0, "reset_expected_s": self.reset_expected_s,
                "reset_expected_note": self.reset_expected_note}

    def status(self) -> dict:
        try:
            health = self.bus.read("system.health")
        except RuntimeApiError as exc:
            health = {"ok": False, "mode": "unavailable", "errors": {"adapter": str(exc)}}
        owner = self.runner.motion_owner()
        telemetry = self.events.describe()        # cached values only: never waits for the ring (Q3-08)
        out = {"instance_id": self.manifest["instance_id"], "name": self.manifest["name"],
               "family": self.manifest["family"], "simulated": self.manifest["simulated"],
               "runtime_version": __version__, "spec_version": spec_caps.SPEC_VERSION,
               "health": health, "mode": health.get("mode"),
               "estop_latched": self.supervisor.latched, "latch_reason": self.supervisor.latch_reason,
               "stop_count": self.supervisor.stop_count, "stop_epoch": self.supervisor.epoch,
               "manifest_changed": self.supervisor.manifest_drift,
               "reset_in_progress": self.supervisor.reset_in_progress,
               **self.supervisor.reset_progress_view(),
               # Q7b-05/06: what this runtime does with a reset, so a client can word its warning
               # and choose expire_s = max(8, reset_expected_s + margin) <= reset_expire_max_s
               "capabilities": self.capabilities_view(),
               "reset_expected_s": self.reset_expected_s,
               "latched_motion": (self.latch_guard.describe() if self.latch_guard is not None
                                  else {"enabled": False}),
               "active_runs": [r.id for r in self.runner.active_runs()],
               "motion_owner": owner.id if owner else None,
               "sandbox": self.sandboxes.describe(),
               "models": self._models_status(), "telemetry": telemetry,
               "telemetry_degraded": telemetry["degraded"]}
        try:
            extra = self.adapter.status() or {}
            out["adapter"] = extra
        except Exception as exc:
            out["adapter"] = {"error": str(exc)}
        return out

    # ------------------------------------------------------------ stop path
    def emergency_stop(self, reason: str = "stop", source: str = "api", keep_reason: bool = False) -> dict:
        """Global stop. Order matters: latch + adapter.stop first, bookkeeping after.

        No database write, lock, queue, model or network call happens before
        the adapter has been told to stop.
        """
        result = self.supervisor.emergency_stop(reason, source, keep_reason=keep_reason)
        stopped_runs = self.runner.stop_all(reason)
        # Persisted by the long-lived persister thread: no thread is created
        # here (thread creation can fail under exhaustion) and no IO happens.
        self._latch_dirty.set()
        return {**result, "accepted": True, "stopped_runs": stopped_runs}

    def annotate_stop_reason(self, reason: str) -> None:
        """Record the operator's reason (read from the stop body after stopping)."""
        if self.supervisor.latched and reason:
            self.supervisor.latch_reason = str(reason)[:500]
            self._latch_dirty.set()

    def restore_stop_reason(self, prior: str, stop_count: int | None) -> bool:
        """R4c-02 (``/v2/stop keep_reason``): put ``prior`` back as the latch reason only if stop
        number ``stop_count`` is still the latest stop and the reason is still its placeholder."""
        if not isinstance(stop_count, int) or not prior:
            return False
        wrote = self.supervisor.set_reason_if(str(prior)[:500], stop_count=stop_count, expect_reason="stop")
        if wrote:
            self._latch_dirty.set()
        return wrote

    def _skill_stop(self, reason: str) -> dict:
        return self.emergency_stop(reason, source="skill")

    def _check_lease(self, payload: dict, what: str) -> None:
        """Q4-15: while a conformance run holds the motion lease, every other motion source (and
        reset) gets 409 ``conformance_running``. Stop is never affected."""
        lease = self.conformance_lease
        if lease is not None and payload.get("conformance_lease") != lease:
            self.conformance_refused += 1
            self.events.emit("conformance.external_refused", what=what)
            raise Conflict("一致性测试正在进行，拒绝其他来源的运动与复位（停止始终可用）", code="conformance_running")

    def reset(self, payload: dict, *, operation: str = "reset", require_request_id: bool = False) -> dict:
        self._check_lease(payload, operation)
        if self.supervisor.reset_in_progress:
            # R4c-01: answered before the ledger so a retry with the same request_id works later.
            # The supervisor re-checks atomically (a reset starting right now is refused there).
            raise self.supervisor.reset_in_progress_error()

        def do() -> dict:
            if self.runner.active_runs():
                raise Conflict("仍有运行未结束，不能复位", code="runs_active")
            expect = payload.get("expect_stop_count")
            if expect is not None and (isinstance(expect, bool) or not isinstance(expect, int)):
                raise BadRequest("expect_stop_count 须为整数")
            expire = payload.get("expire_s")       # Q7-10: how long the caller waits for this reset
            if expire is not None and (isinstance(expire, bool) or not isinstance(expire, (int, float))
                                       or not math.isfinite(expire) or expire <= 0):
                raise BadRequest("expire_s 须为正数（秒）")
            return self.supervisor.reset(payload.get("hardware_authorized") is True,
                                         persist=lambda latched, reason, epoch:
                                         self._write_latch(latched, reason, epoch),
                                         expect_stop_count=expect, expire_s=expire)
        if require_request_id or "request_id" in payload:
            result = dict(self.ledger.execute(operation, payload, do))
        else:
            result = dict(do())
        # A replayed request_id returns the *current* latch state, never a
        # stale cached "latched: false" (R-14).
        result["latched"] = self.supervisor.latched
        return result

    def v1_reset(self, payload: dict) -> dict:
        """POST /v1/reset: same semantics as /v2/reset, request_id required (idempotent)."""
        return self.reset(payload, operation="v1.reset", require_request_id=True)

    # ------------------------------------------------------------ v2
    def read_state(self, ids: list[str] | None) -> dict:
        ids = ids or self.bus.readable()
        return {cap: self.bus.read(cap) for cap in ids}

    def invoke(self, cap_id: str, payload: dict) -> dict:
        try:
            cap_id = spec_caps.normalize(cap_id)
        except ValueError as exc:
            raise NotFound(str(exc)) from None
        if cap_id == "safety.stop":
            args = payload.get("args") or {}
            reason = args.get("reason") if isinstance(args, dict) else None
            result = self.emergency_stop(str(reason or "safety.stop"), source="invoke")
            return {"stopped": result["stopped"]}
        args = payload.get("args", {})
        if not isinstance(args, dict):
            raise BadRequest("args 须为对象")
        epoch = self.supervisor.epoch       # the stop epoch when this request arrived
        kind = self.bus.kind(cap_id)
        if is_motion(cap_id):
            self._check_lease(payload, "invoke:" + cap_id)
        if kind in ("state", "stream"):
            raise BadRequest(f"{cap_id} 是 {kind} 能力，请用 GET /v2/state?ids={cap_id}")
        authorized = payload.get("hardware_authorized") is True
        if self.models is not None and cap_id == MODEL_INSTALL:
            # Synchronous answers for what can be known before a run starts.
            if not authorized:
                raise NotAuthorized("model.install 会变更端侧软件，需要 hardware_authorized=true（现场确认）")
            self.bus._validate_inputs(cap_id, args)          # spec: package_ref, sha256, runtime (+ model_id, name)
            clean = self.models.precheck(args)
            return self.ledger.execute("invoke:" + cap_id, payload,
                                       lambda: self._submit_install(args, clean["sha256"], payload.get("timeout_s"),
                                                                    epoch))

        def do() -> dict:
            if kind == "command":
                if is_motion(cap_id):
                    owner = self.runner.motion_owner()
                    if owner:
                        raise Conflict(f"运动资源正被运行 {owner.id} 占用", code="busy")
                return self.bus.invoke(cap_id, args, authorized, epoch=epoch)
            if cap_id == "policy.action_chunk":
                self._precheck_chunk(args)
            run = self.runner.submit_capability(cap_id, args, authorized, payload.get("timeout_s"), epoch=epoch)
            return {"run_id": run.id, "status": run.status}

        return self.ledger.execute("invoke:" + cap_id, payload, do)

    def _precheck_chunk(self, args: dict) -> None:
        """R4c-05: a joint_position action chunk that can never be limited on this body (decided from
        the manifest alone, no adapter call) is refused synchronously with a 4xx instead of a run id
        that fails a moment later. The run re-checks everything (joint.state names, limits)."""
        self.bus._validate_inputs("policy.action_chunk", args)
        static = self.bus.chunk_static_error()
        if static is None:
            return
        code, message = static
        self.events.emit("safety.rejected", capability="policy.action_chunk", reason=message, code=code)
        raise BadRequest(f"policy.action_chunk 被安全监督器拒绝：{message}", code=code)

    def install_skill(self, payload: dict) -> dict:
        return self.ledger.execute("skills.install", payload,
                                   lambda: self.skills.install(payload.get("package")))

    def run_skill(self, skill_ref: str, payload: dict) -> dict:
        epoch = self.supervisor.epoch       # at arrival, before the ledger touches the disk (N-02)
        self._check_lease(payload, "skills.run")

        def do() -> dict:
            ref = skill_ref
            if payload.get("version"):
                ref = f"{skill_ref}@{payload['version']}"
            run = self.runner.submit_skill(ref, payload.get("parameters") or {},
                                           payload.get("hardware_authorized") is True,
                                           payload.get("timeout_s"), epoch=epoch)
            return {"run_id": run.id, "status": run.status}
        return self.ledger.execute("skills.run:" + skill_ref, payload, do)

    def telemetry(self, since: int) -> dict:
        events = self.events.since(since)
        return {"events": events, "last_seq": events[-1]["seq"] if events else max(since, 0)}

    # ------------------------------------------------------------ v1 compat
    @staticmethod
    def _check_application_shape(app) -> None:
        if not isinstance(app, dict) or app.get("schema") != "robot.application.v1":
            raise BadRequest("需要 robot.application.v1 应用")
        steps = app.get("steps")
        if not isinstance(steps, list) or not 1 <= len(steps) <= 50:
            raise BadRequest("应用需要 1–50 个步骤")
        for step in steps:
            if not isinstance(step, dict):
                raise BadRequest("每个步骤须为对象")

    def v1_validate(self, app) -> dict:
        """Device-side pre-execution check of a robot.application.v1."""
        self._check_application_shape(app)
        cases: list[dict] = []

        def case(name: str, passed: bool, detail: str) -> None:
            cases.append({"name": name, "passed": bool(passed), "detail": detail})

        if "digest" in app:
            body = {k: v for k, v in app.items() if k != "digest"}
            ok = json_digest(body) == app["digest"]
            case("应用摘要一致", ok, "摘要校验通过" if ok else "应用内容与 digest 不一致")
        have = self.bus.capability_ids
        required: set[str] = set()
        unknown_caps: list[str] = []
        for index, step in enumerate(app["steps"]):
            label = f"步骤 {index + 1}（{step.get('entrypoint')}）"
            requires = step.get("requires", []) or []
            if not isinstance(requires, list) or not all(isinstance(c, str) for c in requires):
                case(f"{label} 能力需求格式有效", False, "requires 须为能力 ID 字符串列表")
                requires = []
            for cap in requires:
                try:
                    required.add(spec_caps.normalize(cap))
                except ValueError:
                    unknown_caps.append(cap)
            try:
                record = self.skills.resolve(step.get("entrypoint"))
            except RuntimeApiError as exc:
                case(f"{label} 技能已安装", False, str(exc))
                continue
            case(f"{label} 技能已安装", True, f"{record['id']} digest={record['digest'][:12]}")
            compat = spec_manifest.check_compatibility(record["package"], self.manifest)
            case(f"{label} 技能与本体兼容", compat["compatible"],
                 "兼容" if compat["compatible"] else f"缺少 {compat['missing']}；约束 {compat['violations']}")
            try:
                check_parameters(record["package"]["parameters"], step.get("parameters") or {})
                case(f"{label} 参数有效", True, "参数符合技能声明")
            except RuntimeApiError as exc:
                case(f"{label} 参数有效", False, str(exc))
            timeout = step.get("timeout_s", 60)
            ok = not isinstance(timeout, bool) and isinstance(timeout, (int, float)) and 0 < timeout <= 86400
            case(f"{label} 超时有效", ok, f"timeout_s={timeout}")
        missing = sorted((required - have) | set(unknown_caps))
        case("设备能力覆盖应用需求", not missing,
             "能力声明覆盖应用需求" if not missing else "缺少或非标准能力：" + ", ".join(missing))
        case(LATCH_CASE, not self.supervisor.latched,
             "可运动" if not self.supervisor.latched else
             f"急停已锁存（{self.supervisor.latch_reason}）。请在桌面端解除急停：确认现场安全后点击"
             f"“解除急停”（POST /v1/reset，需 hardware_authorized=true）")
        passed = all(c["passed"] for c in cases)
        return {"accepted": passed, "passed": passed, "cases": cases, "simulated": self.manifest["simulated"]}

    def _pin_steps(self, app: dict) -> list[dict]:
        steps = []
        for step in app["steps"]:
            record = self.skills.resolve(step["entrypoint"])
            steps.append({"entrypoint": step["entrypoint"], "skill_ref": record["id"],
                          "skill_digest": record["digest"], "parameters": step.get("parameters") or {},
                          "timeout_s": step.get("timeout_s", 60)})
        return steps

    def _require_valid(self, app) -> dict:
        check = self.v1_validate(app)
        if not check["passed"]:
            failed = [c for c in check["cases"] if not c["passed"]]
            raise Conflict("设备端检查未通过：" + "；".join(f"{c['name']}：{c['detail']}" for c in failed),
                           code="validation_failed", extra={"cases": check["cases"]})
        return check

    def v1_deploy(self, payload: dict) -> dict:
        def do() -> dict:
            app = payload.get("application")
            self._require_valid(app)
            release = {"id": "rel-" + uuid.uuid4().hex[:12], "application_id": app.get("id"),
                       "application_digest": app.get("digest"), "application_name": app.get("name"),
                       "report_id": payload.get("report_id"), "steps": self._pin_steps(app),
                       "manifest_digest": self.manifest_v1_digest(), "created_at": time.time()}
            self.store.put("releases", release)
            return {"accepted": True, "release_id": release["id"],
                    "pinned": [s["skill_ref"] for s in release["steps"]]}
        return self.ledger.execute("v1.deploy", payload, do)

    def v1_start(self, payload: dict) -> dict:
        epoch = self.supervisor.epoch       # at arrival (N-02)
        self._check_lease(payload, "v1.start")

        def do() -> dict:
            if payload.get("hardware_authorized") is not True:
                raise NotAuthorized("本次硬件操作尚未明确授权")
            release = self.store.get("releases", str(payload.get("release_id")))
            if not release:
                raise NotFound("找不到设备端版本", code="release_not_found")
            if release["manifest_digest"] != self.manifest_v1_digest():
                raise Conflict("本体能力清单已变化，请重新部署", code="manifest_changed")
            run = self.runner.submit_application(release, True, epoch=epoch)
            return {"accepted": True, "state": "running", "run_id": run.id}
        return self.ledger.execute("v1.start", payload, do)

    def v1_evaluate(self, payload: dict) -> dict:
        """On-site test: run the application and report per-step results.

        Synchronous by default (the original v1 behaviour). With
        ``async: true`` it returns ``{accepted, evaluation_id, status}`` at
        once; poll :meth:`get_evaluation` (INTERFACES_V1 §4.1).

        For ``simulated`` bodies the response says so (``simulated``,
        ``evidence_kind="simulation"``, ``simulation://`` evidence ref); it
        must never be treated as on-site evidence.
        """
        epoch = self.supervisor.epoch       # at arrival (N-02)
        self._check_lease(payload, "v1.evaluate")
        mode = payload.get("async", False)
        if not isinstance(mode, bool):
            raise BadRequest("async 须为布尔值")
        if mode:
            return self.ledger.execute("v1.evaluate", payload, lambda: self._start_async_evaluation(payload, epoch))

        def do() -> dict:
            if payload.get("hardware_authorized") is not True:
                raise NotAuthorized("本次硬件操作尚未明确授权")
            app = payload.get("application")
            check = self._require_valid(app)
            release = {"id": "eval-" + uuid.uuid4().hex[:12], "steps": self._pin_steps(app)}
            run = self.runner.submit_application(release, True, epoch=epoch)
            budget = sum(float(s["timeout_s"]) for s in release["steps"])
            record = self.runner.wait(run.id, self.bus.clock.real_seconds(budget) + 10.0)
            if record["status"] in ("queued", "running"):
                self.runner.cancel(run.id)
                record = self.runner.wait(run.id, 5.0)
            return self._evaluation_result({"application_digest": app.get("digest"), "precheck": check["cases"]},
                                           record)
        return self.ledger.execute("v1.evaluate", payload, do)

    def _start_async_evaluation(self, payload: dict, epoch: int) -> dict:
        if payload.get("hardware_authorized") is not True:
            raise NotAuthorized("本次硬件操作尚未明确授权")
        app = payload.get("application")
        check = self.v1_validate(app)
        failed = [c for c in check["cases"] if not c["passed"]]
        latched_only = bool(failed) and all(c["name"] == LATCH_CASE for c in failed)
        if failed and not latched_only:
            self._require_valid(app)        # raises 409 validation_failed, exactly like the sync mode
        evaluation_id = new_evaluation_id()
        self.evaluations.create(evaluation_id, payload["request_id"], app.get("digest"), check["cases"])
        try:
            if latched_only:
                # E-stop latched: the test ends at once, failed, never "passed".
                self.evaluations.complete(evaluation_id, None, "急停已锁存，现场测试未执行")
            else:
                release = {"id": "eval-" + uuid.uuid4().hex[:12], "steps": self._pin_steps(app)}
                try:
                    run = self.runner.submit_application(release, True, epoch=epoch,
                                                         on_finish=self.evaluations.on_run_finished(evaluation_id))
                except SafetyRejected as exc:   # stop / latch between the check and admission
                    self.evaluations.complete(evaluation_id, None, f"现场测试开始前收到停止/急停：{exc}")
                else:
                    self.evaluations.attach_run(evaluation_id, run.id)
        except BaseException:
            self.evaluations.discard(evaluation_id)
            raise
        view = self.evaluations.get(evaluation_id)
        return {"accepted": True, "evaluation_id": evaluation_id, "status": view["status"],
                "run_id": view.get("run_id"), "simulated": self.manifest["simulated"]}

    def get_evaluation(self, evaluation_id: str) -> dict:
        """GET /v1/evaluations/{id} → ``{evaluation_id, status, result?}``."""
        return self.evaluations.get(evaluation_id)

    def _evaluation_result(self, evaluation: dict, run_record: dict | None, stop_note: str | None = None) -> dict:
        """The ``/v1/evaluate`` response for a run (shared by the sync and async modes).

        ``stop_note``: the test was stopped (or never started because of an
        e-stop); it then always fails and the reason is added to ``cases``.
        """
        run_record = run_record or {}
        run_id = run_record.get("id")
        simulated = self.manifest["simulated"]
        status = run_record.get("status", "stopped")
        step_results = (run_record.get("result") or {}).get("steps", [])
        cases = [{"name": f"步骤 {s['index'] + 1} {s['skill']}", "passed": s["status"] == SUCCEEDED,
                  "detail": s.get("error") or ("完成" if s["status"] == SUCCEEDED else "未完成")}
                 for s in step_results]
        error = run_record.get("error")
        if stop_note is not None:
            latch = f"（急停原因：{self.supervisor.latch_reason}）" if self.supervisor.latched else ""
            cases.append({"name": STOP_CASE, "passed": False, "detail": stop_note + latch})
            if status in ACTIVE:
                status = "stopped"
            error = error or stop_note
        scheme = "simulation" if simulated else "runtime"
        where = f"runs/{run_id}" if run_id else f"evaluations/{evaluation.get('id')}"
        return {"passed": status == SUCCEEDED and stop_note is None, "status": status, "error": error,
                "application_digest": evaluation.get("application_digest"),
                "robot_manifest_digest": self.manifest_v1_digest(),
                "evidence_ref": f"{scheme}://{self.manifest['instance_id']}/{where}",
                "evidence_kind": "simulation" if simulated else "device_run",
                "simulated": simulated, "run_id": run_id,
                "cases": cases, "precheck": evaluation.get("precheck", [])}

    def v1_install_model(self, payload: dict) -> dict:
        if "model.install" not in self.bus.capability_ids:
            raise BadRequest("本体未声明 model.install 能力，不能安装端侧模型", code="capability_not_provided")
        if self.models is not None:
            return self._v1_install_native(payload)

        def do() -> dict:
            try:
                return self.adapter.install_model(payload)
            except NotImplementedError as exc:
                raise BadRequest(str(exc), code="not_implemented") from None
        return self.ledger.execute("v1.models.install", payload, do)

    # ------------------------------------------------------------ models (§5.1)
    def _models_status(self) -> dict:
        if self.models is not None:
            return self.models.describe()
        if MODEL_INSTALL in self.bus.capability_ids:
            return {"enabled": True, "provider": "adapter"}
        return {"enabled": False}

    def _model_users(self, model_id: str) -> list[str]:
        """Active skill/application runs that were given ``model_id`` in ``ctx.models``."""
        return [r.id for r in self.runner.active_runs() if model_id in getattr(r, "models_exposed", ())]

    def _require_native_models(self) -> ModelStore:
        if self.models is None:
            if MODEL_INSTALL in self.bus.capability_ids:
                raise BadRequest("model.install 由适配器提供；已安装模型请查询适配器", code="models_by_adapter")
            raise NotFound("运行时未启用端侧模型安装（--models-dir）", code="capability_not_provided")
        return self.models

    def list_models(self) -> dict:
        return {"models": self._require_native_models().list()}

    def delete_model(self, model_id: str, payload: dict) -> dict:
        models = self._require_native_models()
        if payload.get("hardware_authorized") is not True:
            raise NotAuthorized("删除端侧模型需要 hardware_authorized=true（现场确认）")
        if "request_id" in payload:
            return self.ledger.execute("models.delete:" + model_id, payload, lambda: models.delete(model_id))
        return models.delete(model_id)

    def _v1_install_native(self, payload: dict) -> dict:
        """``/v1/models/install`` on the native store: same implementation as
        ``model.install``. Arguments in ``args`` or at the top level; an optional
        ``model_id`` (catalog id) and ``name`` are recorded like on v2. Returns at once with
        ``run_id`` (poll ``/v2/runs/{run_id}``); ``model_id`` here is the sha256 (the installed
        model's identity), ``catalog_model_id`` the catalog id."""
        source = payload.get("args") if isinstance(payload.get("args"), dict) else payload
        args = {k: source.get(k) for k in ("package_ref", "sha256", "runtime", "model_id", "name")
                if source.get(k) is not None}
        if payload.get("hardware_authorized") is not True:
            raise NotAuthorized("model.install 会变更端侧软件，需要 hardware_authorized=true（现场确认）")
        clean = self.models.precheck(args)
        epoch = self.supervisor.epoch

        def do() -> dict:
            out = self._submit_install(clean, clean["sha256"], payload.get("timeout_s"), epoch)
            return {"accepted": True, **out, "model_id": clean["sha256"],
                    "sha256": clean["sha256"], "catalog_model_id": clean.get("model_id")}
        return self.ledger.execute("v1.models.install", payload, do)

    def _submit_install(self, args: dict, sha: str, timeout_s, epoch: int) -> dict:
        """Q3-06: the same sha already installing -> that run (``merged``); at most
        ``max_concurrent`` installs at once -> otherwise 409 ``install_busy``."""
        from .models import InstallBusy
        with self._install_lock:
            owner = self.models.owner(sha)
            if owner is not None:
                try:
                    status = self.runner.get(owner)["status"]
                except NotFound:
                    status = None
                if status in ACTIVE:
                    return {"run_id": owner, "status": status, "merged": True}
                self.models.release(sha, owner)            # its run is gone: stale claim
            if self.models.describe()["claims"] >= self.models.config.max_concurrent:
                raise Conflict(f"已有 {self.models.config.max_concurrent} 个模型正在安装，请稍后重试",
                               code="install_busy")
            run = self.runner.submit_capability(MODEL_INSTALL, args, True, timeout_s, epoch=epoch,
                                                on_finish=lambda r: self.models.release(sha, r.id))
            try:
                self.models.claim(sha, run.id)
            except InstallBusy:
                pass                                       # the run itself will report it
            return {"run_id": run.id, "status": run.status}

    # ------------------------------------------------------------ conformance (§6.1)
    def start_conformance(self, payload: dict) -> dict:
        """POST /v2/conformance ``{request_id, level, hardware_authorized}`` → ``{accepted, conformance_id, status}``.

        Runs the suite in this process against this runtime (asynchronously); poll
        :meth:`get_conformance`. ``motion`` needs ``hardware_authorized=true``."""
        return self.ledger.execute("conformance", payload, lambda: self.conformance.start(payload))

    def get_conformance(self, conformance_id: str) -> dict:
        return self.conformance.get(conformance_id)

    def run_events(self, run_id: str, since: int = 0, limit: int = 1000) -> dict:
        """§5.2 replay: every persisted event of one run (across restarts, bounded by the ring)."""
        known = True
        try:
            self.runner.get(run_id)
        except NotFound:
            known = False
        events = self.events.run_events(run_id, since, limit)
        if not known and not events:
            raise NotFound(f"未知运行 {run_id}")
        return {"run_id": run_id, "events": events, "last_seq": events[-1]["seq"] if events else since,
                "more": len(events) >= limit, **self.events.retention()}
