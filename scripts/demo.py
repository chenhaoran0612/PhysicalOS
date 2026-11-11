#!/usr/bin/env python3
"""One-command demo environment for NeuxMind Robot OS (simulation only).

Starts, from the source tree:
  * two simulated robots (edge runtime): a quadruped and a 6-axis arm
  * the cloud platform (dev mode) seeded with a demo operator, a demo user,
    credits and a few catalog models   -- only if its dependencies are installed
  * the desktop console backend (open it in a browser)

Usage (from the repository root, e.g. apps/robot-os):
    python3 scripts/demo.py up        # start everything, print URLs / tokens
    python3 scripts/demo.py status    # show what is running
    python3 scripts/demo.py down      # stop everything

Everything is local (127.0.0.1) and simulated. Nothing here touches real
hardware, real payment or a real model provider. State lives in .demo/ and is
recreated on every `up`.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / ".demo"
PIDS = DEMO / "pids.json"
INFO = DEMO / "demo.json"

DESKTOP_PORT = int(os.environ.get("NX_DEMO_DESKTOP_PORT", "8788"))
QUAD_PORT = int(os.environ.get("NX_DEMO_QUAD_PORT", "8790"))
ARM_PORT = int(os.environ.get("NX_DEMO_ARM_PORT", "8791"))
CLOUD_PORT = int(os.environ.get("NX_DEMO_CLOUD_PORT", "8800"))

OPS_EMAIL, DEMO_EMAIL = "ops@demo.local", "demo@demo.local"
DEMO_PASSWORD = "NeuxMind-demo-2026"

# Catalog models for the demo. Package references are placeholders: the demo
# shows catalog filtering and licensing, not real model downloads.
DEMO_MODELS = [
    {"id": "echo-llm", "name": "Echo LLM（演示替身，非真实模型）", "source": "closed", "kind": "llm",
     "license": "demo only", "pricing": {"type": "per_1k_tokens", "credits": 10}, "upstream": {"provider": "echo"}},
    {"id": "dog-gait", "name": "四足步态策略（演示）", "source": "native", "kind": "policy", "license": "NeuxMind EULA",
     "pricing": {"type": "license_once", "credits": 200}, "requires_capabilities": ["base.velocity"],
     "families": ["quadruped"], "package": {"ref": "nx://demo/gait:1", "sha256": "d" * 64, "runtime": "onnxruntime"}},
    {"id": "nav-assist", "name": "导航辅助（演示）", "source": "native", "kind": "policy", "license": "NeuxMind EULA",
     "pricing": {"type": "free"}, "requires_capabilities": ["base.navigate_to"],
     "package": {"ref": "nx://demo/nav:1", "sha256": "e" * 64, "runtime": "onnxruntime"}},
    {"id": "arm-grasp-vla", "name": "机械臂抓取 VLA（演示）", "source": "open", "kind": "vla", "license": "Apache-2.0",
     "pricing": {"type": "free"}, "requires_capabilities": ["arm.joint_position", "gripper.position"],
     "families": ["manipulator"], "package": {"ref": "hf://demo/grasp-vla@v1", "sha256": "b" * 64, "runtime": "onnxruntime"}},
]


def env_with_paths() -> dict:
    env = dict(os.environ)
    paths = [str(ROOT / p) for p in ("spec", "runtime", "cloud", "sdk")] + [str(ROOT)]
    if env.get("PYTHONPATH"):
        paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(paths)
    env.pop("HTTP_PROXY", None); env.pop("HTTPS_PROXY", None)
    env.pop("http_proxy", None); env.pop("https_proxy", None)
    env["NO_PROXY"] = "127.0.0.1,localhost"
    return env


def http(method: str, url: str, body=None, token: str = "", timeout: float = 10.0):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, {"raw": raw.decode(errors="replace")}


def wait_for(url: str, token: str = "", seconds: float = 30.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            status, _ = http("GET", url, token=token, timeout=2)
            if status < 500:
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def spawn(name: str, args: list, env: dict, pids: dict) -> None:
    log = open(DEMO / "logs" / f"{name}.log", "w")
    proc = subprocess.Popen(args, cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT,
                            start_new_session=True)
    pids[name] = proc.pid
    PIDS.write_text(json.dumps(pids, indent=2))


def cloud_available() -> bool:
    try:
        import fastapi, sqlalchemy, uvicorn, httpx  # noqa: F401
        return True
    except Exception:
        return False


def seed_cloud(base: str) -> dict:
    api = base + "/api/v1"
    def register(email):
        status, data = http("POST", api + "/auth/register", {"email": email, "password": DEMO_PASSWORD, "name": email.split("@")[0]})
        if status != 201:
            raise RuntimeError(f"注册 {email} 失败：{status} {data}")
        token = data["token"]
        dev = data.get("dev_verification_token")
        if dev:
            s, d = http("POST", api + "/auth/verify-email", {"token": dev}, token)
            if s != 200:
                raise RuntimeError(f"验证 {email} 失败：{s} {d}")
        return token, data
    ops_token, _ = register(OPS_EMAIL)
    for card in DEMO_MODELS:
        s, d = http("POST", api + "/admin/models", card, ops_token)
        if s not in (200, 201):
            raise RuntimeError(f"上架模型 {card['id']} 失败：{s} {d}")
    user_token, user = register(DEMO_EMAIL)
    org = user.get("org_id") or http("GET", api + "/me", token=user_token)[1].get("current_org_id")
    s, d = http("POST", api + f"/admin/wallets/{org}/adjust", {"delta": 50000, "reason": "演示额度"}, ops_token)
    if s not in (200, 201):
        raise RuntimeError(f"充值演示额度失败：{s} {d}")
    return {"operator": OPS_EMAIL, "user": DEMO_EMAIL, "password": DEMO_PASSWORD, "credits": 50000}


def up() -> None:
    down(quiet=True)
    if DEMO.exists():
        shutil.rmtree(DEMO)
    (DEMO / "logs").mkdir(parents=True)
    env = env_with_paths()
    py = sys.executable
    pids: dict = {}
    info: dict = {"python": py, "robots": {}, "cloud": None}

    # Process isolation for skills uses Linux-only system calls; on macOS the
    # demo runs skills in-process (debug mode) and says so.
    sandbox = "process" if sys.platform.startswith("linux") else "inprocess"
    for name, adapter, port in (("quadruped", "sim_quadruped", QUAD_PORT), ("arm", "sim_arm", ARM_PORT)):
        token = secrets.token_hex(16)
        renv = dict(env, BRAIN_EDGE_TOKEN=token)
        spawn(f"robot-{name}", [py, "-m", "neuxmind_runtime", "serve", "--adapter", adapter, "--port", str(port),
                                "--state-dir", str(DEMO / f"robot-{name}"), "--sandbox", sandbox], renv, pids)
        info["robots"][name] = {"url": f"http://127.0.0.1:{port}", "token": token, "adapter": adapter}
    for name, robot in info["robots"].items():
        if not wait_for(robot["url"] + "/v1/status", robot["token"]):
            raise SystemExit(f"仿真机器人 {name} 启动失败，见 {DEMO/'logs'/('robot-'+name+'.log')}")

    if cloud_available():
        cenv = dict(env, NEUXMIND_ENV="dev", NEUXMIND_OPERATOR_EMAILS=OPS_EMAIL,
                    DATABASE_URL=f"sqlite:///{DEMO/'cloud.sqlite3'}",
                    NEUXMIND_PUBLIC_BASE_URL=f"http://127.0.0.1:{CLOUD_PORT}")
        spawn("cloud", [py, "-m", "neuxmind_cloud", "serve", "--host", "127.0.0.1", "--port", str(CLOUD_PORT)], cenv, pids)
        base = f"http://127.0.0.1:{CLOUD_PORT}"
        if not wait_for(base + "/api/v1/capabilities/registry"):
            raise SystemExit(f"云平台启动失败，见 {DEMO/'logs'/'cloud.log'}")
        info["cloud"] = {"url": base, **seed_cloud(base)}
    else:
        info["cloud_missing"] = f"{py} -m pip install -r cloud/requirements.txt"

    spawn("desktop", [py, "-m", "brainos.server", "--port", str(DESKTOP_PORT), "--state-dir", str(DEMO / "desktop")],
          env, pids)
    if not wait_for(f"http://127.0.0.1:{DESKTOP_PORT}/api/health"):
        raise SystemExit(f"桌面控制台启动失败，见 {DEMO/'logs'/'desktop.log'}")
    info["desktop"] = f"http://127.0.0.1:{DESKTOP_PORT}"
    info["sandbox"] = sandbox
    info["examples"] = str(ROOT / "runtime" / "examples" / "skills")
    INFO.write_text(json.dumps(info, ensure_ascii=False, indent=2))
    print_info(info)


def print_info(info: dict) -> None:
    print("\n=== NeuxMind Robot OS 演示环境已启动（全部为本机仿真） ===\n")
    print(f"1) 桌面控制台：在浏览器打开  {info['desktop']}")
    print("   （首次进入先注册一个本地工作空间，任意邮箱 + 10 位以上密码）\n")
    print("2) 仿真机器人（在控制台“连接机器人”时填写）：")
    for name, r in info["robots"].items():
        label = "仿真四足" if name == "quadruped" else "仿真六轴机械臂"
        print(f"   {label}：地址 {r['url']}    令牌 {r['token']}")
    if info["sandbox"] != "process":
        print("   注意：本机不是 Linux，技能以进程内模式运行（仅演示用，生产环境用 Linux/Jetson 的进程隔离）")
    print(f"\n3) 示例技能包文件夹：{info['examples']}/patrol  （四足可装）、 …/wave（机械臂可装）、 …/status_report（都可装）\n")
    if info.get("cloud"):
        c = info["cloud"]
        print(f"4) 云平台：地址 {c['url']}")
        print(f"   演示账号 {c['user']}   密码 {c['password']}   （已验证邮箱，钱包 {c['credits']} 点）")
        print(f"   运营账号 {c['operator']}   密码 {c['password']}")
    else:
        print("4) 云平台未启动（缺依赖）。需要演示云平台时先运行：")
        print(f"   {info['cloud_missing']}")
    print(f"\n日志目录：{DEMO/'logs'}   停止：python3 scripts/demo.py down\n")


def down(quiet: bool = False) -> None:
    if not PIDS.exists():
        if not quiet:
            print("没有正在运行的演示环境")
        return
    for name, pid in json.loads(PIDS.read_text()).items():
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            continue
    time.sleep(1.0)
    for name, pid in json.loads(PIDS.read_text()).items():
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    PIDS.unlink()
    if not quiet:
        print("演示环境已停止")


def status() -> None:
    if not INFO.exists():
        print("演示环境未启动：python3 scripts/demo.py up")
        return
    print_info(json.loads(INFO.read_text()))


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "up"
    {"up": up, "down": down, "status": status}.get(command, lambda: print(__doc__))()
