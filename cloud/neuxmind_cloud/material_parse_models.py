"""The table of material parsing (issue #34): one row per material, its job and its result.

Kept apart from ``library_models.py`` (issue #33). The row has no foreign key
to the material on purpose: deleting a material (``library.delete_material``)
must not have to know about parsing, and a row whose material is gone is how
the worker finds a copy in the knowledge service that has to be removed too
(``material_parse.cleanup``).

Where a material stands is its ``status`` (``library_materials.status``):

=========  ==========  ===================================================
status     shown as    meaning
=========  ==========  ===================================================
parsing    解析中       the job below has not finished
parsed     待确认       a result is there and complete; nobody confirmed it
usable     可用         a person checked and confirmed the result
failed     没解析出来   the parser failed or the result lacks items
                       (``status_detail`` names them); a person may fill
                       them in and confirm, which makes it ``usable``
=========  ==========  ===================================================
"""
from __future__ import annotations

from sqlalchemy import JSON, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base
from .models import now

# Steps of the job. ``done``: nothing more happens until someone asks to parse again.
QUEUED, INDEXING, ASKING, DONE = "queued", "indexing", "asking", "done"
STAGES = (QUEUED, INDEXING, ASKING, DONE)


class MaterialParse(Base):
    __tablename__ = "library_material_parses"
    __table_args__ = (Index("ix_library_parse_due", "stage", "next_at"),
                      Index("ix_library_parse_sha", "org_id", "sha256"))
    material_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(40), index=True)
    sha256: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(10))                  # material_fields.RESULT_KINDS
    stage: Mapped[str] = mapped_column(String(10), default=QUEUED)
    next_at: Mapped[float] = mapped_column(Float, default=0.0)     # the step runs from then on
    stage_since: Mapped[float] = mapped_column(Float, default=now)
    attempts: Mapped[int] = mapped_column(Integer, default=0)      # failed tries of the current step
    resends: Mapped[int] = mapped_column(Integer, default=0)       # the copy in the knowledge service went away
    lease_until: Mapped[float] = mapped_column(Float, default=0.0)  # a worker holds the job until then
    lease_owner: Mapped[str] = mapped_column(String(40), default="")
    knowledge_id: Mapped[str] = mapped_column(String(128), default="")   # the copy in the knowledge service
    parser: Mapped[str] = mapped_column(String(20), default="")    # rules | weknora | manual
    # What the parser found (kept as it was) and what is now filled in (edited by people).
    extracted: Mapped[dict] = mapped_column(JSON, default=dict)
    values: Mapped[dict] = mapped_column(JSON, default=dict)
    not_applicable: Mapped[list] = mapped_column(JSON, default=list)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    error: Mapped[str] = mapped_column(Text, default="")
    revision: Mapped[int] = mapped_column(Integer, default=0)      # +1 on every change; edits must name it
    edited_by: Mapped[str] = mapped_column(String(40), default="")
    edited_by_email: Mapped[str] = mapped_column(String(320), default="")
    edited_at: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    confirmed_by: Mapped[str] = mapped_column(String(40), default="")
    confirmed_by_email: Mapped[str] = mapped_column(String(320), default="")
    confirmed_at: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    created_at: Mapped[float] = mapped_column(Float, default=now)
    updated_at: Mapped[float] = mapped_column(Float, default=now)
