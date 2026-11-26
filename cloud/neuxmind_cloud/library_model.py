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
"""
from __future__ import annotations

from dataclasses import replace

from . import library_types as types
from . import material_fields as fields
from .config import Settings
from .errors import ApiError

MODEL = types.MODEL
MODEL_FORMATS = ("urdf", "zip", "tar.gz")
EXTENSIONS = (".urdf", ".zip", ".tar.gz", ".tgz")


def requested_format(payload: dict, fmt: types.Format, filename: str) -> types.Format:
    """``fmt`` as a model when the upload asks for one; unchanged otherwise."""
    if payload.get("kind") != MODEL:
        return fmt
    if fmt.id not in MODEL_FORMATS or not filename.lower().endswith(EXTENSIONS):
        raise ApiError(415, "unsupported_type",
                       "机器人模型只接受单个 .urdf，或者装着一个 .urdf 和它的网格文件的 .zip / .tar.gz（.xacro 要先展开成 .urdf）",
                       supported=list(EXTENSIONS))
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
