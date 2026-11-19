"""Tables of the material library (issue #33). Every row carries ``org_id`` and is queried by it.

Kept apart from ``models.py`` so that issues working on other tables do not
meet in one file; ``Database.create_all`` imports this module.
"""
from __future__ import annotations

from sqlalchemy import BigInteger, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base
from .models import new_id, now

# What happens to a material after it is stored. Parsing itself is issue #34;
# a finished upload starts in PARSING and waits there.
PARSING, PARSED, PARSE_FAILED = "parsing", "parsed", "failed"
PARSE_STATES = (PARSING, PARSED, PARSE_FAILED)

UPLOAD_OPEN, UPLOAD_ASSEMBLING = "open", "assembling"


class Material(Base):
    """One stored file with its labels. The bytes live in the library volume under
    ``objects/<org_id>/<sha256>``; ``filename`` is only a display name."""

    __tablename__ = "library_materials"
    __table_args__ = (
        # 同一份资料同一版本不重复收: the same content under the same labels …
        UniqueConstraint("org_id", "label_key", "sha256", name="uq_library_material_content"),
        # … and the same name under the same labels with other content.
        UniqueConstraint("org_id", "label_key", "filename_key", name="uq_library_material_name"),
        Index("ix_library_material_org_created", "org_id", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("mat"))
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    created_by: Mapped[str] = mapped_column(String(40))            # user id
    filename: Mapped[str] = mapped_column(String(255))
    filename_key: Mapped[str] = mapped_column(String(64))          # digest of the case-folded name
    kind: Mapped[str] = mapped_column(String(10))                  # document | package
    format: Mapped[str] = mapped_column(String(20))                # library_types.FORMATS id
    # Identifier of the body type (the list itself is issue #32).
    body_type: Mapped[str] = mapped_column(String(64), index=True)
    vendor: Mapped[str] = mapped_column(String(100))
    model: Mapped[str] = mapped_column(String(100))
    version: Mapped[str] = mapped_column(String(50))
    label_key: Mapped[str] = mapped_column(String(64))             # digest of body type, vendor, model, version
    description: Mapped[str] = mapped_column(Text, default="")
    size: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(20), default=PARSING, index=True)
    status_detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[float] = mapped_column(Float, default=now)
    updated_at: Mapped[float] = mapped_column(Float, default=now)


class MaterialUpload(Base):
    """An upload in progress. Its bytes are ``staging/<id>.part`` in the library volume."""

    __tablename__ = "library_uploads"
    __table_args__ = (Index("ix_library_upload_org", "org_id", "created_by"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("upl"))
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"))
    created_by: Mapped[str] = mapped_column(String(40))            # only this user continues it
    filename: Mapped[str] = mapped_column(String(255))
    filename_key: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(10))
    format: Mapped[str] = mapped_column(String(20))
    body_type: Mapped[str] = mapped_column(String(64))
    vendor: Mapped[str] = mapped_column(String(100))
    model: Mapped[str] = mapped_column(String(100))
    version: Mapped[str] = mapped_column(String(50))
    label_key: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text, default="")
    size: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    chunk_bytes: Mapped[int] = mapped_column(Integer)
    chunk_count: Mapped[int] = mapped_column(Integer)
    # Every chunk request counts, repeated ones too (bounded, see library.MAX_ATTEMPTS).
    chunk_attempts: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default=UPLOAD_OPEN)
    created_at: Mapped[float] = mapped_column(Float, default=now)
    updated_at: Mapped[float] = mapped_column(Float, default=now)
    expires_at: Mapped[float] = mapped_column(Float, index=True)


class MaterialUploadChunk(Base):
    """A chunk that has arrived and is on disk."""

    __tablename__ = "library_upload_chunks"
    upload_id: Mapped[str] = mapped_column(ForeignKey("library_uploads.id"), primary_key=True)
    chunk_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    received_at: Mapped[float] = mapped_column(Float, default=now)
