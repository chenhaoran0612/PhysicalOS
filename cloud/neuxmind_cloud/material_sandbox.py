"""Run the rules parser (``material_extract.py``) on one stored file, in a child process (issue #34).

The child gets the file as its standard input (opened here without following
symbolic links), an almost empty environment, ``/`` as its working directory
and its own process group. It caps its own CPU time, memory (Linux), file size
and open files before it reads anything (``material_extract.apply_limits``);
this side adds a wall-clock limit and a cap on what it may print, and kills the
whole group when either is exceeded. Whatever happens in the child — a parser
bug, a hostile file, an exhausted limit — ends as :class:`ExtractFailed` for
that one material; the cloud process itself never parses the file.
"""
from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import sys
import time
from dataclasses import dataclass

WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "material_extract.py")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_ENV = {"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}
_STDERR_KEPT = 4000


@dataclass(frozen=True)
class Limits:
    wall_seconds: float = 60.0
    cpu_seconds: int = 30
    memory_bytes: int = 512 * 1024 * 1024
    unpacked_bytes: int = 4 * 1024 ** 3
    members: int = 100_000
    urdf_bytes: int = 32 * 1024 * 1024
    output_bytes: int = 4 * 1024 * 1024
    xz_memory_bytes: int = 128 * 1024 * 1024
    html_bytes: int = 32 * 1024 * 1024            # issue #45: the largest page that is read
    html_cpu_seconds: int = 10                    # less than for an archive: a small page must not hold a worker long
    html_text_bytes: int = 1024 * 1024            # and how much text is taken out of it (fits output_bytes when escaped)

    def for_child(self) -> dict:
        return {"cpu": self.cpu_seconds, "memory": self.memory_bytes, "unpacked": self.unpacked_bytes,
                "members": self.members, "urdf_bytes": self.urdf_bytes, "depth": 64, "elements": 200_000,
                "xz_memory": self.xz_memory_bytes, "html_bytes": self.html_bytes, "html_text": self.html_text_bytes,
                "html_cpu": min(self.html_cpu_seconds, self.cpu_seconds)}


class ExtractFailed(Exception):
    """The file could not be read by rules; ``message`` says why in words for the page."""

    def __init__(self, code: str, message: str, detail: str = ""):
        super().__init__(message)
        self.code, self.message, self.detail = code, message, detail


def _kill(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _collect(process: subprocess.Popen, deadline: float, cap: int) -> tuple[bytes, bytes]:
    """Read the child's output until it closes both pipes; kill it on the deadline or when it prints too much."""
    out, err = bytearray(), bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ, out)
        selector.register(process.stderr, selectors.EVENT_READ, err)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _kill(process)
                raise ExtractFailed("parse_timeout", "解析超过了时间上限，没有完成（文件可能太大或结构异常）")
            for key, _ in selector.select(min(remaining, 1.0)):
                block = os.read(key.fileobj.fileno(), 65536)
                if not block:
                    selector.unregister(key.fileobj)
                    continue
                key.data.extend(block)
                if len(out) > cap:
                    _kill(process)
                    raise ExtractFailed("parse_output", "解析结果大得不正常，没有采用")
                if len(err) > _STDERR_KEPT:
                    del err[_STDERR_KEPT:]
    return bytes(out), bytes(err)


def extract(mode: str, path: str, limits: Limits = Limits()) -> dict:
    """The facts the rules parser found in the file at ``path`` (read in mode ``mode``)."""
    fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
    try:
        process = subprocess.Popen([sys.executable, "-I", "-S", WORKER, mode, json.dumps(limits.for_child())],
                                   stdin=fd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_ENV, cwd="/",
                                   close_fds=True, start_new_session=True)
    finally:
        os.close(fd)
    try:
        out, err = _collect(process, time.monotonic() + limits.wall_seconds, limits.output_bytes)
        code = process.wait(timeout=max(1.0, limits.wall_seconds))
    except subprocess.TimeoutExpired:
        _kill(process)
        raise ExtractFailed("parse_timeout", "解析超过了时间上限，没有完成") from None
    finally:
        for pipe in (process.stdout, process.stderr):
            pipe.close()
        if process.poll() is None:
            _kill(process)
            process.wait()
    if code == -signal.SIGXCPU or code == -signal.SIGKILL:
        raise ExtractFailed("parse_timeout", "解析超过了 CPU 时间上限，没有完成", err.decode("utf-8", "replace"))
    try:
        result = json.loads(out.decode("utf-8")) if code == 0 else None
    except (UnicodeDecodeError, ValueError):
        result = None
    if not isinstance(result, dict):
        raise ExtractFailed("parse_crashed", "解析程序异常退出，这份资料没有解析出来",
                            f"exit {code}: {err.decode('utf-8', 'replace')[-_STDERR_KEPT:]}")
    if not result.get("ok"):
        raise ExtractFailed(str(result.get("code") or "parse_refused"), str(result.get("error") or "没有解析出来"))
    facts = result.get("facts")
    if not isinstance(facts, dict):
        raise ExtractFailed("parse_crashed", "解析结果格式不对")
    return facts
