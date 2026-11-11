"""Robot adapters: scaffold a package, run the conformance suite on it (INTERFACES_V1 §6.3).

Example::

    from neuxmind import adapters

    summary = adapters.new_adapter("acme_rover", "./acme_rover", family="mobile", vendor="Acme", model="R1",
                                   caps=["safety.stop", "system.health", "base.velocity"],
                                   impls=["safety.stop=ros2", "system.health=ros2", "base.velocity=ros2:/cmd_vel"],
                                   params=["base.velocity.max_vx=0.5", ...])
    report = adapters.test_adapter("./acme_rover", "dry")          # conformance.report.v1
    print(report["dry_ok"], report["field_acceptance"])            # ..., False (never acceptance)

Both run **in this process** with the edge runtime's own code
(``neuxmind_runtime.scaffold`` / ``neuxmind_runtime.conformance``), so the
results are exactly what ``neuxmind-runtime scaffold`` / ``conformance`` give.
The adapter package is imported and instantiated here: it is your own code
(it talks to your hardware), run it only where you would run the adapter.

Safety: ``static`` reads the manifest only; ``dry`` never moves the body (zero
setpoints, e-stop and stop-epoch checks, the runtime wraps the adapter in a
throw-away ``EdgeRuntime``); ``motion`` makes minimal real movements and
requires ``authorize=True`` -- checked here before the adapter is even loaded.
"""
from __future__ import annotations

import copy
import hashlib
import importlib
import inspect
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Callable, Iterable

from . import _bootstrap
from .errors import NeuxMindError

LEVELS = ("static", "dry", "motion")

# Text that ends up inside generated Python / TOML / Markdown (review Q4-09): one line, a small
# character set, bounded length. Anything else is refused before the scaffold runs — a robot's
# manifest is not trusted input (a tampered robot could otherwise plant code in the package).
_SAFE_TEXT = re.compile(r"^[0-9A-Za-z\u4e00-\u9fff][0-9A-Za-z\u4e00-\u9fff _.+,()/:\-]{0,99}$")
_SAFE_PARAM = re.compile(r"^[0-9A-Za-z_][0-9A-Za-z_./:\-]{0,127}$")
_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9_.\-]{0,99}$")


def _scaffold():
    _bootstrap.runtime()
    from neuxmind_runtime import scaffold
    return scaffold


def _conformance():
    _bootstrap.runtime()
    from neuxmind_runtime import conformance
    return conformance


# ------------------------------------------------------------------ new


def load_manifest_file(path: str | Path) -> dict:
    """A ``robot.adapter.v2`` manifest from a JSON file (the manifest itself, or ``{"manifest": ...}``
    as saved from ``nx robot caps --json`` / the cloud's robot record)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise NeuxMindError(f"读取 manifest 文件 {path} 失败：{exc}", code="bad_request") from None
    if isinstance(data, dict) and isinstance(data.get("manifest"), dict):
        data = data["manifest"]
    return data


def scaffold_input_from_manifest(manifest: dict, name: str, *, impls: Iterable[str] = (),
                                 params: Iterable[str] = (), family: str | None = None, vendor: str | None = None,
                                 model: str | None = None, instance_id: str | None = None,
                                 simulated: bool = False) -> dict:
    """``neuxmind.adapter_scaffold.v1`` input from an existing manifest: same capabilities and
    parameters (= safety limits), implementation ``todo`` unless ``impls`` (``cap=hint``) says
    otherwise. The new adapter gets its own identity: ``instance_id`` is not copied and it is
    ``simulated: false`` unless asked (a manifest of a simulated body still describes a body
    whose real counterpart you are adapting)."""
    spec_manifest = _bootstrap.spec().manifest
    try:
        m2 = spec_manifest.validate_manifest_v2(copy.deepcopy(manifest))
    except (ValueError, TypeError, AttributeError) as exc:
        raise NeuxMindError(f"manifest 无效（需要 robot.adapter.v2）：{exc}", code="invalid_manifest") from None
    hints = _pairs(impls, "--impl", "<能力>=<实现提示>")
    caps = []
    for cap in m2["capabilities"]:
        entry: dict[str, Any] = {"id": cap["id"], "params": copy.deepcopy(cap.get("params") or {})}
        entry["implementation"] = hints.pop(cap["id"], "todo")
        caps.append(entry)
    for cid, hint in hints.items():          # extra capabilities named only in --impl
        caps.append({"id": cid, "params": {}, "implementation": hint})
    out = {"name": name, "vendor": vendor or m2.get("vendor") or "", "model": model or m2.get("model") or "",
           "family": family or m2["family"], "simulated": bool(simulated), "capabilities": caps}
    if instance_id:
        out["instance_id"] = instance_id
    stop = next((c for c in m2["capabilities"] if c["id"] == "safety.stop"), None)
    if stop is not None and "hardware_estop" in (stop.get("params") or {}):
        out["safety"] = {"hardware_estop": bool(stop["params"]["hardware_estop"])}
    if params:                               # --param overrides / adds limits (cap.param=JSON)
        try:
            extra = _scaffold().spec_from_args(name, "v", "m", out["family"], [], list(params), [])["capabilities"]
        except ValueError as exc:
            raise NeuxMindError(str(exc), code="bad_request") from None
        by_id = {c["id"]: c for c in caps}
        for item in extra:
            if item["id"] not in by_id:
                by_id[item["id"]] = {"id": item["id"], "params": {}, "implementation": "todo"}
                caps.append(by_id[item["id"]])
            by_id[item["id"]]["params"].update(item["params"])
    return out


def _unsafe(label: str, value: Any, source: str | None, rule: str) -> NeuxMindError:
    shown = repr(value)
    shown = shown if len(shown) <= 80 else shown[:77] + "…"
    origin = f"（来自{source}，可能被篡改）" if source else ""
    return NeuxMindError(f"{label} 的值 {shown}{origin} 不安全，已拒绝生成适配器包：{rule}。"
                         "请改用 --vendor/--model 等参数显式给出，或核对机器人上的 manifest",
                         code="unsafe_scaffold_input", details={"field": label, "hint": rule})


def check_scaffold_input(spec: dict, source: str | None = None) -> None:
    """Refuse scaffold input whose texts could break out of the generated code (defence in depth;
    the runtime scaffold validates too). ``source`` names where the values came from."""
    for key in ("vendor", "model"):
        value = spec.get(key)
        if not isinstance(value, str) or not _SAFE_TEXT.match(value):
            raise _unsafe(key, value, source, "须为 1–100 个字符的单行文本，只允许字母、数字、汉字、空格与 _ . + , ( ) / : -")
    if spec.get("instance_id") is not None and not (isinstance(spec["instance_id"], str)
                                                    and _SAFE_ID.match(spec["instance_id"])):
        raise _unsafe("instance_id", spec["instance_id"], source, "须为小写字母、数字与 _ . -（≤100）")
    for key in ("display_name", "adapter_version", "class_name", "ros2"):
        if key in spec:
            raise _unsafe(key, spec[key], source, "SDK 不传该字段（由脚手架按默认值生成）")

    def walk(value: Any, where: str) -> None:
        if isinstance(value, str):
            if not _SAFE_PARAM.match(value):
                raise _unsafe(where, value, source, "参数中的文本须为 1–128 个字母、数字或 _ . / : -")
        elif isinstance(value, list):
            for i, item in enumerate(value):
                walk(item, f"{where}[{i}]")
        elif isinstance(value, dict):
            raise _unsafe(where, value, source, "参数值不能是对象")
        elif value is not None and not isinstance(value, (bool, int, float)):
            raise _unsafe(where, value, source, "参数值须为数字、布尔、文本或列表")

    for cap in spec.get("capabilities") or []:
        for pname, value in (cap.get("params") or {}).items():
            if not isinstance(pname, str) or not _SAFE_ID.match(pname.lower()):
                raise _unsafe(f"{cap.get('id')}.params", pname, source, "参数名无效")
            walk(value, f"{cap.get('id')}.params.{pname}")


def manifest_digest(manifest: dict) -> str:
    """The digest the runtime puts in reports (``manifest_digest``) and the cloud stores for fleet
    records: SHA-256 of the validated manifest as sorted JSON (contract §6.1)."""
    m2 = _bootstrap.spec().manifest.validate_manifest_v2(copy.deepcopy(manifest))
    return hashlib.sha256(json.dumps(m2, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _pairs(items: Iterable[str], flag: str, form: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in items or ():
        key, sep, value = str(raw).partition("=")
        if not sep or not key.strip():
            raise NeuxMindError(f"{flag} 须为 {form}：{raw!r}", code="bad_request")
        out[key.strip()] = value.strip()
    return out


def new_adapter(name: str, out_dir: str | Path, *, caps: Iterable[str] = (), manifest: dict | None = None,
                family: str | None = None, vendor: str | None = None, model: str | None = None,
                impls: Iterable[str] = (), params: Iterable[str] = (), instance_id: str | None = None,
                simulated: bool = False, force: bool = False, source: str | None = None) -> dict:
    """Generate an adapter package with the runtime scaffold → its summary
    (``path, module, class_name, adapter, implemented, todo, mandatory_todo, files``) plus ``input``
    (the ``neuxmind.adapter_scaffold.v1`` document that was used).

    Either ``manifest`` (an existing ``robot.adapter.v2``: capabilities and limits are copied) or
    ``caps`` (capability IDs; ``params`` as ``cap.param=JSON``, required limits must be given —
    the scaffold never guesses them). ``impls``: ``cap=hint`` with hints ``ros2[:topic]``,
    ``sdk[:name]``, ``serial[:dev]``, ``http[:url]``, ``todo``. Raises ``scaffold_invalid``,
    ``unsafe_scaffold_input`` (texts from ``source`` — e.g. a robot's manifest — that could
    break out of the generated code are refused before anything is written) or ``scaffold_failed``
    (the output directory is removed again if this call created it).
    """
    scaffold = _scaffold()
    caps = [c for raw in caps or () for c in str(raw).split(",") if c.strip()]
    if (manifest is None) == (not caps):
        raise NeuxMindError("需要 --from-manifest 或 --caps 之一（不能同时给）", code="bad_request")
    try:
        if manifest is not None:
            spec = scaffold_input_from_manifest(manifest, name, impls=impls, params=params, family=family,
                                                vendor=vendor, model=model, instance_id=instance_id,
                                                simulated=simulated)
        else:
            spec = scaffold.spec_from_args(name, vendor or "", model or "", family or "custom", caps, list(params),
                                           list(impls), instance_id=instance_id,
                                           simulated=True if simulated else None)
        if not spec["vendor"]:
            spec["vendor"] = "unknown"
        if not spec["model"]:
            spec["model"] = name
    except scaffold.ScaffoldError as exc:
        raise NeuxMindError(str(exc), code="scaffold_invalid") from None
    check_scaffold_input(spec, source)
    target = Path(out_dir).expanduser().resolve()
    created = not target.exists() or (target.is_dir() and not any(target.iterdir()))
    try:
        summary = scaffold.generate(spec, target, force=force)
    except scaffold.ScaffoldError as exc:
        raise NeuxMindError(str(exc), code="scaffold_invalid") from None
    except Exception as exc:                 # e.g. SyntaxError from generated code: never leave it on disk
        if created and target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        code = "scaffold_invalid" if isinstance(exc, OSError) else "scaffold_failed"
        raise NeuxMindError(f"生成适配器包失败（{type(exc).__name__}: {exc}）；输出目录已清理"
                            if created else f"生成适配器包失败（{type(exc).__name__}: {exc}）", code=code) from None
    summary["input"] = spec
    return summary


# ------------------------------------------------------------------ test


def find_package(path: str | Path) -> tuple[Path, str]:
    """``(root, module)`` of an adapter package: ``path`` is the package root (the directory with
    ``pyproject.toml`` and ``<module>/adapter.py``, as generated) or the module directory itself."""
    p = Path(path).expanduser().resolve()
    if not p.is_dir():
        raise NeuxMindError(f"找不到适配器包目录 {path}", code="adapter_not_found")
    if (p / "adapter.py").is_file() and (p / "__init__.py").is_file():
        return p.parent, p.name
    found = sorted(d.name for d in p.iterdir()
                   if d.is_dir() and (d / "adapter.py").is_file() and (d / "__init__.py").is_file())
    if len(found) == 1:
        return p, found[0]
    if not found:
        raise NeuxMindError(f"{path} 不是适配器包：没有 <模块>/adapter.py（用 nx adapter new 生成）",
                            code="adapter_not_found")
    raise NeuxMindError(f"{path} 下有多个适配器模块 {found}：请直接指定模块目录", code="adapter_ambiguous")


def _purge(module: str) -> None:
    for name in list(sys.modules):
        if name == module or name.startswith(module + "."):
            del sys.modules[name]


def _import_error(exc: BaseException) -> NeuxMindError:
    missing = getattr(exc, "name", None) or ""
    if missing.split(".")[0] in ("rclpy", "geometry_msgs", "nav_msgs", "sensor_msgs", "nav2_msgs", "action_msgs"):
        return NeuxMindError(f"适配器依赖 ROS 2（缺少 {missing}）：请在 ROS 2 环境中运行"
                             "（source /opt/ros/<发行版>/setup.bash）", code="ros2_unavailable")
    return NeuxMindError(f"加载适配器失败：{type(exc).__name__}: {exc}", code="adapter_load_failed")


def load_adapter(ref: str | Path, *, time_scale: float = 1.0):
    """Instantiate an adapter → ``(adapter, description)``.

    ``ref``: a package directory (see :func:`find_package`), a built-in name (``sim_quadruped``,
    ``sim_arm``, ``ros2``) or ``module:Class`` (importable). For a directory the package is
    imported from it (a stale copy with the same module name is dropped from ``sys.modules`` first).
    """
    _bootstrap.runtime()
    from neuxmind_runtime.adapters import BUILTIN, load_adapter as runtime_load
    from neuxmind_runtime.hal import RobotAdapter

    text = str(ref)
    if Path(text).expanduser().is_dir():
        root, module = find_package(text)
        _purge(module)
        sys.path.insert(0, str(root))
        try:
            mod = importlib.import_module(module + ".adapter")
            classes = [obj for _, obj in inspect.getmembers(mod, inspect.isclass)
                       if issubclass(obj, RobotAdapter) and obj is not RobotAdapter and obj.__module__ == mod.__name__]
            if len(classes) != 1:
                raise NeuxMindError(f"{module}/adapter.py 中须恰好有一个 RobotAdapter 子类，找到 "
                                    f"{[c.__name__ for c in classes]}", code="adapter_load_failed")
            cls = classes[0]
            kwargs = {"time_scale": time_scale} if "time_scale" in inspect.signature(cls).parameters else {}
            if time_scale != 1.0 and not kwargs:
                raise NeuxMindError(f"{cls.__name__} 不支持 --time-scale（只有仿真本体可以加速）", code="bad_request")
            try:
                adapter = cls(**kwargs)
            except NotImplementedError as exc:
                raise NeuxMindError(f"适配器 {cls.__name__} 还不能加载：{exc}", code="adapter_incomplete") from None
            return adapter, {"kind": "package", "path": str(root), "module": module,
                             "adapter": f"{module}.adapter:{cls.__name__}"}
        except NeuxMindError:
            raise
        except ImportError as exc:
            raise _import_error(exc) from None
        except Exception as exc:                 # the adapter's own constructor failed
            code = "ros2_unavailable" if "rclpy" in str(exc) else "adapter_load_failed"
            raise NeuxMindError(f"创建适配器失败：{type(exc).__name__}: {exc}", code=code) from None
        finally:
            while str(root) in sys.path:
                sys.path.remove(str(root))
    if text in BUILTIN or ":" in text:
        try:
            return runtime_load(text, time_scale=time_scale), {"kind": "builtin" if text in BUILTIN else "module",
                                                               "adapter": text}
        except NotImplementedError as exc:
            raise NeuxMindError(f"适配器 {text} 还不能加载：{exc}", code="adapter_incomplete") from None
        except ImportError as exc:
            raise _import_error(exc) from None
        except (ValueError, AttributeError) as exc:
            raise NeuxMindError(str(exc), code="adapter_load_failed") from None
    raise NeuxMindError(f"找不到适配器 {text}：需要包目录、内置名称（{', '.join(sorted(BUILTIN))}）或 module:Class",
                        code="adapter_not_found")


def check_level(level: str, authorize: bool) -> None:
    if level not in LEVELS:
        raise NeuxMindError(f"一致性测试级别须为 {' / '.join(LEVELS)}，收到 {level!r}", code="invalid_level")
    if level == "motion" and authorize is not True:
        raise NeuxMindError("motion 级别会让机器人做最小幅度的真实动作（约 0.1 m / 0.05 rad / 10% 夹爪开度），"
                            "必须现场确认安全（人员远离、急停在手）后加 --authorize（Python：authorize=True）。"
                            "本次没有加载适配器、没有发送任何指令。", code="authorization_required")


def test_adapter(ref: str | Path, level: str = "static", *, authorize: bool = False, time_scale: float = 1.0,
                 on_progress: Callable[[float], None] | None = None) -> dict:
    """Run the conformance suite on a local adapter (in this process) → ``conformance.report.v1``
    with an extra ``"adapter"`` entry describing what was loaded. ``static`` only reads the
    manifest (without instantiating hardware-backed code when the package has
    ``offline_manifest()``). The report is never acceptance evidence (``field_acceptance: false``)."""
    check_level(level, authorize)
    conformance = _conformance()
    if level == "static" and Path(str(ref)).expanduser().is_dir():
        report = _static_offline(ref)
        if report is not None:
            return report
    adapter, described = load_adapter(ref, time_scale=time_scale)
    try:
        report = conformance.run_adapter(adapter, level, authorize is True, progress=on_progress)
    except NeuxMindError:
        raise
    except Exception as exc:
        code = getattr(exc, "code", None) or "conformance_error"
        raise NeuxMindError(f"一致性测试未能执行：{exc}", code=code) from None
    report["adapter"] = described
    return report


def _static_offline(ref) -> dict | None:
    """Static level of a generated package through ``offline_manifest()`` (no hardware, no ROS 2)."""
    root, module = find_package(ref)
    _purge(module)
    sys.path.insert(0, str(root))
    try:
        try:
            mod = importlib.import_module(module + ".adapter")
        except ImportError as exc:
            raise _import_error(exc) from None
        _bootstrap.runtime()
        from neuxmind_runtime.hal import RobotAdapter
        classes = [obj for _, obj in inspect.getmembers(mod, inspect.isclass)
                   if issubclass(obj, RobotAdapter) and obj is not RobotAdapter and obj.__module__ == mod.__name__]
        if len(classes) != 1 or not callable(getattr(classes[0], "offline_manifest", None)):
            return None
        cls = classes[0]
        report = _conformance().check_manifest(cls.offline_manifest)
        report["adapter"] = {"kind": "package", "path": str(root), "module": module,
                             "adapter": f"{module}.adapter:{cls.__name__}", "offline_manifest": True}
        return report
    finally:
        while str(root) in sys.path:
            sys.path.remove(str(root))


# ------------------------------------------------------------------ text


def level_lines(report: dict) -> list[str]:
    """Human summary of each requested level (shared by ``nx adapter test`` and ``nx robot conformance``)."""
    names = {"static": "static（清单）", "dry": "dry（不运动）", "motion": "motion（最小动作）"}
    lines = []
    for level in LEVELS:
        info = (report.get("levels") or {}).get(level) or {}
        if not info.get("requested"):
            continue
        verdict = "通过" if info.get("ok") else "未通过"
        extra = f"，受阻 {info.get('blocked')}" if info.get("blocked") else ""
        lines.append(f"  {names[level]}：{verdict}（通过 {info.get('passed', 0)}，失败 {info.get('failed', 0)}，"
                     f"跳过 {info.get('skipped', 0)}{extra}）")
        for reason in info.get("blocked_reasons") or []:
            lines.append(f"      受阻原因：{reason}")
    return lines


STATUS_TEXT = {"passed": "通过", "failed": "失败", "skipped": "跳过"}


def capability_rows(report: dict, *, declared_only: bool = True) -> list[list[str]]:
    """Rows ``[能力, 名称, static, dry, motion, 说明]`` for each (declared) capability."""
    rows = []
    for cap in report.get("capabilities") or []:
        if declared_only and not cap.get("declared"):
            continue
        cells, notes = [], []
        for level in LEVELS:
            verdict = (cap.get("levels") or {}).get(level)
            if verdict is None:
                cells.append("-")
                continue
            status = verdict.get("status")
            text = STATUS_TEXT.get(status, str(status))
            if status == "skipped":
                text += "（受阻）" if verdict.get("skip_kind") == "blocked" else "（不适用）"
            cells.append(text)
            if status == "failed" or verdict.get("skip_kind") == "blocked":
                notes.append(f"{level}: {verdict.get('reason')}")
        rows.append([cap.get("id"), cap.get("title") or "", *cells, "；".join(notes)[:160]])
    return rows


def undeclared(report: dict) -> list[str]:
    return [c["id"] for c in report.get("capabilities") or [] if not c.get("declared")]
