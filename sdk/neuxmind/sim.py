"""``nx sim up|down|list``: run the runtime's simulated bodies as local processes.

``nx sim up`` launches ``python3 -m neuxmind_runtime serve --adapter sim_<body>``
(the runtime's own CLI) with a fresh random ``BRAIN_EDGE_TOKEN``, waits until
the gateway answers, and records it as a robot in the nx config. The process
runs in the background (log in ``$NEUXMIND_HOME/sim/<name>.log``) unless
``foreground=True``.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import _bootstrap, config
from .errors import NeuxMindError
from .robot import RobotClient
from .skill.testing import adapter_name

BODY_CHOICES = ("quadruped", "arm")
START_TIMEOUT_S = 20.0


def sim_dir() -> Path:
    return config.home() / "sim"


def _info_path(name: str) -> Path:
    return sim_dir() / f"{name}.json"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # Reap if it is our own zombie child.
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
        return done == 0
    except ChildProcessError:
        return True


def free_port(host: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def _port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError:
            return True
    return False


def _tail(path: Path, lines: int = 15) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def up(body: str, *, port: int = 8790, name: str | None = None, time_scale: float = 1.0,
       host: str = "127.0.0.1", state_dir: str | Path | None = None, foreground: bool = False,
       make_default: bool = False, on_ready=None) -> dict[str, Any]:
    """Start a simulated body and register it as robot ``name`` (default ``sim-<body>``)."""
    if body not in BODY_CHOICES:
        raise NeuxMindError(f"--body 须为 {' / '.join(BODY_CHOICES)}", code="bad_request")
    name = name or f"sim-{body}"
    runtime_dir = _bootstrap.runtime_dir()
    info_file = _info_path(name)
    if info_file.is_file():
        old = json.loads(info_file.read_text(encoding="utf-8"))
        if _alive(int(old.get("pid", 0))):
            raise NeuxMindError(f"仿真本体 {name} 已在运行（pid {old['pid']}，{old['url']}）；先 nx sim down {name}",
                                code="already_running")
    if port == 0:
        port = free_port(host)
    elif _port_in_use(host, port):
        raise NeuxMindError(f"端口 {port} 已被占用；换一个 --port（或 --port 0 自动选择）", code="port_in_use")

    if state_dir is None:
        state_dir = sim_dir() / name / "state"
        shutil.rmtree(state_dir, ignore_errors=True)        # fresh simulation each time
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    env = dict(os.environ, BRAIN_EDGE_TOKEN=token)
    env["PYTHONPATH"] = os.pathsep.join([str(runtime_dir)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    cmd = [sys.executable, "-m", "neuxmind_runtime", "serve", "--adapter", adapter_name(body),
           "--host", host, "--port", str(port), "--state-dir", str(state_dir)]
    if time_scale != 1.0:
        cmd += ["--time-scale", str(time_scale)]

    sim_dir().mkdir(parents=True, exist_ok=True)
    log_path = sim_dir() / f"{name}.log"
    log = open(log_path, "w", encoding="utf-8")
    try:
        proc = subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                start_new_session=not foreground)
    finally:
        log.close()

    url = f"http://{host}:{port}"
    client = RobotClient(url, token, timeout=2.0)
    deadline = time.monotonic() + START_TIMEOUT_S
    manifest = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise NeuxMindError(f"仿真本体启动失败（退出码 {proc.returncode}）：\n{_tail(log_path)}",
                                code="sim_failed")
        try:
            manifest = client.manifest()
            break
        except NeuxMindError:
            time.sleep(0.1)
    if manifest is None:
        proc.terminate()
        raise NeuxMindError(f"仿真本体在 {START_TIMEOUT_S} 秒内没有就绪：\n{_tail(log_path)}", code="sim_failed")

    info = {"name": name, "body": body, "pid": proc.pid, "url": url, "port": port, "log": str(log_path),
            "state_dir": str(state_dir), "time_scale": time_scale, "instance_id": manifest["instance_id"],
            "simulated": manifest.get("simulated") is True, "started_at": time.time()}
    info_file.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    data = config.load()
    data["robots"][name] = {"url": url, "token": token, "instance_id": manifest["instance_id"],
                            "manifest_name": manifest["name"], "family": manifest["family"],
                            "simulated": manifest.get("simulated") is True, "sim": True}
    if make_default or not data.get("default_robot"):
        data["default_robot"] = name
    config.save(data)
    result = {**info, "token": token, "manifest_name": manifest["name"],
              "default": data.get("default_robot") == name}
    if foreground:
        if on_ready is not None:
            on_ready(result)
        try:
            proc.wait()
        except KeyboardInterrupt:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
        info_file.unlink(missing_ok=True)
        result["exit_code"] = proc.returncode
    return result


def down(name: str) -> dict[str, Any]:
    """Stop a background simulated body started by :func:`up` (graceful SIGINT, then SIGTERM)."""
    info_file = _info_path(name)
    if not info_file.is_file():
        raise NeuxMindError(f"没有名为 {name} 的仿真本体（nx sim list 查看）", code="not_found")
    info = json.loads(info_file.read_text(encoding="utf-8"))
    pid = int(info["pid"])
    stopped = False
    if _alive(pid):
        try:
            os.kill(pid, signal.SIGINT)
            for _ in range(50):
                if not _alive(pid):
                    break
                time.sleep(0.1)
            if _alive(pid):
                os.kill(pid, signal.SIGTERM)
                time.sleep(0.2)
            stopped = True
        except ProcessLookupError:
            pass
    info_file.unlink(missing_ok=True)
    return {"name": name, "pid": pid, "stopped": stopped}


def running() -> list[dict[str, Any]]:
    out = []
    if not sim_dir().is_dir():
        return out
    for file in sorted(sim_dir().glob("*.json")):
        try:
            info = json.loads(file.read_text(encoding="utf-8"))
        except ValueError:
            continue
        info["alive"] = _alive(int(info.get("pid", 0)))
        out.append(info)
    return out
