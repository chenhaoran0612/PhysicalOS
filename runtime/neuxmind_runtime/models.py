"""Runtime-native ``model.install`` (INTERFACES_V1 §5.1).

With ``--models-dir`` the runtime itself provides ``model.install`` (unless the
adapter declares its own): it is added to the manifest with the configured
``runtimes`` and works on simulated bodies too.

Install flow (runs on the action's own worker thread, never on the stop path):

1. ``package_ref``: ``https://...``; ``http://`` only for hosts listed in
   ``allow_http_hosts`` (dev/test); ``file://`` only for files inside one of
   ``file_roots`` (resolved real path, regular file). Anything else is refused.
2. Streamed download into ``<models>/.partial/`` (size cap, default 2 GiB,
   checked against ``Content-Length`` and while streaming; per-operation socket
   timeout; at most ``max_redirects`` redirects, **never across schemes** --
   e.g. https → http or → file:// is refused -- and every hop re-checked).
3. SHA-256 computed while streaming; a mismatch deletes the temp file and fails.
4. Staged as ``<models>/.partial/<sha>-<rand>/{<file>, model.json}`` (files 0444,
   directory 0555), fsynced, then **atomically renamed** to ``<models>/<sha256>/``.
   The same sha installed twice (also concurrently) is idempotent.

An installed model is identified by its package's SHA-256 (content address,
directory name, ``DELETE /v2/models/<sha256>``, the ``model_id`` *output* of
``model.install``). The optional inputs ``model_id`` (catalog id) and ``name``
are recorded as metadata: ``/v2/models`` returns ``model_id`` (catalog id,
nullable), ``name`` and ``sha256``. Re-installing an installed sha is a no-op
(the first install's metadata is kept). Every ``model.install_*`` event carries
the ``run_id`` of the run that performs the install, so
``/v2/runs/{id}/events`` replays progress and the failure reason.

Stop: ``cancel()`` only sets an event and shuts the socket down (non-blocking);
the worker notices, deletes its temp files and never publishes a cancelled
install. Installing is ``safety: system``: it needs ``hardware_authorized=true``
(the operator confirms the on-device software may change) but moves nothing,
so it is allowed while the e-stop is latched and runs beside motion.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import ssl
import stat
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urljoin, urlsplit

from .errors import BadRequest, Conflict, NotFound
from .hal import CANCELLED, FAILED, RUNNING, SUCCEEDED, ActionHandle, ActionStatus

CAPABILITY = "model.install"
SHA_RE = re.compile(r"[0-9a-f]{64}")      # always used with fullmatch (Q3-12: "$" accepts a trailing newline)
CHUNK = 64 * 1024
PARTIAL = ".partial"
META = "model.json"
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


class ModelInstallError(Exception):
    """The install failed; ``str()`` is the run error."""


@dataclass(frozen=True)
class ModelConfig:
    """Runtime-wide model store settings (CLI ``--models-dir`` etc.)."""

    models_dir: str
    runtimes: tuple = ("onnxruntime",)
    accelerator: str | None = None
    max_bytes: int = 2 << 30                  # 2 GiB
    timeout_s: float = 30.0                   # connect and every socket read
    max_redirects: int = 5
    file_roots: tuple = ()                    # directories file:// packages may come from
    allow_http_hosts: tuple = ()              # plain http only for these hosts (dev / tests)
    ca_file: str | None = None                # extra CA bundle for https (private mirrors)
    progress_interval_s: float = 1.0          # model.install_progress event rate
    max_concurrent: int = 2                   # installs downloading at once (Q3-06); more -> 409 install_busy
    min_free_bytes: int = 256 << 20           # free space kept on the models filesystem (shared with state)
    max_download_s: float = 3600.0            # whole download, wall clock (Q3-10)
    allow_private_hosts: tuple = ()           # https to private/loopback/link-local only for these (Q3-09)

    def __post_init__(self):
        if not self.runtimes or not all(isinstance(r, str) and r for r in self.runtimes):
            raise ValueError("runtimes 须为非空字符串列表")
        if self.max_bytes <= 0 or self.timeout_s <= 0 or self.max_redirects < 0:
            raise ValueError("模型安装的大小上限、超时须为正数")
        if self.max_concurrent < 1 or self.min_free_bytes < 0 or self.max_download_s <= 0:
            raise ValueError("max_concurrent ≥ 1、min_free_bytes ≥ 0、max_download_s > 0")


class InstallBusy(Exception):
    """Too many installs at once (``max_concurrent``)."""


def _host_is_internal(host: str) -> str | None:
    """Why ``host`` (a name or literal) is internal, or None. Resolves names (Q3-09)."""
    import ipaddress
    try:
        infos = [host] if _is_ip(host) else sorted({ai[4][0] for ai in socket.getaddrinfo(host, None)})
    except (socket.gaierror, UnicodeError, OSError):
        return None                                   # unresolvable: the download fails on its own
    for addr in infos:
        ip = ipaddress.ip_address(addr.split("%")[0])
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        if str(ip) in ("169.254.169.254", "fd00:ec2::254", "100.100.100.200"):
            return f"云元数据地址 {ip}"
        if ip.is_loopback or ip.is_link_local or ip.is_private or ip.is_reserved or ip.is_unspecified \
                or ip.is_multicast or (ip.version == 4 and ip in ipaddress.ip_network("100.64.0.0/10")):
            return f"内网/本机/链路本地地址 {ip}"
    return None


def _is_ip(host: str) -> bool:
    import ipaddress
    try:
        ipaddress.ip_address(host.split("%")[0])
        return True
    except ValueError:
        return False


def _printable(value: str) -> bool:
    """No control, format (bidi overrides, zero-width) or separator characters (Q3-12)."""
    import unicodedata
    return all(unicodedata.category(c) not in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp") for c in value)


def _safe_filename(url_path: str) -> str:
    name = os.path.basename(unquote(url_path or ""))
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)[:100].lstrip(".")
    return name if name and name != META else "model.bin"


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _remove(path: Path) -> None:
    """Delete a (read-only) model tree; never follows symlinks."""
    try:
        st = os.lstat(path)
    except OSError:
        return
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        try:
            os.unlink(path)
        except OSError:
            pass
        return
    try:
        for _root, _dirs, _files, dirfd in os.fwalk(path, follow_symlinks=False):
            try:
                os.fchmod(dirfd, 0o700)
            except OSError:
                pass
    except OSError:
        pass
    shutil.rmtree(path, ignore_errors=True)


def _describe_failure(exc: BaseException) -> str:
    import errno
    import http.client
    if isinstance(exc, http.client.IncompleteRead):
        return f"下载不完整（服务器提前断开，收到 {len(exc.partial)} 字节）"
    if isinstance(exc, OSError):
        reasons = {errno.ENOSPC: "磁盘空间不足", errno.EDQUOT: "超出磁盘配额", errno.EACCES: "没有写权限",
                   errno.EROFS: "文件系统只读", errno.EIO: "磁盘 I/O 错误"}
        if exc.errno in reasons:
            return f"模型安装失败：{reasons[exc.errno]}（{exc.strerror}）"
    return f"模型安装失败：{type(exc).__name__}: {exc}"


class _RedirectPolicy(urllib.request.HTTPRedirectHandler):
    """Follow redirects only within the same scheme, each hop re-checked."""

    def __init__(self, store: "ModelStore"):
        super().__init__()
        self._store = store
        self.max_redirections = store.config.max_redirects

    def http_error_302(self, req, fp, code, msg, headers):
        # Checked before urllib's own handling, which turns a file:// target into a bare
        # "HTTP 302" error: say what was refused.
        location = headers.get("location") or headers.get("uri")
        if location:
            target = urljoin(req.full_url, location)
            old, new = urlsplit(req.full_url).scheme.lower(), urlsplit(target).scheme.lower()
            if new != old:
                raise ModelInstallError(f"拒绝跨协议重定向（{old} → {new}）")
        return super().http_error_302(req, fp, code, msg, headers)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        newurl = urljoin(req.full_url, newurl)
        old, new = urlsplit(req.full_url).scheme.lower(), urlsplit(newurl).scheme.lower()
        if new != old:
            raise ModelInstallError(f"拒绝跨协议重定向（{old} → {new}）")
        self._store.check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class InstallHandle(ActionHandle):
    """One install on its own worker thread. ``cancel()`` never blocks."""

    def __init__(self, store: "ModelStore", args: dict, run_id: str | None = None,
                 on_done: Callable[[], None] | None = None):
        self._store = store
        self._args = dict(args)
        self.run_id = run_id
        self._cancel = threading.Event()
        self._sock: socket.socket | None = None
        self._state = RUNNING
        self._progress = 0.0
        self._bytes = 0                       # downloaded so far (poll feedback; iter04)
        self._total: int | None = None        # Content-Length when known
        self._result: dict | None = None
        self._error: str | None = None
        self._on_done = on_done
        self.thread = threading.Thread(target=self._work, name="nx-model-install", daemon=True)

    def start(self) -> "InstallHandle":
        self.thread.start()
        return self

    # ---------------------------------------------------------- ActionHandle
    def poll(self) -> ActionStatus:
        return ActionStatus(state=self._state, progress=self._progress, result=self._result, error=self._error,
                            feedback={"bytes": self._bytes, "total": self._total})

    def cancel(self) -> None:
        """Stop path safe: an Event and a socket shutdown, nothing else."""
        self._cancel.set()
        sock = self._sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    # ---------------------------------------------------------- worker
    def _work(self) -> None:
        try:
            result = self._store._install(self._args, self)
        except ModelInstallError as exc:
            self._finish(CANCELLED if self._cancel.is_set() else FAILED, error=str(exc))
        except Exception as exc:                                # never leave the run hanging
            state = CANCELLED if self._cancel.is_set() else FAILED
            self._finish(state, error=f"模型安装失败：{type(exc).__name__}: {exc}")
        else:
            self._finish(SUCCEEDED, result=result)

    def _finish(self, state: str, result: dict | None = None, error: str | None = None) -> None:
        self._result, self._error = result, error
        if state == SUCCEEDED:
            self._progress = 1.0
        self._state = state
        if self._on_done is not None:
            try:
                self._on_done()
            except Exception:
                pass

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def check_cancel(self) -> None:
        if self._cancel.is_set():
            raise ModelInstallError("模型安装已被停止/取消；未安装任何内容")

    def attach_socket(self, sock: socket.socket | None) -> None:
        self._sock = sock
        if sock is not None and self._cancel.is_set():
            self.cancel()


class _FollowerHandle(ActionHandle):
    """A second install of a sha that is already downloading follows the first one (Q3-06)."""

    def __init__(self, leader: InstallHandle):
        self._leader = leader
        self._cancelled = False
        self.run_id = None

    def poll(self) -> ActionStatus:
        if self._cancelled:
            return ActionStatus(state=CANCELLED, error="已取消（合并到的安装仍在进行）")
        status = self._leader.poll()
        if status.state == CANCELLED:
            return ActionStatus(state=FAILED, error="合并到的同一模型安装已被停止/取消；请重新发起")
        return status

    def cancel(self) -> None:
        self._cancelled = True                 # never cancels the leader: it belongs to another run


class ModelStore:
    """Installed models under ``models_dir/<sha256>/`` plus the install machinery."""

    def __init__(self, config: ModelConfig, events=None):
        self.config = config
        self.events = events
        self.dir = Path(config.models_dir).absolute()
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o755)
        self.partial = self.dir / PARTIAL
        _remove(self.partial)                    # leftovers of an interrupted install: never published
        self.partial.mkdir(mode=0o700)
        # At rest the models directory is read-only too (0555): skills read models in
        # place, and not even a same-user skill (non-root runtime) can rename or add
        # entries without first changing permissions. Only install/delete open it,
        # briefly and under the store lock (_writable).
        os.chmod(self.dir, 0o555)
        self._file_roots = tuple(os.path.realpath(r) for r in config.file_roots)
        self._lock = threading.Lock()            # index + in-use bookkeeping; never on the stop path
        self._active: list[InstallHandle] = []
        self._claims: dict[str, str] = {}        # sha256 -> run id installing it (Q3-06)
        self._leaders: dict[str, InstallHandle] = {}
        self.in_use: Callable[[str], list[str]] = lambda _model_id: []   # set by the runtime
        self._index: dict[str, dict] = {}
        self.invalid: list[str] = []
        for entry in sorted(self.dir.iterdir()):
            if entry.name == PARTIAL:
                continue
            record = self._load(entry)
            if record is None:
                self.invalid.append(entry.name)
            else:
                self._index[record["sha256"]] = record

    def _writable(self):
        """Context manager: models dir writable for one rename (caller holds ``_lock``)."""
        store = self

        class _Open:
            def __enter__(self):
                os.chmod(store.dir, 0o755)

            def __exit__(self, *exc):
                try:
                    os.chmod(store.dir, 0o555)
                except OSError:
                    pass
                return False
        return _Open()

    # ------------------------------------------------------------ manifest / views
    def capability_entry(self) -> dict:
        params: dict[str, Any] = {"runtimes": list(self.config.runtimes)}
        if self.config.accelerator:
            params["accelerator"] = self.config.accelerator
        return {"id": CAPABILITY, "params": params}

    def _load(self, entry: Path) -> dict | None:
        if not SHA_RE.fullmatch(entry.name) or entry.is_symlink() or not entry.is_dir():
            return None
        try:
            record = json.loads((entry / META).read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(record, dict) or record.get("sha256") != entry.name:
            return None
        if record.get("model_id") == entry.name:       # 0.3.0 (996d40b) layout: model_id was the sha
            record["model_id"] = None
        path = entry / str(record.get("file", ""))
        if path.parent != entry or not path.is_file() or path.is_symlink():
            return None
        record["path"] = str(path)
        try:
            os.chmod(entry, 0o555)                   # a crash between rename and chmod left it writable
        except OSError:
            pass
        return record

    def list(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in sorted(self._index.values(), key=lambda r: r["installed_at"])]

    def _resolve(self, ident: str) -> str:
        """sha256 of an installed model given its sha256 or (if unique) its catalog ``model_id``.
        Caller holds ``_lock``."""
        if ident in self._index:
            return ident
        if isinstance(ident, str) and SHA_RE.fullmatch(ident.lower()) and ident.lower() in self._index:
            return ident.lower()
        matches = [sha for sha, r in self._index.items() if r.get("model_id") == ident]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise Conflict(f"目录 ID {ident} 对应多个已安装版本，请用 sha256 指定：{sorted(matches)}",
                           code="model_ambiguous", extra={"sha256": sorted(matches)})
        raise NotFound(f"未安装模型 {ident}", code="model_not_found")

    def get(self, ident: str) -> dict:
        with self._lock:
            return dict(self._index[self._resolve(ident)])

    def skill_view(self, run=None) -> dict:
        """What ``ctx.models`` shows: ``{sha256: {model_id, name, runtime, sha256, size, path, file}}``
        (``model_id`` = catalog id or None).

        With ``run``, the exposed ids are recorded on it under the store lock, so a
        concurrent ``delete`` sees the run as a user (409 ``model_in_use``)."""
        with self._lock:
            view = {sha: {k: r.get(k) for k in ("model_id", "name", "runtime", "sha256", "size", "path", "file")}
                    for sha, r in self._index.items()}
            if run is not None:
                run.models_exposed = set(view)
            return view

    def describe(self) -> dict:
        with self._lock:
            count = len(self._index)
            installing = sum(1 for h in self._active if h.poll().state == RUNNING)
            claims = len(self._claims)
        return {"enabled": True, "dir": str(self.dir), "runtimes": list(self.config.runtimes),
                "installed": count, "installing": installing, "max_bytes": self.config.max_bytes,
                "max_concurrent": self.config.max_concurrent, "claims": claims,
                "min_free_bytes": self.config.min_free_bytes,
                "file_roots": list(self._file_roots), **({"invalid_entries": self.invalid} if self.invalid else {})}

    # ------------------------------------------------------------ checks (synchronous, before a run starts)
    def check_url(self, url: str) -> str:
        """Scheme / host / path policy; returns the scheme. Raises ModelInstallError."""
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        if scheme == "https":
            if not parts.hostname:
                raise ModelInstallError("package_ref 缺少主机名")
            host = parts.hostname.lower()
            allowed = {h.lower() for h in self.config.allow_private_hosts + self.config.allow_http_hosts}
            if host not in allowed:
                why = _host_is_internal(host)
                if why:
                    raise ModelInstallError(f"拒绝下载：{host} 解析为{why}（私有镜像请在运行时配置中显式允许）")
            return scheme
        if scheme == "http":
            if parts.hostname and parts.hostname.lower() in {h.lower() for h in self.config.allow_http_hosts}:
                return scheme
            raise ModelInstallError("模型包只能经 https 下载（http 仅限运行时配置允许的主机）")
        if scheme == "file":
            self._file_path(url)
            return scheme
        raise ModelInstallError(f"不支持的 package_ref 协议 {scheme or '（无）'}；只允许 https 或配置允许的 file:// 目录")

    def _file_path(self, url: str) -> Path:
        parts = urlsplit(url)
        if parts.netloc not in ("", "localhost"):
            raise ModelInstallError("file:// 只能指向本机")
        if not self._file_roots:
            raise ModelInstallError("运行时未配置允许的 file:// 目录（--model-file-root）")
        real = os.path.realpath(unquote(parts.path))
        if not any(os.path.commonpath([real, root]) == root for root in self._file_roots):
            raise ModelInstallError("file:// 路径不在运行时配置允许的目录内")
        try:
            st = os.stat(real)
        except OSError as exc:
            raise ModelInstallError(f"无法读取模型包：{exc.strerror}") from None
        if not stat.S_ISREG(st.st_mode):
            raise ModelInstallError("file:// 模型包须为普通文件")
        return Path(real)

    def precheck(self, args: Any) -> dict:
        """Validate ``model.install`` inputs; raise BadRequest (400) before any run starts."""
        if not isinstance(args, dict):
            raise BadRequest("args 须为对象")
        sha = args.get("sha256")
        if not isinstance(sha, str) or not SHA_RE.fullmatch(sha.lower()):
            raise BadRequest("sha256 须为 64 位十六进制字符串")
        runtime = args.get("runtime")
        if runtime not in self.config.runtimes:
            raise BadRequest(f"不支持的推理运行时 {runtime!r}；本机支持 {list(self.config.runtimes)}",
                             code="runtime_unsupported")
        ref = args.get("package_ref")
        if not isinstance(ref, str) or not ref or len(ref) > 4096:
            raise BadRequest("package_ref 须为非空字符串")
        try:
            self.check_url(ref)
        except ModelInstallError as exc:
            raise BadRequest(str(exc), code="package_ref_refused") from None
        clean = {"package_ref": ref, "sha256": sha.lower(), "runtime": runtime}
        for key in ("model_id", "name"):                # optional catalog metadata (spec inputs)
            value = args.get(key)
            if value is None:
                continue
            if not isinstance(value, str) or not 0 < len(value) <= 200 or not _printable(value):
                raise BadRequest(f"{key} 须为 1–200 个可打印字符（不允许控制字符、双向覆盖符等）")
            clean[key] = value
        return clean

    # ------------------------------------------------------------ install
    def start_action(self, cap_id: str, args: dict, run_id: str | None = None) -> InstallHandle:
        """Capability-bus entry point (``CapabilityBus.native``)."""
        return self.start_install(args, run_id=run_id)

    # --- concurrency (Q3-06): one download per sha, at most max_concurrent at once ---
    def claim(self, sha: str, run_id: str) -> str:
        """Reserve the install of ``sha`` for ``run_id``; returns the owning run id (an earlier
        run when one is already installing this sha). Raises InstallBusy at the limit."""
        with self._lock:
            owner = self._claims.get(sha)
            if owner is not None:
                return owner
            if len(self._claims) >= self.config.max_concurrent:
                raise InstallBusy(f"已有 {len(self._claims)} 个模型正在安装（上限 {self.config.max_concurrent}）")
            self._claims[sha] = run_id
            return run_id

    def owner(self, sha: str) -> str | None:
        with self._lock:
            return self._claims.get(sha)

    def release(self, sha: str, run_id: str) -> None:
        with self._lock:
            if self._claims.get(sha) == run_id:
                del self._claims[sha]
                self._leaders.pop(sha, None)

    def start_install(self, args: dict, run_id: str | None = None):
        sha = args.get("sha256").lower() if isinstance(args.get("sha256"), str) else None
        token = run_id or f"anon-{os.urandom(6).hex()}"
        if sha is not None and SHA_RE.fullmatch(sha):
            with self._lock:
                leader = self._leaders.get(sha)
                owner = self._claims.get(sha)
            if owner is not None and owner != token and leader is not None:
                return _FollowerHandle(leader)          # same sha already downloading: merge
            owner = self.claim(sha, token)               # may raise InstallBusy
            if owner != token:                           # claimed by another run that has not started yet
                with self._lock:
                    leader = self._leaders.get(sha)
                if leader is not None:
                    return _FollowerHandle(leader)
                raise InstallBusy(f"该模型正由运行 {owner} 安装")

        def done():
            with self._lock:
                self._active = [h for h in self._active if h is not handle]
            if sha is not None:
                self.release(sha, token)
        handle = InstallHandle(self, args, run_id, on_done=done)
        with self._lock:
            self._active.append(handle)
            if sha is not None:
                self._leaders[sha] = handle
        return handle.start()

    def _event(self, type_: str, handle: InstallHandle | None = None, **data) -> None:
        if self.events is None:
            return
        if handle is not None:
            base = {"run_id": handle.run_id, **getattr(handle, "event_ids", {})}
            data = {**{k: v for k, v in base.items() if v is not None}, **data}
        self.events.emit(type_, **data)

    def _install(self, args: dict, handle: InstallHandle) -> dict:
        try:
            clean = self.precheck(args)
        except BadRequest as exc:
            self._event("model.install_failed", handle, error=str(exc), cancelled=False)
            raise ModelInstallError(str(exc)) from None
        sha, ref, runtime = clean["sha256"], clean["package_ref"], clean["runtime"]
        catalog_id, name = clean.get("model_id"), clean.get("name")
        handle.event_ids = {"sha256": sha, "model_id": catalog_id}
        with self._lock:
            existing = self._index.get(sha)
        if existing is not None:
            self._event("model.install_skipped", handle, reason="already_installed")
            return {"installed": True, "model_id": sha}
        source = urlsplit(ref)._replace(query="", fragment="").geturl()   # never store signed tokens
        self._event("model.install_started", handle, source=source, runtime=runtime)
        tmp = self.partial / f"{sha}-{os.urandom(6).hex()}.part"
        try:
            size, digest = self._fetch(ref, tmp, handle)
            if digest != sha:
                # Never return the actual digest (Q3-09: a digest oracle for files/URLs one cannot read).
                raise ModelInstallError(f"SHA-256 校验失败（期望 {sha}）；已删除下载内容")
            handle.check_cancel()
            record = self._publish(sha, tmp, _safe_filename(urlsplit(ref).path), size, runtime, source,
                                   catalog_id, name, handle)
        except ModelInstallError as exc:
            self._event("model.install_failed", handle, error=str(exc), cancelled=handle.cancelled())
            raise
        except Exception as exc:                       # disk full, permissions, ... (Q3-13)
            error = _describe_failure(exc)
            self._event("model.install_failed", handle, error=error, cancelled=handle.cancelled())
            raise ModelInstallError(error) from None
        finally:
            _remove(tmp)
        self._event("model.installed", handle, size=record["size"], runtime=runtime, name=name)
        return {"installed": True, "model_id": sha}

    def _open(self, ref: str, handle: InstallHandle):
        scheme = self.check_url(ref)
        if scheme == "file":
            path = self._file_path(ref)
            fh = open(path, "rb")
            return fh, os.fstat(fh.fileno()).st_size
        host = (urlsplit(ref).hostname or "").lower()
        handlers: list = [_RedirectPolicy(self)]
        if host in _LOOPBACK or host in {h.lower() for h in self.config.allow_http_hosts}:
            handlers.append(urllib.request.ProxyHandler({}))      # local mirror: never via a proxy
        context = ssl.create_default_context(cafile=self.config.ca_file) if self.config.ca_file else \
            ssl.create_default_context()
        handlers.append(urllib.request.HTTPSHandler(context=context))
        opener = urllib.request.build_opener(*handlers)
        request = urllib.request.Request(ref, headers={"User-Agent": "neuxmind-runtime/model-install"})
        try:
            resp = opener.open(request, timeout=self.config.timeout_s)
        except ModelInstallError:
            raise
        except urllib.error.HTTPError as exc:
            raise ModelInstallError(f"下载失败：HTTP {exc.code}") from None
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, ModelInstallError):
                raise reason from None
            raise ModelInstallError(f"下载失败：{reason}") from None
        except (OSError, ValueError) as exc:
            raise ModelInstallError(f"下载失败：{exc}") from None
        final = urlsplit(resp.geturl()).scheme.lower()
        if final != scheme:                       # defence in depth: the redirect handler refuses this
            resp.close()
            raise ModelInstallError(f"拒绝跨协议重定向（{scheme} → {final}）")
        try:
            handle.attach_socket(resp.fp.raw._sock)       # so cancel() can interrupt a blocked read
        except AttributeError:
            pass
        length = resp.headers.get("Content-Length")
        try:
            total = int(length) if length is not None else None
        except ValueError:
            total = None
        return resp, total

    def _fetch(self, ref: str, tmp: Path, handle: InstallHandle) -> tuple[int, str]:
        handle.check_cancel()
        src, total = self._open(ref, handle)
        limit = self.config.max_bytes
        try:
            if total is not None and total > limit:
                raise ModelInstallError(f"模型包过大（{total} 字节，上限 {limit}）")
            self._check_space(total or 0)
            started = time.monotonic()
            digest = hashlib.sha256()
            size = 0
            next_event = time.monotonic() + self.config.progress_interval_s
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            # read1: return what has arrived (<= CHUNK) instead of blocking until a full chunk, so
            # progress and cancellation keep up with a slow mirror (iter04: run progress).
            read = getattr(src, "read1", None) or src.read
            with os.fdopen(fd, "wb") as out:
                while True:
                    handle.check_cancel()
                    try:
                        chunk = read(CHUNK)
                    except (OSError, ValueError) as exc:
                        handle.check_cancel()
                        raise ModelInstallError(f"下载中断：{exc}") from None
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > limit:
                        raise ModelInstallError(f"模型包超过大小上限 {limit} 字节；已删除下载内容")
                    if time.monotonic() - started > self.config.max_download_s:
                        raise ModelInstallError(f"下载超过总时限 {self.config.max_download_s:g} s")
                    if size % (8 << 20) < len(chunk):         # every 8 MiB: is the disk filling up?
                        self._check_space(0 if total is None else max(0, total - size))
                    digest.update(chunk)
                    out.write(chunk)
                    handle._bytes, handle._total = size, total
                    if total:
                        # Run progress follows this (``/v2/runs/{id}.progress``); 1.0 only once installed.
                        handle._progress = min(0.99, size / total)
                    if time.monotonic() >= next_event:
                        next_event = time.monotonic() + self.config.progress_interval_s
                        self._event("model.install_progress", handle, bytes=size, total=total)
                handle.check_cancel()
                if total is not None and size != total:
                    raise ModelInstallError(f"下载不完整（{size}/{total} 字节）")
                out.flush()
                os.fsync(out.fileno())
            return size, digest.hexdigest()
        finally:
            handle.attach_socket(None)
            try:
                src.close()
            except Exception:
                pass

    def _check_space(self, needed: int) -> None:
        """Q3-06: keep ``min_free_bytes`` free on the filesystem the models (and usually the
        runtime state, latch and telemetry) live on."""
        try:
            free = shutil.disk_usage(self.partial).free
        except OSError:
            return
        if free - needed < self.config.min_free_bytes:
            raise ModelInstallError(f"磁盘可用空间不足：需要 {needed} 字节并保留 {self.config.min_free_bytes} 字节，"
                                    f"当前可用 {free} 字节")

    def _publish(self, sha: str, tmp: Path, filename: str, size: int, runtime: str, source: str,
                 catalog_id: str | None, name: str | None, handle: InstallHandle) -> dict:
        staging = self.partial / f"{sha}-{os.urandom(6).hex()}"
        staging.mkdir(mode=0o700)
        try:
            target_file = staging / filename
            os.replace(tmp, target_file)
            os.chmod(target_file, 0o444)
            record = {"model_id": catalog_id, "sha256": sha, "name": name, "runtime": runtime, "source": source,
                      "file": filename, "size": size, "installed_at": time.time()}
            meta = staging / META
            with open(meta, "w") as fh:
                json.dump(record, fh, ensure_ascii=False, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(meta, 0o444)
            _fsync_dir(staging)
            handle.check_cancel()
            final = self.dir / sha
            with self._lock:
                try:
                    # Atomic; fails if another install won. The directory is made read-only
                    # only *after* the rename: moving a directory needs write permission on it
                    # (its ".." entry) for anyone but root (§5.0 non-root runtime).
                    with self._writable():
                        os.rename(staging, final)
                except OSError:
                    if sha not in self._index:
                        loaded = self._load(final)
                        if loaded is None:
                            raise ModelInstallError("模型目录已存在但无效；请先删除") from None
                        self._index[sha] = loaded
                    return dict(self._index[sha])
                os.chmod(final, 0o555)
                _fsync_dir(self.dir)
                record["path"] = str(final / filename)
                self._index[sha] = record
                return dict(record)
        finally:
            _remove(staging)                         # only left when not renamed

    # ------------------------------------------------------------ delete
    def delete(self, ident: str) -> dict:
        """Delete by sha256 or (if unique) catalog ``model_id``."""
        with self._lock:
            sha = self._resolve(ident)
            users = self.in_use(sha)
            if users:
                raise Conflict(f"模型正被运行中的技能使用：{', '.join(users)}", code="model_in_use",
                               extra={"runs": users})
            trash = self.partial / f"trash-{sha}-{os.urandom(6).hex()}"
            os.chmod(self.dir / sha, 0o700)           # a directory can only be moved when writable (non-root)
            with self._writable():
                os.rename(self.dir / sha, trash)     # atomic: gone from the index and the directory at once
            record = self._index.pop(sha)
        _remove(trash)
        self._event("model.deleted", sha256=sha, model_id=record.get("model_id"))
        # Unchanged since 0.3.0: like model.install's output, ``model_id`` here is the installed
        # identity (sha256). The catalog id is in the ``model.deleted`` event.
        return {"deleted": True, "model_id": sha}

    def close(self) -> None:
        with self._lock:
            active = list(self._active)
        for handle in active:
            handle.cancel()
