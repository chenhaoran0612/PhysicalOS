"""Edge models: from the cloud catalog onto a robot (INTERFACES_V1 §5.1, §5.4).

Example::

    from neuxmind.models import install_from_cloud

    result = install_from_cloud(cloud, robot, "edge-detector", authorize=True,
                                on_progress=lambda run: print(run["progress"]))
    print(result["sha256"], result["model"]["path"], result["already_installed"])

Flow: the cloud hands out the entitled package (``/catalog/models/{id}/package``;
not entitled → ``entitlement_required`` with the price and a ``nx models buy``
hint) → the robot's ``model.install`` (v2 invoke, with the catalog ``model_id``)
downloads it, checks the SHA-256 and installs it atomically → the SDK polls the
run until it ends. Installing a sha that is already there is a no-op success.
The robot never moves; ``authorize=True`` is still required because on-device
software changes (checked locally, before any request).
"""
from __future__ import annotations

import time
from typing import Any, Callable

from .errors import NeuxMindError
from .robot import ROBOT_HINTS, TERMINAL_STATUSES, RobotClient, _authorization_required, is_sha256


def _price_text(pricing: dict | None) -> str:
    pricing = pricing or {}
    kind, credits = pricing.get("type"), pricing.get("credits", 0)
    return {"per_1k_tokens": f"{credits} 点/千 token", "per_call": f"{credits} 点/次",
            "license_once": f"一次性 {credits} 点", "free": "免费"}.get(kind, "") if kind else ""


def _entitlement_error(cloud, model_id: str, exc: NeuxMindError) -> NeuxMindError:
    """Turn the cloud's 403 ``entitlement_required`` into a purchase prompt with the price."""
    details: dict[str, Any] = {**exc.details, "model_id": model_id}
    price = ""
    try:
        card = cloud.model(model_id)
        details["pricing"] = card.get("pricing")
        price = _price_text(card.get("pricing"))
    except NeuxMindError:
        pass
    try:
        details["available_credits"] = cloud.wallet().get("available")
    except NeuxMindError:
        pass
    wallet = f"，钱包可用 {details['available_credits']} 点" if "available_credits" in details else ""
    message = (f"尚未获得模型 {model_id} 的授权，不能下载端侧部署包。请先购买授权：nx models buy {model_id}"
               f"（{price or '价格见 nx models list'}{wallet}），然后重新安装。本次没有向机器人发送任何请求")
    details["hint"] = f"nx models buy {model_id}"
    return NeuxMindError(message, code="entitlement_required", status=exc.status, details=details)


def install_from_cloud(cloud, robot: RobotClient, model_id: str, *, authorize: bool = False,
                       on_progress: Callable[[dict], None] | None = None, wait_timeout: float | None = 1800.0,
                       interval: float = 0.2, on_start: Callable[[dict], None] | None = None) -> dict:
    """Install the catalog model ``model_id`` on ``robot`` and wait for the result.

    Returns ``{model_id, sha256, runtime, run_id, run, already_installed, model, simulated}``
    (``model`` = the robot's ``/v2/models`` record). ``on_start(info)`` is called once the
    robot accepted the install (``info``: model_id, sha256, runtime, run_id, already_installed);
    ``on_progress({status, progress, bytes, total, run})`` whenever the status or the download
    progress changes (from the run's ``model.install_progress`` events). Raises :class:`NeuxMindError`:
    ``authorization_required`` (nothing sent), ``capability_not_provided`` (no
    ``--models-dir``), ``entitlement_required`` / ``no_package`` (cloud),
    ``install_failed`` (the run failed — e.g. SHA-256 mismatch; ``details["run"]``),
    ``wait_timeout`` (the install keeps going on the robot).
    """
    if authorize is not True:
        raise _authorization_required("安装端侧模型会变更机器人上的软件，")
    if not robot.supports_model_install():
        raise NeuxMindError("机器人没有 model.install 能力，不能安装端侧模型。" + ROBOT_HINTS["capability_not_provided"],
                            code="capability_not_provided", details={"hint": ROBOT_HINTS["capability_not_provided"]})
    try:
        args = cloud.model_install_args(model_id)
    except NeuxMindError as exc:
        if exc.code == "entitlement_required":
            raise _entitlement_error(cloud, model_id, exc) from None
        raise
    sha = args["sha256"].lower()
    already = robot.find_model(sha) is not None
    started = robot.install_model(args["package_ref"], sha, args["runtime"], model_id=model_id, name=model_id,
                                  authorize=True)
    run_id = started["run_id"]
    if on_start is not None:
        on_start({"model_id": model_id, "sha256": sha, "runtime": args["runtime"], "run_id": run_id,
                  "already_installed": already})
    record = _wait_install(robot, run_id, wait_timeout, interval, on_progress)
    out = {"model_id": model_id, "sha256": sha, "runtime": args["runtime"], "run_id": run_id, "run": record,
           "already_installed": already, "simulated": robot.simulated}
    if record.get("status") != "succeeded":
        raise NeuxMindError(f"模型 {model_id} 安装失败（{record.get('status')}）：{record.get('error') or '未知原因'}",
                            code="install_failed", details=out)
    out["model"] = robot.find_model(sha)
    return out


def install_progress(robot: RobotClient, run_id: str, record: dict, cursor: int, state: dict) -> tuple[float, int]:
    """Download progress of a ``model.install`` run → ``(progress, events cursor)``; updates
    ``state["bytes"]`` / ``state["total"]``.

    Runtimes since round 04 report it on the run itself (contract §6.2): ``progress`` follows the
    download (≤ 0.99 until installed; 0 without a ``Content-Length``) and
    ``progress_detail.feedback = {bytes, total}``. Older runtimes (no ``progress_detail``) kept
    ``progress`` at 0, so the SDK falls back to the run's ``model.install_progress`` events."""
    progress = record.get("progress") if isinstance(record.get("progress"), (int, float)) else 0.0
    detail = record.get("progress_detail")
    if isinstance(detail, dict):
        feedback = detail.get("feedback") if isinstance(detail.get("feedback"), dict) else {}
        if isinstance(feedback.get("bytes"), int):
            state["bytes"] = feedback["bytes"]
        if isinstance(feedback.get("total"), int):
            state["total"] = feedback["total"]
    else:                                   # runtime before round 04: progress only in the events
        try:
            events, cursor = robot.events_since(cursor, run_id=run_id)
        except NeuxMindError:
            events = []
        for event in events:
            if event.get("type") == "model.install_progress":
                state["bytes"], state["total"] = event.get("bytes"), event.get("total")
        if isinstance(state["bytes"], int) and isinstance(state["total"], int) and state["total"] > 0:
            progress = max(progress, min(0.99, state["bytes"] / state["total"]))
    if record.get("status") == "succeeded":
        progress = 1.0
    elif record.get("status") in ("queued", "running"):
        progress = min(progress, 0.99)
    return float(progress), cursor


def _wait_install(robot: RobotClient, run_id: str, timeout: float | None, interval: float,
                  on_progress: Callable[[dict], None] | None) -> dict:
    """Poll the install run until it ends (progress: :func:`install_progress`). ``on_progress`` gets
    ``{status, progress, bytes, total, run}`` whenever one of them changes."""
    deadline = None if timeout is None else time.monotonic() + timeout
    cursor, last, state = 0, None, {"bytes": None, "total": None}
    while True:
        record = robot.run(run_id)
        progress, cursor = install_progress(robot, run_id, record, cursor, state)
        update = {"status": record.get("status"), "progress": progress, "bytes": state["bytes"],
                  "total": state["total"], "run": record}
        marker = (update["status"], round(progress, 3), state["bytes"])
        if on_progress is not None and marker != last:
            on_progress(update)
        last = marker
        if record.get("status") in TERMINAL_STATUSES:
            return record
        if deadline is not None and time.monotonic() >= deadline:
            raise NeuxMindError(f"等待模型安装 {run_id} 超时（仍为 {record.get('status')}）；安装没有被取消",
                                code="wait_timeout", details={"run": record})
        time.sleep(interval)


def resolve_model(robot: RobotClient, ref: str, cloud=None) -> str:
    """What to pass to :meth:`RobotClient.remove_model` for ``ref`` (a sha256 or a catalog id).

    * a sha256 → itself (lower case);
    * a catalog id the robot recorded (``/v2/models`` ``model_id``) → the id itself: the
      runtime resolves it and answers 409 ``model_ambiguous`` if several versions are installed;
    * a ``name`` match → that record's sha256;
    * otherwise (records from runtimes that did not keep catalog ids) with ``cloud``: the
      catalog card's package sha256, if that is installed.

    Raises ``model_not_found`` if nothing matches.
    """
    if is_sha256(ref):
        return ref.lower()
    models = robot.models()
    if any(m.get("model_id") == ref for m in models):
        return ref
    named = [m for m in models if m.get("name") == ref]
    if len(named) == 1:
        return named[0]["sha256"]
    if cloud is not None:
        try:
            package = (cloud.model(ref) or {}).get("package") or {}
        except NeuxMindError:
            package = {}
        sha = package.get("sha256")
        if is_sha256(sha or "") and any(m.get("sha256") == sha.lower() for m in models):
            return sha.lower()
    raise NeuxMindError(f"机器人上没有找到模型 {ref}：请用 nx models installed 查看已安装模型的 SHA-256 后按 SHA-256 删除",
                        code="model_not_found", details={"model": ref, "hint": ROBOT_HINTS["model_not_found"]})
