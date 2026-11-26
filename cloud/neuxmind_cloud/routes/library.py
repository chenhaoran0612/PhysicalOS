"""Material library (issue #33): ``/library/*``. See :mod:`neuxmind_cloud.library` for the rules.

Stable for the parser (#34) and for sending driver packages to a device (#31):
``GET /library/materials`` (filters), ``GET /library/materials/{id}`` (labels, size,
``sha256``, ``status``) and ``GET /library/materials/{id}/content`` (the bytes).
"""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Body, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse

from .. import library
from ..library_store import LibraryStore
from . import ctx, principal

router = APIRouter(prefix="/library")

# A stored file is only ever handed out as a download: a fixed binary type, never
# sniffed, never rendered, never run as a page of this origin.
_DOWNLOAD_HEADERS = {"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'; sandbox",
                     "Cache-Control": "private, no-store", "Cross-Origin-Resource-Policy": "same-origin"}


def _store(request: Request) -> LibraryStore:
    state = request.app.state
    if getattr(state, "library_store", None) is None:
        settings = ctx(request).settings
        state.library_store = LibraryStore(settings.effective_library_dir, settings.library_max_concurrent_completions)
    return state.library_store


def _disposition(filename: str) -> str:
    fallback = "".join(ch if 32 <= ord(ch) < 127 and ch not in '"\\' else "_" for ch in filename)
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"


@router.get("/formats")
def formats(request: Request):
    with ctx(request).db.transaction() as s:
        principal(request, s).require(library.READ)
        return library.formats_view(ctx(request).settings)


# ------------------------------------------------------------------ materials
@router.get("/materials")
def list_materials(request: Request, body_type: str | None = None, vendor: str | None = None, model: str | None = None,
                   version: str | None = None, kind: str | None = None, format: str | None = None,
                   status: str | None = None, sha256: str | None = None, q: str | None = None,
                   limit: int = library.DEFAULT_LIST, offset: int = 0, device_id: str | None = None):
    filters = {"body_type": body_type, "vendor": vendor, "model": model, "version": version, "kind": kind,
               "format": format, "status": status, "sha256": sha256, "q": q, "limit": limit, "offset": offset,
               "device_id": device_id}
    with ctx(request).db.transaction() as s:
        return library.list_materials(s, principal(request, s), filters)


@router.post("/materials/rebind")
def rebind_materials(request: Request, payload=Body(default=None)):
    """Issue #51: the materials still bound to one device go to that device's brand and model."""
    from .. import library_product
    return library_product.rebind(ctx(request).db, _store(request), lambda s: principal(request, s), payload)


@router.put("/materials/{material_id}/product")
def move_material(request: Request, material_id: str, payload=Body(default=None)):
    """Issue #51: one material goes to another brand and model."""
    from .. import library_product
    return library_product.move(ctx(request).db, lambda s: principal(request, s), material_id, payload)


@router.get("/materials/{material_id}")
def get_material(request: Request, material_id: str):
    with ctx(request).db.transaction() as s:
        return library.get_material(s, principal(request, s), material_id)


@router.get("/materials/{material_id}/content")
def material_content(request: Request, material_id: str):
    """The stored bytes (``Range`` is honoured). ``X-Checksum-SHA256`` is the digest of the whole file."""
    with ctx(request).db.transaction() as s:
        path, material = library.content_location(s, principal(request, s), _store(request), material_id)
    headers = {**_DOWNLOAD_HEADERS, "Content-Disposition": _disposition(material["filename"]),
               "X-Checksum-SHA256": material["sha256"]}
    return FileResponse(path, media_type=library.OCTET_STREAM, headers=headers)


@router.delete("/materials/{material_id}")
def delete_material(request: Request, material_id: str):
    return library.delete_material(ctx(request).db, _store(request), lambda s: principal(request, s), material_id)


# ------------------------------------------------------------------ uploads
@router.post("/uploads")
def start_upload(request: Request, payload: dict = Body(...)):
    """Declare a file. 201 for a new upload, 200 when an interrupted one is continued (``resumed``)."""
    context = ctx(request)
    view, created = library.start_upload(context.db, _store(request), context.settings,
                                         lambda s: principal(request, s), payload)
    return JSONResponse(view, status_code=201 if created else 200)


@router.get("/uploads/{upload_id}")
def upload_status(request: Request, upload_id: str):
    with ctx(request).db.transaction() as s:
        return library.upload_status(s, principal(request, s), upload_id)


@router.put("/uploads/{upload_id}/chunks/{index}")
async def put_chunk(request: Request, upload_id: str, index: int):
    """One chunk as ``application/octet-stream``; optional ``X-Chunk-SHA256`` is checked against the body."""
    data = await request.body()
    context = ctx(request)
    return await run_in_threadpool(
        library.receive_chunk, context.db, _store(request), context.settings, lambda s: principal(request, s),
        upload_id, index, data, request.headers.get("content-type", ""), request.headers.get("x-chunk-sha256"))


@router.post("/uploads/{upload_id}/complete", status_code=201)
def complete_upload(request: Request, upload_id: str):
    context = ctx(request)
    return library.complete_upload(context.db, _store(request), lambda s: principal(request, s), upload_id)


@router.delete("/uploads/{upload_id}")
def cancel_upload(request: Request, upload_id: str):
    context = ctx(request)
    return library.cancel_upload(context.db, _store(request), lambda s: principal(request, s), upload_id)
