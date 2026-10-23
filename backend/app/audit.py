import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog, User

SYSTEM = "system"


def audit(
    session: AsyncSession,
    action: str,
    target_type: str,
    target_id: object | None = None,
    *,
    actor: User | str = SYSTEM,
    **details: Any,
) -> None:
    """Add an audit record to the session (committed with the caller's transaction)."""
    actor_name = actor.email if isinstance(actor, User) else actor
    actor_id: uuid.UUID | None = actor.id if isinstance(actor, User) else None
    session.add(
        AuditLog(
            actor=actor_name,
            actor_user_id=actor_id,
            action=action,
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            details={k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in details.items()},
        )
    )
