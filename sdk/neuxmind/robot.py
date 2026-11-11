"""Client for the NeuxMind edge runtime HTTP API (INTERFACES_V1 §1).

Example::

    from neuxmind.robot import RobotClient

    robot = RobotClient("http://127.0.0.1:8790", token)
    print(robot.manifest()["name"], robot.simulated)
    robot.install_skill("my-patrol/")                       # a directory or a package dict
    run = robot.run_skill("my-patrol", {"waypoints": [[1, 0]]}, authorize=True)
    print(robot.wait(run["run_id"])["status"])
    robot.stop()                                            # always allowed

Safety rules enforced locally, before anything is sent:

* motion capabilities (``safety == "motion"`` in the spec, and every
  ``x.*`` vendor extension), skill runs and ``reset`` require an explicit
  ``authorize=True``; otherwise :class:`NeuxMindError` ``authorization_required``
  is raised and **no request is made**;
* ``stop()`` never needs authorization and never waits on anything else.

Every mutating request gets a fresh ``request_id`` (uuid4) unless you pass
one. The SDK never retries a mutating request on its own: if a response is
lost you get ``outcome_unknown`` with the ``request_id`` in ``details`` —
re-send with *the same* id (the runtime is idempotent) or query state.
"""
from __future__ import annotations

import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any, Callable

from . import _bootstrap
from ._http import request_json
from .errors import NeuxMindError

TERMINAL_STATUSES = ("succeeded", "failed", "stopped", "unknown")
CONFORMANCE_LEVELS = ("static", "dry", "motion")
MODEL_INSTALL = "model.install"
_SHA256_HEX = frozenset("0123456789abcdef")

# Friendly explanations appended to runtime errors (``details["hint"]``).
ROBOT_HINTS = {
    "model_in_use": "该模型正被运行中的技能使用（技能启动时在 ctx.models 中拿到了它）：等这些运行结束，"
                    "或先 nx skill cancel <run_id> / nx stop，再删除。details.runs 列出了占用它的运行。",
    "model_not_found": "机器人上没有安装这个模型：nx models installed 查看已安装模型（SHA-256 与目录 ID）。",
    "model_ambiguous": "这个目录 ID 在机器人上装了多个版本（不同 SHA-256），删除时请用 SHA-256 指定其中一个；"
                       "details.sha256 列出了这些版本，nx models installed 也能看到。",
    "runtime_unsupported": "机器人不支持该模型的推理运行时：运行时用 --model-runtimes 声明本机支持的运行时。",
    "package_ref_refused": "机器人拒绝了这个下载地址：运行时只允许 https，file:// 只允许 --model-file-root 目录内的文件。"
                           "云端未配置签名下载时返回的是原始 package_ref，需要设备能直接访问。",
    "capability_not_provided": "机器人没有启用该功能：端侧模型安装需要运行时以 --models-dir 启动。",
    "models_by_adapter": "该机器人的 model.install 由适配器提供，运行时无法列出或删除已安装模型。",
    "conformance_busy": "机器人上已有一致性测试在执行（同一时刻只允许一个）：等它结束，nx robot conformance <robot> 可查看。",
    "runs_active": "机器人上有运行未结束：dry/motion 一致性测试需要空闲的本体，先等运行结束或 nx skill cancel / nx stop。",
    "conformance_not_found": "机器人上没有这个一致性测试记录（运行时状态目录被清空或 ID 有误）。",
}


def is_sha256(text: str) -> bool:
    """True for a 64-character hexadecimal SHA-256 (any case)."""
    return isinstance(text, str) and len(text) == 64 and set(text.lower()) <= _SHA256_HEX


def new_request_id(prefix: str = "sdk") -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def is_motion_capability(cap_id: str) -> bool:
    """True if invoking ``cap_id`` can move the robot (spec ``safety == "motion"``).

    Vendor extensions (``x.*``) count as motion, as in the runtime.
    Raises :class:`NeuxMindError` ``unknown_capability`` for non-standard IDs.
    """
    caps = _bootstrap.spec().capabilities
    try:
        cid = caps.normalize(cap_id)
    except ValueError as exc:
        raise NeuxMindError(str(exc), code="unknown_capability") from None
    if cid.startswith("x."):
        return True
    return caps.get(cid).safety == "motion"


def _authorization_required(what: str) -> NeuxMindError:
    return NeuxMindError(
        f"{what}需要显式硬件授权：Python 传 authorize=True，命令行加 --authorize。"
        "授权前请确认现场安全、人员远离、急停在手。本次请求未发送。",
        code="authorization_required")


class RobotClient:
    """Talks to one robot's edge runtime gateway."""

    def __init__(self, url: str, token: str, *, timeout: float = 15.0):
        if not url:
            raise NeuxMindError("需要机器人地址，如 http://127.0.0.1:8790", code="config_error")
        if not token:
            raise NeuxMindError("需要机器人访问令牌（BRAIN_EDGE_TOKEN）", code="config_error")
        self.url = url.rstrip("/")
        self._token = token
        self.timeout = timeout
        self._manifest_cache: dict | None = None

    def __repr__(self) -> str:
        return f"RobotClient({self.url!r})"

    # ------------------------------------------------------------ transport
    def _request(self, method: str, path: str, body: Any = None, *, timeout: float | None = None) -> Any:
        headers = {"Authorization": f"Bearer {self._token}"}
        try:
            return request_json(method, self.url + path, body=body, headers=headers,
                                timeout=timeout or self.timeout)
        except NeuxMindError as exc:
            if isinstance(body, dict) and "request_id" in body:
                exc.details.setdefault("request_id", body["request_id"])
            hint = ROBOT_HINTS.get(exc.code)
            if hint and "hint" not in exc.details:
                exc.details["hint"] = hint
                exc.message = f"{exc.message}。{hint}"
                exc.args = (exc.message,)
            raise

    def _post(self, path: str, body: dict, **kw) -> Any:
        return self._request("POST", path, body, **kw)

    def _get(self, path: str, **kw) -> Any:
        return self._request("GET", path, **kw)

    def _delete(self, path: str, body: dict, **kw) -> Any:
        return self._request("DELETE", path, body, **kw)

    # ------------------------------------------------------------ manifests & state
    def manifest(self, *, refresh: bool = False) -> dict:
        """``robot.adapter.v2`` manifest (cached; ``refresh=True`` re-reads it)."""
        if self._manifest_cache is None or refresh:
            self._manifest_cache = self._get("/v2/manifest")
        return self._manifest_cache

    def manifest_v1(self) -> dict:
        """Legacy ``robot.adapter.v1`` view (``GET /v1/manifest``)."""
        return self._get("/v1/manifest")

    @property
    def simulated(self) -> bool:
        """True when the body is a simulation (``manifest.simulated``)."""
        return self.manifest().get("simulated") is True

    def status(self) -> dict:
        """``GET /v1/status``: health, ``estop_latched``, active runs, ..."""
        return self._get("/v1/status")

    def registry(self) -> dict:
        """The capability registry the runtime was built against."""
        return self._get("/v2/capabilities/registry")

    def state(self, *ids: str) -> dict:
        """Read state/stream capabilities → ``{cap_id: outputs}``; no ids = everything readable."""
        query = ""
        if ids:
            query = "?ids=" + urllib.parse.quote(",".join(ids), safe=",.")
        return self._get("/v2/state" + query)

    # ------------------------------------------------------------ capabilities
    def invoke(self, cap_id: str, args: dict | None = None, *, authorize: bool = False,
               request_id: str | None = None) -> dict:
        """Invoke a command or action capability.

        Commands return their outputs; actions return ``{"run_id", "status"}``
        (follow with :meth:`wait`). ``safety.stop`` is routed to :meth:`stop`.
        """
        if cap_id == "safety.stop":
            return self.stop((args or {}).get("reason"))
        if is_motion_capability(cap_id) and authorize is not True:
            raise _authorization_required(f"运动能力 {cap_id} 可能使机器人运动，")
        body = {"request_id": request_id or new_request_id("invoke"), "args": dict(args or {}),
                "hardware_authorized": authorize is True}
        return self._post(f"/v2/capabilities/{urllib.parse.quote(cap_id, safe='.')}/invoke", body)

    # ------------------------------------------------------------ skills
    def skills(self) -> list[dict]:
        """Installed skill packages (``GET /v2/skills``)."""
        return self._get("/v2/skills").get("skills", [])

    def install_skill(self, package: dict | str | Path, *, request_id: str | None = None) -> dict:
        """Install a skill. ``package`` is a directory or a package dict with ``files``.

        The package is validated locally first. The runtime re-checks
        compatibility and answers 409 ``incompatible`` (``details["compatibility"]``).
        """
        from .package import build_package, validate_package

        if isinstance(package, (str, Path)):
            package = build_package(package)
        validate_package(package)
        body = {"request_id": request_id or new_request_id("install"), "package": package}
        return self._post("/v2/skills/install", body, timeout=max(self.timeout, 30.0))

    def run_skill(self, skill_id: str, parameters: dict | None = None, *, authorize: bool = False,
                  version: str | None = None, timeout_s: float | None = None,
                  request_id: str | None = None) -> dict:
        """Start a skill run → ``{"run_id", "status"}``.

        The runtime requires hardware authorization for **every** skill run
        (a skill may move the robot), so ``authorize=True`` is mandatory.
        """
        if authorize is not True:
            raise _authorization_required(f"技能 {skill_id} 的运行可能使机器人运动，")
        body: dict[str, Any] = {"request_id": request_id or new_request_id("run"),
                                "parameters": dict(parameters or {}), "hardware_authorized": True}
        if version:
            body["version"] = version
        if timeout_s is not None:
            body["timeout_s"] = timeout_s
        return self._post(f"/v2/skills/{urllib.parse.quote(skill_id, safe='.')}/run", body)

    # ------------------------------------------------------------ runs
    def runs(self) -> list[dict]:
        return self._get("/v2/runs").get("runs", [])

    def run(self, run_id: str) -> dict:
        """One run: ``{status, progress, result, error, events, stop_reason, ...}``."""
        return self._get(f"/v2/runs/{urllib.parse.quote(run_id)}")

    def wait(self, run_id: str, timeout: float | None = 300.0, interval: float = 0.2,
             on_update: Callable[[dict], None] | None = None) -> dict:
        """Poll a run until it reaches a terminal status and return it.

        ``on_update(run)`` is called whenever status or progress changes.
        Raises ``wait_timeout`` (the run keeps going; cancel or stop it yourself).
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        last = None
        while True:
            record = self.run(run_id)
            marker = (record.get("status"), record.get("progress"), len(record.get("events") or []))
            if on_update is not None and marker != last:
                on_update(record)
            last = marker
            if record.get("status") in TERMINAL_STATUSES:
                return record
            if deadline is not None and time.monotonic() >= deadline:
                raise NeuxMindError(f"等待运行 {run_id} 超时（仍为 {record.get('status')}）；运行没有被取消",
                                    code="wait_timeout", details={"run": record})
            time.sleep(interval)

    def cancel(self, run_id: str) -> dict:
        """Cancel one run (``POST /v2/runs/{id}/cancel``); does not latch the e-stop."""
        return self._post(f"/v2/runs/{urllib.parse.quote(run_id)}/cancel", {})

    # ------------------------------------------------------------ safety
    def stop(self, reason: str | None = None) -> dict:
        """Global stop. Always allowed; latches the e-stop until :meth:`reset`."""
        body = {"reason": reason} if reason else {}
        return self._post("/v2/stop", body, timeout=min(self.timeout, 5.0))

    def reset(self, *, authorize: bool = False, request_id: str | None = None) -> dict:
        """Release the e-stop latch. Requires ``authorize=True`` (on-site confirmation)."""
        if authorize is not True:
            raise _authorization_required("解除急停锁存（reset）后机器人可以再次运动，")
        return self._post("/v2/reset", {"request_id": request_id or new_request_id("reset"),
                                        "hardware_authorized": True})

    # ------------------------------------------------------------ telemetry (§5.2)
    def telemetry(self, since: int = 0) -> dict:
        """Events after sequence number ``since`` → ``{events, last_seq}`` (at most 500 per call;
        the runtime keeps them across restarts in a bounded ring)."""
        return self._get(f"/v2/telemetry?since={int(since)}")

    def run_events(self, run_id: str, since: int = 0, limit: int = 1000) -> dict:
        """Replay one run's events (``GET /v2/runs/{id}/events``, across restarts)
        → ``{run_id, events, last_seq, more, oldest_seq}``."""
        return self._get(f"/v2/runs/{urllib.parse.quote(run_id)}/events?since={int(since)}&limit={int(limit)}")

    def latest_seq(self) -> int:
        """The newest telemetry sequence number (``/v1/status.telemetry.last_seq``)."""
        telemetry = self.status().get("telemetry") or {}
        seq = telemetry.get("last_seq")
        return int(seq) if isinstance(seq, int) and not isinstance(seq, bool) else 0

    def events_since(self, since: int = 0, *, run_id: str | None = None) -> tuple[list[dict], int]:
        """Every event after ``since`` (pages until caught up) → ``(events, last_seq)``.

        With ``run_id`` the run's own event log is used (``/v2/runs/{id}/events``),
        otherwise the robot-wide stream (``/v2/telemetry``). ``last_seq`` is the cursor
        for the next call; sequence numbers can have gaps (lost on a crash), never repeats.
        """
        cursor, out = int(since), []
        while True:
            if run_id is not None:
                page = self.run_events(run_id, cursor)
                events, more = page.get("events") or [], bool(page.get("more"))
            else:
                page = self.telemetry(cursor)
                events = page.get("events") or []
                more = len(events) >= 500                       # the runtime's page size
            if not events:
                return out, cursor
            out.extend(events)
            cursor = max(cursor, *(e.get("seq", cursor) for e in events))
            if not more:
                return out, cursor

    def tail(self, *, since: int | None = None, lines: int | None = None, run_id: str | None = None,
             follow: bool = False, interval: float = 0.5, duration: float | None = None,
             should_stop: Callable[[], bool] | None = None):
        """Yield telemetry events like ``tail [-f]``.

        ``since`` given: everything after it. Otherwise the last ``lines`` events
        (robot-wide default 20; for a run the whole run log; ``lines=0``: only new ones).
        ``follow``: keep polling every ``interval`` seconds — for a run until it has
        ended and its last event was delivered, otherwise until ``duration`` seconds
        pass or ``should_stop()`` returns true.
        """
        if since is not None:
            backlog, cursor = self.events_since(since, run_id=run_id)
        elif run_id is not None:
            backlog, cursor = self.events_since(0, run_id=run_id)
            if lines is not None:
                backlog = backlog[-lines:] if lines > 0 else []
        else:
            lines = 20 if lines is None else lines
            latest = self.latest_seq()
            backlog, cursor = self.events_since(max(0, latest - lines))
            cursor = max(cursor, latest)
            backlog = backlog[-lines:] if lines > 0 else []
        yield from backlog
        if not follow:
            return
        deadline = None if duration is None else time.monotonic() + duration
        while True:
            finished = False
            if run_id is not None:
                try:
                    finished = self.run(run_id).get("status") in TERMINAL_STATUSES
                except NeuxMindError as exc:
                    if exc.status != 404:          # a run from before a restart: replay only
                        raise
                    finished = True
            events, cursor = self.events_since(cursor, run_id=run_id)
            yield from events
            if finished:
                return
            if should_stop is not None and should_stop():
                return
            if deadline is not None and time.monotonic() >= deadline:
                return
            time.sleep(interval)

    # ------------------------------------------------------------ edge models (§5.1)
    def models(self) -> list[dict]:
        """Installed edge models (``GET /v2/models``). The installed identity is ``sha256``;
        ``model_id`` is the catalog id given at install time (``None`` if none was given,
        and for records written by runtime 0.3.0 before 07e0d0c), ``name`` a display name."""
        return self._get("/v2/models").get("models", [])

    def find_models(self, ref: str) -> list[dict]:
        """Installed models matching ``ref``: its ``sha256`` (any case), else every record whose
        catalog ``model_id`` is ``ref`` (several versions possible), else whose ``name`` is ``ref``."""
        models = self.models()
        if is_sha256(ref):
            return [m for m in models if m.get("sha256") == ref.lower()]
        by_id = [m for m in models if m.get("model_id") == ref]
        return by_id or [m for m in models if m.get("name") == ref]

    def find_model(self, ref: str) -> dict | None:
        """The one installed model for ``ref`` (see :meth:`find_models`); ``None`` if there is
        none or the catalog id matches several versions."""
        found = self.find_models(ref)
        return found[0] if len(found) == 1 else None

    def supports_model_install(self) -> bool:
        return any(c.get("id") == MODEL_INSTALL for c in self.manifest(refresh=True).get("capabilities") or [])

    def install_model(self, package_ref: str, sha256: str, runtime: str, *, model_id: str | None = None,
                      name: str | None = None, authorize: bool = False, request_id: str | None = None) -> dict:
        """Start ``model.install`` (v2 invoke) → ``{"run_id", "status"}``; follow with :meth:`wait`.

        The runtime downloads ``package_ref``, checks the SHA-256 and installs it
        atomically; installing the same sha again is a no-op success. Changing
        on-device software needs on-site confirmation: ``authorize=True`` is
        mandatory (nothing is sent without it). It never moves the robot.
        """
        if authorize is not True:
            raise _authorization_required("安装端侧模型会变更机器人上的软件，")
        args: dict[str, Any] = {"package_ref": package_ref, "sha256": sha256, "runtime": runtime}
        if model_id is not None:
            args["model_id"] = model_id
        if name is not None:
            args["name"] = name
        body = {"request_id": request_id or new_request_id("model-install"), "args": args,
                "hardware_authorized": True}
        return self._post(f"/v2/capabilities/{MODEL_INSTALL}/invoke", body)

    def remove_model(self, model_id: str, *, authorize: bool = False, request_id: str | None = None) -> dict:
        """``DELETE /v2/models/{sha256 | catalog id}`` → ``{deleted, model_id: <sha256>}``. Needs
        ``authorize=True``. A catalog id must be unique on the robot, else 409 ``model_ambiguous``
        (``details["sha256"]`` lists the versions); 409 ``model_in_use`` while a running skill
        was given the model (``details["runs"]``)."""
        if authorize is not True:
            raise _authorization_required("删除端侧模型会变更机器人上的软件，")
        body = {"request_id": request_id or new_request_id("model-remove"), "hardware_authorized": True}
        return self._delete(f"/v2/models/{urllib.parse.quote(model_id.lower() if is_sha256(model_id) else model_id)}",
                            body)

    # ------------------------------------------------------------ conformance (§6.1, §6.3)
    def start_conformance(self, level: str = "static", *, authorize: bool = False,
                          request_id: str | None = None) -> dict:
        """``POST /v2/conformance`` → ``{accepted, conformance_id, status:"running", level, simulated}``.

        The runtime runs the capability conformance suite on itself, asynchronously. ``motion``
        moves the robot and needs ``authorize=True`` (checked here, nothing is sent without it).
        ``static`` and ``dry`` never move it: dry's zero setpoints and the reset of the suite's own
        e-stop latch carry ``hardware_authorized=true`` inside the runtime (contract §6.1).
        """
        if level not in CONFORMANCE_LEVELS:
            raise NeuxMindError(f"一致性测试级别须为 {' / '.join(CONFORMANCE_LEVELS)}，收到 {level!r}",
                                code="invalid_level")
        if level == "motion" and authorize is not True:
            raise _authorization_required("motion 级别一致性测试会让机器人做最小幅度的真实动作，")
        body = {"request_id": request_id or new_request_id("conformance"), "level": level,
                "hardware_authorized": authorize is True}
        try:
            return self._post("/v2/conformance", body)
        except NeuxMindError as exc:
            if exc.status in (404, 405) and not str(exc.code).startswith("conformance"):
                raise NeuxMindError("该机器人的运行时不支持 POST /v2/conformance（第 04 轮之前的版本）：请升级运行时，"
                                    "或在本机用 neuxmind-runtime conformance --url … --mode client 做黑盒测试",
                                    code="conformance_unsupported", status=exc.status,
                                    details={**exc.details, "server_code": exc.code}) from None
            raise

    def conformance(self, conformance_id: str) -> dict:
        """``GET /v2/conformance/{id}`` → ``{id, status: running|completed|failed|unknown, level,
        progress, report?, error?}`` (``report`` = ``conformance.report.v1``)."""
        return self._get(f"/v2/conformance/{urllib.parse.quote(conformance_id)}")

    def conformance_runs(self) -> list[dict]:
        """Recent conformance runs (``GET /v2/conformance``, newest first)."""
        return self._get("/v2/conformance").get("conformance", [])

    def wait_conformance(self, conformance_id: str, timeout: float | None = 600.0, interval: float = 0.25,
                         on_update: Callable[[dict], None] | None = None) -> dict:
        """Poll until the conformance run is no longer ``running`` and return its record.
        Raises ``wait_timeout`` (the suite keeps running on the robot; ``nx stop`` aborts it)."""
        deadline = None if timeout is None else time.monotonic() + timeout
        last = None
        while True:
            record = self.conformance(conformance_id)
            marker = (record.get("status"), record.get("progress"))
            if on_update is not None and marker != last:
                on_update(record)
            last = marker
            if record.get("status") != "running":
                return record
            if deadline is not None and time.monotonic() >= deadline:
                raise NeuxMindError(f"等待一致性测试 {conformance_id} 超时（仍在运行）；测试没有被中止，"
                                    "需要中止请 nx stop", code="wait_timeout", details={"conformance": record})
            time.sleep(interval)

    def run_conformance(self, level: str = "static", *, authorize: bool = False, timeout: float | None = 600.0,
                        on_update: Callable[[dict], None] | None = None) -> dict:
        """Start a conformance run and wait for it → ``conformance.report.v1``.

        Raises ``conformance_failed`` if the run itself failed (``details["conformance"]``) and
        ``conformance_unknown`` if the runtime restarted meanwhile. A *completed* run returns its
        report even when the level did not pass — check ``report[f"{level}_ok"]``. The report is
        never on-site acceptance evidence (``field_acceptance: false``).
        """
        started = self.start_conformance(level, authorize=authorize)
        record = self.wait_conformance(started["conformance_id"], timeout=timeout, on_update=on_update)
        if record.get("status") == "completed" and isinstance(record.get("report"), dict):
            return record["report"]
        error = record.get("error") or {}
        reason = error.get("error") if isinstance(error, dict) else error
        code = "conformance_unknown" if record.get("status") == "unknown" else "conformance_failed"
        raise NeuxMindError(f"一致性测试 {started['conformance_id']} 未完成（{record.get('status')}）："
                            f"{reason or '未知原因'}", code=code,
                            details={"conformance": record})
