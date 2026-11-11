"""Robot manifest ``robot.adapter.v2`` and skill package ``skill.package.v1``.

A *manifest* is what a robot body says about itself. A *skill package* is
what a portable skill needs. ``check_compatibility`` decides whether a skill
can run on a body, and is the single rule used by runtime, cloud, SDK and
desktop, so every surface gives the same answer.
"""
from __future__ import annotations

import math
import re
from typing import Any

from .capabilities import CAPABILITIES, FIELD_TYPES, MANDATORY, check_field, get, normalize

FAMILIES = ("manipulator", "mobile", "quadruped", "humanoid", "aerial", "composite", "custom")
_ID = re.compile(r"^[a-z0-9][a-z0-9_.\-]{0,99}$")
_SEMVER = re.compile(r"^\d+\.\d+\.\d+([\-+][0-9A-Za-z.\-]+)?$")


def _text(value: Any, label: str, max_len: int = 200) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_len:
        raise ValueError(f"{label} 需要 1–{max_len} 个字符")
    return value.strip()


# ---------------------------------------------------------------- manifest
def _check_joint_limits(cid: str, params: dict) -> None:
    """``lower``/``upper`` must both be given, match ``dof`` and satisfy lower < upper."""
    lower, upper = params.get("lower"), params.get("upper")
    if lower is None and upper is None:
        return
    if lower is None or upper is None:
        raise ValueError(f"{cid}.params 需要同时声明 lower 与 upper")
    dof = params.get("dof")
    if dof is None:
        raise ValueError(f"{cid}.params 声明关节限位时必须声明 dof")
    if len(lower) != dof or len(upper) != dof:
        raise ValueError(f"{cid}.params lower/upper 长度须等于 dof={dof}")
    if any(lo >= hi for lo, hi in zip(lower, upper)):
        raise ValueError(f"{cid}.params 每个关节须满足 lower < upper")


def _check_joint_names(cid: str, params: dict) -> None:
    """Optional ``joint_names`` (v1.1): unique non-empty strings, one per ``dof``."""
    if "joint_names" not in params:
        return
    names, dof = params["joint_names"], params.get("dof")
    if not isinstance(names, list) or not all(isinstance(n, str) and n.strip() and len(n) <= 200 for n in names):
        raise ValueError(f"{cid}.params.joint_names 须为非空字符串列表")
    if len(set(names)) != len(names):
        raise ValueError(f"{cid}.params.joint_names 不得重复")
    if dof is None or len(names) != dof:
        raise ValueError(f"{cid}.params.joint_names 长度须等于 dof={dof}")


# ------------------------------------------------- latched-state guard calibration (round 07)
# ``safety.latch_guard`` lets an integrator calibrate the runtime's latched-state guard per body
# (Q6-01/Q6-05). key: (default, lowest allowed, highest allowed, integer?). Values outside the
# bounds are clamped (never rejected: a calibration typo must not stop a robot from booting),
# anything that is not a finite number, or an unknown key, is refused (fail closed).
LATCH_GUARD_FIELDS: dict[str, tuple[float, float, float, bool]] = {
    "pos_m": (0.05, 0.01, 0.2, False),           # base displacement (m) that counts as motion
    "yaw_rad": (0.05, 0.01, 0.2, False),         # base yaw change (rad) that counts as motion
    "yaw_total_rad": (0.05, 0.05, 1.0, False),   # hard bound on base yaw from the latch snapshot and per step after a
                                                 # yaw re-stop (never re-based, Q7-01/Q7b-01); raise it for an IMU that drifts
    "joint_rad": (0.05, 0.01, 0.2, False),       # joint position change (rad) that counts as motion
    "noise_m": (0.0, 0.0, 0.01, False),          # per-sample position noise floor (1 sigma, m); 0 = measured only
    "noise_rad": (0.0, 0.0, 0.01, False),        # per-sample yaw / joint noise floor (1 sigma, rad); 0 = measured only
    "yaw_drift_rad_s": (0.0003, 0.0, 0.005, False),  # tolerated IMU yaw drift rate (rad/s; 0.0003 ~ 1 deg/min)
    "stop_settle_s": (1.0, 1.0, 5.0, False),     # robot seconds stop() takes to come to rest
    "escalate_after": (10, 3, 30, True),         # re-stops before "driver does not obey stop" (max 30, Q7-05)
}
LATCH_GUARD_ALIASES = {"settle_s": "stop_settle_s"}
NOISE_MARGIN = 6.0   # a motion threshold below NOISE_MARGIN x declared noise would alarm on noise


def normalize_latch_guard(value: Any, where: str = "safety.latch_guard") -> tuple[dict, list[str]]:
    """Validate and clamp a latch-guard calibration. Returns ``(values, notes)``: only the keys
    that were given (aliases resolved), clamped to :data:`LATCH_GUARD_FIELDS`; ``notes`` says
    what was clamped. Raises ``ValueError`` for a non-object, an unknown key, a value that is not
    a finite number (bool, NaN, inf, string ...), or a non-integer ``escalate_after``."""
    if not isinstance(value, dict):
        raise ValueError(f"{where} 须为对象")
    out: dict[str, float] = {}
    notes: list[str] = []
    for key, raw in value.items():
        name = LATCH_GUARD_ALIASES.get(key, key)
        if name not in LATCH_GUARD_FIELDS:
            raise ValueError(f"{where} 未定义参数：{key}（可用 {sorted(LATCH_GUARD_FIELDS)}）")
        default, lo, hi, integer = LATCH_GUARD_FIELDS[name]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
            raise ValueError(f"{where}.{key} 须为有限数值，收到 {raw!r}")
        if integer and raw != int(raw):
            raise ValueError(f"{where}.{key} 须为整数，收到 {raw!r}")
        if name in out and out[name] != raw:
            raise ValueError(f"{where} 同时给出 {name} 与别名 {key} 且取值不同")
        v = int(raw) if integer else float(raw)
        clamped = min(max(v, lo), hi)
        if clamped != v:
            notes.append(f"{where}.{key}={raw!r} 超出 [{lo}, {hi}]，按 {clamped} 执行")
        out[name] = clamped
    # A motion threshold must stay well above the declared noise, or the guard alarms on noise.
    for limit, noise in (("pos_m", "noise_m"), ("yaw_rad", "noise_rad"), ("joint_rad", "noise_rad")):
        if noise in out:
            current = out.get(limit, LATCH_GUARD_FIELDS[limit][0])
            floor = min(NOISE_MARGIN * out[noise], LATCH_GUARD_FIELDS[limit][2])
            if current < floor:
                out[limit] = round(floor, 6)
                notes.append(f"{where}.{limit}={current} 低于 {NOISE_MARGIN:g}×{noise}，按 {out[limit]} 执行")
    return out, notes


RESET_EXPIRE_CAP_S = 30.0     # the runtime voids any reset whose adapter.reset() takes longer than 30 s (Q7-10)
RESET_EXPECTED_MARGIN_S = 5.0  # the desktop waits expire_s = reset_expected_s + 5 (capped at 30)
# Q7c-07 (round 07 third fix pass): (0, 25] so expire_s = reset_expected_s + 5 always fits under the cap
RESET_EXPECTED_MAX_S = RESET_EXPIRE_CAP_S - RESET_EXPECTED_MARGIN_S


def normalize_reset_expected(value: Any, where: str = "safety.reset_expected_s") -> float:
    """``safety.reset_expected_s`` (round 07 second fix pass, Q7b-06): typical duration of
    ``adapter.reset()`` on this body (a legged stand-up ~10 s), so a client can wait long enough
    (``expire_s = max(8, reset_expected_s + margin)``). A positive finite number of seconds,
    at most :data:`RESET_EXPECTED_MAX_S` (25 s: the desktop adds a 5 s margin and the runtime voids
    any reset slower than 30 s, Q7c-07); anything else is refused (fail closed)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{where} 须为有限数值（秒），收到 {value!r}")
    if not 0 < value <= RESET_EXPECTED_MAX_S:
        raise ValueError(f"{where} 须在 (0, {RESET_EXPECTED_MAX_S:g}] 秒之内（桌面再留 5 s 余量，超过 30 s 的复位会被运行时作废），收到 {value!r}")
    return float(value)


def validate_manifest_v2(m: dict) -> dict:
    """Validate a v2 manifest and return a normalised copy.

    Shape::

        {"schema": "robot.adapter.v2", "spec_version": "1.0",
         "instance_id": "...", "name": "...", "vendor": "...", "model": "...",
         "family": "quadruped", "adapter_version": "0.1.0",
         "simulated": false,
         "capabilities": [{"id": "base.velocity", "major": 1,
                           "params": {"max_vx": 1.0, ...}}, ...],
         "model_runtimes": ["onnxruntime"],
         "safety": {"hardware_estop": true, "max_linear_speed": 1.5,
                    "latch_guard": {"pos_m": 0.05, "noise_m": 0.002, "stop_settle_s": 3}}}

    ``safety.latch_guard`` (optional, round 07) calibrates the runtime's latched-state guard; see
    :func:`normalize_latch_guard` (validated, clamped to :data:`LATCH_GUARD_FIELDS`).
    ``safety.reset_expected_s`` (optional, round 07 second fix pass): typical ``adapter.reset()``
    duration in seconds, ``(0, 30]``; see :func:`normalize_reset_expected`.
    """
    if not isinstance(m, dict) or m.get("schema") != "robot.adapter.v2":
        raise ValueError("需要 robot.adapter.v2 能力清单")
    out = {"schema": "robot.adapter.v2", "spec_version": str(m.get("spec_version", "1.0"))}
    for key in ("instance_id", "name", "adapter_version"):
        out[key] = _text(m.get(key), key)
    for key in ("vendor", "model"):
        out[key] = _text(m.get(key, "unknown"), key)
    if m.get("family") not in FAMILIES:
        raise ValueError(f"family 须为 {FAMILIES} 之一")
    out["family"] = m["family"]
    out["simulated"] = m.get("simulated") is True
    caps = m.get("capabilities")
    if not isinstance(caps, list) or not caps:
        raise ValueError("capabilities 须为非空列表")
    seen: dict[str, dict] = {}
    for c in caps:
        if not isinstance(c, dict):
            raise ValueError("每项能力须为对象 {id, major, params}")
        cid = normalize(_text(c.get("id"), "能力 ID", 100))
        if cid in seen:
            raise ValueError(f"能力重复声明：{cid}")
        major = c.get("major", 1)
        params = c.get("params", {})
        if not isinstance(params, dict):
            raise ValueError(f"{cid}.params 须为对象")
        if not cid.startswith("x."):
            spec = get(cid)
            if major != spec.major:
                raise ValueError(f"{cid} 主版本 {major} 与标准 v{spec.major} 不符")
            unknown = set(params) - {f.name for f in spec.params}
            if unknown:
                raise ValueError(f"{cid} 未定义参数：{sorted(unknown)}")
            # Limits feed the safety supervisor: a wrong sign or NaN here
            # would be amplified into dangerous motion, so fail closed.
            for f in spec.params:
                if f.name in params:
                    check_field(f, params[f.name], f"{cid}.params")
            _check_joint_limits(cid, params)
            if cid == "arm.joint_position":
                _check_joint_names(cid, params)
        seen[cid] = {"id": cid, "major": major, "params": params}
    missing = [x for x in MANDATORY if x not in seen]
    if missing:
        raise ValueError(f"缺少必备能力：{missing}")
    out["capabilities"] = list(seen.values())
    rts = m.get("model_runtimes", [])
    if not isinstance(rts, list) or any(not isinstance(r, str) for r in rts):
        raise ValueError("model_runtimes 须为字符串列表")
    out["model_runtimes"] = rts
    safety = m.get("safety", {})
    if not isinstance(safety, dict):
        raise ValueError("safety 须为对象")
    if "latch_guard" in safety:
        safety = dict(safety)
        safety["latch_guard"], _notes = normalize_latch_guard(safety["latch_guard"])
    if "reset_expected_s" in safety:
        safety = dict(safety)
        safety["reset_expected_s"] = normalize_reset_expected(safety["reset_expected_s"])
    out["safety"] = safety
    return out


def manifest_v1_view(m2: dict) -> dict:
    """Down-convert a v2 manifest into the legacy ``robot.adapter.v1`` shape
    still consumed by the desktop control plane (brainos/contracts.py)."""
    # ``simulated``, ``vendor`` and ``model`` are carried so that the desktop
    # manifest digest distinguishes a simulated body from real hardware.
    v1 = {"schema": "robot.adapter.v1", "instance_id": m2["instance_id"], "name": m2["name"],
          "adapter_version": m2["adapter_version"], "family": m2["family"],
          "capabilities": [c["id"] for c in m2["capabilities"]],
          "simulated": m2.get("simulated") is True,
          "vendor": m2.get("vendor", "unknown"), "model": m2.get("model", "unknown")}
    if m2.get("model_runtimes"):
        v1["model_runtimes"] = m2["model_runtimes"]
    return v1


def capability_ids(manifest: dict) -> set[str]:
    caps = manifest.get("capabilities", [])
    return {normalize(c["id"] if isinstance(c, dict) else c) for c in caps}


# ------------------------------------------------------------ skill package
def validate_skill_package(s: dict) -> dict:
    """Validate a portable skill package.

    Shape::

        {"schema": "skill.package.v1", "id": "patrol.waypoints", "version": "0.1.0",
         "title": "...", "description": "...",
         "entry": "skill.py:run",          # python entry inside the package
         "requires": [{"id": "base.navigate_to"}, {"id": "safety.stop"}],
         "optional": [{"id": "sensor.camera.rgb"}],
         "constraints": {"base.velocity": {"max_vx": {"min": 0.3}}},
         "parameters": [{"name": "waypoints", "type": "object", "required": true}],
         "models": [{"ref": "catalog:yolo-v8n", "role": "detector"}],
         "timeout_s": 600}
    """
    if not isinstance(s, dict) or s.get("schema") != "skill.package.v1":
        raise ValueError("需要 skill.package.v1 技能包")
    sid = _text(s.get("id"), "技能 ID", 100)
    if not _ID.match(sid):
        raise ValueError("技能 ID 只能包含小写字母、数字、点、下划线和连字符")
    version = _text(s.get("version"), "版本", 50)
    if not _SEMVER.match(version):
        raise ValueError("技能版本须为语义化版本，如 0.1.0")
    out = {"schema": "skill.package.v1", "id": sid, "version": version,
           "title": _text(s.get("title", sid), "标题", 100),
           "description": str(s.get("description", ""))[:2000],
           "entry": _text(s.get("entry"), "入口", 200)}

    def reqs(key: str) -> list[dict]:
        items = s.get(key, [])
        if not isinstance(items, list):
            raise ValueError(f"{key} 须为列表")
        norm = []
        for r in items:
            cid = normalize(r["id"] if isinstance(r, dict) else r)
            if not cid.startswith("x."):
                get(cid)
            norm.append({"id": cid, "major": (r.get("major", 1) if isinstance(r, dict) else 1)})
        return norm

    out["requires"] = reqs("requires")
    if not out["requires"]:
        raise ValueError("技能至少需要依赖一项能力")
    out["optional"] = reqs("optional")
    constraints = s.get("constraints", {})
    if not isinstance(constraints, dict):
        raise ValueError("constraints 须为对象")
    out["constraints"] = constraints
    params = s.get("parameters", [])
    if not isinstance(params, list):
        raise ValueError("parameters 须为列表")
    for p in params:
        if not isinstance(p, dict) or p.get("type") not in FIELD_TYPES:
            raise ValueError("每个参数需要 name 与合法 type")
        _text(p.get("name"), "参数名", 100)
    out["parameters"] = params
    models = s.get("models", [])
    if not isinstance(models, list):
        raise ValueError("models 须为列表")
    out["models"] = models
    timeout = s.get("timeout_s", 300)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 86400:
        raise ValueError("timeout_s 须在 0–86400 秒之间")
    out["timeout_s"] = timeout
    return out


def check_compatibility(skill: dict, manifest: dict) -> dict:
    """Return ``{"compatible": bool, "missing": [...], "violations": [...], "optional_missing": [...]}``.

    * missing: required capability IDs the body does not provide
    * violations: constraint failures, e.g. the skill needs max_vx >= 0.3
    """
    have = {c["id"]: c for c in manifest.get("capabilities", []) if isinstance(c, dict)}
    missing = [r["id"] for r in skill.get("requires", []) if r["id"] not in have]
    optional_missing = [r["id"] for r in skill.get("optional", []) if r["id"] not in have]
    violations = []
    for cid, rules in (skill.get("constraints") or {}).items():
        cap = have.get(cid)
        if not cap:
            continue
        for pname, rule in rules.items():
            value = cap.get("params", {}).get(pname)
            if value is None:
                violations.append(f"{cid}.{pname} 未声明")
                continue
            if isinstance(rule, dict):
                if "min" in rule and not (isinstance(value, (int, float)) and value >= rule["min"]):
                    violations.append(f"{cid}.{pname}={value} < {rule['min']}")
                if "max" in rule and not (isinstance(value, (int, float)) and value <= rule["max"]):
                    violations.append(f"{cid}.{pname}={value} > {rule['max']}")
                if "equals" in rule and value != rule["equals"]:
                    violations.append(f"{cid}.{pname}={value} ≠ {rule['equals']}")
    return {"compatible": not missing and not violations, "missing": missing,
            "violations": violations, "optional_missing": optional_missing}


def is_motion(cap_id: str) -> bool:
    """Vendor extensions (``x.*``) have unknown physical effect, so they are
    treated as motion: they require hardware authorization (fail closed)."""
    if cap_id.startswith("x."):
        return True
    return CAPABILITIES[cap_id].safety == "motion"
