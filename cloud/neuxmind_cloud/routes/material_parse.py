"""Parsing results of library materials (issue #34). The rules are in :mod:`neuxmind_cloud.material_parse`.

* ``GET  /library/materials/{id}/parse``          the result (``library:read``)
* ``PUT  /library/materials/{id}/parse``          a person changes it: ``{revision, values, not_applicable}``
* ``POST /library/materials/{id}/parse/confirm``  … and confirms it: it becomes 「可用」
* ``POST /library/materials/{id}/parse/retry``    parse again from the start

Changing, confirming and parsing again need ``library:write`` and a verified
e-mail address, like uploading. ``revision`` must be the one last read: a
result changed meanwhile (by someone else, or by parsing again) is not
overwritten (409 ``parse_changed``).
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Request

from .. import material_parse
from . import ctx, principal

router = APIRouter(prefix="/library/materials")


@router.get("/{material_id}/parse")
def get_result(request: Request, material_id: str):
    with ctx(request).db.transaction() as s:
        return material_parse.get_result(s, principal(request, s), material_id)


@router.put("/{material_id}/parse")
def save(request: Request, material_id: str, payload: dict = Body(...)):
    return material_parse.save(ctx(request).db, lambda s: principal(request, s), material_id, payload)


@router.post("/{material_id}/parse/confirm")
def confirm(request: Request, material_id: str, payload: dict = Body(...)):
    return material_parse.confirm(ctx(request).db, lambda s: principal(request, s), material_id, payload)


@router.post("/{material_id}/parse/retry")
def retry(request: Request, material_id: str):
    return material_parse.retry(ctx(request).db, lambda s: principal(request, s), material_id)
