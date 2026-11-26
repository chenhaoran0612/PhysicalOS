"""Tables of the cloud skills (issue #43): the skill store, the robots that keep a link to the cloud,
the calls third parties place on them and the stops they ask for. Kept apart from ``models.py``;
registered by ``Database.create_all``. Every row carries ``org_id`` and is queried by it."""
from __future__ import annotations

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base
from .models import new_id, now

# Call states. A call is ACTIVE until it reaches one of the others.
QUEUED, DISPATCHED, RUNNING = "queued", "dispatched", "running"
SUCCEEDED, FAILED, CANCELLED, STOPPED, REJECTED = "succeeded", "failed", "cancelled", "stopped", "rejected"
EXPIRED, LOST = "expired", "lost"
ACTIVE = (QUEUED, DISPATCHED, RUNNING)
REPORTED = (RUNNING, SUCCEEDED, FAILED, CANCELLED, STOPPED, REJECTED)     # what a robot may report
STATES = (*ACTIVE, SUCCEEDED, FAILED, CANCELLED, STOPPED, REJECTED, EXPIRED, LOST)


class SkillRelease(Base):
    """One version of one skill of one organisation (``skill.package.v1`` with its files)."""

    __tablename__ = "skill_releases"
    __table_args__ = (UniqueConstraint("org_id", "skill_id", "version", name="uq_skill_release"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("skr"))
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    skill_id: Mapped[str] = mapped_column(String(100))
    version: Mapped[str] = mapped_column(String(50))
    title: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text, default="")
    digest: Mapped[str] = mapped_column(String(64))               # as the robot computes it at install
    motion: Mapped[bool] = mapped_column(Boolean, default=False)
    size: Mapped[int] = mapped_column(Integer)                    # bytes of all files
    file_index: Mapped[list] = mapped_column(JSON)                # [{path, size}]
    spec: Mapped[dict] = mapped_column(JSON)                      # the validated package without its files
    # {path: text}; up to 1.5 MiB, so only loaded when someone downloads the package.
    files: Mapped[dict] = mapped_column(JSON, deferred=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[float] = mapped_column(Float, default=now)
    # Issue #48: the brand and model of robot the release is for ("" on both: not said yet).
    vendor: Mapped[str] = mapped_column(String(100), default="")
    model: Mapped[str] = mapped_column(String(100), default="")


class DeviceLink(Base):
    """A robot that synchronises with the cloud (``POST /device-link/sync``): what it last said about
    itself, its installed skills and their remote switches."""

    __tablename__ = "device_links"
    __table_args__ = (UniqueConstraint("org_id", "instance_id", name="uq_device_link"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("dlk"))
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    instance_id: Mapped[str] = mapped_column(String(200))
    name: Mapped[str] = mapped_column(String(200), default="")
    family: Mapped[str] = mapped_column(String(30), default="")
    simulated: Mapped[bool] = mapped_column(Boolean, default=False)
    runtime_version: Mapped[str] = mapped_column(String(40), default="")
    estop_latched: Mapped[bool] = mapped_column(Boolean, default=False)
    busy: Mapped[bool] = mapped_column(Boolean, default=False)
    motion_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    # Up to 200 entries: only loaded where the entries themselves are needed.
    skills: Mapped[list] = mapped_column(JSON, default=list, deferred=True)
    skill_count: Mapped[int] = mapped_column(Integer, default=0)
    skills_digest: Mapped[str] = mapped_column(String(64), default="")   # rewritten only when the list changes
    first_seen_at: Mapped[float] = mapped_column(Float, default=now)
    last_seen_at: Mapped[float] = mapped_column(Float, default=now, index=True)
    syncs: Mapped[int] = mapped_column(Integer, default=0)
    # Syncs in the current minute (the per-device rate bound).
    window_start: Mapped[float] = mapped_column(Float, default=0.0)
    window_count: Mapped[int] = mapped_column(Integer, default=0)


class SkillCall(Base):
    """One call a third party placed on a robot through the open platform."""

    __tablename__ = "skill_calls"
    __table_args__ = (UniqueConstraint("org_id", "request_id", name="uq_skill_call_request"),
                      Index("ix_skill_calls_org_created", "org_id", "created_at"),
                      Index("ix_skill_calls_device_status", "device_id", "status"),
                      Index("ix_skill_calls_status", "status", "created_at"))
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("cal"))
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    device_id: Mapped[str] = mapped_column(String(40))            # device_links.id (the call outlives the link)
    skill_id: Mapped[str] = mapped_column(String(100))
    version: Mapped[str] = mapped_column(String(50))
    digest: Mapped[str] = mapped_column(String(64), default="")   # of the installed skill when the call was placed
    parameters: Mapped[dict] = mapped_column(JSON, default=dict)
    timeout_s: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(12), default=QUEUED)
    progress: Mapped[float | None] = mapped_column(Float, nullable=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)        # {"value": ...}: any JSON value
    error: Mapped[dict | None] = mapped_column(JSON, nullable=True)         # {code, message}
    run_id: Mapped[str] = mapped_column(String(100), default="")
    request_id: Mapped[str] = mapped_column(String(100))
    request_hash: Mapped[str] = mapped_column(String(64))         # device, skill, parameters, timeout of the request
    created_by: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[float] = mapped_column(Float, default=now)
    dispatched_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    started_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    finished_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    # When the robot last mentioned the call; None until it has (the call is sent again until then).
    reported_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    cancel_requested_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    cancel_requested_by: Mapped[str] = mapped_column(String(100), default="")


class DeviceStop(Base):
    """A stop a third party asked for; handed to the robot with its next sync."""

    __tablename__ = "device_stops"
    __table_args__ = (Index("ix_device_stops_device", "device_id", "created_at"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("stp"))
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    device_id: Mapped[str] = mapped_column(String(40))
    reason: Mapped[str] = mapped_column(String(200), default="")
    created_by: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[float] = mapped_column(Float, default=now)
    delivered_at: Mapped[float | None] = mapped_column(Float, nullable=True)      # first put into an answer
    acknowledged_at: Mapped[float | None] = mapped_column(Float, nullable=True)   # the robot said it stopped
    expired_at: Mapped[float | None] = mapped_column(Float, nullable=True)        # never acknowledged in time
