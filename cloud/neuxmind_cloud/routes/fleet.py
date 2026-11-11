"""Robot fleet registry: validated ``robot.adapter.v2`` manifests per org, plus the latest
conformance report summary (INTERFACES_V1 §6.4, :mod:`neuxmind_cloud.conformance`)."""
from __future__ import annotations

from fastapi import APIRouter, Body, Request
from sqlalchemy import select

from .. import audit, catalog, conformance
from ..errors import bad_request, not_found
from ..models import Robot, now
from ..spec_bridge import manifest
from . import ctx, principal

router = APIRouter()


def robot_view(robot: Robot, *, full: bool = False) -> dict:
    view = {"id": robot.id, "instance_id": robot.instance_id, "name": robot.name, "family": robot.family,
            "simulated": robot.simulated, "manifest_digest": robot.manifest_digest,
            "capabilities": robot.capabilities, "model_runtimes": robot.model_runtimes,
            "created_at": robot.created_at, "updated_at": robot.updated_at}
    if full:
        view["manifest"] = robot.manifest
        view["conformance"] = conformance.view(robot.conformance, robot)
    else:
        view["conformance"] = conformance.brief(robot.conformance, robot)
    return view


@router.post("/robots", status_code=201)
def register_robot(request: Request, payload: dict = Body(...)):
    """``{"manifest": robot.adapter.v2}``. Re-registering the same instance_id updates it."""
    with ctx(request).db.transaction() as s:
        p = principal(request, s).require("robots")
        try:
            m2 = manifest.validate_manifest_v2(payload.get("manifest"))
        except ValueError as exc:
            raise bad_request(str(exc), code="invalid_manifest") from None
        digest = catalog.manifest_digest(m2)
        robot = s.execute(select(Robot).where(Robot.org_id == p.org_id, Robot.instance_id == m2["instance_id"])).scalar_one_or_none()
        fields = {"name": m2["name"], "family": m2["family"], "simulated": m2["simulated"], "manifest_digest": digest,
                  "capabilities": sorted(manifest.capability_ids(m2)), "model_runtimes": m2["model_runtimes"],
                  "manifest": m2}
        if robot is None:
            robot = Robot(org_id=p.org_id, instance_id=m2["instance_id"], **fields)
            s.add(robot)
        else:
            for key, value in fields.items():
                setattr(robot, key, value)
            robot.updated_at = now()
        s.flush()
        audit.record(s, "robot.register", p.actor, org_id=p.org_id, target=robot.id, detail={"digest": digest})
        return robot_view(robot, full=True)


@router.get("/robots")
def list_robots(request: Request):
    with ctx(request).db.transaction() as s:
        p = principal(request, s).require("robots")
        rows = s.execute(select(Robot).where(Robot.org_id == p.org_id).order_by(Robot.created_at)).scalars()
        return {"robots": [robot_view(r) for r in rows]}


def _own_robot(s, p, robot_id: str) -> Robot:
    robot = s.get(Robot, robot_id)
    if robot is None or robot.org_id != p.org_id:
        raise not_found("机器人不存在")
    return robot


@router.get("/robots/{robot_id}")
def get_robot(request: Request, robot_id: str):
    with ctx(request).db.transaction() as s:
        p = principal(request, s).require("robots")
        return robot_view(_own_robot(s, p, robot_id), full=True)


@router.post("/robots/{robot_id}/conformance")
def upload_conformance(request: Request, robot_id: str, payload: dict = Body(...)):
    """``{"report": conformance.report.v1}`` → the stored summary. Only reports with
    ``field_acceptance: false`` and ``evidence_kind`` ``conformance``/``simulation`` that match the
    registered manifest are accepted; the full report is not kept (only the summary)."""
    with ctx(request).db.transaction() as s:
        p = principal(request, s).require("robots")
        robot = _own_robot(s, p, robot_id)
        summary = conformance.validate_report(payload.get("report"))
        stored = conformance.bind(summary, robot, robot.conformance, p.actor)
        robot.conformance = stored
        robot.updated_at = now()
        s.flush()
        audit.record(s, "robot.conformance", p.actor, org_id=p.org_id, target=robot.id,
                     detail={"conformance_id": stored["conformance_id"], "level": stored["level"],
                             "static_ok": stored["static_ok"], "dry_ok": stored["dry_ok"],
                             "motion_ok": stored["motion_ok"], "simulated": stored["simulated"]})
        return {"robot_id": robot.id, "conformance": conformance.view(stored, robot)}


@router.post("/robots/{robot_id}/skills/check")
def check_skill(request: Request, robot_id: str, payload: dict = Body(...)):
    """``{"skill": skill.package.v1 metadata, "require_level"?: "static"|"dry"}`` →
    ``spec.check_compatibility`` against the registered manifest; with ``require_level`` only
    the capabilities that passed that conformance level count as provided."""
    with ctx(request).db.transaction() as s:
        p = principal(request, s).require("robots")
        robot = _own_robot(s, p, robot_id)
        level = conformance.check_require_level(payload.get("require_level"))
        skill = payload.get("skill")
        if isinstance(skill, dict):
            skill = {k: v for k, v in skill.items() if k != "files"}
        try:
            skill = manifest.validate_skill_package(skill)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise bad_request(f"技能包元数据无效：{exc}", code="invalid_skill") from None
        if level is None:
            m2, usable = robot.manifest, sorted(manifest.capability_ids(robot.manifest))
        else:
            m2, usable = conformance.restricted_manifest(robot, level)
        result = manifest.check_compatibility(skill, m2)
        not_verified = sorted(set(result["missing"]) & set(robot.capabilities or [])) if level else []
        return {"robot_id": robot.id, "skill": f"{skill['id']}@{skill['version']}", "require_level": level,
                "usable_capabilities": usable, "not_verified": not_verified, **result}


@router.delete("/robots/{robot_id}")
def delete_robot(request: Request, robot_id: str):
    with ctx(request).db.transaction() as s:
        p = principal(request, s).require("robots")
        robot = _own_robot(s, p, robot_id)
        s.delete(robot)
        audit.record(s, "robot.delete", p.actor, org_id=p.org_id, target=robot_id)
        return {"deleted": True, "id": robot_id}
