"""``nx`` — the NeuxMind command line.

Every command prints friendly Chinese text, or one JSON document with
``--json``. Exit codes: 0 success, 1 failure (error, incompatible, run not
succeeded), 2 usage error.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import unicodedata
from pathlib import Path
from typing import Any, Callable

from . import __version__, config
from .errors import NeuxMindError

# ------------------------------------------------------------------ output helpers


def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _width(text))


def table(headers: list[str], rows: list[list[Any]]) -> str:
    cells = [[str(c) for c in headers]] + [["" if c is None else str(c) for c in row] for row in rows]
    widths = [max(_width(row[i]) for row in cells) for i in range(len(headers))]
    lines = ["  ".join(_pad(c, widths[i]) for i, c in enumerate(cells[0])).rstrip(),
             "  ".join("-" * w for w in widths)]
    for row in cells[1:]:
        lines.append("  ".join(_pad(c, widths[i]) for i, c in enumerate(row)).rstrip())
    return "\n".join(lines)


class Output:
    def __init__(self, as_json: bool):
        self.as_json = as_json

    def result(self, data: Any, text: str | Callable[[], str]) -> None:
        if self.as_json:
            print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
        else:
            print(text() if callable(text) else text)

    def info(self, text: str) -> None:
        """Progress lines (text mode only)."""
        if not self.as_json:
            print(text, flush=True)


def _sim_tag(simulated: bool) -> str:
    return "【仿真】" if simulated else ""


# ------------------------------------------------------------------ clients from config


def _cloud(cfg: dict, *, auth: bool = True):
    from .cloud import CloudClient

    url = os.environ.get("NEUXMIND_CLOUD_URL") or cfg.get("cloud_url")
    if not url:
        raise NeuxMindError("尚未配置云平台地址：nx config set cloud_url http://127.0.0.1:8800", code="config_error")
    token = os.environ.get("NEUXMIND_API_KEY") or cfg.get("cloud_token")
    if auth and not token:
        raise NeuxMindError("尚未登录云平台：先运行 nx login", code="not_logged_in")
    return CloudClient(url, token, org_id=cfg.get("org_id"))


def _robot(cfg: dict, name: str | None):
    from .robot import RobotClient

    name, entry = config.get_robot(cfg, name)
    return name, RobotClient(entry["url"], entry["token"])


def _password(args) -> str:
    if getattr(args, "password", None):
        return args.password
    if os.environ.get("NEUXMIND_PASSWORD"):
        return os.environ["NEUXMIND_PASSWORD"]
    if getattr(args, "password_stdin", False):
        return sys.stdin.readline().rstrip("\n")
    return getpass.getpass("密码：")


def parse_param(text: str) -> tuple[str, Any]:
    """``k=v`` → (k, value); value parsed as JSON, falling back to the raw string."""
    key, sep, raw = text.partition("=")
    if not sep or not key.strip():
        raise NeuxMindError(f"--param 需要 k=v 格式，收到 {text!r}", code="bad_request")
    try:
        value = json.loads(raw)
    except ValueError:
        value = raw
    return key.strip(), value


# ------------------------------------------------------------------ config & account


def cmd_config_set(args, cfg, out):
    if args.key not in config.SETTABLE_KEYS:
        raise NeuxMindError(f"可设置的键：{', '.join(config.SETTABLE_KEYS)}", code="bad_request")
    value = None if args.value in ("", "null", "none") else args.value
    cfg[args.key] = value
    path = config.save(cfg)
    out.result({"key": args.key, "value": value, "path": str(path)}, f"已设置 {args.key} = {value}（{path}）")
    return 0


def cmd_config_show(args, cfg, out):
    data = config.masked(cfg)
    if args.key:
        value = data.get(args.key)
        out.result({args.key: value}, f"{args.key} = {value}")
    else:
        out.result({"path": str(config.config_path()), "config": data},
                   f"配置文件：{config.config_path()}\n" + json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def _store_login(cfg, email, token):
    # The personal org is the server default; org_id stays unset unless chosen with nx config set.
    cfg["cloud_token"] = token
    cfg["cloud_email"] = email
    config.save(cfg)


def cmd_register(args, cfg, out):
    cloud = _cloud(cfg, auth=False)
    result = cloud.register(args.email, _password(args), args.name)
    if result.get("status") == "claim_sent":
        lines = ["该邮箱已被一个未验证的账户占用，认领邮件已发送到该邮箱（没有创建新账户，也没有登录）。",
                 "  如果这是你的邮箱：收到认领令牌后运行 nx claim <令牌>，设置新密码并获得一个已验证的新账户；",
                 "  原来占用该邮箱的未验证账户会被停用。"]
        if result.get("dev_claim_token"):
            lines.append(f"  【仅开发环境】服务端没有真实发信（simulated），认领令牌：{result['dev_claim_token']}")
        out.result({**result, "logged_in": False}, "\n".join(lines))
        return 0
    _store_login(cfg, args.email, result["token"])
    data = {"user": result.get("user"), "org_id": result.get("org_id"), "logged_in": True,
            "verification": result.get("verification")}
    if result.get("dev_verification_token"):
        data["dev_verification_token"] = result["dev_verification_token"]
    lines = [f"已注册并登录：{args.email}（组织 {result.get('org_id')}）"]
    lines += _verification_lines(result.get("verification") or {}, result.get("dev_verification_token"))
    out.result(data, "\n".join(lines))
    return 0


def _verification_lines(verification: dict, dev_token: str | None) -> list[str]:
    """Explain what the user must do to verify the e-mail."""
    status = verification.get("status")
    lines = ["邮箱尚未验证：验证前可以浏览目录、钱包和设备，但不能购买、支付或调用网关。"]
    if status == "sent":
        lines.append("  已发送验证邮件，收到令牌后运行：nx verify-email <令牌>")
    elif status == "unavailable":
        lines.append("  服务端没有配置邮件发送，请联系平台运营人员人工核实后验证。")
    else:
        lines.append("  验证邮件发送失败，可稍后运行：nx verify-email --resend")
    if dev_token:
        lines.append(f"  【仅开发环境】服务端没有真实发信（simulated），验证令牌：{dev_token}")
        lines.append(f"  运行：nx verify-email {dev_token}")
    lines.append("  验证必须在本人登录状态下完成；验证后其他登录会话和验证前创建的 API Key 会被吊销。")
    return lines


def cmd_verify_email(args, cfg, out):
    if args.resend:
        result = _cloud(cfg).resend_verification()
        if result.get("email_verified"):
            out.result(result, "邮箱已经验证过了")
            return 0
        lines = _verification_lines(result.get("verification") or {}, result.get("dev_verification_token"))
        out.result(result, "已重新发送验证邮件（旧令牌已作废）\n" + "\n".join(lines[1:]))
        return 0
    if not args.token:
        raise NeuxMindError("需要验证令牌：nx verify-email <令牌>，或 nx verify-email --resend 重新获取",
                            code="bad_request")
    result = _cloud(cfg).verify_email(args.token)      # must be the account's own session
    email = (result.get("user") or {}).get("email")
    lines = [f"邮箱已验证：{email}。现在可以购买模型授权、调用网关了"]
    revoked_keys = result.get("api_keys_revoked") or 0
    revoked_sessions = result.get("sessions_revoked") or 0
    if revoked_sessions:
        lines.append(f"  已注销该账户的其他 {revoked_sessions} 个登录会话（本机会话保留）")
    if revoked_keys:
        lines.append(f"  验证前创建的 {revoked_keys} 个 API Key 已被吊销，请重新创建（机器人/CI 里用到的要更新）")
    out.result(result, "\n".join(lines))
    return 0


def cmd_claim(args, cfg, out):
    cloud = _cloud(cfg, auth=False)
    result = cloud.claim(args.token, _password(args), args.name)
    email = (result.get("user") or {}).get("email")
    _store_login(cfg, email, result["token"])
    out.result({**result, "logged_in": True},
               f"已认领邮箱 {email}：新建了一个已验证账户并登录（组织 {result.get('org_id')}）。"
               "\n  原来占用该邮箱的未验证账户已停用，它的会话与 API Key 均已失效。")
    return 0


def cmd_password_reset_request(args, cfg, out):
    email = args.email or input("邮箱：").strip()
    result = _cloud(cfg, auth=False).request_password_reset(email)
    lines = ["如果该邮箱已注册，重设密码邮件已发送（未验证的账户会收到认领邮件）。",
             "  收到令牌后：nx password-reset confirm <令牌>（认领令牌请用 nx claim <令牌>）"]
    for key, label in (("dev_reset_token", "重设令牌"), ("dev_claim_token", "认领令牌")):
        if result.get(key):
            lines.append(f"  【仅开发环境】服务端没有真实发信（simulated），{label}：{result[key]}")
    out.result(result, "\n".join(lines))
    return 0


def cmd_password_reset_confirm(args, cfg, out):
    result = _cloud(cfg, auth=False).reset_password(args.token, _password(args))
    email = (result.get("user") or {}).get("email")
    _store_login(cfg, email, result["token"])
    out.result({**result, "logged_in": True},
               f"已为 {email} 设置新密码并登录；该账户其他登录会话已全部注销。"
               "\n  API Key 未被吊销（机器人可能依赖它们），请用 API 自查 /api-keys，不认识的请吊销。")
    return 0


def cmd_login(args, cfg, out):
    cloud = _cloud(cfg, auth=False)
    email = args.email or input("邮箱：").strip()
    result = cloud.login(email, _password(args))
    _store_login(cfg, email, result["token"])
    text = f"已登录：{email}，令牌已保存到 {config.config_path()}（权限 600）"
    if (result.get("user") or {}).get("email_verified") is False:
        text += ("\n邮箱尚未验证：不能购买、支付或调用网关。运行 nx verify-email <令牌>，"
                 "或 nx verify-email --resend 重新获取验证邮件")
    out.result({"user": result.get("user"), "expires_at": result.get("expires_at"), "logged_in": True}, text)
    return 0


def cmd_logout(args, cfg, out):
    if cfg.get("cloud_token"):
        try:
            _cloud(cfg).logout()
        except NeuxMindError:
            pass
    cfg.pop("cloud_token", None)
    config.save(cfg)
    out.result({"logged_out": True}, "已退出登录")
    return 0


def cmd_whoami(args, cfg, out):
    me = _cloud(cfg).me()
    user = me.get("user") or {}

    def text():
        lines = [f"用户：{user.get('email')}（{user.get('name') or '-'}）",
                 f"当前组织：{me.get('current_org_id')}，角色：{me.get('role')}",
                 "邮箱：已验证" if me.get("email_verified", user.get("email_verified")) else
                 "邮箱：未验证（nx verify-email <令牌>，或 --resend 重新获取）"]
        if me.get("platform_operator"):
            lines.append("平台运营员：是")
        lines.append(f"云平台：{cfg.get('cloud_url')}")
        return "\n".join(lines)

    out.result(me, text)
    return 0


# ------------------------------------------------------------------ robots


def cmd_robot_add(args, cfg, out):
    from .robot import RobotClient

    token = args.token or os.environ.get("NEUXMIND_ROBOT_TOKEN")
    if not token:
        raise NeuxMindError("需要 --token（或环境变量 NEUXMIND_ROBOT_TOKEN）", code="bad_request")
    entry: dict[str, Any] = {"url": args.url.rstrip("/"), "token": token}
    if not args.no_verify:
        manifest = RobotClient(args.url, token).manifest()
        entry.update(instance_id=manifest["instance_id"], manifest_name=manifest["name"], family=manifest["family"],
                     simulated=manifest.get("simulated") is True)
    cfg["robots"][args.name] = entry
    if args.default or not cfg.get("default_robot"):
        cfg["default_robot"] = args.name
    config.save(cfg)
    desc = f"{entry.get('manifest_name', '未验证')} {_sim_tag(entry.get('simulated', False))}".strip()
    out.result({"name": args.name, **{k: v for k, v in entry.items() if k != "token"}},
               f"已添加机器人 {args.name}：{entry['url']}（{desc}）")
    return 0


def cmd_robot_list(args, cfg, out):
    from .robot import RobotClient

    rows, data = [], []
    for name, entry in cfg.get("robots", {}).items():
        item = {"name": name, **{k: v for k, v in entry.items() if k != "token"},
                "default": name == cfg.get("default_robot")}
        if args.check:
            try:
                status = RobotClient(entry["url"], entry["token"], timeout=3).status()
                item["online"] = True
                item["estop_latched"] = status.get("estop_latched")
            except NeuxMindError as exc:
                item["online"] = False
                item["error"] = exc.code
        data.append(item)
        state = ""
        if args.check:
            state = ("在线" + ("，急停锁存" if item.get("estop_latched") else "")) if item["online"] else "离线"
        rows.append([("* " if item["default"] else "  ") + name, entry["url"], entry.get("family", "-"),
                     "是" if entry.get("simulated") else "否", state])
    headers = ["名称", "地址", "类型", "仿真", "状态" if args.check else ""]
    out.result({"robots": data}, lambda: table(headers, rows) if rows else "还没有机器人；nx robot add 或 nx sim up")
    return 0


def _fmt_params(params: dict) -> str:
    parts = []
    for key, value in (params or {}).items():
        if isinstance(value, list) and len(value) > 4:
            value = f"[{len(value)} 项]"
        parts.append(f"{key}={json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value}")
    return ", ".join(parts)


def cmd_robot_caps(args, cfg, out):
    from . import _bootstrap

    name, robot = _robot(cfg, args.name)
    manifest = robot.manifest()
    caps_spec = _bootstrap.spec().capabilities
    rows = []
    for cap in manifest["capabilities"]:
        spec = caps_spec.CAPABILITIES.get(cap["id"])
        rows.append([cap["id"], spec.title if spec else "扩展能力", spec.kind if spec else "-",
                     spec.safety if spec else "motion", _fmt_params(cap.get("params", {}))])

    def text():
        head = (f"{name}：{manifest['name']}  family={manifest['family']}  instance={manifest['instance_id']}"
                f"  simulated={'true（仿真本体，结果不能作为现场证据）' if manifest.get('simulated') else 'false'}")
        return head + "\n" + table(["能力", "名称", "类型", "安全", "参数"], rows)

    out.result({"robot": name, "simulated": manifest.get("simulated") is True, "manifest": manifest}, text)
    return 0


def cmd_robot_remove(args, cfg, out):
    if args.name not in cfg.get("robots", {}):
        raise NeuxMindError(f"未添加机器人 {args.name}", code="robot_not_found")
    del cfg["robots"][args.name]
    if cfg.get("default_robot") == args.name:
        cfg["default_robot"] = next(iter(cfg["robots"]), None)
    config.save(cfg)
    out.result({"removed": args.name}, f"已移除 {args.name}（本地配置）")
    return 0


def _sandbox_lines(sandbox: dict) -> list[str]:
    if not sandbox:
        return ["  技能沙箱：运行时未报告（旧版本运行时）"]
    level = sandbox.get("level")
    level_text = {"full": "完整隔离（full）", "best-effort": "尽力而为（best-effort）"}.get(level, str(level or "未报告"))
    lines = [f"  技能沙箱：{sandbox.get('mode')}  隔离等级：{level_text}"]
    who = f"运行时用户 {sandbox.get('runtime_user', '?')}，技能用户 {sandbox.get('user', '?')}"
    if "privilege_drop" in sandbox:
        who += "，已降权" if sandbox.get("privilege_drop") else "，未降权（技能与运行时同用户）"
    lines.append("    " + who)
    if "python_usable" in sandbox:
        if sandbox.get("python_usable"):
            lines.append(f"    技能解释器：{sandbox.get('python')}（技能用户可执行）")
        else:
            lines.append(f"    技能解释器：{sandbox.get('python')} —— 技能用户无法执行，技能运行会失败；"
                         "请用 --sandbox-python 指定一个技能用户可执行的同版本解释器（如 /usr/bin/python3.10）")
    extras = []
    if "pids_cgroup" in sandbox:
        extras.append("pids cgroup " + ("已启用" if sandbox.get("pids_cgroup") else "未启用"))
    if sandbox.get("network_isolated") is False:
        extras.append("网络未隔离")
    if extras:
        lines.append("    " + "，".join(extras))
    for reason in sandbox.get("best_effort_reasons") or []:
        lines.append(f"    尽力而为原因：{reason}")
    return lines


def cmd_robot_status(args, cfg, out):
    name, robot = _robot(cfg, args.name)
    status = robot.status()

    def text():
        health = status.get("health") or {}
        lines = [
            f"{name}：{status.get('name')} {_sim_tag(status.get('simulated'))}",
            f"  运行时版本：{status.get('runtime_version') or '?'}",
            f"  健康：{'正常' if health.get('ok') else '异常'}  模式：{status.get('mode')}",
            f"  急停锁存：{'是（' + str(status.get('latch_reason')) + '）' if status.get('estop_latched') else '否'}",
            f"  运行中：{', '.join(status.get('active_runs') or []) or '无'}"]
        lines += _sandbox_lines(status.get("sandbox") or {})
        models = status.get("models") or {}
        if models.get("enabled") and "installed" in models:
            lines.append(f"  端侧模型：已安装 {models.get('installed')} 个，安装中 {models.get('installing', 0)} 个"
                         f"（运行时 {', '.join(models.get('runtimes') or [])}）")
            if models.get("invalid_entries"):
                lines.append(f"    无效条目：{models['invalid_entries']}")
        elif models.get("enabled"):
            lines.append(f"  端侧模型：由{'适配器' if models.get('provider') == 'adapter' else '运行时'}提供")
        elif models:
            lines.append("  端侧模型：未启用（运行时需 --models-dir）")
        telemetry = status.get("telemetry") or {}
        if telemetry:
            lines.append(f"  遥测：最新 seq {telemetry.get('last_seq')}，"
                         f"{'持久化（容量 ' + str(telemetry.get('capacity')) + ' 条）' if telemetry.get('persistent') else '仅内存'}"
                         + (f"，写入错误 {telemetry['write_errors']}" if telemetry.get("write_errors") else ""))
        return "\n".join(lines)

    out.result(status, text)
    return 0


def cmd_robot_register(args, cfg, out):
    name, robot = _robot(cfg, args.name)
    view = _cloud(cfg).register_robot(robot.manifest())
    cfg["robots"][name]["cloud_id"] = view["id"]
    config.save(cfg)
    out.result(view, f"已在云端登记 {name}：id={view['id']}，{len(view['capabilities'])} 项能力"
                     f"{'，仿真本体' if view.get('simulated') else ''}")
    return 0


# ------------------------------------------------------------------ adapters & conformance (§6.3)


MOTION_NOTICE = ("motion 级别会让机器人真实运动：最小动作约 0.1 m / 0.05 rad / 10% 夹爪开度，但中断与停止用例的目标更大"
                 "（以运行时 README §3.1 为准，导航可达约 0.5 m、关节约 0.4 rad、夹爪可能全程张开），中断失效时会走满；"
                 "请清空周围、急停在手")


def _report_text(report: dict, title: str) -> str:
    from .adapters import capability_rows, level_lines, undeclared

    level = report.get("level")
    ok = report.get(f"{level}_ok")
    simulated = report.get("simulated") is True
    lines = [title + (" 【仿真本体】" if simulated else ""), *level_lines(report)]
    if report.get("prelatched"):
        lines.append("  测试前急停已锁存：动态级别受阻（测试不会替你解除别人按下的急停）")
    if report.get("aborted"):
        lines.append(f"  测试中止：{report['aborted']}")
    if report.get("final_latched"):
        lines.append("  测试结束时急停仍锁存：确认现场安全后 nx reset <robot> --authorize")
    lines.append(f"  结论：{level} 级别{'通过' if ok else '未通过'}")
    rows = capability_rows(report)
    if rows:
        lines.append(table(["能力", "名称", "static", "dry", "motion", "说明"], rows))
    missing = undeclared(report)
    if missing:
        lines.append(f"未声明（不支持或尚未实现，{len(missing)} 项）：{', '.join(missing)}")
    lines.append(f"注意：一致性测试结果不等于现场验收证据（field_acceptance={str(report.get('field_acceptance')).lower()}，"
                 f"evidence_kind={report.get('evidence_kind')}）。真机部署前仍须现场确认安全后运行 motion 级别并由操作员验收。")
    return "\n".join(lines)


def cmd_adapter_new(args, cfg, out):
    from .adapters import load_manifest_file, new_adapter

    manifest = None
    source = None
    if args.from_manifest:
        if Path(args.from_manifest).expanduser().is_file():
            manifest, source = load_manifest_file(Path(args.from_manifest).expanduser()), f"文件 {args.from_manifest}"
        else:
            robot_name, robot = _robot(cfg, args.from_manifest)
            manifest, source = robot.manifest(), f"机器人 {robot_name} 的 /v2/manifest"
    target = Path(args.out or args.name)
    summary = new_adapter(args.name, target, caps=args.caps or [], manifest=manifest, family=args.family,
                          vendor=args.vendor, model=args.model, impls=args.impl or [], params=args.param or [],
                          instance_id=args.instance_id, simulated=args.simulated, force=args.force,
                          source=source)
    if source:
        summary["source"] = source

    def text():
        lines = [f"已生成适配器包 {summary['module']}：{summary['path']}" + (f"（能力与限值取自{source}）" if source else ""),
                 *([f"  注意：厂商 {summary['input']['vendor']!r}、型号 {summary['input']['model']!r} 与限值来自{source}，"
                    "请对照规格书核对"] if source else []),
                 f"  加载方式：{summary['adapter']}",
                 f"  已实现：{', '.join(summary['implemented']) or '无'}",
                 f"  待实现（抛 NotImplementedError，不出现在 manifest 中）：{', '.join(summary['todo']) or '无'}"]
        if summary["mandatory_todo"]:
            lines.append(f"  注意：必备能力 {', '.join(summary['mandatory_todo'])} 尚未实现，实现前运行时不能加载该适配器，"
                         "static 级别也不会通过")
        lines += [f"下一步：实现待实现的方法并加入 IMPLEMENTED →  nx adapter test {summary['path']} --level static",
                  f"        接替身/仿真或已连好的本体 →  nx adapter test {summary['path']} --level dry",
                  f"        启动 →  BRAIN_EDGE_TOKEN=… PYTHONPATH={summary['path']} neuxmind-runtime serve --adapter "
                  f"{summary['adapter']}"]
        return "\n".join(lines)

    out.result(summary, text)
    return 0


def cmd_adapter_test(args, cfg, out):
    from .adapters import test_adapter

    seen = {"pct": None}

    def progress(fraction: float) -> None:
        pct = int(fraction * 100) // 10 * 10
        if pct != seen["pct"]:
            seen["pct"] = pct
            out.info(f"  进行中 {pct}%")

    if args.level == "motion":
        out.info(MOTION_NOTICE + "；随时可 Ctrl-C（停止并锁存）或按硬件急停")
    report = test_adapter(args.path, args.level, authorize=args.authorize, time_scale=args.time_scale,
                          on_progress=progress)
    if args.out:
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                                  encoding="utf-8")
    described = report.get("adapter") or {}
    robot = report.get("robot") or {}
    title = (f"一致性测试（本地进程内）{described.get('adapter', args.path)}："
             f"{robot.get('name') or ''} 级别 {args.level}")
    text = _report_text(report, title) + (f"\n报告已写入 {args.out}" if args.out else "")
    out.result(report, text)
    return 0 if report.get(f"{args.level}_ok") else 1


def _cloud_robot_id(cfg, name: str, robot, cloud, out) -> str:
    """Register (or refresh) the robot in the cloud fleet with its current manifest → fleet id."""
    view = cloud.register_robot(robot.manifest(refresh=True))
    if cfg["robots"][name].get("cloud_id") != view["id"]:
        cfg["robots"][name]["cloud_id"] = view["id"]
        config.save(cfg)
    out.info(f"已在云端设备群登记/刷新 {name}（id={view['id']}，按当前 manifest）")
    return view["id"]


def cmd_robot_conformance(args, cfg, out):
    name, robot = _robot(cfg, args.name)
    if args.level == "motion" and not args.authorize:
        raise NeuxMindError("motion 级别一致性测试会让机器人做最小幅度的真实动作：确认现场安全（人员远离、急停在手）后"
                            "加 --authorize。本次没有向机器人发送任何请求。", code="authorization_required")
    cloud = _cloud(cfg) if args.upload else None          # fail early when not logged in
    wait = args.wait or args.upload
    if args.id:
        record = robot.conformance(args.id)
        if record.get("status") == "running" and wait:
            record = robot.wait_conformance(args.id, timeout=args.wait_timeout)
    else:
        if args.level == "motion":
            out.info(f"{name}：" + MOTION_NOTICE + f"；随时可 nx stop {name}（测试中止并保持锁存）")
        started = robot.start_conformance(args.level, authorize=args.authorize)
        cid = started["conformance_id"]
        out.info(f"已在 {name}{_sim_tag(started.get('simulated'))} 上开始一致性测试 {cid}（级别 {args.level}）")
        if not wait:
            out.result(started, f"测试在机器人上异步执行；查看结果：nx robot conformance {name} --id {cid} --wait")
            return 0
        seen = {"pct": None}

        def update(record: dict) -> None:
            pct = int((record.get("progress") or 0) * 100) // 10 * 10
            if record.get("status") == "running" and pct != seen["pct"]:
                seen["pct"] = pct
                out.info(f"  进行中 {pct}%")

        record = robot.wait_conformance(cid, timeout=args.wait_timeout, on_update=update)
    status = record.get("status")
    if status == "running":
        out.result(record, f"一致性测试 {record.get('id')} 仍在进行（{int((record.get('progress') or 0) * 100)}%）；"
                           f"加 --wait 等待结果")
        return 0
    if status != "completed" or not isinstance(record.get("report"), dict):
        error = record.get("error") or {}
        reason = error.get("error") if isinstance(error, dict) else error
        raise NeuxMindError(f"一致性测试 {record.get('id')} 未完成（{status}）：{reason or '未知原因'}"
                            + ("；运行时重启时它没有结束，结果未知（不会自动重跑）" if status == "unknown" else ""),
                            code="conformance_unknown" if status == "unknown" else "conformance_failed",
                            details={"conformance": record})
    report = record["report"]
    level = report.get("level")
    upload = None
    if args.upload:
        from .adapters import manifest_digest

        current = manifest_digest(robot.manifest(refresh=True))
        reported = report.get("manifest_digest")
        if reported is not None and reported != current:
            raise NeuxMindError(f"一致性测试 {report.get('conformance_id')} 是对机器人之前的 manifest 运行的（manifest_digest 不同），"
                                f"不能上传到当前 manifest：请重新运行 nx robot conformance {name} --level {level} --upload。"
                                "本次没有向云端发送任何请求", code="conformance_manifest_mismatch",
                                details={"report_manifest_digest": reported, "manifest_digest": current})
        if reported is None:
            out.info("注意：报告没有 manifest_digest（旧运行时），云端会保存摘要但不会把它用于 require_level")
        robot_id = _cloud_robot_id(cfg, name, robot, cloud, out)
        upload = cloud.upload_conformance(robot_id, report)
    title = f"一致性测试 {report.get('conformance_id')}：{name}（{(report.get('robot') or {}).get('name')}）级别 {level}"
    text = _report_text(report, title)
    if upload is not None:
        summary = upload["conformance"]
        text += (f"\n已上传摘要到云端设备群（robot {upload['robot_id']}）：static_ok={summary['static_ok']} "
                 f"dry_ok={summary['dry_ok']} motion_ok={summary['motion_ok']}；云端只保存摘要，"
                 f"且标注为自报的一致性结果（不是现场验收）")
    out.result({"report": report, "upload": upload} if upload is not None else report, text)
    return 0 if report.get(f"{level}_ok") else 1


# ------------------------------------------------------------------ skills


def cmd_skill_new(args, cfg, out):
    from . import templates

    target = templates.create(args.template, args.id, args.dir, title=args.title, force=args.force)
    files = sorted(str(p.relative_to(target)) for p in target.rglob("*") if p.is_file())
    out.result({"path": str(target), "template": args.template, "files": files},
               lambda: f"已创建技能 {args.id}（模板 {args.template}）：{target}\n"
                       + "\n".join(f"  {f}" for f in files)
                       + f"\n下一步：python3 -m unittest discover -s {target}/tests"
                       + f"\n        nx skill check {target} --robot <机器人>")
    return 0


def _check_targets(args, cfg) -> list[tuple[str, dict]]:
    """Resolve --robot / --body / --all into (label, manifest) pairs."""
    targets = []
    names = list(args.robot or [])
    if args.all:
        names = list(cfg.get("robots", {}))
    for name in names:
        label, robot = _robot(cfg, name)
        targets.append((label, robot.manifest()))
    for body in args.body or []:
        from .skill.testing import sim_manifest

        targets.append((f"仿真 {body}（离线）", sim_manifest(body)))
    if not targets:
        label, robot = _robot(cfg, None)
        targets.append((label, robot.manifest()))
    return targets


def cmd_skill_check(args, cfg, out):
    from .package import explain_compatibility, format_compatibility, load_skill

    _package, skill = load_skill(args.dir)
    reports = []
    for label, manifest in _check_targets(args, cfg):
        report = explain_compatibility(skill, manifest)
        report["target"] = label
        reports.append(report)
    ok = all(r["compatible"] for r in reports)
    out.result({"skill": f"{skill['id']}@{skill['version']}", "compatible": ok, "results": reports},
               lambda: "\n".join(format_compatibility(r, r["target"]) for r in reports))
    return 0 if ok else 1


def cmd_skill_install(args, cfg, out):
    from .package import explain_compatibility, format_compatibility, load_skill

    package, skill = load_skill(args.dir)
    name, robot = _robot(cfg, args.robot)
    report = explain_compatibility(skill, robot.manifest())
    if not report["compatible"]:
        out.result({"installed": False, "code": "incompatible", "compatibility": report},
                   "未安装：技能与机器人不兼容\n" + format_compatibility(report, name))
        return 1
    result = robot.install_skill(package)
    again = "（该版本已安装，内容相同）" if result.get("already_installed") else ""
    out.result(result, f"已安装 {result['id']}@{result['version']} 到 {name}{again}  digest={result['digest'][:12]}")
    return 0


def cmd_skill_list(args, cfg, out):
    name, robot = _robot(cfg, args.robot)
    skills = robot.skills()
    rows = [[s["ref"], s.get("title", ""), "兼容" if s["compatibility"]["compatible"] else "不兼容",
             ", ".join(r["id"] for r in s["requires"])] for s in skills]
    out.result({"robot": name, "skills": skills},
               lambda: table(["技能", "标题", "兼容性", "必需能力"], rows) if rows else f"{name} 上还没有安装技能")
    return 0


def _coerce_params(robot, skill_id: str, params: dict) -> dict:
    """Keep string parameters as strings even if they look like JSON numbers (``--param name=007``)."""
    try:
        installed = [s for s in robot.skills() if s["id"] == skill_id]
    except NeuxMindError:
        return params
    if not installed:
        return params
    decls = {p["name"]: p for p in installed[-1].get("parameters", [])}
    out = {}
    for key, value in params.items():
        decl = decls.get(key)
        if decl and decl.get("type") in ("str", "blob_ref", "enum") and not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False)
        out[key] = value
    return out


def _print_run_updates(out: Output):
    seen = {"events": 0}

    def update(run: dict) -> None:
        events = run.get("events") or []
        for event in events[seen["events"]:]:
            kind = event.get("type")
            if kind in ("log", "progress"):
                extra = f"  {int(run.get('progress', 0) * 100)}%" if kind == "progress" else ""
                out.info(f"  [{kind}]{extra} {event.get('message', '')}")
            elif kind == "finished":
                pass
        seen["events"] = len(events)

    return update


def _run_summary(run: dict) -> str:
    status = run.get("status")
    label = {"succeeded": "成功", "failed": "失败", "stopped": "已停止", "unknown": "未知",
             "running": "运行中", "queued": "排队中"}.get(status, status)
    lines = [f"运行 {run.get('id') or run.get('run_id')}：{label}（{status}）  进度 {int((run.get('progress') or 0) * 100)}%"]
    if run.get("error"):
        lines.append(f"  错误：{run['error']}")
    if run.get("result") is not None:
        lines.append("  结果：" + json.dumps(run["result"], ensure_ascii=False))
    return "\n".join(lines)


def cmd_skill_run(args, cfg, out):
    name, robot = _robot(cfg, args.robot)
    params = dict(parse_param(p) for p in (args.param or []))
    if args.params_json:
        params = {**json.loads(args.params_json), **params}
    if not args.authorize:
        raise NeuxMindError(
            "技能运行需要硬件授权：确认现场安全、人员远离、急停在手后，加 --authorize 重新运行。本次未发送任何请求。",
            code="authorization_required")
    params = _coerce_params(robot, args.id, params)
    simulated = robot.simulated
    started = robot.run_skill(args.id, params, authorize=True, version=args.version, timeout_s=args.timeout_s)
    run_id = started["run_id"]
    if not args.wait:
        out.result({**started, "robot": name, "simulated": simulated},
                   f"已启动 {args.id} 于 {name}{_sim_tag(simulated)}：run_id={run_id}\n"
                   f"  查看：nx skill status {run_id} --robot {name}    停止：nx stop {name}")
        return 0
    out.info(f"已启动 {args.id} 于 {name}{_sim_tag(simulated)}：run_id={run_id}，等待结束（Ctrl-C 只停止等待，不停止机器人）")
    record = robot.wait(run_id, timeout=args.wait_timeout, on_update=_print_run_updates(out))
    out.result({**record, "robot": name, "simulated": simulated}, lambda: _run_summary(record))
    return 0 if record.get("status") == "succeeded" else 1


def cmd_skill_status(args, cfg, out):
    name, robot = _robot(cfg, args.robot)
    if args.run_id:
        record = robot.run(args.run_id)
        out.result(record, lambda: _run_summary(record))
        return 0
    runs = robot.runs()
    rows = [[r["id"], r["kind"], r["target"], r["status"], f"{int((r.get('progress') or 0) * 100)}%",
             r.get("error") or ""] for r in runs[: args.limit]]
    out.result({"robot": name, "runs": runs[: args.limit]},
               lambda: table(["运行", "类型", "目标", "状态", "进度", "错误"], rows) if rows else f"{name} 还没有运行记录")
    return 0


def cmd_skill_cancel(args, cfg, out):
    name, robot = _robot(cfg, args.robot)
    result = robot.cancel(args.run_id)
    out.result(result, f"取消请求{'已接受' if result.get('accepted') else '未接受：' + str(result.get('reason'))}"
                       f"（{args.run_id}，状态 {result.get('status')}）")
    return 0


# ------------------------------------------------------------------ safety


def cmd_stop(args, cfg, out):
    name, robot = _robot(cfg, args.robot)
    result = robot.stop(args.reason)
    out.result({**result, "robot": name},
               f"已停止 {name}：急停已锁存，停止了 {len(result.get('stopped_runs') or [])} 个运行。"
               f"\n  恢复运动需现场确认后：nx reset {name} --authorize")
    return 0


def cmd_reset(args, cfg, out):
    name, robot = _robot(cfg, args.robot)
    result = robot.reset(authorize=args.authorize)
    out.result({**result, "robot": name}, f"已解除 {name} 的急停锁存，可以再次运动")
    return 0


# ------------------------------------------------------------------ cloud


def cmd_wallet(args, cfg, out):
    cloud = _cloud(cfg)
    wallet = cloud.wallet()
    data = {"wallet": wallet}
    if args.ledger:
        data["ledger"] = cloud.ledger(limit=args.limit)

    def text():
        lines = [f"余额 {wallet['balance']} 点  预留 {wallet['reserved']}  可用 {wallet['available']}（组织 {wallet['org_id']}）"]
        if args.ledger:
            lines.append(table(["时间", "变动", "类型", "原因"],
                               [[e.get("created_at"), e.get("delta"), e.get("kind"), e.get("reason") or ""]
                                for e in data["ledger"]]))
        return "\n".join(lines)

    out.result(data, text)
    return 0


def _price(pricing: dict) -> str:
    kind, credits = pricing.get("type"), pricing.get("credits", 0)
    return {"per_1k_tokens": f"{credits} 点/千 token", "per_call": f"{credits} 点/次",
            "license_once": f"一次性 {credits} 点", "free": "免费"}.get(kind, str(pricing))


def cmd_models_list(args, cfg, out):
    cloud = _cloud(cfg)
    robot_name = None
    result: dict = {}
    if args.require_level and not args.robot and not cfg.get("default_robot"):
        raise NeuxMindError("--require-level 需要 --robot（设备群里的机器人）", code="bad_request")
    if args.robot or args.require_level:
        robot_name, robot = _robot(cfg, args.robot)
        if args.require_level:
            robot_id = _cloud_robot_id(cfg, robot_name, robot, cloud, out)
            result = cloud.compatible_models(robot_id=robot_id, require_level=args.require_level)
        else:
            result = cloud.compatible_models(robot.manifest())
        models = result["models"]
    else:
        models = cloud.models(kind=args.kind)
    if args.kind and args.robot:
        models = [m for m in models if m.get("kind") == args.kind]
    rows = [[m["id"], m["name"], m["kind"], m["source"], _price(m.get("pricing") or {}),
             "已授权" if m.get("entitled") else "", ", ".join(m.get("requires_capabilities") or []) or "-"]
            for m in models]
    head = f"适用于 {robot_name} 的模型（按其能力清单过滤）：\n" if robot_name else ""
    if args.require_level:
        conf = result.get("conformance") or {}
        head = (f"适用于 {robot_name} 的模型（只算一致性报告 {conf.get('conformance_id')} 中 {args.require_level} 级别已通过的能力"
                f"{'，仿真本体' if conf.get('simulated') else ''}）：\n"
                f"  已验证可用：{', '.join(result.get('usable_capabilities') or []) or '无'}\n"
                f"  未通过/未测到 {args.require_level} 级别：{', '.join(result.get('not_verified') or []) or '无'}\n")
    out.result({"robot": robot_name, "models": models,
                **({k: result.get(k) for k in ("require_level", "usable_capabilities", "not_verified", "conformance")}
                   if args.require_level else {})},
               lambda: head + (table(["ID", "名称", "类型", "来源", "价格", "授权", "所需能力"], rows) if rows else "没有可用模型"))
    return 0


def cmd_models_buy(args, cfg, out):
    result = _cloud(cfg).purchase_model(args.id)
    order = result.get("order") or {}
    out.result(result, f"已购买 {args.id} 的授权：扣除 {order.get('credits', 0)} 点（订单 {order.get('id')}）")
    return 0


def _size_text(size) -> str:
    if not isinstance(size, int):
        return "?"
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return str(size)


def _time_text(ts) -> str:
    import time as _time

    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        return ""
    return _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(ts))


def cmd_models_install(args, cfg, out):
    from .models import install_from_cloud

    if not args.authorize:
        raise NeuxMindError(
            "安装端侧模型会变更机器人上的软件，需要现场确认：确认后加 --authorize 重新运行。本次未发送任何请求。",
            code="authorization_required")
    cloud = _cloud(cfg)
    name, robot = _robot(cfg, args.robot)
    seen = {"progress": None}

    def progress(update: dict) -> None:
        pct = int((update.get("progress") or 0) * 100)
        if update.get("status") in ("queued", "running") and pct != seen["progress"]:
            got, total = update.get("bytes"), update.get("total")
            detail = f"（{_size_text(got)} / {_size_text(total)}）" if isinstance(got, int) and isinstance(total, int) else ""
            out.info(f"  下载与校验中 {pct}%{detail}")
            seen["progress"] = pct

    def started(info: dict) -> None:
        out.info(f"正在把 {args.id} 安装到 {name}{_sim_tag(robot.simulated)}（运行 {info['run_id']}：下载 → 校验 SHA-256 → 原子安装）…")

    result = install_from_cloud(cloud, robot, args.id, authorize=True, on_progress=progress, on_start=started,
                                wait_timeout=args.wait_timeout)
    result["robot"] = name
    model = result.get("model") or {}

    def text():
        head = (f"{args.id} 已在 {name} 上（同一 SHA-256 已安装，未重复下载）" if result["already_installed"]
                else f"已安装 {args.id} 到 {name}")
        return "\n".join([head,
                          f"  SHA-256：{result['sha256']}",
                          f"  运行时：{result['runtime']}  大小：{_size_text(model.get('size'))}",
                          f"  路径：{model.get('path')}（技能中通过 ctx.models 只读访问）",
                          f"  删除：nx models remove {args.id} --robot {name} --authorize"])

    out.result(result, text)
    return 0


def cmd_models_installed(args, cfg, out):
    name, robot = _robot(cfg, args.robot)
    models = robot.models()
    rows = [[(m.get("sha256") or "")[:12], m.get("model_id") or "-", m.get("name") or "-", m.get("runtime"),
             _size_text(m.get("size")), _time_text(m.get("installed_at")), m.get("file")] for m in models]
    out.result({"robot": name, "models": models},
               lambda: (table(["SHA-256", "目录 ID", "名称", "运行时", "大小", "安装时间", "文件"], rows) if rows
                        else f"{name} 上没有安装端侧模型"))
    return 0


def cmd_models_remove(args, cfg, out):
    from .models import resolve_model

    if not args.authorize:
        raise NeuxMindError(
            "删除端侧模型会变更机器人上的软件，需要现场确认：确认后加 --authorize 重新运行。本次未发送任何请求。",
            code="authorization_required")
    name, robot = _robot(cfg, args.robot)
    cloud = None
    if (os.environ.get("NEUXMIND_CLOUD_URL") or cfg.get("cloud_url")) and \
            (os.environ.get("NEUXMIND_API_KEY") or cfg.get("cloud_token")):
        cloud = _cloud(cfg)          # fallback only: catalog id → package sha256 for records without one
    target = resolve_model(robot, args.model, cloud)
    try:
        result = robot.remove_model(target, authorize=True)
    except NeuxMindError as exc:
        if exc.code == "model_ambiguous":
            versions = exc.details.get("sha256") or []
            raise NeuxMindError(
                f"目录 ID {args.model} 在 {name} 上有 {len(versions)} 个已安装版本，没有删除。请用 SHA-256 指定："
                + "；".join(f"nx models remove {v} --robot {name} --authorize" for v in versions),
                code="model_ambiguous", status=exc.status, details=exc.details) from None
        if exc.code == "model_in_use":
            runs = exc.details.get("runs") or []
            raise NeuxMindError(
                f"模型 {args.model} 正被运行中的技能使用（{', '.join(runs) or '运行中'}），没有删除。"
                f"等这些运行结束，或先 nx skill cancel <run_id> --robot {name}（或 nx stop {name}）后再删除",
                code="model_in_use", status=exc.status, details=exc.details) from None
        raise
    sha = result.get("model_id")
    out.result({**result, "sha256": sha, "robot": name, "model": args.model},
               f"已从 {name} 删除模型 {args.model}（SHA-256 {sha}）")
    return 0


def format_event(event: dict) -> str:
    """One telemetry event as a text line: ``[seq] time type key=value ...``."""
    import time as _time

    ts = event.get("ts")
    when = _time.strftime("%H:%M:%S", _time.localtime(ts)) if isinstance(ts, (int, float)) else "--:--:--"
    fields = []
    for key, value in event.items():
        if key in ("seq", "ts", "type"):
            continue
        if isinstance(value, str):
            text = value if len(value) <= 160 else value[:157] + "…"
        else:
            text = json.dumps(value, ensure_ascii=False, default=str)
            text = text if len(text) <= 160 else text[:157] + "…"
        fields.append(f"{key}={text}")
    return f"[{event.get('seq')}] {when} {event.get('type')} " + " ".join(fields)


def cmd_telemetry_tail(args, cfg, out):
    name, robot = _robot(cfg, args.robot)
    events_iter = robot.tail(since=args.since, lines=args.lines, run_id=args.run, follow=args.follow,
                             interval=args.interval, duration=args.duration)
    if not args.follow:
        events = list(events_iter)
        last = max([e.get("seq", 0) for e in events] or [args.since or 0])
        data = {"robot": name, "run_id": args.run, "events": events, "last_seq": last}
        out.result(data, lambda: "\n".join(format_event(e) for e in events) if events else "（没有新事件）")
        return 0
    if not out.as_json:
        target = f"运行 {args.run}" if args.run else name
        print(f"跟踪 {target} 的遥测（Ctrl-C 结束跟踪，不影响机器人）…", flush=True)
    try:
        for event in events_iter:
            if out.as_json:            # --follow --json: one JSON object per line (NDJSON)
                print(json.dumps(event, ensure_ascii=False, default=str), flush=True)
            else:
                print(format_event(event), flush=True)
    except KeyboardInterrupt:
        if not out.as_json:
            print("\n已结束跟踪（机器人不受影响）", file=sys.stderr)
    return 0


def cmd_infer(args, cfg, out):
    options = dict(parse_param(o) for o in (args.option or []))
    result = _cloud(cfg).infer(args.model, args.prompt, max_tokens=args.max_tokens, **options)
    usage = result.get("usage") or {}

    def text():
        lines = [(result.get("output") or {}).get("text", ""),
                 f"—— 用量 {usage.get('total_tokens')} token，扣费 {result.get('credits_charged')} 点"]
        if result.get("simulated"):
            lines.append("—— 注意：该模型是测试替身（simulated=true），不是真实模型输出")
        return "\n".join(lines)

    out.result(result, text)
    return 0


def cmd_chat(args, cfg, out):
    """Open a metered session → one call (optionally streamed) → close → print metering."""
    from .gateway import GatewayStreamError, StreamInterrupted

    cloud = _cloud(cfg)
    session = cloud.gateway.open_session(args.model, args.max_tokens, max_minutes=args.max_minutes)
    data: dict[str, Any] = {"session_id": session.session_id, "model": args.model, "api": args.api,
                            "stream": bool(args.stream)}
    failure: NeuxMindError | None = None
    reply_text = ""
    simulated = False
    try:
        if args.api == "responses":
            call = lambda **kw: session.responses(args.prompt, **kw)      # noqa: E731
        else:
            call = lambda **kw: session.chat([{"role": "user", "content": args.prompt}], **kw)   # noqa: E731
        if args.stream:
            stream = call(stream=True)
            simulated = stream.simulated
            try:
                for piece in stream.text_deltas():
                    if not out.as_json:
                        sys.stdout.write(piece)
                        sys.stdout.flush()
            except (GatewayStreamError, StreamInterrupted) as exc:
                failure = exc
            finally:
                if not out.as_json and stream.text:
                    sys.stdout.write("\n")
                    sys.stdout.flush()
            reply_text = stream.text
            data["events"] = len(stream.events)
        else:
            reply = call()
            simulated = bool((reply.get("neuxmind") or {}).get("simulated"))
            if args.api == "responses":
                reply_text = reply.get("output_text") or ""
            else:
                choices = reply.get("choices") or [{}]
                reply_text = ((choices[0] or {}).get("message") or {}).get("content") or ""
            if not out.as_json:
                print(reply_text)
    except NeuxMindError as exc:
        failure = exc
    finally:
        try:
            close = session.close()
        except NeuxMindError as exc:
            close = None
            data["close_error"] = exc.to_dict()
            out.info(f"关闭会话失败：{exc.message}（代码 {exc.code}）；稍后 nx sessions show {session.session_id}")
    metering = session.last_metering
    data.update(text=reply_text, simulated=simulated, metering=metering.to_dict() if metering else None,
                close=close)
    lines = []
    if failure is not None:
        data["error"] = failure.to_dict()
        lines.append(f"错误：{failure.message}（代码 {failure.code}）")
    if metering is not None:
        lines.append("—— " + metering.describe())
    if close is not None:
        lines.append(f"—— 会话 {session.session_id} 已关闭：{_close_text(close)}")
    if simulated:
        lines.append("—— 注意：该模型是测试替身（simulated=true），不是真实模型输出")
    if out.as_json:
        out.result({"ok": failure is None, **data}, "")
    else:
        stream_to = sys.stderr if failure is not None else sys.stdout
        print("\n".join(lines), file=stream_to)
    return 1 if failure is not None else 0


def _close_text(close: dict) -> str:
    if close.get("status") == "closed":
        return (f"用量 {close.get('usage_tokens')} token（{close.get('usage_units')} 千 token 单位），"
                f"扣费 {close.get('credits_charged')} 点")
    if close.get("status") == "reconciliation_required":
        return "结果未知的调用待运营核对（reconciliation_required），预留暂不结算"
    return str(close.get("status"))


def cmd_sessions_show(args, cfg, out):
    view = _cloud(cfg).gateway.session(args.id)

    def text():
        lines = [f"会话 {view.get('session_id')}  模型 {view.get('model')}  状态 {view.get('status')}",
                 f"  已用 {view.get('used_tokens')}/{view.get('max_tokens')} token  在途 {view.get('inflight_tokens')}"
                 f"  调用 {view.get('calls')}  结果未知 {view.get('unknown_calls')}  超额 {view.get('overage_calls')}"
                 f"（{view.get('overage_tokens')} token）",
                 f"  预留 {view.get('reserved_credits')} 点（{view.get('reservation_id')}）  到期 {view.get('expires_at')}"]
        if view.get("inflight_tokens"):
            lines.append("  有调用仍在进行（或服务端正在读完断开的流）：结算后再查看")
        if view.get("close_result"):
            lines.append("  关闭结果：" + _close_text(view["close_result"]))
        return "\n".join(lines)

    out.result(view, text)
    return 0


# ------------------------------------------------------------------ simulation


def cmd_sim_up(args, cfg, out):
    from . import sim

    def ready(info):
        out.info(_sim_ready_text(info) + "\n前台运行中，Ctrl-C 结束仿真本体")

    info = sim.up(args.body, port=args.port, name=args.name, time_scale=args.time_scale,
                  state_dir=args.state_dir, foreground=args.foreground, make_default=args.default,
                  on_ready=ready)
    if not args.foreground:
        out.result(info, _sim_ready_text(info))
    else:
        out.result(info, f"仿真本体 {info['name']} 已退出")
    return 0


def _sim_ready_text(info: dict) -> str:
    return "\n".join([
        f"仿真本体已启动：{info['manifest_name']}（simulated=true）",
        f"  地址：{info['url']}    pid：{info['pid']}",
        f"  令牌：{info['token']}",
        f"  已添加为机器人 {info['name']}{'（默认机器人）' if info.get('default') else ''}。日志：{info['log']}",
        f"  试试：nx robot caps {info['name']}    结束：nx sim down {info['name']}"])


def cmd_sim_down(args, cfg, out):
    from . import sim

    names = [args.name] if args.name else [i["name"] for i in sim.running()]
    results = [sim.down(n) for n in names]
    out.result({"stopped": results}, "\n".join(f"已结束仿真本体 {r['name']}（pid {r['pid']}）" for r in results)
               or "没有正在运行的仿真本体")
    return 0


def cmd_sim_list(args, cfg, out):
    from . import sim

    items = sim.running()
    rows = [[i["name"], i["body"], i["url"], i["pid"], "运行中" if i["alive"] else "已退出"] for i in items]
    out.result({"sims": items}, lambda: table(["名称", "本体", "地址", "pid", "状态"], rows) if rows else "没有仿真本体")
    return 0


# ------------------------------------------------------------------ parser


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="输出机器可读的 JSON")

    parser = argparse.ArgumentParser(prog="nx", description="NeuxMind Robot OS 命令行：几分钟让任意机器人开始工作",
                                     parents=[common])
    parser.add_argument("--version", action="version", version=f"nx {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="<命令>")

    def add(subparsers, name, func, help_text, **kw):
        p = subparsers.add_parser(name, help=help_text, description=help_text, parents=[common], **kw)
        p.set_defaults(func=func)
        return p

    # config
    p_config = sub.add_parser("config", help="本地配置（~/.neuxmind/config.json）", parents=[common])
    cs = p_config.add_subparsers(dest="config_command", required=True, metavar="<子命令>")
    p = add(cs, "set", cmd_config_set, "设置配置项：cloud_url / org_id / default_robot")
    p.add_argument("key")
    p.add_argument("value")
    p = add(cs, "show", cmd_config_show, "显示配置（令牌已遮盖）")
    p.add_argument("key", nargs="?")
    p = add(cs, "get", cmd_config_show, "读取一个配置项")
    p.add_argument("key", nargs="?")

    # account
    p = add(sub, "register", cmd_register, "注册云平台账户并登录")
    p.add_argument("--email", required=True)
    p.add_argument("--name")
    p.add_argument("--password", help="不推荐：会留在 shell 历史里；可用 NEUXMIND_PASSWORD 或 --password-stdin")
    p.add_argument("--password-stdin", action="store_true")
    p = add(sub, "login", cmd_login, "登录云平台，令牌保存到配置文件（权限 600）")
    p.add_argument("--email")
    p.add_argument("--password", help="不推荐：会留在 shell 历史里；可用 NEUXMIND_PASSWORD 或 --password-stdin")
    p.add_argument("--password-stdin", action="store_true")
    add(sub, "logout", cmd_logout, "退出登录")
    add(sub, "whoami", cmd_whoami, "当前登录用户与组织")
    p = add(sub, "verify-email", cmd_verify_email, "在本人登录状态下验证邮箱（购买与调用网关前必须完成）")
    p.add_argument("token", nargs="?", help="验证邮件中的令牌（nxv_...）")
    p.add_argument("--resend", action="store_true", help="重新发送验证邮件（需已登录；旧令牌作废）")
    p = add(sub, "claim", cmd_claim, "认领被未验证账户占用的邮箱：用认领令牌新建已验证账户并登录")
    p.add_argument("token", help="认领邮件中的令牌")
    p.add_argument("--name")
    p.add_argument("--password", help="新密码；不推荐命令行传入，可用 NEUXMIND_PASSWORD 或 --password-stdin")
    p.add_argument("--password-stdin", action="store_true")
    p_reset = sub.add_parser("password-reset", help="找回密码", parents=[common])
    prs = p_reset.add_subparsers(dest="reset_command", required=True, metavar="<子命令>")
    p = add(prs, "request", cmd_password_reset_request, "申请重设密码邮件（不会暴露邮箱是否注册）")
    p.add_argument("--email")
    p = add(prs, "confirm", cmd_password_reset_confirm, "用重设令牌设置新密码并登录（注销其他会话）")
    p.add_argument("token")
    p.add_argument("--password", help="新密码；不推荐命令行传入，可用 NEUXMIND_PASSWORD 或 --password-stdin")
    p.add_argument("--password-stdin", action="store_true")

    # robots
    p_robot = sub.add_parser("robot", help="管理机器人", parents=[common])
    rs = p_robot.add_subparsers(dest="robot_command", required=True, metavar="<子命令>")
    p = add(rs, "add", cmd_robot_add, "添加机器人（端侧运行时地址 + 令牌）")
    p.add_argument("name")
    p.add_argument("url")
    p.add_argument("--token", help="BRAIN_EDGE_TOKEN；也可用环境变量 NEUXMIND_ROBOT_TOKEN")
    p.add_argument("--default", action="store_true", help="设为默认机器人")
    p.add_argument("--no-verify", action="store_true", help="不连接机器人验证")
    p = add(rs, "list", cmd_robot_list, "列出已添加的机器人（* 为默认）")
    p.add_argument("--check", action="store_true", help="同时检查是否在线")
    p = add(rs, "caps", cmd_robot_caps, "能力与参数表（标出仿真）")
    p.add_argument("name", nargs="?")
    p = add(rs, "status", cmd_robot_status, "健康、急停锁存、运行中的任务")
    p.add_argument("name", nargs="?")
    p = add(rs, "remove", cmd_robot_remove, "从本地配置移除")
    p.add_argument("name")
    p = add(rs, "register", cmd_robot_register, "在云端设备群登记该机器人（上传 manifest）")
    p.add_argument("name", nargs="?")
    p = add(rs, "conformance", cmd_robot_conformance,
            "在机器人上运行能力一致性测试（POST /v2/conformance）并显示每项能力结果；不等于现场验收")
    p.add_argument("name", nargs="?", help="机器人名称（默认机器人）")
    p.add_argument("--level", choices=("static", "dry", "motion"), default="dry",
                   help="static=只查清单；dry=不运动的检查（默认）；motion=最小幅度真实动作（需要 --authorize）")
    p.add_argument("--authorize", action="store_true", help="motion 级别必需：确认现场安全后再加")
    p.add_argument("--wait", action="store_true", help="轮询直到测试结束并显示报告")
    p.add_argument("--wait-timeout", type=float, default=900.0, help="--wait 最长等待秒数（不会中止测试）")
    p.add_argument("--id", help="查看已有的一致性测试（不新开测试）")
    p.add_argument("--upload", action="store_true",
                   help="把报告摘要上传到云端设备群（自动按当前 manifest 登记/刷新该机器人；隐含 --wait）")

    # adapters
    p_adapter = sub.add_parser("adapter", help="生成与测试机器人适配器（本体适配）", parents=[common])
    ads = p_adapter.add_subparsers(dest="adapter_command", required=True, metavar="<子命令>")
    p = add(ads, "new", cmd_adapter_new, "用运行时脚手架生成适配器包（adapter.py + adapter.json + tests + README）")
    p.add_argument("name", help="Python 包名，如 acme_rover")
    p.add_argument("--from-manifest", metavar="FILE|ROBOT", help="从 manifest JSON 文件或已添加机器人的 /v2/manifest 复制能力与限值")
    p.add_argument("--caps", action="append", metavar="ID,ID,...", help="能力列表（逗号分隔，可重复）")
    p.add_argument("--family", help="本体类型（--caps 时默认 custom）")
    p.add_argument("--vendor", help="厂商（来自本体档案）")
    p.add_argument("--model", help="型号（来自本体档案）")
    p.add_argument("--impl", action="append", metavar="CAP=HINT",
                   help="实现提示：ros2[:话题/动作] | sdk[:名] | serial[:设备] | http[:地址] | todo；可重复")
    p.add_argument("--param", action="append", metavar="CAP.PARAM=JSON", help="能力参数（安全限值，来自规格书）；可重复")
    p.add_argument("--instance-id")
    p.add_argument("--simulated", action="store_true", help="生成的本体标为仿真（simulated=true）")
    p.add_argument("--out", help="输出目录（默认 ./<name>）")
    p.add_argument("--force", action="store_true", help="输出目录非空时覆盖")
    p = add(ads, "test", cmd_adapter_test,
            "在本机进程内对适配器跑能力一致性测试（static | dry | motion）。注意：导入适配器包即以你的权限执行其全部代码"
            "（static 也一样，网络与文件不隔离），只测试你信任的适配器；报告由同一进程产生，适配器代码可以篡改它")
    p.add_argument("path", help="适配器包目录（nx adapter new 生成的），或 sim_quadruped / sim_arm / module:Class")
    p.add_argument("--level", choices=("static", "dry", "motion"), default="static")
    p.add_argument("--authorize", action="store_true", help="motion 级别必需：确认现场安全后再加")
    p.add_argument("--time-scale", type=float, default=1.0, help="仅仿真本体：仿真时间倍速")
    p.add_argument("--out", help="把 conformance.report.v1 写入该文件")

    # skills
    p_skill = sub.add_parser("skill", help="开发、检查、安装、运行技能", parents=[common])
    ss = p_skill.add_subparsers(dest="skill_command", required=True, metavar="<子命令>")
    from .templates import TEMPLATES

    p = add(ss, "new", cmd_skill_new, "从模板创建技能目录")
    p.add_argument("id", help="技能 ID，如 my-patrol")
    p.add_argument("--template", choices=TEMPLATES, default="blank")
    p.add_argument("--dir", help="目标目录（默认 ./<id>）")
    p.add_argument("--title")
    p.add_argument("--force", action="store_true")
    p = add(ss, "check", cmd_skill_check, "检查技能与机器人是否兼容（spec.check_compatibility）")
    p.add_argument("dir")
    p.add_argument("--robot", action="append", help="可重复；默认用默认机器人")
    p.add_argument("--body", action="append", choices=("quadruped", "arm"), help="离线对照仿真本体清单，可重复")
    p.add_argument("--all", action="store_true", help="检查所有已添加的机器人")
    p = add(ss, "install", cmd_skill_install, "打包并安装到机器人")
    p.add_argument("dir")
    p.add_argument("--robot")
    p = add(ss, "list", cmd_skill_list, "机器人上已安装的技能")
    p.add_argument("--robot")
    p = add(ss, "run", cmd_skill_run, "运行技能（需要 --authorize）")
    p.add_argument("id")
    p.add_argument("--robot")
    p.add_argument("--param", action="append", metavar="K=V", help="值按 JSON 解析，失败按字符串；可重复")
    p.add_argument("--params-json", help="整个参数对象的 JSON")
    p.add_argument("--authorize", action="store_true", help="发送硬件运动授权（确认现场安全后再加）")
    p.add_argument("--wait", action="store_true", help="轮询直到运行结束")
    p.add_argument("--wait-timeout", type=float, default=600.0, help="--wait 最长等待秒数（不会取消运行）")
    p.add_argument("--timeout-s", type=float, help="本次运行超时（机器人时间，不能超过技能声明）")
    p.add_argument("--version", dest="version", help="指定已安装版本")
    p = add(ss, "status", cmd_skill_status, "查看运行状态；不带 run_id 时列出最近的运行")
    p.add_argument("run_id", nargs="?")
    p.add_argument("--robot")
    p.add_argument("--limit", type=int, default=20)
    p = add(ss, "cancel", cmd_skill_cancel, "取消一个运行（不锁存急停）")
    p.add_argument("run_id")
    p.add_argument("--robot")

    # safety
    p = add(sub, "stop", cmd_stop, "立即停止机器人（永远可用，不需要授权；之后急停锁存）")
    p.add_argument("robot", nargs="?")
    p.add_argument("--reason")
    p = add(sub, "reset", cmd_reset, "解除急停锁存（需要 --authorize）")
    p.add_argument("robot", nargs="?")
    p.add_argument("--authorize", action="store_true")

    # cloud
    p = add(sub, "wallet", cmd_wallet, "钱包余额（token 点）")
    p.add_argument("--ledger", action="store_true", help="同时显示账本")
    p.add_argument("--limit", type=int, default=20)
    p_models = sub.add_parser("models", help="模型市场", parents=[common])
    ms = p_models.add_subparsers(dest="models_command", required=True, metavar="<子命令>")
    p = add(ms, "list", cmd_models_list, "列出模型；--robot 按该机器人能力清单过滤")
    p.add_argument("--robot")
    p.add_argument("--require-level", choices=("static", "dry"),
                   help="只把云端最新一致性报告中该级别已通过的能力视为可用（需先 nx robot conformance --upload）")
    p.add_argument("--kind", choices=("llm", "vla", "vision", "speech", "policy"))
    p = add(ms, "buy", cmd_models_buy, "用钱包点数购买模型授权")
    p.add_argument("id")
    p = add(ms, "install", cmd_models_install,
            "把已授权模型的端侧包安装到机器人（云端取包 → 机器人 model.install → 等待完成；需要 --authorize）")
    p.add_argument("id", help="模型 ID（云端模型市场）")
    p.add_argument("--robot")
    p.add_argument("--authorize", action="store_true", help="现场确认可以变更机器人上的软件（不会运动）")
    p.add_argument("--wait-timeout", type=float, default=1800.0, help="最长等待秒数（超时不取消安装）")
    p = add(ms, "installed", cmd_models_installed, "机器人上已安装的端侧模型（GET /v2/models）")
    p.add_argument("--robot")
    p = add(ms, "remove", cmd_models_remove, "从机器人删除端侧模型（模型 ID 或 SHA-256；需要 --authorize）")
    p.add_argument("model", help="云端模型 ID 或已安装模型的 SHA-256")
    p.add_argument("--robot")
    p.add_argument("--authorize", action="store_true", help="现场确认可以变更机器人上的软件")
    p = add(sub, "infer", cmd_infer, "通过云端网关调用模型")
    p.add_argument("model")
    p.add_argument("prompt")
    p.add_argument("--max-tokens", type=int, default=256, help="输出上限（服务端按预留再限制，只能调小）")
    p.add_argument("--option", action="append", metavar="K=V",
                   help="其他生成参数；网关只接受白名单，不允许的参数会被明确拒绝（n 只能为 1）")

    p = add(sub, "chat", cmd_chat, "开计量会话 → 调用模型（可流式）→ 关闭会话并打印计量")
    p.add_argument("model")
    p.add_argument("prompt")
    p.add_argument("--stream", action="store_true", help="流式输出（逐字打印）")
    p.add_argument("--max-tokens", type=int, default=1024,
                   help="会话预算（输入 + 输出的 token 上限，开会话时按它预留）；本次输出上限由服务端按剩余预算设置")
    p.add_argument("--api", choices=("chat", "responses"), default="chat", help="chat/completions 或 responses")
    p.add_argument("--max-minutes", type=int, help="会话时间上限（1–120 分钟，默认 15）")
    p_sessions = sub.add_parser("sessions", help="网关计量会话", parents=[common])
    gs = p_sessions.add_subparsers(dest="sessions_command", required=True, metavar="<子命令>")
    p = add(gs, "show", cmd_sessions_show, "查看会话状态与用量（GET /gateway/sessions/{id}）")
    p.add_argument("id")

    # telemetry
    p_tel = sub.add_parser("telemetry", help="机器人遥测事件", parents=[common])
    ts = p_tel.add_subparsers(dest="telemetry_command", required=True, metavar="<子命令>")
    p = add(ts, "tail", cmd_telemetry_tail,
            "显示最近的遥测事件（增量拉取 /v2/telemetry?since=；--run 时回放 /v2/runs/{id}/events）")
    p.add_argument("--robot")
    p.add_argument("--run", help="只看某次运行的事件（跨重启回放）")
    p.add_argument("--follow", "-f", action="store_true",
                   help="持续跟踪新事件；带 --run 时运行结束后自动退出；--json 时每行一个 JSON 事件")
    p.add_argument("--since", type=int, help="从该 seq 之后开始（默认：最近 --lines 条；--run 时为整个运行）")
    p.add_argument("--lines", "-n", type=int, help="先显示最近多少条（默认 20；--run 时默认全部）")
    p.add_argument("--interval", type=float, default=0.5, help="--follow 轮询间隔（秒）")
    p.add_argument("--duration", type=float, help="--follow 最长跟踪秒数（默认一直跟踪）")

    # sim
    p_sim = sub.add_parser("sim", help="本地仿真本体", parents=[common])
    sms = p_sim.add_subparsers(dest="sim_command", required=True, metavar="<子命令>")
    p = add(sms, "up", cmd_sim_up, "启动仿真本体并自动添加为机器人")
    p.add_argument("--body", choices=("quadruped", "arm"), default="quadruped")
    p.add_argument("--port", type=int, default=8790, help="0 = 自动选择空闲端口")
    p.add_argument("--name", help="机器人名称（默认 sim-<body>）")
    p.add_argument("--time-scale", type=float, default=1.0, help="仿真时间倍速")
    p.add_argument("--state-dir", type=Path)
    p.add_argument("--foreground", action="store_true", help="前台运行，Ctrl-C 结束")
    p.add_argument("--default", action="store_true", help="设为默认机器人（没有默认机器人时自动设为默认）")
    p = add(sms, "down", cmd_sim_down, "结束仿真本体（不带名称则结束全部）")
    p.add_argument("name", nargs="?")
    add(sms, "list", cmd_sim_list, "列出仿真本体")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:          # argparse: --help (0) or usage error (2)
        return int(exc.code or 0)
    out = Output(getattr(args, "json", False))
    try:
        cfg = config.load()
        return int(args.func(args, cfg, out) or 0)
    except NeuxMindError as exc:
        if out.as_json:
            print(json.dumps({"ok": False, **exc.to_dict()}, ensure_ascii=False, indent=2, default=str))
        else:
            print(f"错误：{exc.message}（代码 {exc.code}）", file=sys.stderr)
            if exc.details.get("parameters"):
                print("  不允许的参数：" + ", ".join(map(str, exc.details["parameters"])), file=sys.stderr)
            compat = exc.details.get("compatibility")
            if compat:
                print("  兼容性：" + json.dumps(compat, ensure_ascii=False), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中断（机器人上的运行不受影响；需要停止请 nx stop）", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
