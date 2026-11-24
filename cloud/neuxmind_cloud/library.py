"""Material library (issue #33): uploads in chunks, stored files, and who may do what.

A material is one file (a document or a driver package) labelled with the body
type, the vendor and model, and a version. It belongs to one organisation.

Upload protocol (``routes/library.py``):

1. ``POST /library/uploads`` declares the file: name, size, SHA-256 and labels.
   The type is taken from the name's suffix and refused at once when it is not
   supported; size limits, the organisation quota and duplicates are checked
   before a byte is sent. If the same person already has an open upload of the
   same file with the same labels, that one is returned (``resumed``) with the
   chunks it already holds: this is how an interrupted upload continues.
2. ``PUT /library/uploads/{id}/chunks/{n}`` sends one chunk (every chunk has the
   size the server chose, the last one the remainder). Chunks may arrive in any
   order and more than once. Each request is authenticated and authorised
   again, and the first chunk is checked against the declared type.
3. ``POST /library/uploads/{id}/complete`` verifies size, SHA-256 and content
   type of the whole file and only then stores it. The material starts in
   「解析中」 (``parsing``); parsing itself is issue #34.

Nothing of a refused upload is kept. Database transactions are short: no
transaction is open while a file is hashed.

The stored bytes are only *recognised* as one of the accepted types
(``library_types``); nothing here opens an archive or interprets a document.
Whoever does that later (the parser, #34; sending a driver package to a device,
#31) must still treat the content as untrusted.
"""
from __future__ import annotations

import hashlib
import math
import re
import time
import unicodedata
from typing import Callable

from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from . import audit
from . import library_types as types
from . import library_bundle      # issue #43: kind "bundle"
from .auth import Principal, _throttle, bearer_token
from .config import Settings
from .db import Database, advisory_lock
from .errors import ApiError, bad_request, conflict, forbidden, not_found, unauthorized
from .library_models import (PARSE_STATES, PARSING, UPLOAD_ASSEMBLING, UPLOAD_OPEN, Material, MaterialUpload,
                             MaterialUploadChunk)
from .library_store import SHA256, UPLOAD_ID, LibraryStore, StagingLost, StorageFull
from .models import ApiKey, AuthSession, now
from .security import API_KEY_PREFIX, token_hash

# Which role has which of the two is in auth.LIBRARY_PERMISSIONS.
READ, WRITE = "library:read", "library:write"

MATERIAL_ID = re.compile(r"mat_[0-9a-f]{20}")
_BODY_TYPE = re.compile(r"[a-z0-9][a-z0-9_.\-]{0,63}")
# Issue #34: the device (a robot of the desktop app) a material belongs to, as that app names it.
DEVICE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:\-]{0,63}")
# The one route whose body is a chunk. Written out in full (prefix as app.API_PREFIX, id as
# library_store.UPLOAD_ID) and matched as a whole, so nothing else gets the larger body bound.
CHUNK_ROUTE = re.compile(r"/api/v1/library/uploads/upl_[0-9a-f]{20}/chunks/[0-9]{1,9}")
_UNPRINTABLE = types.UNPRINTABLE
MAX_LIST, DEFAULT_LIST, MAX_OFFSET = 200, 50, 100_000
MAX_DESCRIPTION = 2000
# However often it is continued, an upload must finish within this time of its start.
MAX_UPLOAD_AGE_SECONDS = 7 * 24 * 3600
# An upload being verified is kept this long, then counts as abandoned (the process died meanwhile).
ASSEMBLY_GRACE_SECONDS = 3600
# Files on the volume that no database row knows are removed once they are this old.
ORPHAN_AGE_SECONDS = 3600
OBJECT_SCAN_INTERVAL_SECONDS = 3600
PURGE_BATCH = 50
STALE_SCAN_LIMIT = 500
MISSING_SHOWN = 200
BUSY_RETRY_SECONDS = 5
OCTET_STREAM = "application/octet-stream"

Who = Callable[[Session], Principal]


# ------------------------------------------------------------------ the body bound of a chunk
def _live_credential(db: Database, authorization: str | None) -> bool:
    """Does the header carry a login session or an API key that exists right now?"""
    try:
        token = bearer_token(authorization)
    except ApiError:
        return False
    digest = token_hash(token)
    with db.transaction() as s:
        if token.startswith(API_KEY_PREFIX):
            return s.execute(select(ApiKey.id).where(ApiKey.key_hash == digest,
                                                     ApiKey.revoked_at.is_(None))).first() is not None
        row = s.get(AuthSession, digest)
        return row is not None and row.expires_at > now()


async def guard_body(scope, send, default: int) -> int | None:
    """Body bound of a request, for the body-size middleware (``app.py``), before any of the body is read.

    Every request keeps ``default`` except ``PUT …/library/uploads/<id>/chunks/<n>``:
    that one may be as large as a chunk, but only for a caller whose credential
    exists. Anyone else is answered 401 here, without the body being read
    (``None`` is returned then), so the larger bound is never spent on the
    unauthenticated. Permissions and ownership are checked by the route."""
    if scope.get("method") != "PUT" or not CHUNK_ROUTE.fullmatch(scope.get("path", "")):
        return default
    context = scope["app"].state.ctx
    header = next((value.decode("latin-1") for name, value in scope.get("headers", []) if name == b"authorization"), None)
    if await run_in_threadpool(_live_credential, context.db, header):
        return context.settings.library_chunk_bytes
    response = JSONResponse(unauthorized().body(), status_code=401)
    await send({"type": "http.response.start", "status": 401, "headers": response.raw_headers})
    await send({"type": "http.response.body", "body": response.body})
    return None


# ------------------------------------------------------------------ errors
def unsupported(reason: str) -> ApiError:
    return ApiError(415, "unsupported_type", f"{reason}。{types.supported_text()}", supported=list(types.EXTENSIONS))


def _storage_full() -> ApiError:
    return ApiError(507, "storage_full", "云端的存储空间不足，暂时不能接收文件；请联系平台运营人员")


def _writer(p: Principal) -> Principal:
    """Uploading and deleting need the write permission and a verified e-mail address:
    anyone can register, and an unverified account must not be able to fill the volume."""
    p.require(WRITE)
    if not p.user.email_verified:
        raise forbidden("请先验证邮箱，之后才能上传或删除资料（POST /auth/verify-email）", code="email_unverified")
    return p


# ------------------------------------------------------------------ input
def _digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def _label(value, name: str, max_len: int) -> str:
    if not isinstance(value, str):
        raise bad_request(f"需要填写{name}")
    if any(unicodedata.category(ch) in _UNPRINTABLE for ch in value):
        raise bad_request(f"{name}不能包含控制字符")
    text = " ".join(unicodedata.normalize("NFC", value).split())
    if not 1 <= len(text) <= max_len:
        raise bad_request(f"{name}需要 1–{max_len} 个字符")
    return text


def _description(value) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > MAX_DESCRIPTION:
        raise bad_request(f"说明须为不超过 {MAX_DESCRIPTION} 个字符的文字")
    if any(unicodedata.category(ch) in _UNPRINTABLE and ch not in "\n\r\t" for ch in value):
        raise bad_request("说明不能包含控制字符")
    return unicodedata.normalize("NFC", value).strip()


def _upload_fields(payload, settings: Settings) -> dict:
    """Validate what the client declares. Returns the columns shared by an upload and its material."""
    if not isinstance(payload, dict):
        raise bad_request("请求须为对象")
    try:
        filename, fmt = types.clean_filename(payload.get("filename"))
    except ValueError as exc:
        raise bad_request(str(exc)) from None
    if fmt is None:
        raise unsupported(f"不支持这种文件类型（{filename}）")
    fmt = library_bundle.requested_format(payload, fmt)          # issue #43: kind "bundle"
    size = payload.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size < 1:
        raise bad_request("size 须为文件的字节数（正整数）")
    limit = settings.library_max_package_bytes if fmt.kind == types.PACKAGE else settings.library_max_document_bytes
    limit = library_bundle.size_limit(fmt, settings, limit)
    if size > limit:
        what = "驱动安装包" if fmt.kind == types.PACKAGE else "文档"
        what = library_bundle.label(fmt, what)
        raise ApiError(413, "file_too_large", f"文件太大：{what}最大 {limit} 字节，这个文件 {size} 字节",
                       max_bytes=limit, kind=fmt.kind)
    sha256 = payload.get("sha256")
    if not isinstance(sha256, str) or not SHA256.fullmatch(sha256.lower()):
        raise bad_request("sha256 须为文件内容的 SHA-256（64 位十六进制）")
    body_type = payload.get("body_type")
    if not isinstance(body_type, str) or not _BODY_TYPE.fullmatch(body_type):
        raise bad_request("body_type 须为本体类型的标识（小写字母、数字、点、下划线、连字符，最多 64 个字符）")
    vendor, model = _label(payload.get("vendor"), "厂家", 100), _label(payload.get("model"), "型号", 100)
    version = _label(payload.get("version"), "版本", 50)
    device_id, device_name = _device(payload)
    library_bundle.require_device(fmt, device_id, device_name)
    labels = (body_type, vendor.casefold(), model.casefold(), version.casefold())
    # Issue #34: a material of one device is not a duplicate of the same file kept for another device.
    label_key = _digest(*labels, "device", device_id) if device_id else _digest(*labels)
    return {"filename": filename, "filename_key": _digest(filename.casefold()), "kind": fmt.kind, "format": fmt.id,
            "body_type": body_type, "vendor": vendor, "model": model, "version": version, "label_key": label_key,
            "description": _description(payload.get("description")), "size": size, "sha256": sha256.lower(),
            "device_id": device_id, "device_name": device_name}


def _device(payload: dict) -> tuple[str, str]:
    """``(device id, device name)`` of the device the file is uploaded for (issue #34); empty strings for none."""
    device_id = payload.get("device_id")
    if device_id in (None, ""):
        return "", ""
    if not isinstance(device_id, str) or not DEVICE_ID.fullmatch(device_id):
        raise bad_request("device_id 须为设备的编号（字母、数字、点、下划线、冒号、连字符，最多 64 个字符）")
    name = payload.get("device_name")
    return device_id, ("" if name in (None, "") else _label(name, "设备名称", 200))


_SHARED_COLUMNS = ("filename", "filename_key", "kind", "format", "body_type", "vendor", "model", "version", "label_key",
                   "description", "size", "sha256", "device_id", "device_name")


# ------------------------------------------------------------------ views
def material_view(m: Material) -> dict:
    return {"id": m.id, "filename": m.filename, "kind": m.kind, "format": m.format, "body_type": m.body_type,
            "vendor": m.vendor, "model": m.model, "version": m.version, "description": m.description,
            "size": m.size, "sha256": m.sha256, "status": m.status, "status_detail": m.status_detail,
            "device_id": m.device_id, "device_name": m.device_name,
            "created_by": m.created_by, "created_at": m.created_at, "updated_at": m.updated_at}


def _received(session: Session, upload_id: str) -> list[tuple[int, int]]:
    rows = session.execute(select(MaterialUploadChunk.chunk_index, MaterialUploadChunk.size)
                           .where(MaterialUploadChunk.upload_id == upload_id)
                           .order_by(MaterialUploadChunk.chunk_index)).all()
    return [(row[0], row[1]) for row in rows]


def upload_view(session: Session, u: MaterialUpload, *, resumed: bool = False) -> dict:
    received = _received(session, u.id)
    return {"id": u.id, "status": u.status, "resumed": resumed, "filename": u.filename, "kind": u.kind,
            "format": u.format, "body_type": u.body_type, "vendor": u.vendor, "model": u.model, "version": u.version,
            "description": u.description, "size": u.size, "sha256": u.sha256, "chunk_bytes": u.chunk_bytes,
            "device_id": u.device_id, "device_name": u.device_name,
            "chunk_count": u.chunk_count, "received": [index for index, _ in received],
            "received_count": len(received), "received_bytes": sum(size for _, size in received),
            "created_at": u.created_at, "expires_at": u.expires_at}


def formats_view(settings: Settings) -> dict:
    return {"formats": [{"id": f.id, "label": f.label, "kind": f.kind, "extensions": list(f.extensions)}
                        for f in types.FORMATS],
            "extensions": list(types.EXTENSIONS), "supported": types.supported_text(),
            "limits": {"document_bytes": settings.library_max_document_bytes,
                       "package_bytes": settings.library_max_package_bytes,
                       "org_quota_bytes": settings.library_org_quota_bytes,
                       "chunk_bytes": settings.library_chunk_bytes}}


# ------------------------------------------------------------------ lookups
def _own_material(session: Session, p: Principal, material_id: str) -> Material:
    material = session.get(Material, material_id) if MATERIAL_ID.fullmatch(material_id or "") else None
    if material is None or material.org_id != p.org_id:
        raise not_found("资料不存在")
    return material


def _locked_upload(session: Session, upload_id: str) -> MaterialUpload | None:
    return session.execute(select(MaterialUpload).where(MaterialUpload.id == upload_id)
                           .with_for_update()).scalar_one_or_none()


def _own_upload(session: Session, p: Principal, upload_id: str) -> MaterialUpload:
    """The caller's own unexpired upload, locked for this transaction; 404 for anything else."""
    upload = _locked_upload(session, upload_id) if UPLOAD_ID.fullmatch(upload_id or "") else None
    if upload is None or upload.org_id != p.org_id or upload.created_by != p.user.id or upload.expires_at <= now():
        raise not_found("这次上传不存在或已过期")
    return upload


def _drop_upload(session: Session, upload_id: str) -> None:
    session.execute(delete(MaterialUploadChunk).where(MaterialUploadChunk.upload_id == upload_id))
    session.execute(delete(MaterialUpload).where(MaterialUpload.id == upload_id))


def _duplicate(session: Session, org_id: str, fields: dict) -> ApiError | None:
    """同一份资料同一版本不重复收: the refusal, or ``None`` when the file is new."""
    labelled = select(Material).where(Material.org_id == org_id, Material.label_key == fields["label_key"])
    same = session.execute(labelled.where(Material.sha256 == fields["sha256"])).scalar_one_or_none()
    if same is not None:
        return ApiError(409, "material_exists", "这份资料的这个版本已经在资料库里，不重复接收", material_id=same.id)
    named = session.execute(labelled.where(Material.filename_key == fields["filename_key"])).scalar_one_or_none()
    if named is not None:
        return ApiError(409, "version_exists",
                        f"资料库里已有同名、同版本（{fields['version']}）的资料，内容与这个文件不同；"
                        "如果这是新版本，请改版本号后再上传", material_id=named.id)
    return None


def _keep_until(upload: MaterialUpload, settings: Settings) -> float:
    """When an upload that was just continued expires: the idle limit, never past its maximum age."""
    return min(now() + settings.library_upload_ttl_seconds, upload.created_at + MAX_UPLOAD_AGE_SECONDS)


# ------------------------------------------------------------------ room
def _used_bytes(session: Session, org_id: str) -> int:
    stored = session.execute(select(func.coalesce(func.sum(Material.size), 0))
                             .where(Material.org_id == org_id)).scalar_one()
    reserved = session.execute(select(func.coalesce(func.sum(MaterialUpload.size), 0))
                               .where(MaterialUpload.org_id == org_id, MaterialUpload.expires_at > now())).scalar_one()
    return int(stored) + int(reserved)


def _unwritten_bytes(session: Session) -> int:
    """Bytes the open uploads of *every* organisation may still write to the volume."""
    live = MaterialUpload.expires_at > now()
    declared = session.execute(select(func.coalesce(func.sum(MaterialUpload.size), 0)).where(live)).scalar_one()
    written = session.execute(select(func.coalesce(func.sum(MaterialUploadChunk.size), 0))
                              .join(MaterialUpload, MaterialUpload.id == MaterialUploadChunk.upload_id)
                              .where(live)).scalar_one()
    return max(0, int(declared) - int(written))


def _check_room(session: Session, org_id: str, size: int, settings: Settings, store: LibraryStore) -> None:
    """Refuse a new upload of ``size`` bytes when the organisation or the volume has no room for it."""
    open_count = session.execute(select(func.count()).select_from(MaterialUpload).where(
        MaterialUpload.org_id == org_id, MaterialUpload.expires_at > now())).scalar_one()
    if open_count >= settings.library_max_open_uploads:
        raise conflict(f"本组织同时进行的上传已达上限（{settings.library_max_open_uploads} 个）；"
                       "请等它们完成，或取消不再需要的上传", code="too_many_uploads")
    used = _used_bytes(session, org_id)
    if used + size > settings.library_org_quota_bytes:
        raise ApiError(409, "library_quota_exceeded",
                       f"本组织的资料库空间不够：上限 {settings.library_org_quota_bytes} 字节，已用 {used} 字节",
                       quota_bytes=settings.library_org_quota_bytes, used_bytes=used)
    stored = session.execute(select(func.count()).select_from(Material).where(Material.org_id == org_id)).scalar_one()
    if stored + open_count >= settings.library_org_max_materials:
        raise ApiError(409, "library_quota_exceeded",
                       f"本组织的资料份数已达上限（{settings.library_org_max_materials} 份）",
                       max_materials=settings.library_org_max_materials)
    # The volume: what is free now, less what uploads already admitted (of any organisation) have yet to write.
    if store.free_bytes() - _unwritten_bytes(session) - size < settings.library_min_free_bytes:
        raise _storage_full()


# ------------------------------------------------------------------ clean-up
def purge_expired(session: Session, store: LibraryStore) -> int:
    """Drop uploads nobody continued in time, with their bytes, and staging files without an upload."""
    expired = session.execute(select(MaterialUpload.id).where(MaterialUpload.expires_at <= now())
                              .limit(PURGE_BATCH).with_for_update(skip_locked=True)).scalars().all()
    for upload_id in expired:
        _drop_upload(session, upload_id)
        store.discard(upload_id)
    # Files of uploads that no longer exist (a process died between two steps). Uploads that are merely
    # idle are skipped, so they cannot hide the orphans behind them.
    stale = store.stale_staging(ORPHAN_AGE_SECONDS)[:STALE_SCAN_LIMIT]
    known = set(session.execute(select(MaterialUpload.id).where(MaterialUpload.id.in_(stale))).scalars()) if stale else set()
    for upload_id in [candidate for candidate in stale if candidate not in known][:PURGE_BATCH]:
        store.discard(upload_id)
    return len(expired)


def release_object(db: Database, store: LibraryStore, org_id: str, sha256: str) -> bool:
    """Remove a stored file that no material of the organisation refers to (any more).

    Its own transaction, under the lock a completing upload of the same
    content takes: either that upload's record is counted here, or it finds
    the file gone and puts its own verified copy in place."""
    with db.transaction() as s:
        advisory_lock(s, f"library:{org_id}:{sha256}")
        referenced = s.execute(select(func.count()).select_from(Material).where(
            Material.org_id == org_id, Material.sha256 == sha256)).scalar_one()
        if referenced:
            return False
        store.delete_object(org_id, sha256)
        return True


_last_object_scan = 0.0


def sweep(db: Database, settings: Settings, store: LibraryStore | None = None, *, scan_objects: bool | None = None) -> dict:
    """Periodic clean-up (``maintenance.sweep``): expired uploads; and, at most once an hour per
    process, stored files left without a record by a process that died between two steps."""
    global _last_object_scan
    store = store or LibraryStore(settings.effective_library_dir)
    counts = {"library_uploads_expired": 0, "library_orphan_files": 0}
    with db.transaction() as s:
        counts["library_uploads_expired"] = purge_expired(s, store)
    due = time.monotonic() - _last_object_scan >= OBJECT_SCAN_INTERVAL_SECONDS or _last_object_scan == 0.0
    if scan_objects if scan_objects is not None else due:
        _last_object_scan = time.monotonic()
        for org_id, sha256 in store.old_objects(ORPHAN_AGE_SECONDS):
            counts["library_orphan_files"] += release_object(db, store, org_id, sha256)
    return counts


# ------------------------------------------------------------------ start
def _open_upload(session: Session, p: Principal, payload, settings: Settings, store: LibraryStore) -> tuple[dict, bool]:
    fields = _upload_fields(payload, settings)
    advisory_lock(session, f"library-quota:{p.org_id}")
    refusal = _duplicate(session, p.org_id, fields)
    if refusal is not None:
        raise refusal
    # The same person offering the same file with the same labels again continues what is there. That
    # includes an upload stuck in its last step (the process died while verifying): "complete" may be retried.
    existing = session.execute(select(MaterialUpload).where(
        MaterialUpload.org_id == p.org_id, MaterialUpload.expires_at > now(),
        MaterialUpload.created_by == p.user.id, MaterialUpload.status.in_((UPLOAD_OPEN, UPLOAD_ASSEMBLING)),
        MaterialUpload.sha256 == fields["sha256"], MaterialUpload.size == fields["size"],
        MaterialUpload.label_key == fields["label_key"], MaterialUpload.filename_key == fields["filename_key"],
        MaterialUpload.chunk_bytes == settings.library_chunk_bytes).with_for_update()).scalars().first()
    if existing is not None and existing.status == UPLOAD_OPEN and existing.chunk_attempts >= _max_attempts(existing):
        # It can take no more chunks, so it cannot be continued: it makes way for a fresh one.
        _drop_upload(session, existing.id)
        store.discard(existing.id)
        existing = None
    library_bundle.refuse_other_kind(existing, fields)          # issue #43: never continue an upload of the other kind
    if existing is not None:
        existing.expires_at = max(existing.expires_at, _keep_until(existing, settings))
        existing.description = fields["description"]
        return upload_view(session, existing, resumed=True), False
    _check_room(session, p.org_id, fields["size"], settings, store)
    upload = MaterialUpload(org_id=p.org_id, created_by=p.user.id, chunk_bytes=settings.library_chunk_bytes,
                            chunk_count=math.ceil(fields["size"] / settings.library_chunk_bytes),
                            expires_at=now() + settings.library_upload_ttl_seconds, **fields)
    session.add(upload)
    session.flush()
    audit.record(session, "library.upload_start", p.actor, org_id=p.org_id, target=upload.id,
                 detail={"filename": upload.filename, "size": upload.size, "sha256": upload.sha256,
                         "format": upload.format})
    return upload_view(session, upload), True


def start_upload(db: Database, store: LibraryStore, settings: Settings, who: Who, payload) -> tuple[dict, bool]:
    """``(upload view, created)``. ``created`` is false when an open upload of the same file is continued."""
    # 1. Who is asking. Every attempt counts towards the hourly limit, the refused ones too:
    #    committed here, so a refusal further down cannot take the count back.
    with db.transaction() as s:
        p = _writer(who(s))
        _throttle(s, "library_upload", p.user.id, settings.library_uploads_per_hour, 3600,
                  "开始上传的次数过于频繁，请稍后再试")
    # 2. Clear away what expired (kept whatever happens to this request).
    with db.transaction() as s:
        purge_expired(s, store)
    # 3. The upload itself.
    with db.transaction() as s:
        return _open_upload(s, _writer(who(s)), payload, settings, store)


def upload_status(session: Session, p: Principal, upload_id: str) -> dict:
    p.require(WRITE)
    return upload_view(session, _own_upload(session, p, upload_id))


# ------------------------------------------------------------------ chunks
def _max_attempts(upload: MaterialUpload) -> int:
    """Chunk requests one upload may make: every chunk three times over, plus a margin."""
    return upload.chunk_count * 3 + 16


def _expected_length(upload: MaterialUpload, index: int) -> int:
    if not 0 <= index < upload.chunk_count:
        raise ApiError(400, "invalid_chunk", f"分片序号须为 0–{upload.chunk_count - 1}", chunk_count=upload.chunk_count)
    if index < upload.chunk_count - 1:
        return upload.chunk_bytes
    return upload.size - upload.chunk_bytes * (upload.chunk_count - 1)


def _reject(db: Database, store: LibraryStore, upload_id: str, org_id: str, actor: str, problem: ApiError, *,
            only_if_open: bool = False) -> None:
    """Forget a refused upload: no row, no bytes, one audit line.

    ``only_if_open``: the caller does not hold the upload's exclusive lock (a
    chunk that failed its check). An upload that is being completed is then
    left alone: the completion decides about it, on the whole file."""
    with db.transaction() as s:
        if only_if_open:
            upload = _locked_upload(s, upload_id)
            if upload is None or upload.status != UPLOAD_OPEN:
                return
        _drop_upload(s, upload_id)
        audit.record(s, "library.upload_rejected", actor, org_id=org_id, target=upload_id, detail={"code": problem.code})
    store.discard(upload_id)


class _Gone(Exception):
    def __init__(self, deleted: bool):
        self.deleted = deleted


def _admit_chunk(session: Session, p: Principal, upload_id: str, index: int, data: bytes, content_type: str,
                 settings: Settings, store: LibraryStore) -> MaterialUpload:
    """Is this chunk expected from this caller? Counts the attempt before anything is written."""
    upload = _own_upload(session, p, upload_id)
    if content_type.split(";")[0].strip().lower() != OCTET_STREAM:
        raise ApiError(415, "octet_stream_required", f"分片须以 {OCTET_STREAM} 发送")
    if upload.status != UPLOAD_OPEN:
        raise conflict("这次上传正在收尾，不再接收分片", code="upload_not_open")
    expected = _expected_length(upload, index)
    if len(data) != expected:
        raise ApiError(400, "invalid_chunk", f"第 {index} 片应为 {expected} 字节，收到 {len(data)} 字节",
                       expected_bytes=expected)
    if upload.chunk_attempts >= _max_attempts(upload):
        raise ApiError(429, "chunk_attempts_exceeded", "这次上传发送分片的次数过多；请取消后重新上传")
    if store.free_bytes() - len(data) < settings.library_min_free_bytes:
        raise _storage_full()
    upload.chunk_attempts += 1
    return upload


def _write_chunk(db: Database, store: LibraryStore, settings: Settings, upload_id: str, index: int, offset: int,
                 data: bytes, digest: str) -> dict:
    """Write and record one chunk while the upload is still open (shared lock: a completion waits for us)."""
    with store.staging(upload_id, exclusive=False, create=True) as staging, db.transaction() as s:
        upload = _locked_upload(s, upload_id)
        if upload is None or upload.status != UPLOAD_OPEN or upload.expires_at <= now():
            raise _Gone(deleted=upload is None)
        staging.write(offset, data)
        row = s.get(MaterialUploadChunk, (upload_id, index))
        if row is None:
            s.add(MaterialUploadChunk(upload_id=upload_id, chunk_index=index, size=len(data), sha256=digest))
        else:
            row.size, row.sha256, row.received_at = len(data), digest, now()
        upload.updated_at, upload.expires_at = now(), _keep_until(upload, settings)
        s.flush()
        received = _received(s, upload_id)
        return {"upload_id": upload_id, "index": index, "sha256": digest, "received_count": len(received),
                "chunk_count": upload.chunk_count, "received_bytes": sum(size for _, size in received)}


def receive_chunk(db: Database, store: LibraryStore, settings: Settings, who: Who, upload_id: str, index: int,
                  data: bytes, content_type: str, digest: str | None) -> dict:
    # 1. Who is asking, and is this chunk expected?
    with db.transaction() as s:
        p = _writer(who(s))
        upload = _admit_chunk(s, p, upload_id, index, data, content_type, settings, store)
        fmt, whole = types.BY_ID[upload.format], upload.size == len(data)
        org_id, actor, offset = upload.org_id, p.actor, index * upload.chunk_bytes
    # 2. Is it what it should be?
    actual = hashlib.sha256(data).hexdigest()
    if digest is not None and digest.strip().lower() != actual:
        raise ApiError(400, "chunk_digest_mismatch", "分片内容与随它发送的 SHA-256 不一致，请重发这一片")
    if index == 0:
        try:
            types.check_head(fmt, data, whole=whole)
        except types.Unsupported as exc:
            problem = unsupported(f"文件内容与它的类型不符：{exc}")
            _reject(db, store, upload_id, org_id, actor, problem, only_if_open=True)
            raise problem from None
    # 3. Write it and record it.
    try:
        return _write_chunk(db, store, settings, upload_id, index, offset, data, actual)
    except _Gone as gone:
        if gone.deleted:
            store.discard(upload_id)            # we may just have created the file of an upload that is gone
        raise not_found("这次上传不存在或已结束") from None
    except StorageFull:
        raise _storage_full() from None


# ------------------------------------------------------------------ complete
def _verify(staging, fields: dict) -> ApiError | None:
    """Size, SHA-256 and type of the complete file; the refusal, or ``None`` when all three hold."""
    mismatch = ApiError(409, "digest_mismatch", "文件内容的 SHA-256 与开始上传时声明的不一致，文件没有保存；请重新上传")
    if staging.size() != fields["size"] or staging.sha256() != fields["sha256"]:
        return mismatch
    try:
        types.check_file(types.BY_ID[fields["format"]], staging.path, fields["size"])
    except types.Unsupported as exc:
        return unsupported(f"文件内容与它的类型不符：{exc}")
    return None


def _store_material(db: Database, store: LibraryStore, staging, upload_id: str, org_id: str, user_id: str,
                    actor: str, fields: dict, keep_existing: bool) -> dict | ApiError:
    """Record the verified file and move it into place; the refusal when it turns out to be a duplicate."""
    with db.transaction() as s:
        advisory_lock(s, f"library:{org_id}:{fields['sha256']}",
                      f"library-name:{org_id}:{fields['label_key']}:{fields['filename_key']}")
        if s.get(MaterialUpload, upload_id) is None:
            raise not_found("这次上传不存在或已结束")
        refusal = _duplicate(s, org_id, fields)
        if refusal is not None:
            return refusal
        material = Material(org_id=org_id, created_by=user_id, status=PARSING, **fields)
        s.add(material)
        library_bundle.on_stored(material)                       # issue #43: a bundle is usable at once
        try:
            s.flush()                               # the unique constraints speak before the file moves
        except IntegrityError:
            s.rollback()
            return conflict("这份资料刚刚已被接收，不重复接收", code="material_exists")
        store.promote(staging, org_id, fields["sha256"], keep_existing=keep_existing)
        _drop_upload(s, upload_id)
        audit.record(s, "library.material_create", actor, org_id=org_id, target=material.id,
                     detail={"filename": material.filename, "size": material.size, "sha256": material.sha256,
                             "format": material.format, "body_type": material.body_type})
        return material_view(material)


def _begin_completion(session: Session, p: Principal, upload_id: str) -> dict:
    """All chunks are there: close the upload to further chunks and return what it declared."""
    upload = _own_upload(session, p, upload_id)
    have = {index for index, _ in _received(session, upload_id)}
    missing = [index for index in range(upload.chunk_count) if index not in have]
    if missing:
        raise ApiError(409, "chunks_missing", f"还有 {len(missing)} 片没有收到，不能完成",
                       missing=missing[:MISSING_SHOWN], missing_count=len(missing))
    upload.status, upload.updated_at = UPLOAD_ASSEMBLING, now()
    upload.expires_at = now() + ASSEMBLY_GRACE_SECONDS
    return {column: getattr(upload, column) for column in _SHARED_COLUMNS}


def _lost(db: Database, upload_id: str) -> ApiError:
    with db.transaction() as s:
        _drop_upload(s, upload_id)
    return conflict("这次上传的数据已经不在了，请重新上传", code="upload_lost")


def complete_upload(db: Database, store: LibraryStore, who: Who, upload_id: str) -> dict:
    if not store.verify_slots.acquire(blocking=False):
        raise ApiError(503, "library_busy", "云端正在核对其他文件，请稍后再试", retry_after=BUSY_RETRY_SECONDS)
    try:
        with db.transaction() as s:
            p = _writer(who(s))
            fields = _begin_completion(s, p, upload_id)
            org_id, user_id, actor = p.org_id, p.user.id, p.actor
        # Exclusive lock: every chunk writer has finished, and none starts (the upload is no longer open).
        with store.staging(upload_id, exclusive=True, create=False) as staging:
            outcome = _verify(staging, fields)
            if outcome is None:
                staging.sync()
                keep = store.object_matches(org_id, fields["sha256"])
                outcome = _store_material(db, store, staging, upload_id, org_id, user_id, actor, fields, keep)
            if isinstance(outcome, ApiError):
                _reject(db, store, upload_id, org_id, actor, outcome)
                raise outcome
            return outcome
    except (FileNotFoundError, StagingLost):
        raise _lost(db, upload_id) from None
    finally:
        store.verify_slots.release()


def cancel_upload(db: Database, store: LibraryStore, who: Who, upload_id: str) -> dict:
    with db.transaction() as s:
        p = who(s).require(WRITE)
        _own_upload(s, p, upload_id)
        org_id, actor = p.org_id, p.actor
    def forget(session: Session) -> None:
        if session.get(MaterialUpload, upload_id) is None:             # a completion stored it while we waited
            raise not_found("这次上传不存在或已结束")
        _drop_upload(session, upload_id)
        audit.record(session, "library.upload_cancel", actor, org_id=org_id, target=upload_id)

    try:
        with store.staging(upload_id, exclusive=True, create=False):   # waits for a completion in flight
            with db.transaction() as s:
                forget(s)
            store.discard(upload_id)
    except FileNotFoundError:
        with db.transaction() as s:                                    # no chunk had arrived yet
            forget(s)
    return {"cancelled": True, "id": upload_id}


# ------------------------------------------------------------------ materials
def _like(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def list_materials(session: Session, p: Principal, filters: dict) -> dict:
    p.require(READ)
    limit, offset = filters.get("limit", DEFAULT_LIST), filters.get("offset", 0)
    if not 1 <= limit <= MAX_LIST or not 0 <= offset <= MAX_OFFSET:
        raise bad_request(f"limit 须为 1–{MAX_LIST}，offset 须为 0–{MAX_OFFSET}")
    query = select(Material).where(Material.org_id == p.org_id)
    for column, allowed in (("kind", types.KINDS), ("status", PARSE_STATES), ("format", tuple(types.BY_ID))):
        value = filters.get(column)
        if value is None:
            continue
        if value not in allowed:
            raise bad_request(f"{column} 须为 {allowed} 之一")
        query = query.where(getattr(Material, column) == value)
    if filters.get("body_type") is not None:
        query = query.where(Material.body_type == filters["body_type"])
    if filters.get("sha256") is not None:
        query = query.where(Material.sha256 == filters["sha256"].lower())
    if filters.get("device_id") is not None:                       # issue #34: the materials of one device
        query = query.where(Material.device_id == filters["device_id"])
    for column in ("vendor", "model", "version"):
        if filters.get(column) is not None:
            query = query.where(func.lower(getattr(Material, column)) == filters[column].strip().lower())
    if filters.get("q"):
        pattern = _like(filters["q"].strip().lower()[:100])
        query = query.where(or_(*(func.lower(column).like(pattern, escape="\\")
                                  for column in (Material.filename, Material.vendor, Material.model))))
    total = session.execute(select(func.count()).select_from(query.subquery())).scalar_one()
    rows = session.execute(query.order_by(Material.created_at.desc(), Material.id.desc())
                           .limit(limit).offset(offset)).scalars()
    return {"materials": [material_view(m) for m in rows], "total": total, "limit": limit, "offset": offset}


def get_material(session: Session, p: Principal, material_id: str) -> dict:
    p.require(READ)
    return material_view(_own_material(session, p, material_id))


def content_location(session: Session, p: Principal, store: LibraryStore, material_id: str) -> tuple[str, dict]:
    """``(path of the stored file, material view)`` for a download."""
    p.require(READ)
    material = _own_material(session, p, material_id)
    if store.object_size(material.org_id, material.sha256) is None:
        raise not_found("这份资料的文件已经不在云端", code="content_missing")
    return store.object_path(material.org_id, material.sha256), material_view(material)


def delete_material(db: Database, store: LibraryStore, who: Who, material_id: str) -> dict:
    """Delete the record, then (committed) the file when nothing else of the organisation uses it.
    A process that dies in between leaves a file without a record, which :func:`sweep` removes."""
    with db.transaction() as s:
        p = _writer(who(s))
        material = _own_material(s, p, material_id)
        org_id, sha256, filename = material.org_id, material.sha256, material.filename
        s.delete(material)
        audit.record(s, "library.material_delete", p.actor, org_id=org_id, target=material_id,
                     detail={"filename": filename, "sha256": sha256})
    release_object(db, store, org_id, sha256)
    return {"deleted": True, "id": material_id}


def set_parse_state(session: Session, material: Material, status: str, detail: str = "") -> None:
    """For the parser (issue #34): move a material out of 「解析中」."""
    if status not in PARSE_STATES:
        raise ValueError(f"status must be one of {PARSE_STATES}")
    material.status, material.status_detail, material.updated_at = status, detail[:2000], now()
