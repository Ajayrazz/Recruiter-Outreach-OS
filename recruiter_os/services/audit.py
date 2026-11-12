"""Append-only audit trail for important actions."""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from recruiter_os.models import AuditLog


def audit(s: Session, action: str, entity: str = "", entity_id: Any = "", actor: str = "user", **detail: Any) -> None:
    s.add(AuditLog(action=action, entity=entity, entity_id=str(entity_id), actor=actor, detail=detail))
