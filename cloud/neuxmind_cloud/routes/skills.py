"""The cloud skill store (issue #43): ``/skills``. Rules in :mod:`neuxmind_cloud.skill_store`.

Reading needs ``skills:read`` (every role), publishing and deleting ``skills:write`` (owner, admin,
developer) and a verified e-mail address; both are API-key scopes too. Releases of another organisation
do not exist for the caller (404 ``skill_not_found``)."""
from __future__ import annotations

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

from .. import skill_store
from . import client_ip, ctx, principal

router = APIRouter(prefix="/skills")


@router.post("/releases")
def create_release(request: Request, payload=Body(...)):
    """``{package: skill.package.v1 with files, notes?}`` → 201 the release; 200 with ``existing: true``
    when exactly this release is already there; 409 ``skill_version_exists`` when the version exists
    with other content."""
    context = ctx(request)
    with context.db.transaction() as s:
        view, created = skill_store.create_release(s, principal(request, s), payload, context.settings,
                                                   ip=client_ip(request))
    return JSONResponse(view, status_code=201 if created else 200)


@router.get("")
def list_skills(request: Request):
    with ctx(request).db.transaction() as s:
        return skill_store.list_skills(s, principal(request, s))


@router.get("/{skill_id}")
def get_skill(request: Request, skill_id: str):
    with ctx(request).db.transaction() as s:
        return skill_store.get_skill(s, principal(request, s), skill_id)


@router.get("/{skill_id}/releases/{version}")
def get_release(request: Request, skill_id: str, version: str):
    with ctx(request).db.transaction() as s:
        return skill_store.get_release(s, principal(request, s), skill_id, version)


@router.get("/{skill_id}/releases/{version}/package")
def get_package(request: Request, skill_id: str, version: str):
    """The release and its package with files: what a console downloads to install it on a robot."""
    with ctx(request).db.transaction() as s:
        return skill_store.get_package(s, principal(request, s), skill_id, version)


@router.put("/{skill_id}/releases/{version}/product")
def set_product(request: Request, skill_id: str, version: str, payload=Body(...)):
    """Issue #48: ``{vendor, model}`` for a release that names none yet; 409 ``skill_product_set`` when it names another."""
    with ctx(request).db.transaction() as s:
        return skill_store.set_product(s, principal(request, s), skill_id, version, payload, ip=client_ip(request))


@router.delete("/{skill_id}/releases/{version}")
def delete_release(request: Request, skill_id: str, version: str):
    with ctx(request).db.transaction() as s:
        return skill_store.delete_release(s, principal(request, s), skill_id, version, ip=client_ip(request))
