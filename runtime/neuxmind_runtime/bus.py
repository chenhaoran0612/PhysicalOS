"""Capability bus: the single path from skills / HTTP to the adapter.

Every call is:  spec input validation → safety supervisor → adapter →
spec output validation. Nothing else in the runtime calls the adapter's
motion methods directly (the supervisor's stop/watchdog excepted).

Every motion call carries the stop ``epoch`` recorded when its request or
run was admitted, and optionally a ``guard`` callable (the run's own
"am I stopped/revoked?" check). Both are re-checked immediately before the
adapter is called -- after any slow state read -- and the epoch again
right after dispatch, so a call that was in flight across a stop (and a
reset) can never reach or keep driving the robot.
"""
from __future__ import annotations

import copy
from typing import Callable

from neuxmind_spec import capabilities as spec_caps
from neuxmind_spec import manifest as spec_manifest

from . import effects
from .errors import AdapterFault, BadRequest, NotAuthorized, NotFound
from .events import EventBus
from .hal import ActionHandle, ActionStatus, RobotAdapter, SUCCEEDED
from .safety import SafetySupervisor, is_motion


def manifest_fingerprint(manifest) -> str:
    """Stable digest of an adapter manifest, used to detect runtime changes."""
    import hashlib
    import json
    data = json.dumps(manifest, sort_keys=True, default=repr, allow_nan=True)
    return hashlib.sha256(data.encode()).hexdigest()


def _kind(cap_id: str) -> str:
    if cap_id.startswith("x."):
        return "command"
    return spec_caps.get(cap_id).kind


class ValidatedHandle(ActionHandle):
    """Wraps an adapter handle and validates the final outputs against the spec."""

    def __init__(self, cap_id: str, inner: ActionHandle):
        self.cap_id = cap_id
        self._inner = inner

    def poll(self) -> ActionStatus:
        try:
            status = self._inner.poll()
        except Exception as exc:
            return ActionStatus(state="failed", error=f"适配器 poll 异常：{exc}")
        if status.state == SUCCEEDED:
            try:
                spec_caps.validate_payload(self.cap_id, status.result or {}, "outputs")
            except ValueError as exc:
                return ActionStatus(state="failed", progress=status.progress,
                                    error=f"适配器输出不符合能力标准：{exc}")
        return status

    def cancel(self) -> None:
        try:
            self._inner.cancel()
        except Exception:
            pass


class CapabilityBus:
    def __init__(self, adapter: RobotAdapter, events: EventBus | None = None, native: dict | None = None):
        """``native``: runtime-provided capabilities ``{cap_id: provider}`` (e.g. ``model.install``
        from :class:`~neuxmind_runtime.models.ModelStore`, §5.1). A provider is used only when the
        adapter does not declare the capability itself -- an adapter's own implementation wins."""
        self.adapter = adapter
        self.clock = adapter.clock
        self.events = events or EventBus()
        raw = copy.deepcopy(adapter.manifest())
        self.manifest_fingerprint = manifest_fingerprint(raw)     # drift is checked on the adapter's own view
        declared = {c.get("id") for c in raw.get("capabilities", []) if isinstance(c, dict)}
        self.native = {cap: provider for cap, provider in (native or {}).items() if cap not in declared}
        if self.native:
            entries = [provider.capability_entry() for provider in self.native.values()]
            raw["capabilities"] = list(raw.get("capabilities", [])) + entries
            for entry in entries:
                if entry["id"] == "model.install":
                    # Contract (§5.1): top-level model_runtimes == model.install params.runtimes,
                    # so the cloud can filter edge packages by manifest.
                    raw["model_runtimes"] = list(entry["params"]["runtimes"])
        # Deep copies: the adapter's own objects are never shared (N-04).
        self.manifest = copy.deepcopy(spec_manifest.validate_manifest_v2(copy.deepcopy(raw)))
        self.capability_ids = {c["id"] for c in self.manifest["capabilities"]}
        arm = next((c for c in self.manifest["capabilities"] if c["id"] == "arm.joint_position"), None)
        # v1.1 (R4-01): declared arm joint names -> joint.state is mapped by name, never by order.
        self.arm_joint_names = list(arm["params"]["joint_names"]) if arm and "joint_names" in arm.get("params", {}) else None
        self.supervisor = SafetySupervisor(self.manifest, adapter, self.events, self.clock)

    # ----------------------------------------------------------- helpers
    def manifest_copy(self) -> dict:
        return copy.deepcopy(self.manifest)

    def check_manifest_drift(self) -> str | None:
        """Compare the adapter's current manifest with the one validated at start-up.

        On any difference (or an exception) motion is refused until restart.
        Returns the drift reason once, when it is first detected.
        """
        if self.supervisor.manifest_drift:
            return None
        try:
            current = manifest_fingerprint(self.adapter.manifest())
        except Exception as exc:
            reason = f"读取清单失败：{exc}"
        else:
            if current == self.manifest_fingerprint:
                return None
            reason = "manifest() 返回的内容与启动时校验的不同"
        self.supervisor.manifest_drift = reason
        self.supervisor.halt_motion("本体能力清单在运行中变化")
        self.events.emit("safety.manifest_changed", reason=reason)
        return reason

    def _require(self, cap_id: str) -> str:
        try:
            cap_id = spec_caps.normalize(cap_id)
        except ValueError as exc:
            raise NotFound(str(exc)) from None
        if cap_id not in self.capability_ids:
            raise NotFound(f"本体未提供能力 {cap_id}", code="capability_not_provided")
        return cap_id

    @staticmethod
    def _validate_inputs(cap_id: str, args: dict) -> None:
        try:
            spec_caps.validate_payload(cap_id, args, "inputs")
        except (ValueError, TypeError, OverflowError) as exc:
            raise BadRequest(f"{cap_id} 参数无效：{exc}") from None

    @staticmethod
    def _validate_outputs(cap_id: str, out) -> dict:
        try:
            return spec_caps.validate_payload(cap_id, out, "outputs")
        except ValueError as exc:
            raise AdapterFault(f"适配器输出不符合能力标准：{exc}") from None

    def kind(self, cap_id: str) -> str:
        return _kind(self._require(cap_id))

    @staticmethod
    def _require_system_authorization(cap_id: str, authorized: bool) -> None:
        """``safety: system`` (e.g. ``model.install``) changes on-device software: it needs
        ``hardware_authorized=true`` but moves nothing, so it is not motion-supervised."""
        if not cap_id.startswith("x.") and spec_caps.get(cap_id).safety == "system" and authorized is not True:
            raise NotAuthorized(f"{cap_id} 会变更端侧软件，需要 hardware_authorized=true（现场确认）")

    # -------------------------------------------------------------- read
    def read(self, cap_id: str) -> dict:
        cap_id = self._require(cap_id)
        if _kind(cap_id) not in ("state", "stream"):
            raise BadRequest(f"{cap_id} 不是 state/stream 能力，不能读取")
        try:
            out = self.adapter.read(cap_id)
        except Exception as exc:
            raise AdapterFault(f"读取 {cap_id} 失败：{exc}") from None
        return self._validate_outputs(cap_id, out)

    def readable(self) -> list[str]:
        return sorted(c for c in self.capability_ids if _kind(c) in ("state", "stream"))

    # ----------------------------------------------------------- command
    def _pre_dispatch(self, cap_id: str, epoch: int | None, guard: Callable[[], None] | None) -> None:
        """Last check before the adapter: stop epoch unchanged and the run still live."""
        if not is_motion(cap_id):
            return
        if guard is not None:
            guard()
        self.supervisor.ensure_epoch(epoch, cap_id)

    def invoke(self, cap_id: str, args: dict, authorized: bool, *, epoch: int | None = None,
               guard: Callable[[], None] | None = None) -> dict:
        """Execute a command capability. ``safety.stop`` is handled by the caller.

        ``epoch``: stop epoch recorded when the request/run was admitted; if
        omitted, the epoch at entry is used.
        """
        if epoch is None:
            epoch = self.supervisor.epoch
        cap_id = self._require(cap_id)
        if _kind(cap_id) != "command":
            raise BadRequest(f"{cap_id} 不是 command 能力")
        self._validate_inputs(cap_id, args)
        self._require_system_authorization(cap_id, authorized)
        limited, _notes = self.supervisor.check(cap_id, args, authorized, epoch=epoch)
        if cap_id == "base.velocity":
            # Serialise with the watchdog; the stop path never takes this lock.
            with self.supervisor.velocity_lock:
                out = self._dispatch_command(cap_id, limited, epoch, guard)
        else:
            out = self._dispatch_command(cap_id, limited, epoch, guard)
        return self._validate_outputs(cap_id, out)

    def _dispatch_command(self, cap_id: str, args: dict, epoch: int | None,
                          guard: Callable[[], None] | None) -> dict:
        self._pre_dispatch(cap_id, epoch, guard)
        effects.mark()
        try:
            out = self.adapter.invoke(cap_id, args)
        except Exception as exc:
            if is_motion(cap_id):
                self.supervisor.halt_motion(f"{cap_id} 适配器异常")
            raise AdapterFault(f"{cap_id} 执行失败：{exc}") from None
        self.supervisor.after_dispatch(cap_id, args, epoch=epoch)
        return out

    # ------------------------------------------------------------ action
    def chunk_static_error(self) -> tuple[str, str] | None:
        """``(code, message)`` if a ``policy.action_chunk`` can never be limited on this body, from the
        manifest alone (no adapter call); ``None`` otherwise. Used synchronously at submission
        (R4c-05) and again inside the run (R4b-03)."""
        params = self.supervisor.params.get("policy.action_chunk", {})
        if params.get("action_space") != "joint_position":
            return None
        if "joint.state" not in self.capability_ids:
            return "joint_state_required", "本体未声明 joint.state"
        if self.arm_joint_names is None:
            # R4b-03: fail closed. Without declared joint_names the order of joint.state is not
            # the command order (ROS /joint_states is often alphabetical): the per-step limiter
            # would start from the wrong joints and could be bypassed. Never assume it.
            return ("joint_names_required",
                    "清单未声明 arm.joint_position.params.joint_names（能力标准 v1.1），无法按关节名把 joint.state "
                    "对应到动作维度；请按机械臂指令顺序（ros2_control 的 joints:）声明 joint_names")
        return None

    def start_action(self, cap_id: str, args: dict, authorized: bool, *, epoch: int | None = None,
                     guard: Callable[[], None] | None = None, run_id: str | None = None) -> ValidatedHandle:
        """``run_id``: the run performing the action; runtime-native providers tag their events with it."""
        if epoch is None:
            epoch = self.supervisor.epoch
        cap_id = self._require(cap_id)
        if _kind(cap_id) != "action":
            raise BadRequest(f"{cap_id} 不是 action 能力")
        self._validate_inputs(cap_id, args)
        self._require_system_authorization(cap_id, authorized)
        if cap_id in self.native:
            # Runtime-provided, never motion: no supervisor limits apply and the
            # adapter is not involved. The provider's handle runs on its own thread.
            effects.mark()
            try:
                handle = self.native[cap_id].start_action(cap_id, args, run_id=run_id)
            except Exception as exc:
                raise AdapterFault(f"{cap_id} 启动失败：{exc}") from None
            return ValidatedHandle(cap_id, handle)
        context = {}
        if cap_id == "policy.action_chunk":
            # Fail closed (R-04): the supervisor rejects a joint_position chunk
            # without a successfully read joint.state.
            static = self.chunk_static_error()
            if static is not None:
                context["joint_state_error"] = static[1]
            else:
                try:
                    state = self.read("joint.state")
                    names = list(state.get("names") or [])
                    index = {n: i for i, n in enumerate(names)}
                    missing = [n for n in self.arm_joint_names if n not in index]
                    if len(index) != len(names):
                        context["joint_state_error"] = f"joint.state 关节名重复（{names}）"
                    elif missing:
                        context["joint_state_error"] = f"joint.state 缺少声明的机械臂关节 {missing}"
                    else:
                        context["joint_position"] = [state["position"][index[n]] for n in self.arm_joint_names]
                except Exception as exc:
                    context["joint_state_error"] = f"读取 joint.state 失败：{exc}"
        # The state read above may have blocked for a long time (stale topic,
        # serial timeout): the epoch check in check() and _pre_dispatch catch
        # a stop (+ reset) that happened meanwhile.
        limited, _notes = self.supervisor.check(cap_id, args, authorized, context, epoch=epoch)
        self._pre_dispatch(cap_id, epoch, guard)
        effects.mark()
        try:
            handle = self.adapter.start_action(cap_id, limited)
        except Exception as exc:
            if is_motion(cap_id):
                self.supervisor.halt_motion(f"{cap_id} 适配器异常")
            raise AdapterFault(f"{cap_id} 启动失败：{exc}") from None
        self.supervisor.after_dispatch(cap_id, limited, handle, epoch=epoch)
        return ValidatedHandle(cap_id, handle)
