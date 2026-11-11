"""Audit trail for security- and money-relevant actions."""
from __future__ import annotations

from sqlalchemy.orm import Session

from .models import AuditLog


def record(session: Session, action: str, actor: str, *, org_id: str | None = None, target: str = "",
           detail: dict | None = None, ip: str = "") -> None:
    """Add an audit row to the current transaction (committed with the action itself)."""
    session.add(AuditLog(org_id=org_id, actor=actor, action=action, target=target, detail=detail or {}, ip=ip))
