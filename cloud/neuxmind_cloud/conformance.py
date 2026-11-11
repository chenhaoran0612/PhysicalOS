"""Conformance report summaries for the fleet (INTERFACES_V1 §6.4).

A robot's runtime produces ``conformance.report.v1`` (runtime
``neuxmind_runtime.conformance``, contract §6.1). The cloud does not run the
suite and cannot re-check the evidence: it validates the *shape and internal
consistency* of an uploaded report, binds it to the registered robot
(``instance_id``, ``simulated`` and the set of declared capabilities must match
the registered manifest) and keeps a compact summary of the latest one.

What the cloud refuses:

* anything that is not a well-formed ``conformance.report.v1``;
* ``field_acceptance`` other than ``false`` -- a conformance report is never
  on-site acceptance evidence (ADR-011);
* ``evidence_kind: "device_run"`` (``device_run_refused``): conformance reports
  are ``"conformance"`` (real body) or ``"simulation"``, never field runs;
* a report for another body (``conformance_robot_mismatch``) or for another
  capability set (``conformance_manifest_mismatch``);
* a report older than the stored one for the same manifest (``conformance_outdated``).

Who can upload: anyone with the ``robots`` scope in the org (the same people
who can register the robot). The report is self-reported -- the summary says
so (``self_reported: true``) and is never shown as acceptance.

``require_level`` (catalog / skill compatibility): a capability is *usable*
at level L only when the latest, non-stale summary covers L and the
capability's verdict is ``passed`` at L and at every lower level. If
``safety.stop`` is not usable, nothing is (fail closed). ``model_runtimes`` are
kept only when ``model.install`` is usable.
"""
from __future__ import annotations

import calendar
import copy
import re
import time
from typing import Any

from .errors import ApiError, bad_request, conflict
from .spec_bridge import capabilities

SCHEMA = "conformance.report.v1"
LEVELS = ("static", "dry", "motion")
REQUIRE_LEVELS = ("static", "dry")
STATUSES = ("passed", "failed", "skipped")
SKIP_KINDS = ("not_applicable", "blocked")
EVIDENCE_KINDS = ("conformance", "simulation")
MAX_CAPABILITIES = 200
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,99}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
FUTURE_SLACK_S = 300          # generated_at may be at most this far ahead of the server clock
DISCLAIMER = ("一致性测试报告摘要，仅供参考：报告由适配器所在进程自行产生、由上传者自报（self_reported），"
              "适配器代码或上传者都可以篡改它，云端只校验格式、内部一致性与 manifest 绑定，无法复核证据；"
              "它说明适配器声称实现了哪些标准能力，不等于现场验收证据（ADR-011）。")


def _invalid(message: str) -> ApiError:
    return bad_request(f"一致性报告无效：{message}", code="invalid_conformance_report")


def _bool(report: dict, key: str) -> bool:
    value = report.get(key)
    if not isinstance(value, bool):
        raise _invalid(f"{key} 须为 true/false")
    return value


def _opt_text(value: Any, label: str, max_len: int = 200) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > max_len:
        raise _invalid(f"{label} 须为不超过 {max_len} 个字符的字符串")
    return value


def _capability_id(value: Any) -> str:
    if not isinstance(value, str):
        raise _invalid("capabilities[].id 须为字符串")
    if value.startswith("x."):
        if not re.fullmatch(r"x\.[a-z0-9_]{1,40}\.[a-z0-9_.]{1,60}", value):
            raise _invalid(f"厂商扩展能力 ID 无效：{value!r}")
        return value
    try:
        cid = capabilities.normalize(value)
    except ValueError:
        raise _invalid(f"未知能力 {value!r}") from None
    if cid != value:
        raise _invalid(f"能力 ID 须为标准 ID（{value!r} → {cid}）")
    return cid


def _verdict(value: Any, where: str) -> dict:
    if not isinstance(value, dict):
        raise _invalid(f"{where} 须为对象")
    status = value.get("status")
    if status not in STATUSES:
        raise _invalid(f"{where}.status 须为 {'/'.join(STATUSES)}")
    out: dict[str, Any] = {"status": status}
    reason = value.get("reason")
    if reason is not None:
        if not isinstance(reason, str):
            raise _invalid(f"{where}.reason 须为字符串")
        if reason:
            out["reason"] = reason[:300]
    if status == "skipped":
        kind = value.get("skip_kind")
        if kind not in SKIP_KINDS:
            raise _invalid(f"{where}.skip_kind 须为 {'/'.join(SKIP_KINDS)}")
        out["skip_kind"] = kind
    elif value.get("skip_kind") is not None:
        raise _invalid(f"{where}.skip_kind 只用于 skipped")
    return out


def validate_report(report: Any) -> dict:
    """Check a ``conformance.report.v1`` and return its summary (without robot binding)."""
    if not isinstance(report, dict):
        raise _invalid("须为 JSON 对象（conformance.report.v1）")
    if report.get("schema") != SCHEMA:
        raise _invalid(f"schema 须为 {SCHEMA}")
    cid = report.get("conformance_id")
    if not isinstance(cid, str) or not _ID.match(cid):
        raise _invalid("conformance_id 须为 1–100 位字母、数字或 _.:-")
    evidence = report.get("evidence_kind")
    if evidence == "device_run":
        raise bad_request("一致性报告不能声称 evidence_kind=device_run：它不是现场运行证据（ADR-011）；"
                          "现场验收走现场测试流程（/v1/evaluate），不经由一致性报告上传",
                          code="device_run_refused")
    if evidence not in EVIDENCE_KINDS:
        raise _invalid(f"evidence_kind 须为 {'/'.join(EVIDENCE_KINDS)}")
    if report.get("field_acceptance") is not False:
        raise bad_request("一致性报告的 field_acceptance 必须为 false：一致性测试不等于现场验收（ADR-011）",
                          code="field_acceptance_refused")
    simulated = _bool(report, "simulated")
    if simulated != (evidence == "simulation"):
        raise _invalid("simulated 与 evidence_kind 不一致（仿真本体须为 simulation，真机须为 conformance）")
    level = report.get("level")
    if level not in LEVELS:
        raise _invalid(f"level 须为 {'/'.join(LEVELS)}")
    authorized = _bool(report, "hardware_authorized")
    if level == "motion" and not authorized:
        raise _invalid("motion 级别报告须为 hardware_authorized=true")
    index = LEVELS.index(level)
    requested = LEVELS[: index + 1]
    oks: dict[str, bool | None] = {}
    for i, lvl in enumerate(LEVELS):
        value = report.get(f"{lvl}_ok")
        if i <= index:
            if not isinstance(value, bool):
                raise _invalid(f"{lvl}_ok 须为 true/false（已请求该级别）")
        elif value is not None:
            raise _invalid(f"{lvl}_ok 须为 null（未请求该级别）")
        oks[lvl] = value
    if oks["dry"] is True and oks["static"] is not True or oks["motion"] is True and oks["dry"] is not True:
        raise _invalid("级别结论不一致：某级别通过要求更低级别都通过")
    levels = report.get("levels")
    if not isinstance(levels, dict):
        raise _invalid("levels 须为对象")
    level_info: dict[str, dict] = {}
    for lvl in LEVELS:
        info = levels.get(lvl)
        if not isinstance(info, dict):
            raise _invalid(f"levels.{lvl} 须为对象")
        if info.get("requested") is not (lvl in requested):
            raise _invalid(f"levels.{lvl}.requested 与 level 不一致")
        if info.get("ok") is not oks[lvl]:
            raise _invalid(f"levels.{lvl}.ok 与 {lvl}_ok 不一致")
        counts = {}
        for key in ("passed", "failed", "skipped", "blocked"):
            value = info.get(key, 0)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise _invalid(f"levels.{lvl}.{key} 须为非负整数")
            counts[key] = value
        if oks[lvl] is True and (counts["failed"] or counts["blocked"]):
            raise _invalid(f"levels.{lvl} 有失败或受阻用例却标为通过")
        level_info[lvl] = {"requested": lvl in requested, "ok": oks[lvl], **counts}
    robot = report.get("robot")
    if not isinstance(robot, dict) or not isinstance(robot.get("instance_id"), str) or not robot["instance_id"]:
        raise _invalid("robot.instance_id 缺失")
    caps_in = report.get("capabilities")
    if not isinstance(caps_in, list) or not caps_in or len(caps_in) > MAX_CAPABILITIES:
        raise _invalid(f"capabilities 须为 1–{MAX_CAPABILITIES} 项的列表")
    caps: dict[str, dict] = {}
    for item in caps_in:
        if not isinstance(item, dict):
            raise _invalid("capabilities[] 须为对象")
        cap_id = _capability_id(item.get("id"))
        if cap_id in caps:
            raise _invalid(f"能力重复：{cap_id}")
        declared = item.get("declared")
        if not isinstance(declared, bool):
            raise _invalid(f"{cap_id}.declared 须为 true/false")
        lv = item.get("levels")
        if not isinstance(lv, dict) or set(lv) != set(requested):
            raise _invalid(f"{cap_id}.levels 须恰好包含已请求的级别 {list(requested)}")
        verdicts = {lvl: _verdict(lv[lvl], f"{cap_id}.levels.{lvl}") for lvl in requested}
        for lvl, verdict in verdicts.items():
            if not declared and verdict["status"] != "skipped":
                raise _invalid(f"{cap_id} 未声明，却在 {lvl} 级别为 {verdict['status']}")
            if oks[lvl] is True and (verdict["status"] == "failed" or verdict.get("skip_kind") == "blocked"):
                raise _invalid(f"{lvl} 级别标为通过，但 {cap_id} 为 {verdict['status']}")
        caps[cap_id] = {"declared": declared, "levels": verdicts}
    if not any(c["declared"] for c in caps.values()):
        raise _invalid("报告中没有任何已声明的能力")
    aborted = report.get("aborted")
    if aborted is not None and not isinstance(aborted, (bool, str)):
        raise _invalid("aborted 须为字符串、布尔或 null")
    summary = {
        "conformance_id": cid, "level": level, "static_ok": oks["static"], "dry_ok": oks["dry"],
        "motion_ok": oks["motion"], "simulated": simulated, "evidence_kind": evidence, "field_acceptance": False,
        "hardware_authorized": authorized, "instance_id": robot["instance_id"],
        "generated_at": _generated_at(report.get("generated_at")),
        "report_manifest_digest": _digest(report.get("manifest_digest")),
        "runtime_version": _opt_text(report.get("runtime_version"), "runtime_version", 40),
        "spec_version": _opt_text(report.get("spec_version"), "spec_version", 20),
        "aborted": (aborted[:300] if isinstance(aborted, str) else aborted) or None,
        "final_latched": report.get("final_latched") if isinstance(report.get("final_latched"), bool) else None,
        "levels": level_info,
        "capabilities": caps,
    }
    return summary


def _digest(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _SHA256.match(value):
        raise _invalid("manifest_digest 须为 64 位小写十六进制 SHA-256")
    return value


def _generated_at(value: Any, now: float | None = None) -> str | None:
    """``YYYY-MM-DDTHH:MM:SSZ`` and not in the future (review Q4-17: a far-future stamp would make
    every later real report "outdated")."""
    if value is None:
        return None
    if not isinstance(value, str) or not _STAMP.match(value):
        raise _invalid("generated_at 须为 UTC 时间 YYYY-MM-DDTHH:MM:SSZ")
    try:
        stamp = calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        raise _invalid("generated_at 不是有效时间") from None
    if stamp > (time.time() if now is None else now) + FUTURE_SLACK_S:
        raise _invalid("generated_at 晚于服务器时间（超过 5 分钟）：拒绝未来时间戳")
    return value


def bind(summary: dict, robot, stored: dict | None, actor: str) -> dict:
    """Check the summary belongs to ``robot`` (a fleet ``Robot`` row) and stamp it."""
    if summary["instance_id"] != robot.instance_id:
        raise conflict(f"报告属于 {summary['instance_id']}，不是该机器人（{robot.instance_id}）",
                       code="conformance_robot_mismatch")
    if summary["simulated"] != bool(robot.simulated):
        raise conflict("报告的 simulated 与登记的 manifest 不一致", code="conformance_robot_mismatch")
    reported = summary.get("report_manifest_digest")
    if reported is not None and reported != robot.manifest_digest:
        # Review Q4-12: the report says which manifest it was run against; it must be the registered one.
        raise ApiError(409, "conformance_manifest_mismatch",
                       "报告是对另一份 manifest 运行的（manifest_digest 与登记的不同）：请用当前 manifest 重新登记机器人"
                       "后重新运行一致性测试再上传，旧报告不能绑定到新 manifest",
                       report_manifest_digest=reported, registered_manifest_digest=robot.manifest_digest)
    declared = {cid for cid, c in summary["capabilities"].items() if c["declared"]}
    registered = set(robot.capabilities or [])
    if declared != registered:
        raise ApiError(409, "conformance_manifest_mismatch",
                       "报告声明的能力与登记的 manifest 不一致：请先用当前 manifest 重新登记机器人（nx robot register），"
                       "再上传对该 manifest 运行的一致性报告",
                       only_in_report=sorted(declared - registered), only_in_manifest=sorted(registered - declared))
    if (stored and stored.get("manifest_digest") == robot.manifest_digest and stored.get("generated_at")
            and summary.get("generated_at") and summary["generated_at"] < stored["generated_at"]
            and stored.get("conformance_id") != summary["conformance_id"]):
        raise conflict(f"已保存更新的一致性报告 {stored['conformance_id']}（{stored['generated_at']}）",
                       code="conformance_outdated")
    return {**summary, "manifest_digest": robot.manifest_digest, "digest_verified": reported is not None,
            "uploaded_at": time.time(), "uploaded_by": actor, "self_reported": True}


def view(summary: dict | None, robot) -> dict | None:
    if not summary:
        return None
    out = copy.deepcopy(summary)
    out["stale"] = summary.get("manifest_digest") != robot.manifest_digest
    out["disclaimer"] = DISCLAIMER
    return out


def brief(summary: dict | None, robot) -> dict | None:
    if not summary:
        return None
    return {k: summary.get(k) for k in ("conformance_id", "level", "static_ok", "dry_ok", "motion_ok",
                                         "simulated", "evidence_kind", "generated_at", "digest_verified")} | {
        "stale": summary.get("manifest_digest") != robot.manifest_digest}


def check_require_level(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if value not in REQUIRE_LEVELS:
        raise bad_request(f"require_level 须为 {'/'.join(REQUIRE_LEVELS)}", code="invalid_level")
    return value


def usable_capabilities(robot, level: str) -> set[str]:
    """Capabilities of ``robot`` that passed ``level`` (and every lower level) in the latest report.

    Raises 409 ``conformance_required`` when there is no usable summary."""
    summary = robot.conformance
    reason = None
    if not summary:
        reason = "该机器人还没有上传一致性报告"
    elif summary.get("manifest_digest") != robot.manifest_digest:
        reason = "一致性报告对应的是旧的 manifest（机器人已重新登记），请重新运行并上传"
    elif summary.get("digest_verified") is not True:
        reason = "一致性报告没有 manifest_digest（旧运行时），无法确认它是对当前 manifest 运行的；请升级运行时后重新运行并上传"
    elif LEVELS.index(summary["level"]) < LEVELS.index(level):
        reason = f"最新一致性报告只到 {summary['level']} 级别"
    if reason:
        raise ApiError(409, "conformance_required",
                       f"{reason}；require_level={level} 需要 {level} 级别的一致性报告："
                       f"nx robot conformance <robot> --level {level} --upload", robot_id=robot.id,
                       require_level=level)
    upto = LEVELS[: LEVELS.index(level) + 1]
    usable = {cid for cid, c in summary["capabilities"].items()
              if c["declared"] and all(c["levels"].get(lvl, {}).get("status") == "passed" for lvl in upto)}
    if "safety.stop" not in usable:
        return set()
    return usable & set(robot.capabilities or [])


def restricted_manifest(robot, level: str) -> tuple[dict, list[str]]:
    """The registered manifest reduced to the capabilities usable at ``level``."""
    usable = usable_capabilities(robot, level)
    m2 = copy.deepcopy(robot.manifest)
    m2["capabilities"] = [c for c in m2.get("capabilities", []) if isinstance(c, dict) and c.get("id") in usable]
    if "model.install" not in usable:
        m2["model_runtimes"] = []
    return m2, sorted(usable)
