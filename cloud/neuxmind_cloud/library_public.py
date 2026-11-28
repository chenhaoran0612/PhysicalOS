"""Issue #78: the public library (公共资料库) — materials every account sees.

The user's decision of 2026-11-28: the collected robot models (URDF of every brand and model of the
catalogue) and the device materials (Unitree R1-7A arm, WonderDuck) go into one public library that any
account can see, instead of into one account's library.

How it is kept:

* Public materials are ordinary :class:`Material` rows of one fixed organisation, :data:`PUBLIC_ORG_ID`
  (「公共资料库」, no members). Their files live in the library volume under that organisation like
  any other, and the parser (WeKnora, rules) treats them like any other.
* Every account **reads** them: they are in its list (``public: true``), can be opened, downloaded and
  their parse result read; repair sessions, package delivery and the simulation find them by brand and
  model like the account's own.
* Only **platform operators** change them (delete, edit or confirm the parse result, change the brand
  and model). Anyone else gets 403 ``public_read_only``; the list says per row whether the caller may
  change it (``read_only``).
* They are put there by an operator on the server: ``python -m neuxmind_cloud library-import --dir DIR``
  reads ``DIR/manifest.json`` (written by ``scripts/library_collect.py``) and stores each file it lists
  with the same checks as an upload (labels, type, size, content). Running it again stores only what is
  new; a file under a name that already holds other content is reported and left alone.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
from pathlib import Path

from sqlalchemy.exc import IntegrityError

from . import audit, library, library_bundle
from .config import Settings
from .db import Database, advisory_lock
from .errors import ApiError, forbidden
from .library_models import PARSING, Material
from .library_store import LibraryStore, StorageFull
from .models import Organization

PUBLIC_ORG_ID = "org_" + "0" * 20         # the shape of an organisation id (library_store.ORG_ID)
PUBLIC_ORG_NAME = "公共资料库"
IMPORTER = "system:library-import"        # created_by / audit actor of imported materials
MANIFEST = "manifest.json"
MANIFEST_VERSION = 1
LABELS = ("kind", "body_type", "vendor", "model", "version", "description")
_BLOCK = 1024 * 1024


# ------------------------------------------------------------------ who sees and who changes
def orgs(p) -> tuple[str, str]:
    """The organisations whose materials ``p`` reads: its own and the public library."""
    return (p.org_id, PUBLIC_ORG_ID)


def is_public(org_id: str) -> bool:
    return org_id == PUBLIC_ORG_ID


def may_change(p, org_id: str) -> bool:
    return org_id == p.org_id or (is_public(org_id) and bool(getattr(p, "platform_operator", False)))


def check(p, org_id: str, read: bool) -> bool:
    """Whether ``p`` may use a material of ``org_id``: read (``read``) or change it.
    A public material that ``p`` may only read is refused with 403 ``public_read_only``."""
    if org_id == p.org_id or (read and is_public(org_id)):
        return True
    if is_public(org_id):
        if may_change(p, org_id):
            return True
        raise forbidden("这是公共资料库里的资料，所有账号都能查看，只有平台运营人员能修改、确认或删除",
                        code="public_read_only")
    return False


def marks(p, m: Material) -> dict:
    """What a material says besides itself: whether it is public, and whether ``p`` may only read it."""
    return {"public": is_public(m.org_id), "read_only": not may_change(p, m.org_id)}


def result_marks(p, material: Material, view: dict) -> dict:
    """A parse result with the same two marks (its ``editable`` keeps meaning "parsing is done")."""
    return {**view, **marks(p, material)}


# ------------------------------------------------------------------ the organisation
def ensure_org(session) -> None:
    if session.get(Organization, PUBLIC_ORG_ID) is None:
        session.add(Organization(id=PUBLIC_ORG_ID, name=PUBLIC_ORG_NAME, personal=False))
        session.flush()


# ------------------------------------------------------------------ import
class ManifestError(ValueError):
    """The manifest cannot be used at all; nothing was imported."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_BLOCK), b""):
            digest.update(block)
    return digest.hexdigest()


def read_manifest(directory: Path) -> list[dict]:
    """The entries of ``directory/manifest.json``, each with its file resolved inside ``directory``."""
    base = directory.resolve()
    try:
        data = json.loads((base / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ManifestError(f"读不了 {MANIFEST}：{exc}") from None
    if not isinstance(data, dict) or data.get("version") != MANIFEST_VERSION or not isinstance(data.get("materials"), list):
        raise ManifestError(f"{MANIFEST} 须为 {{\"version\": {MANIFEST_VERSION}, \"materials\": [...]}}")
    entries = []
    for index, item in enumerate(data["materials"]):
        if not isinstance(item, dict) or not isinstance(item.get("file"), str) or not item["file"]:
            raise ManifestError(f"第 {index + 1} 项没有 file")
        try:
            path = (base / item["file"]).resolve()
        except (OSError, ValueError):
            raise ManifestError(f"第 {index + 1} 项的文件名不能用：{item['file']!r}") from None
        if base not in path.parents:
            raise ManifestError(f"第 {index + 1} 项的文件不在 {base} 里：{item['file']}")
        entries.append({**{k: item.get(k) for k in LABELS}, "file": item["file"], "path": path})
    return entries


def _payload(entry: dict, path: Path) -> dict:
    payload = {"filename": entry.get("filename") or path.name, "size": path.stat().st_size, "sha256": _sha256(path),
               **{k: entry[k] for k in LABELS if entry.get(k) not in (None, "")}}
    if payload.get("kind") not in (None, "model"):          # documents and packages follow from the file's type
        payload.pop("kind")
    return payload


def _check_one(settings: Settings, entry: dict) -> tuple[str, str]:
    """What a dry run says of one entry: its labels, type and size (the content is checked on import)."""
    if not entry["path"].is_file():
        return "refused", "文件不存在"
    try:
        library._upload_fields(_payload(entry, entry["path"]), settings)
    except ApiError as exc:
        return "refused", f"{exc.code}：{exc.message}"
    return "passed", ""


def _store_one(db: Database, store: LibraryStore, settings: Settings, entry: dict) -> tuple[str, str]:
    """Import one entry: ``(outcome, detail)`` with outcome ``stored`` | ``exists`` | ``refused``."""
    path = entry["path"]
    if not path.is_file():
        return "refused", "文件不存在"
    try:
        fields = library._upload_fields(_payload(entry, path), settings)
    except ApiError as exc:
        return "refused", f"{exc.code}：{exc.message}"
    staging_id = "upl_" + secrets.token_hex(10)
    try:
        with store.staging(staging_id, exclusive=True, create=True) as staging:
            with path.open("rb") as handle:
                offset = 0
                for block in iter(lambda: handle.read(_BLOCK), b""):
                    staging.write(offset, block)
                    offset += len(block)
            staging.sync()
            problem = library._verify(staging, fields)
            if problem is not None:
                return "refused", f"{problem.code}：{problem.message}"
            with db.transaction() as s:
                ensure_org(s)
                advisory_lock(s, f"library:{PUBLIC_ORG_ID}:{fields['sha256']}",
                              f"library-name:{PUBLIC_ORG_ID}:{fields['label_key']}:{fields['filename_key']}")
                refusal = library._duplicate(s, PUBLIC_ORG_ID, fields)
                if refusal is not None:
                    return ("exists", "已在公共资料库里") if refusal.code == "material_exists" else ("refused", f"{refusal.code}：{refusal.message}")
                material = Material(org_id=PUBLIC_ORG_ID, created_by=IMPORTER, status=PARSING, **fields)
                s.add(material)
                library_bundle.on_stored(material)
                try:
                    s.flush()
                except IntegrityError:
                    s.rollback()
                    return "exists", "已在公共资料库里"
                keep = store.object_matches(PUBLIC_ORG_ID, fields["sha256"])
                store.promote(staging, PUBLIC_ORG_ID, fields["sha256"], keep_existing=keep)
                audit.record(s, "library.public_import", IMPORTER, org_id=PUBLIC_ORG_ID, target=material.id,
                             detail={"filename": material.filename, "size": material.size, "sha256": material.sha256,
                                     "vendor": material.vendor, "model": material.model, "kind": material.kind})
                return "stored", material.id
    except StorageFull:
        return "refused", "云端的存储空间不足"
    finally:
        store.discard(staging_id)


def import_dir(db: Database, settings: Settings, directory, store: LibraryStore | None = None, *,
               dry_run: bool = False) -> dict:
    """Import every file ``directory/manifest.json`` lists into the public library.

    Returns ``{"stored": n, "exists": n, "passed": n, "refused": [{"file", "reason"}], "checked": n}``.
    ``dry_run`` checks labels, type and size of each file without storing anything (counted in ``passed``)."""
    entries = read_manifest(Path(directory))
    store = store or LibraryStore(settings.effective_library_dir)
    result = {"stored": 0, "exists": 0, "passed": 0, "refused": [], "checked": len(entries)}
    if not dry_run:
        with db.transaction() as s:
            ensure_org(s)
    for entry in entries:
        outcome, detail = _check_one(settings, entry) if dry_run else _store_one(db, store, settings, entry)
        if outcome == "refused":
            result["refused"].append({"file": entry["file"], "reason": detail})
        else:
            result[outcome] += 1
    return result


def main(settings: Settings, directory: str, dry_run: bool = False) -> int:
    """``python -m neuxmind_cloud library-import --dir DIR [--dry-run]``."""
    if not directory or not os.path.isdir(directory):
        print("library-import 需要 --dir：放着 manifest.json 的文件夹（scripts/library_collect.py 的输出）")
        return 2
    db = Database(settings.database_url)
    if not dry_run:
        db.create_all()
    try:
        result = import_dir(db, settings, directory, dry_run=dry_run)
    except ManifestError as exc:
        print(f"没有导入：{exc}")
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 1 if result["refused"] else 0
