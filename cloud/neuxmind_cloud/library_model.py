"""Library kind ``model`` (issue #51, docs/MODEL_LIBRARY.md): the robot model of a brand and model.

A simulation needs the robot's URDF, and usually the mesh files the URDF names. So the robot model is
kept apart from the manuals and driver packages, as its own kind of material:

* it is asked for with ``kind: "model"`` on ``POST /library/uploads``;
* it is one plain ``.urdf``, or a ``.zip`` / ``.tar.gz`` that holds exactly one ``.urdf`` and its meshes
  (``.xacro`` is not taken: it has to be expanded by ROS tools first, which nothing here runs);
* it is read by the same rules as a URDF document (links, joints, limits), in the same sandboxed child
  process; for an archive the rules also report named meshes that are missing or that a simulation
  cannot load. The result is shown, edited and confirmed like any other.

The cloud never unpacks a model. Whoever does (the console, before a simulation) treats it as untrusted.
``library.py`` and ``material_parse.py`` only call in here.

Issue #77: a model may also be MJCF (MuJoCo's own ``.xml``; read like a URDF and simulated), or SDF (``.sdf``)
or USD (``.usd``, ``.usda``, ``.usdc``, ``.usdz``), or STP (STEP CAD geometry, ``.stp``, ``.step``), alone or in an
archive. MuJoCo cannot load SDF, USD or STP, so those are kept as they are: 「已入库」, never read, never
confirmed, never used by a simulation.
"""
from __future__ import annotations

from dataclasses import replace

from . import library_types as types
from . import material_fields as fields
from .config import Settings
from .errors import ApiError

MODEL = types.MODEL
MODEL_FORMATS = ("urdf", "zip", "tar.gz")
# Issue #77: more formats. A simulation reads URDF and MJCF; SDF, USD and STP are kept only.
EXTENSIONS = (".urdf", ".xml", ".sdf", ".usd", ".usda", ".usdc", ".usdz", ".stp", ".step", ".zip", ".tar.gz", ".tgz")
SIMULATED = ("urdf", "mjcf")
KEPT_ONLY = ("sdf", "usd", "usdz", "stp")
LABELS = {"sdf": "SDF", "usd": "USD", "stp": "STP"}
REFUSAL = ("机器人模型只接受 URDF（.urdf）、MJCF（.xml）、SDF（.sdf）、USD（.usd、.usda、.usdc、.usdz）或 STP（.stp、.step），"
           "或者把一个模型文件和它的网格文件打成的 .zip / .tar.gz（.xacro 要先展开成 .urdf）")


def requested_format(payload: dict, fmt: types.Format | None, filename: str) -> types.Format | None:
    """``fmt`` as a model when the upload asks for one; unchanged otherwise. A model-only suffix (issue #77)
    names its own format."""
    if payload.get("kind") != MODEL:
        return fmt
    lowered = filename.lower()
    own = next((f for f in types.MODEL_ONLY if lowered.endswith(f.extensions)), None)
    if own is not None:
        return own
    if fmt is None or fmt.id not in MODEL_FORMATS or not lowered.endswith(EXTENSIONS) or lowered.endswith(".xacro"):
        raise ApiError(415, "unsupported_type", REFUSAL, supported=list(EXTENSIONS))
    return replace(fmt, kind=MODEL)


def size_limit(fmt: types.Format, settings: Settings, default: int) -> int:
    return settings.library_max_model_bytes if fmt.kind == MODEL else default


def label(fmt: types.Format, default: str) -> str:
    return "机器人模型" if fmt.kind == MODEL else default


def formats_view(settings: Settings) -> dict:
    return {"extensions": list(EXTENSIONS), "max_bytes": settings.library_max_model_bytes}


def result_kind(material_kind: str, default: str) -> str:
    """What the parse result of a material is: a model is read as a URDF whatever file it came in."""
    return fields.URDF if material_kind == MODEL else default


def extract_mode(material_kind: str, fmt: str, default: str) -> str:
    return f"model.{fmt}" if material_kind == MODEL else default


# ------------------------------------------------------------------ issue #77: kept-only models
def kept_only(facts) -> str:
    """The format of a model the simulation cannot load (``"sdf"``, ``"usd"``, ``"sdf+usd"``), else ``""``."""
    value = facts.get("stored_only") if isinstance(facts, dict) else None
    return value if isinstance(value, str) else ""


def kept_detail(facts: dict) -> str:
    """The words under such a model in the list."""
    labels = "、".join(LABELS.get(part, part.upper()) for part in kept_only(facts).split("+"))
    files = [f for f in facts.get("files") or [] if isinstance(f, str)]
    inside = f"压缩包里是 {labels} 模型（{'、'.join(files[:3])}{' 等' if facts.get('file_count', 0) > 3 else ''}）" if files else f"{labels} 模型"
    return f"{inside}，已保存；仿真（MuJoCo）暂时用不了这种格式"


def refuse_edit(job) -> None:
    """A kept-only model has nothing to check or confirm."""
    facts = (getattr(job, "extracted", None) or {}).get("facts")
    if kept_only(facts):
        raise ApiError(409, "model_kept_only", "这种格式的机器人模型只保存在资料库里，仿真暂时用不了，不需要核对或确认")
