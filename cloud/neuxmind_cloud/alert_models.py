"""Tables for device alerts (issue #26, requirement R7-08). Kept apart from ``models.py``; registered
by ``Database.create_all``. Every row carries ``org_id`` and is queried by it."""
from __future__ import annotations

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base
from .models import new_id, now


class DeviceAlert(Base):
    """One alert: level, time, device, reason, advice, handled or not.

    ``source`` is ``device`` (reported by the device under its own ``alert_id``; the unique
    constraint makes a second delivery a no-op) or ``cloud`` (raised here, e.g. "device offline")."""

    __tablename__ = "device_alerts"
    __table_args__ = (UniqueConstraint("org_id", "instance_id", "alert_id"),
                      Index("ix_device_alerts_org_received", "org_id", "received_at"),
                      Index("ix_device_alerts_org_instance_received", "org_id", "instance_id", "received_at"),
                      Index("ix_device_alerts_org_handled", "org_id", "handled_at"))
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("alr"))
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    instance_id: Mapped[str] = mapped_column(String(200))
    # The robot registered under this instance id when the alert arrived, if any (no foreign key:
    # the alert outlives a deleted robot).
    robot_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    alert_id: Mapped[str] = mapped_column(String(100))
    source: Mapped[str] = mapped_column(String(10), default="device")
    kind: Mapped[str] = mapped_column(String(60))
    level: Mapped[str] = mapped_column(String(10))               # critical | warning | info
    ts: Mapped[float] = mapped_column(Float)                     # when it happened, by the device's clock
    # Alerts of one report are a microsecond apart, in the order the device sent them.
    received_at: Mapped[float] = mapped_column(Float, default=now)
    device_name: Mapped[str] = mapped_column(String(200), default="")
    reason: Mapped[str] = mapped_column(Text)
    advice: Mapped[str] = mapped_column(Text, default="")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    handled_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    handled_by: Mapped[str | None] = mapped_column(String(100), nullable=True)


class DeviceReport(Base):
    """The last report of each device that ever reported: how the cloud knows it is still there."""

    __tablename__ = "device_reports"
    __table_args__ = (UniqueConstraint("org_id", "instance_id"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("drp"))
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    instance_id: Mapped[str] = mapped_column(String(200))
    name: Mapped[str] = mapped_column(String(200), default="")
    family: Mapped[str] = mapped_column(String(30), default="")
    simulated: Mapped[bool] = mapped_column(Boolean, default=False)
    runtime_version: Mapped[str] = mapped_column(String(40), default="")
    first_report_at: Mapped[float] = mapped_column(Float, default=now)
    last_report_at: Mapped[float] = mapped_column(Float, default=now, index=True)
    reports: Mapped[int] = mapped_column(Integer, default=0)
    # Alerts the device said it still holds after its last report (they did not fit, or were refused).
    pending: Mapped[int] = mapped_column(Integer, default=0)
    # The "device offline" alert of the silence in progress; None while the device is reporting.
    offline_alert_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
