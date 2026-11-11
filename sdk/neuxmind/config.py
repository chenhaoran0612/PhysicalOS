"""Local configuration for ``nx``: ``~/.neuxmind/config.json``.

``NEUXMIND_HOME`` overrides the directory (handy for tests and CI). The file
holds secrets (cloud session token, robot tokens), so it is written with
mode ``0600`` and its directory with ``0700``.

Shape::

    {"cloud_url": "http://127.0.0.1:8800",
     "cloud_token": "nxs_...", "cloud_email": "me@example.com", "org_id": null,
     "robots": {"sim-quadruped": {"url": "http://127.0.0.1:8790", "token": "...",
                                  "instance_id": "sim-quadruped-01", "manifest_name": "...",
                                  "family": "quadruped", "simulated": true}},
     "default_robot": "sim-quadruped"}
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .errors import NeuxMindError

SETTABLE_KEYS = ("cloud_url", "org_id", "default_robot")
SECRET_KEYS = ("cloud_token", "token")


def home() -> Path:
    override = os.environ.get("NEUXMIND_HOME")
    return Path(override).expanduser() if override else Path.home() / ".neuxmind"


def config_path() -> Path:
    return home() / "config.json"


def load() -> dict[str, Any]:
    path = config_path()
    if not path.is_file():
        return {"robots": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise NeuxMindError(f"配置文件 {path} 无法读取：{exc}", code="config_error") from None
    if not isinstance(data, dict):
        raise NeuxMindError(f"配置文件 {path} 格式错误", code="config_error")
    data.setdefault("robots", {})
    return data


def save(data: dict[str, Any]) -> Path:
    """Atomically write the config with owner-only permissions."""
    directory = home()
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    path = config_path()
    fd, tmp = tempfile.mkstemp(prefix=".config.", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.chmod(path, 0o600)
    return path


def masked(data: dict[str, Any]) -> dict[str, Any]:
    """Copy of the config with secrets shortened, for display."""
    def mask(value: Any) -> Any:
        if isinstance(value, str) and value:
            return value[:6] + "…" if len(value) > 8 else "…"
        return value

    out = {k: (mask(v) if k in SECRET_KEYS else v) for k, v in data.items() if k != "robots"}
    out["robots"] = {name: {k: (mask(v) if k in SECRET_KEYS else v) for k, v in robot.items()}
                     for name, robot in data.get("robots", {}).items()}
    return out


def get_robot(data: dict[str, Any], name: str | None) -> tuple[str, dict]:
    robots = data.get("robots", {})
    name = name or data.get("default_robot")
    if not name:
        if len(robots) == 1:
            name = next(iter(robots))
        else:
            raise NeuxMindError("请用 --robot 指定机器人（nx robot list 查看已添加的机器人）", code="config_error")
    if name not in robots:
        raise NeuxMindError(f"未添加机器人 {name}；先运行 nx robot add {name} <url> --token ...",
                            code="robot_not_found")
    return name, robots[name]
