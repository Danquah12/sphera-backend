"""Notifications router — list, unread count, mark read."""
import asyncio
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import desc
from sqlalchemy.orm import Session

from database import get_db
from auth import get_current_user
import models
from ws import manager

router = APIRouter(prefix="/notifications", tags=["notifications"])

PAGE_SIZE = 30


# ── Inline schemas (notifications-specific) ───────────────────────
class NotificationOut(BaseModel):
    id: int
    notif_type: str
    actor_id: Optional[int]
    actor_name: Optional[str]
    entity_id: Optional[int]
    entity_type: str
    body: str
    is_read: bool
    created_at: str

    model_config = {"from_attributes": True}


class UnreadCountOut(BaseModel):
    unread: int


# ── Helper used by other routers ──────────────────────────────────
def create_notification(
    db: Session,
    *,
    recipient_id: int,
    actor_id: int,
    notif_type: str,
    body: str,
    entity_id: int = None,
    entity_type: str = "",
) -> models.Notification:
    """
    Create a persisted notification and push a WS event.
    DB write always completes; WS push is best-effort (recipient may be offline).
    """
    # Don't notify yourself
    if recipient_id == actor_id:
        return None

    notif = models.Notification(
        recipient_id=recipient_id,
        actor_id=actor_id,
        notif_type=notif_type,
        body=body,
        entity_id=entity_id,
        entity_type=entity_type,
    )
    db.add(notif)
    db.commit()
    db.refresh(notif)

    # Push WS event — schedule on running event loop (best-effort)
    actor = db.query(models.User).filter(models.User.id == actor_id).first()
    actor_name = actor.username if actor else "someone"
    event = {
        "event": "notification",
        "type": notif_type,
        "actor": actor_name,
        "message": body,
        "entity_id": entity_id,
        "entity_type": entity_type,
        "notif_id": notif.id,
    }
    import asyncio
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(manager.send_to(recipient_id, event))
    except RuntimeError:
        pass  # No event loop in this thread — DB record saved, WS skipped

    return notif


# ── Endpoints ─────────────────────────────────────────────────────
@router.get("", response_model=List[NotificationOut])
def list_notifications(
    page: int = 1,
    unread_only: bool = False,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    q = db.query(models.Notification).filter(
        models.Notification.recipient_id == current_user.id
    )
    if unread_only:
        q = q.filter(models.Notification.is_read == False)

    notifications = (
        q.order_by(desc(models.Notification.created_at))
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
        .all()
    )

    result = []
    for n in notifications:
        actor = db.query(models.User).filter(models.User.id == n.actor_id).first() if n.actor_id else None
        result.append(NotificationOut(
            id=n.id,
            notif_type=n.notif_type,
            actor_id=n.actor_id,
            actor_name=actor.username if actor else None,
            entity_id=n.entity_id,
            entity_type=n.entity_type or "",
            body=n.body,
            is_read=n.is_read,
            created_at=n.created_at.isoformat(),
        ))
    return result


@router.get("/unread-count", response_model=UnreadCountOut)
def unread_count(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    count = db.query(models.Notification).filter(
        models.Notification.recipient_id == current_user.id,
        models.Notification.is_read == False,
    ).count()
    return UnreadCountOut(unread=count)


@router.post("/{notif_id}/read")
def mark_one_read(
    notif_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    notif = db.query(models.Notification).filter(
        models.Notification.id == notif_id,
        models.Notification.recipient_id == current_user.id,
    ).first()
    if not notif:
        raise HTTPException(404, "Notification not found")
    notif.is_read = True
    db.commit()
    return {"marked_read": True}


@router.post("/read-all")
def mark_all_read(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    db.query(models.Notification).filter(
        models.Notification.recipient_id == current_user.id,
        models.Notification.is_read == False,
    ).update({"is_read": True})
    db.commit()
    return {"marked_all_read": True}


@router.delete("/{notif_id}")
def delete_notification(
    notif_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    notif = db.query(models.Notification).filter(
        models.Notification.id == notif_id,
        models.Notification.recipient_id == current_user.id,
    ).first()
    if not notif:
        raise HTTPException(404, "Notification not found")
    db.delete(notif)
    db.commit()
    return {"deleted": True}
