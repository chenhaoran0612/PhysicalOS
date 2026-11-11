"""Command line: ``python3 -m neuxmind_runtime serve|check|conformance|scaffold ...``."""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from pathlib import Path

from neuxmind_spec import manifest as spec_manifest

from .adapters import load_adapter
from .core import EdgeRuntime
from .models import ModelConfig
from .latch_guard import parse_overrides as parse_guard_overrides
from .gateway import MAX_CONNECTIONS, STOP_RESERVED, GatewayServer, check_token
from .sandbox import INPROCESS, MODES, PROCESS, SandboxConfig
from .skills import load_package_dir


def _serve(args) -> int:
    try:
        token = check_token(os.environ.get("BRAIN_EDGE_TOKEN", ""))
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    try:
        sandbox = SandboxConfig(mode=args.sandbox, user=args.sandbox_user or "auto",
                                memory_bytes=args.skill_memory_mb << 20, python=args.sandbox_python)
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    models = None
    if args.models_dir is not None:
        try:
            models = ModelConfig(models_dir=str(args.models_dir),
                                 runtimes=tuple(r.strip() for r in args.model_runtimes.split(",") if r.strip()),
                                 max_bytes=args.model_max_mb << 20, timeout_s=args.model_timeout,
                                 file_roots=tuple(str(p) for p in args.model_file_root),
                                 ca_file=args.model_ca_file)
        except ValueError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return 2
    try:
        guard_overrides = parse_guard_overrides(args.latch_guard)
        if guard_overrides:
            spec_manifest.normalize_latch_guard(guard_overrides, "--latch-guard")   # fail before touching the body
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    adapter = load_adapter(args.adapter, time_scale=args.time_scale)
    runtime = EdgeRuntime(adapter, args.state_dir, sandbox=sandbox, models=models,
                          telemetry_capacity=args.telemetry_capacity, latch_guard_overrides=guard_overrides or None)
    server = GatewayServer(runtime, token, args.host, args.port, max_connections=args.max_connections,
                           stop_reserved=args.stop_reserved)

    def _terminate(_signum, _frame):
        raise KeyboardInterrupt      # SIGTERM = clean shutdown (not an "abnormal shutdown" latch)

    try:
        signal.signal(signal.SIGTERM, _terminate)
    except ValueError:               # not in the main thread
        pass
    manifest = runtime.manifest
    tag = "（仿真本体 simulated=true）" if manifest["simulated"] else ""
    latched = "；急停已锁存：" + str(runtime.supervisor.latch_reason) if runtime.supervisor.latched else ""
    print(f"NeuxMind runtime {manifest['name']}{tag} 监听 {server.url}", flush=True)   # URL stays last
    print(f"连接上限 {args.max_connections}，另保留 {args.stop_reserved} 个停止专用名额{latched}", flush=True)
    guard = runtime.latch_guard
    if guard is not None and guard.sources:
        cfg = guard.describe()
        src = {k: v for k, v in cfg["config_source"].items() if v != "default"}
        print("锁存守护标定：" + json.dumps(cfg["config"], ensure_ascii=False)
              + ("（来源 " + json.dumps(src, ensure_ascii=False) + "）" if src else "（默认值）"), flush=True)
        for note in cfg["config_notes"]:
            print("警告：" + note, flush=True)
        if cfg.get("relaxed"):      # Q7-05: looser than the default -- also event safety.latch_guard_relaxed
            print("警告：锁存守护标定比默认宽松：" + "，".join(
                f"{v['key']}={v['value']}（默认 {v['default']}，来源 {v['source']}）" for v in cfg["relaxed_keys"])
                + "。急停锁存期间本体要移动更多才会被发现；请确认取值来自现场实测并保留硬件急停", flush=True)
    box = runtime.sandboxes.describe()
    if box["mode"] == INPROCESS:
        print("警告：技能在运行时进程内执行（--sandbox inprocess，仅供调试）；技能可直接访问适配器", flush=True)
    else:
        print(f"技能沙箱：独立进程，用户 {box['user']}，级别 {box['level']}；网络未隔离（见 README 信任边界）",
              flush=True)
        if not box["privilege_drop"]:
            print("警告：运行时不是 root，技能与运行时同一用户（尽力而为模式）：技能可读写状态目录；"
                  "生产部署请以 root 启动（自动降权为 neuxmind-skill/nobody，或用 --sandbox-user 指定），见 README §0.2。", flush=True)
        if not box["pids_cgroup"]:
            print("警告：无可写 pids cgroup，进程树收回为尽力而为（脱离进程组的后代在运行结束后由孤儿收割者清理，"
                  "存在短暂窗口）；真机部署建议提供 cgroup（systemd Delegate=yes），见 README。", flush=True)
        if box.get("python_note"):
            print("警告：" + box["python_note"], flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        # Stop motion and runs on shutdown without latching the next boot.
        runtime.runner.stop_all("runtime shutdown")
        runtime.supervisor.halt_motion("runtime shutdown")
        server.close()
        runtime.close()
    return 0


def _check(args) -> int:
    package = load_package_dir(args.skill_dir)
    meta = {k: v for k, v in package.items() if k != "files"}
    skill = spec_manifest.validate_skill_package(meta)
    adapter = load_adapter(args.adapter)
    manifest = spec_manifest.validate_manifest_v2(adapter.manifest())
    result = spec_manifest.check_compatibility(skill, manifest)
    print(json.dumps({"skill": f"{skill['id']}@{skill['version']}", "robot": manifest["name"], **result},
                     ensure_ascii=False, indent=2))
    return 0 if result["compatible"] else 1


def _write_report(report: dict, out: Path | None) -> None:
    text = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    if out is None:
        print(text)
    else:
        out.write_text(text + "\n", encoding="utf-8")


def _conformance_server(url: str, token: str, level: str, authorized: bool, timeout: float) -> dict:
    """``--mode server``: the runtime runs the suite on itself (POST /v2/conformance) and we poll."""
    import time
    import uuid
    from .conformance import CallError, HttpTarget
    client = HttpTarget(url, token)
    started = client.request("POST", "/v2/conformance", {"request_id": "cli-" + uuid.uuid4().hex, "level": level,
                                                          "hardware_authorized": authorized})
    cid = started["conformance_id"]
    deadline = time.monotonic() + timeout
    while True:
        record = client.request("GET", "/v2/conformance/" + cid)
        if record["status"] != "running":
            break
        if time.monotonic() > deadline:
            raise CallError(f"一致性测试 {cid} 在 {timeout:g} s 内未结束（仍在运行时上执行）", "timeout")
        time.sleep(0.2)
    if record["status"] != "completed":
        error = record.get("error") or {}
        raise CallError(error.get("error") or f"一致性测试状态 {record['status']}", error.get("code") or record["status"])
    return record["report"]


def _conformance(args) -> int:
    from . import conformance
    from .errors import RuntimeApiError
    if args.level == "motion" and not args.authorize:
        print("错误：motion 级别会让机器人运动，必须现场确认后加 --authorize（hardware_authorized=true）", file=sys.stderr)
        return 2
    if bool(args.adapter) == bool(args.url):
        print("错误：--adapter 与 --url 二选一", file=sys.stderr)
        return 2
    try:
        if args.url:
            token = args.token or os.environ.get("BRAIN_EDGE_TOKEN", "")
            if not token:
                print("错误：--url 需要 --token 或环境变量 BRAIN_EDGE_TOKEN", file=sys.stderr)
                return 2
            if args.mode == "server":
                report = _conformance_server(args.url, token, args.level, args.authorize, args.timeout)
            else:
                report = conformance.run(conformance.HttpTarget(args.url, token), args.level, args.authorize)
        else:
            adapter = load_adapter(args.adapter, time_scale=args.time_scale)
            report = conformance.run_adapter(adapter, args.level, args.authorize)
    except (conformance.CallError, RuntimeApiError, ValueError, RuntimeError, ImportError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    _write_report(report, args.out)
    print(conformance.summarize(report), file=sys.stderr)
    return 0 if report[f"{args.level}_ok"] else 1


def _scaffold(args) -> int:
    from . import scaffold
    try:
        if args.spec is not None:
            spec = json.loads(args.spec.read_text(encoding="utf-8"))
        else:
            missing = [f"--{k}" for k in ("name", "vendor", "model", "family") if not getattr(args, k)]
            if missing or not (args.cap or args.impl or args.param):
                print(f"错误：需要 --spec 文件，或 {', '.join(missing) or ''} 与 --cap/--impl/--param", file=sys.stderr)
                return 2
            spec = scaffold.spec_from_args(args.name, args.vendor, args.model, args.family, args.cap, args.param,
                                           args.impl, instance_id=args.instance_id,
                                           simulated=True if args.simulated else None)
        summary = scaffold.generate(spec, args.out, force=args.force)
    except (scaffold.ScaffoldError, ValueError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if summary["mandatory_todo"]:
        print(f"注意：必备能力 {summary['mandatory_todo']} 尚未实现，适配器在实现前不能被运行时加载", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neuxmind-runtime", description="NeuxMind Robot OS 端侧运行时")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="启动端侧运行时网关（token 读取 BRAIN_EDGE_TOKEN）")
    serve.add_argument("--adapter", required=True, help="sim_quadruped | sim_arm | ros2 | module:Class")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8790)
    serve.add_argument("--state-dir", type=Path, default=Path(".nx-runtime-state"))
    serve.add_argument("--time-scale", type=float, default=1.0, help="仅仿真本体：仿真时间倍速")
    serve.add_argument("--max-connections", type=int, default=MAX_CONNECTIONS,
                       help="普通并发连接上限；超出立即 503")
    serve.add_argument("--stop-reserved", type=int, default=STOP_RESERVED,
                       help="额外保留给 /v1/stop、/v2/stop 的连接名额")
    serve.add_argument("--sandbox", choices=MODES, default=PROCESS,
                       help="技能隔离：process（默认，独立子进程）| inprocess（仅调试，技能可直接访问适配器）")
    serve.add_argument("--sandbox-user", default=None,
                       help="技能子进程的用户（以 root 运行时默认 neuxmind-skill，否则 nobody；非 root 时与运行时同用户）")
    serve.add_argument("--skill-memory-mb", type=int, default=1024, help="技能子进程地址空间上限（RLIMIT_AS）")
    serve.add_argument("--sandbox-python", default=None,
                       help="技能子进程使用的 Python 解释器（须对技能用户可执行；默认运行时自身的解释器）")
    serve.add_argument("--models-dir", type=Path, default=None,
                       help="启用运行时原生 model.install，模型安装到该目录（技能只读可见，须对技能用户可穿越）")
    serve.add_argument("--model-runtimes", default="onnxruntime", help="本机支持的推理运行时，逗号分隔")
    serve.add_argument("--model-max-mb", type=int, default=2048, help="模型包大小上限（MiB）")
    serve.add_argument("--model-timeout", type=float, default=30.0, help="下载连接/单次读取超时（秒）")
    serve.add_argument("--model-file-root", type=Path, action="append", default=[],
                       help="允许 file:// 模型包所在的目录（可重复）；未配置时拒绝 file://")
    serve.add_argument("--model-ca-file", default=None, help="https 下载额外信任的 CA 证书（私有镜像）")
    serve.add_argument("--telemetry-capacity", type=int, default=100_000,
                       help="持久化遥测环形存储保留的事件数（SQLite，跨重启 seq 连续）")
    serve.add_argument("--latch-guard", action="append", default=[], metavar="KEY=VALUE",
                       help="现场标定锁存守护（可重复，或一个 JSON 对象）：pos_m, yaw_rad, joint_rad, noise_m, noise_rad, "
                            "yaw_total_rad, yaw_drift_rad_s, stop_settle_s, escalate_after；覆盖清单 safety.latch_guard，"
                            "取值按同一范围校验并限幅（见 README §2 锁存守护标定）")
    serve.set_defaults(func=_serve)

    check = sub.add_parser("check", help="离线检查技能包与本体是否兼容")
    check.add_argument("skill_dir", type=Path)
    check.add_argument("--adapter", required=True)
    check.set_defaults(func=_check)

    conf = sub.add_parser("conformance", help="能力一致性测试（static | dry | motion），输出 conformance.report.v1")
    conf.add_argument("--adapter", help="sim_quadruped | sim_arm | ros2 | module:Class（包一层临时运行时测试）")
    conf.add_argument("--url", help="运行中运行时的地址（与 --adapter 二选一）")
    conf.add_argument("--token", help="运行时令牌（默认读 BRAIN_EDGE_TOKEN）")
    conf.add_argument("--mode", choices=("server", "client"), default="server",
                      help="--url 时：server=运行时在进程内自测（POST /v2/conformance，完整）；"
                           "client=本机通过公开 API 黑盒测试（停止代数项无法测试）")
    conf.add_argument("--level", choices=("static", "dry", "motion"), default="static")
    conf.add_argument("--authorize", action="store_true",
                      help="hardware_authorized=true：motion 级别必需（现场确认机器人周围安全）")
    conf.add_argument("--time-scale", type=float, default=1.0, help="仅仿真本体")
    conf.add_argument("--timeout", type=float, default=600.0, help="--mode server 时等待报告的上限（秒）")
    conf.add_argument("--out", type=Path, default=None, help="报告 JSON 输出路径（默认标准输出）")
    conf.set_defaults(func=_conformance)

    scaf = sub.add_parser("scaffold", help="由能力映射生成适配器包（adapter.py + adapter.json + tests + README）")
    scaf.add_argument("--out", type=Path, required=True, help="输出目录（包根目录）")
    scaf.add_argument("--spec", type=Path, default=None, help="neuxmind.adapter_scaffold.v1 输入 JSON")
    scaf.add_argument("--name", help="Python 包名（小写下划线）")
    scaf.add_argument("--vendor")
    scaf.add_argument("--model")
    scaf.add_argument("--family", choices=spec_manifest.FAMILIES)
    scaf.add_argument("--instance-id", default=None)
    scaf.add_argument("--simulated", action="store_true", help="本体是仿真器")
    scaf.add_argument("--cap", action="append", default=[], help="能力 ID（可重复或逗号分隔）")
    scaf.add_argument("--param", action="append", default=[], help="<能力>.<参数>=<JSON 值>，如 base.velocity.max_vx=0.8")
    scaf.add_argument("--impl", action="append", default=[], help="<能力>=<实现提示>，如 base.velocity=ros2:/cmd_vel")
    scaf.add_argument("--force", action="store_true", help="允许写入非空目录")
    scaf.set_defaults(func=_scaffold)

    args = parser.parse_args(argv)
    return args.func(args)
