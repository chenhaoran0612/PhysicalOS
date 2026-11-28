"""Materials belong to a brand and model of robot (issue #51, docs/MODEL_LIBRARY.md).

Issue #34 bound every material to the one device it was uploaded for. Since issue #51 a material
belongs to its ``vendor`` and ``model`` labels (compared without regard to case), and every robot of
that brand and model uses it; only a bundle (issue #43) still belongs to one device. New uploads are
stored that way by ``library._upload_fields``. This module moves what was stored before:

``POST /library/materials/rebind`` ``{device_id, vendor, model}`` gives every material that is still
bound to that device the brand and model of the device, which only the caller knows (robots live in
the console, not here). It may be called any number of times.

* A material whose content is already kept under that brand and model (same labels, same SHA-256) is a
  duplicate: one of the two is removed, the confirmed one stays (the one already there when neither or
  both are confirmed).
* A material whose file name is already taken there by other content stays with its device and is
  named in the answer: nothing is renamed or overwritten.
"""
from __future__ import annotations

from sqlalchemy import select

from . import audit, library
from . import library_types as types
from .db import Database
from .errors import ApiError, bad_request, conflict
from .library_models import PARSED, USABLE, Material
from .material_parse_models import MaterialParse
from .library_store import LibraryStore
from .models import now
from .skill_store import _product

MAX_MOVED = 2000


def _request(payload) -> tuple[str, dict]:
    if not isinstance(payload, dict):
        raise bad_request("请求须为对象")
    device_id, _name = library._device({"device_id": payload.get("device_id")})
    if not device_id:
        raise bad_request("需要 device_id：把哪台设备的资料归到品牌型号")
    return device_id, _product(payload, required=True)


def _key(material: Material, product: dict) -> str:
    labels = (material.body_type, product["vendor"].casefold(), product["model"].casefold(), material.version.casefold())
    return library._digest(*labels, "kind", types.MODEL) if material.kind == types.MODEL else library._digest(*labels)


def _clash(s, org_id: str, material: Material, key: str) -> tuple[Material | None, Material | None]:
    """``(same content, same name)`` already kept under ``key``, each or ``None``."""
    library.advisory_lock(s, f"library-name:{org_id}:{key}:{material.filename_key}")
    there = select(Material).where(Material.org_id == org_id, Material.label_key == key, Material.id != material.id)
    same = s.execute(there.where(Material.sha256 == material.sha256).with_for_update()).scalar_one_or_none()
    named = s.execute(there.where(Material.filename_key == material.filename_key, Material.sha256 != material.sha256)
                      ).scalar_one_or_none()
    return same, named


def _relabel(material: Material, product: dict, key: str) -> None:
    material.vendor, material.model, material.label_key = product["vendor"], product["model"], key
    material.device_id = material.device_name = ""
    material.updated_at = now()


def rebind(db: Database, store: LibraryStore, who: library.Who, payload) -> dict:
    """Move the device's materials to the brand and model; ``{moved, merged, kept[]}``."""
    device_id, product = _request(payload)
    released: list[tuple[str, str]] = []
    moved = merged = 0
    kept: list[dict] = []
    with db.transaction() as s:
        p = library._writer(who(s))
        rows = s.execute(select(Material).where(Material.org_id == p.org_id, Material.device_id == device_id,
                                                Material.kind != types.BUNDLE)
                         .order_by(Material.created_at, Material.id).limit(MAX_MOVED + 1).with_for_update()).scalars().all()
        more, rows = len(rows) > MAX_MOVED, rows[:MAX_MOVED]
        for material in rows:
            key = _key(material, product)
            same, named = _clash(s, p.org_id, material, key)
            if named is not None:           # also when the same content is there under another name: the name is taken
                kept.append({"id": material.id, "filename": material.filename, "version": material.version,
                             "reason": "这个品牌型号下已有同名、同版本的另一份资料，内容不同"})
                continue
            if same is not None:
                loser = same if material.status == USABLE and same.status != USABLE else material
                audit.record(s, "library.material_merged", p.actor, org_id=p.org_id, target=loser.id,
                             detail={"filename": loser.filename, "sha256": loser.sha256,
                                     "kept": (material if loser is same else same).id})
                released.append((p.org_id, loser.sha256))
                s.delete(loser)
                s.flush()
                merged += 1
                if loser is material:
                    continue
            _relabel(material, product, key)
            s.flush()
            moved += 1
        if moved or merged:
            audit.record(s, "library.materials_rebound", p.actor, org_id=p.org_id, target=device_id,
                         detail={**product, "moved": moved, "merged": merged, "kept": len(kept)})
    for org_id, sha256 in released:
        library.release_object(db, store, org_id, sha256)            # a no-op while the kept copy refers to the file
    # ``more``: this call reached its limit; the caller asks again.
    return {"device_id": device_id, **product, "moved": moved, "merged": merged, "kept": kept[:50], "more": more}


def move(db: Database, who: library.Who, material_id: str, payload) -> dict:
    """``PUT /library/materials/{id}/product``: one material goes to another brand and model (a label
    typed wrongly, or typed before the robot had its brand and model). Nothing is merged or replaced:
    a material that is already there is a refusal."""
    if not isinstance(payload, dict):
        raise bad_request("请求须为对象")
    product = _product(payload, required=True)
    with db.transaction() as s:
        p = library._writer(who(s))
        material = s.execute(select(Material).where(Material.id == library._own_material(s, p, material_id).id)
                             .with_for_update()).scalar_one()
        if material.kind == types.BUNDLE:
            raise conflict("程序和场景数据属于一台设备，不能改到品牌型号下", code="bundle_needs_device")
        key = _key(material, product)
        same, named = _clash(s, material.org_id, material, key)       # issue #78: a public one is checked where it is
        if same is not None:
            raise ApiError(409, "material_exists", "这个品牌型号下已经有这份资料的这个版本", material_id=same.id)
        if named is not None:
            raise ApiError(409, "version_exists", f"这个品牌型号下已有同名、同版本（{material.version}）的资料，内容不同",
                           material_id=named.id)
        before = {"vendor": material.vendor, "model": material.model, "device_id": material.device_id}
        changed = key != material.label_key
        _relabel(material, product, key)
        if changed and material.status == USABLE:
            # It was confirmed for another brand and model: a person looks at it again before it is used for this one.
            job = s.get(MaterialParse, material.id)
            if job is not None:
                job.confirmed_by = job.confirmed_by_email = ""
                job.confirmed_at = None
            material.status, material.status_detail = PARSED, "改了品牌型号，请重新核对后确认"
        s.flush()
        audit.record(s, "library.material_moved", p.actor, org_id=p.org_id, target=material.id,
                     detail={"from": before, **product})
        return library.material_view(material)
