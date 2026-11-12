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
    python3 scripts/demo.py seed [URL]   # seed a cloud that is already running

NX_DEMO_CLOUD=docker runs the cloud platform in Docker instead (cloud/compose.yaml:
PostgreSQL + the cloud API); its database is kept between runs.

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
DEMO_CREDITS = 50000

# Where the cloud platform runs: "local" = a process from this source tree (SQLite,
# recreated on every `up`); "docker" = cloud/compose.yaml (PostgreSQL, data kept).
CLOUD_MODE = os.environ.get("NX_DEMO_CLOUD", "local")
COMPOSE_FILE = ROOT / "cloud" / "compose.yaml"
DOCKER_CLOUD_MARK = DEMO / "cloud.docker"

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
    """Demo operator, demo user, catalog models and credits.

    Safe to repeat: the Docker cloud keeps its database between starts, so every
    step accepts what an earlier run already created and books the credits only once.
    """
    api = base + "/api/v1"

    def verify(email, token, dev):
        s, d = http("POST", api + "/auth/verify-email", {"token": dev}, token)
        if s != 200:
            raise RuntimeError(f"验证 {email} 失败：{s} {d}")

    def account(email):
        status, data = http("POST", api + "/auth/login", {"email": email, "password": DEMO_PASSWORD})
        if status == 200:
            if not data.get("user", {}).get("email_verified"):
                # An earlier seed stopped between registering and verifying.
                s, resent = http("POST", api + "/auth/verify-email/resend", token=data["token"])
                dev = resent.get("dev_verification_token")
                if not dev:
                    raise RuntimeError(f"{email} 的邮箱未验证，云平台也没有返回开发验证令牌（不是 dev 环境？）：{s} {resent}")
                verify(email, data["token"], dev)
            return data["token"]
        status, data = http("POST", api + "/auth/register", {"email": email, "password": DEMO_PASSWORD, "name": email.split("@")[0]})
        if status == 409 and data.get("code") == "email_taken":
            raise RuntimeError(f"{email} 已存在，但不能用演示密码登录，这个数据库里不是本脚本写入的演示数据。"
                               f"要重建 Docker 云平台的演示数据库：docker compose -f cloud/compose.yaml down -v")
        if status != 201:
            raise RuntimeError(f"注册 {email} 失败：{status} {data}")
        dev = data.get("dev_verification_token")
        if dev:
            verify(email, data["token"], dev)
        return data["token"]

    ops_token = account(OPS_EMAIL)
    for card in DEMO_MODELS:
        s, d = http("POST", api + "/admin/models", card, ops_token)
        if s not in (200, 201) and d.get("code") != "model_exists":
            raise RuntimeError(f"上架模型 {card['id']} 失败：{s} {d}")
    user_token = account(DEMO_EMAIL)
    s, wallet = http("GET", api + "/wallet", token=user_token)
    if s != 200:
        raise RuntimeError(f"读取演示钱包失败：{s} {wallet}")
    # The ledger books a reference once, so repeating this adjustment adds nothing
    # (and refuses a different amount: changing DEMO_CREDITS needs a fresh database).
    s, d = http("POST", api + f"/admin/wallets/{wallet['org_id']}/adjust",
                {"delta": DEMO_CREDITS, "reason": "演示额度", "reference": "demo-seed-credits"}, ops_token)
    if s not in (200, 201):
        raise RuntimeError(f"充值演示额度失败：{s} {d}")
    return {"operator": OPS_EMAIL, "user": DEMO_EMAIL, "password": DEMO_PASSWORD, "credits": DEMO_CREDITS}


def compose(*args: str) -> None:
    env = dict(os.environ, NEUXMIND_CLOUD_PORT=str(CLOUD_PORT))
    subprocess.run(["docker", "compose", "-f", str(COMPOSE_FILE), *args], cwd=str(ROOT), env=env, check=True)


def start_docker_cloud(base: str) -> dict:
    """PostgreSQL + the cloud API from cloud/compose.yaml, seeded from here."""
    # Marked first, so that `down` also removes containers of a start that failed half-way.
    DOCKER_CLOUD_MARK.touch()
    try:
        # Only db + cloud: the compose file's own `seed` job would race the seeding below.
        compose("up", "-d", "--build", "--wait", "cloud")
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"Docker 云平台启动失败（{exc}）。确认 Docker 正在运行；"
                         f"日志：docker compose -f {COMPOSE_FILE} logs cloud")
    if not wait_for(base + "/api/v1/capabilities/registry", seconds=60):
        raise SystemExit(f"Docker 云平台启动失败，见 docker compose -f {COMPOSE_FILE} logs cloud")
    return {"url": base, "docker": True, **seed_cloud(base)}


def require_free_cloud_port(base: str) -> None:
    """The local cloud must own its port. Anything else answering there (e.g. the Docker
    cloud started by hand) would be seeded and shown as the fresh local one."""
    try:
        http("GET", base + "/healthz", timeout=1)
    except Exception:
        return
    raise SystemExit(f"{base} 已有服务在响应（可能是 Docker 云平台）。改用 NX_DEMO_CLOUD=docker python3 scripts/demo.py up，"
                     f"或先停止它：docker compose -f {COMPOSE_FILE} down")


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

    base = f"http://127.0.0.1:{CLOUD_PORT}"
    if CLOUD_MODE == "docker":
        info["cloud"] = start_docker_cloud(base)
    elif cloud_available():
        require_free_cloud_port(base)
        cenv = dict(env, NEUXMIND_ENV="dev", NEUXMIND_OPERATOR_EMAILS=OPS_EMAIL,
                    DATABASE_URL=f"sqlite:///{DEMO/'cloud.sqlite3'}",
                    NEUXMIND_PUBLIC_BASE_URL=f"http://127.0.0.1:{CLOUD_PORT}")
        spawn("cloud", [py, "-m", "neuxmind_cloud", "serve", "--host", "127.0.0.1", "--port", str(CLOUD_PORT)], cenv, pids)
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
        where = "（Docker：cloud/compose.yaml，数据库保留在卷中）" if c.get("docker") else ""
        print(f"4) 云平台：地址 {c['url']} {where}")
        print(f"   演示账号 {c['user']}   密码 {c['password']}   （已验证邮箱，钱包 {c['credits']} 点）")
        print(f"   运营账号 {c['operator']}   密码 {c['password']}")
    else:
        print("4) 云平台未启动（缺依赖）。需要演示云平台时先运行：")
        print(f"   {info['cloud_missing']}")
    print(f"\n日志目录：{DEMO/'logs'}   停止：python3 scripts/demo.py down\n")


def down(quiet: bool = False) -> None:
    docker_cloud = DOCKER_CLOUD_MARK.exists()
    if not PIDS.exists() and not docker_cloud:
        if not quiet:
            print("没有正在运行的演示环境")
        return
    if PIDS.exists():
        stop_processes(json.loads(PIDS.read_text()))
        PIDS.unlink()
    if docker_cloud:
        try:
            compose("down")         # containers only; the database volume stays
        except (OSError, subprocess.CalledProcessError) as exc:
            # Docker missing or not running: do not block the next up / down on it.
            print(f"警告：没能停止 Docker 云平台（{exc}）。Docker 可用后执行：docker compose -f {COMPOSE_FILE} down")
        DOCKER_CLOUD_MARK.unlink(missing_ok=True)
    if not quiet:
        print("演示环境已停止")


def stop_processes(pids: dict) -> None:
    for pid in pids.values():
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            continue
    time.sleep(1.0)
    for pid in pids.values():
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def seed(base: str = "") -> None:
    """Seed a cloud that is already running (the `seed` job of cloud/compose.yaml)."""
    base = (base or f"http://127.0.0.1:{CLOUD_PORT}").rstrip("/")
    if not wait_for(base + "/api/v1/capabilities/registry", seconds=60):
        raise SystemExit(f"云平台 {base} 没有响应")
    c = seed_cloud(base)
    print(f"云平台 {base} 演示数据已就绪：演示账号 {c['user']}，运营账号 {c['operator']}，"
          f"密码 {c['password']}，钱包 {c['credits']} 点，模型 {len(DEMO_MODELS)} 个")


def status() -> None:
    if not INFO.exists():
        print("演示环境未启动：python3 scripts/demo.py up")
        return
    print_info(json.loads(INFO.read_text()))


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "up"
    if command == "seed":
        seed(*sys.argv[2:3])
    else:
        {"up": up, "down": down, "status": status}.get(command, lambda: print(__doc__))()
