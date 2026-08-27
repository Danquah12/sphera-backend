"""Admin router — role-gated user and post management."""
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc
from sqlalchemy.orm import Session

from database import get_db
from auth import get_current_user
import models
import schemas

router = APIRouter(prefix="/admin", tags=["admin"])


# ── Guard: admin-only dependency ──────────────────────────────────
def require_admin(current_user: models.User = Depends(get_current_user)) -> models.User:
    if current_user.role != "admin":
        raise HTTPException(403, "Admin access required")
    return current_user


# ── Inline admin schemas ───────────────────────────────────────────
from pydantic import BaseModel

class AdminUserOut(BaseModel):
    id: int
    username: str
    email: str
    display_name: str
    role: str
    is_active: bool
    email_verified: Optional[bool] = False
    score: int
    created_at: str

    model_config = {"from_attributes": True}


class AdminStatsOut(BaseModel):
    total_users: int
    active_users: int
    total_posts: int
    total_messages: int
    total_notifications: int


class UpdateUserRequest(BaseModel):
    role: Optional[str]       = None   # "admin" | "user"
    is_active: Optional[bool] = None
    score: Optional[int]      = None
    display_name: Optional[str] = None


# ── Dashboard Stats ───────────────────────────────────────────────
@router.get("/stats", response_model=AdminStatsOut)
def admin_stats(
    db: Session = Depends(get_db),
    _: models.User = Depends(require_admin),
):
    return AdminStatsOut(
        total_users        = db.query(models.User).count(),
        active_users       = db.query(models.User).filter(models.User.is_active == True).count(),
        total_posts        = db.query(models.Post).count(),
        total_messages     = db.query(models.Message).count(),
        total_notifications= db.query(models.Notification).count(),
    )


# ── User Management ───────────────────────────────────────────────
@router.get("/users", response_model=List[AdminUserOut])
def list_users(
    page: int = 1,
    q: str = "",
    db: Session = Depends(get_db),
    _: models.User = Depends(require_admin),
):
    PAGE = 50
    query = db.query(models.User)
    if q:
        pattern = f"%{q.lower()}%"
        query = query.filter(
            models.User.username.ilike(pattern) |
            models.User.email.ilike(pattern) |
            models.User.display_name.ilike(pattern)
        )
    users = query.order_by(desc(models.User.created_at)).offset((page - 1) * PAGE).limit(PAGE).all()
    return [AdminUserOut(
        id=u.id, username=u.username, email=u.email,
        display_name=u.display_name, role=u.role,
        is_active=u.is_active,
        email_verified=getattr(u, "email_verified", False),
        score=u.score,
        created_at=u.created_at.isoformat(),
    ) for u in users]


@router.get("/users/{user_id}", response_model=AdminUserOut)
def get_user(
    user_id: int,
    db: Session = Depends(get_db),
    _: models.User = Depends(require_admin),
):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(404, "User not found")
    return AdminUserOut(
        id=user.id, username=user.username, email=user.email,
        display_name=user.display_name, role=user.role,
        is_active=user.is_active,
        email_verified=getattr(user, "email_verified", False),
        score=user.score,
        created_at=user.created_at.isoformat(),
    )


@router.patch("/users/{user_id}", response_model=AdminUserOut)
def update_user(
    user_id: int,
    req: UpdateUserRequest,
    db: Session = Depends(get_db),
    admin: models.User = Depends(require_admin),
):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(404, "User not found")
    if user.id == admin.id:
        raise HTTPException(400, "Admins cannot modify their own role via API")

    if req.role is not None:
        if req.role not in ("admin", "user"):
            raise HTTPException(400, "role must be 'admin' or 'user'")
        user.role = req.role
    if req.is_active is not None:
        user.is_active = req.is_active
    if req.score is not None:
        user.score = req.score
    if req.display_name is not None:
        user.display_name = req.display_name

    db.commit()
    db.refresh(user)
    return AdminUserOut(
        id=user.id, username=user.username, email=user.email,
        display_name=user.display_name, role=user.role,
        is_active=user.is_active,
        email_verified=getattr(user, "email_verified", False),
        score=user.score,
        created_at=user.created_at.isoformat(),
    )


@router.delete("/users/{user_id}")
def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    admin: models.User = Depends(require_admin),
):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(404, "User not found")
    if user.id == admin.id:
        raise HTTPException(400, "Cannot delete yourself")
    db.delete(user)
    db.commit()
    return {"deleted": True, "username": user.username}


# ── Post Management ───────────────────────────────────────────────
@router.get("/posts")
def list_all_posts(
    page: int = 1,
    q: str = "",
    post_type: str = "",
    db: Session = Depends(get_db),
    _: models.User = Depends(require_admin),
):
    PAGE = 50
    query = db.query(models.Post)
    if q:
        query = query.filter(models.Post.content.ilike(f"%{q}%"))
    if post_type:
        query = query.filter(models.Post.post_type == post_type)
    posts = query.order_by(desc(models.Post.created_at)).offset((page - 1) * PAGE).limit(PAGE).all()
    return [{
        "id": p.id,
        "author_id": p.author_id,
        "author": p.author.username if p.author else "?",
        "content": p.content[:120],
        "post_type": p.post_type,
        "likes_count": p.likes_count,
        "comments_count": p.comments_count,
        "created_at": p.created_at.isoformat(),
    } for p in posts]


@router.delete("/posts/{post_id}")
def delete_post(
    post_id: int,
    db: Session = Depends(get_db),
    _: models.User = Depends(require_admin),
):
    post = db.query(models.Post).filter(models.Post.id == post_id).first()
    if not post:
        raise HTTPException(404, "Post not found")
    db.delete(post)
    db.commit()
    return {"deleted": True, "post_id": post_id}


# ── Notification Broadcast ────────────────────────────────────────
class BroadcastRequest(BaseModel):
    recipient_ids: List[int]   # empty list = send to all users
    message: str
    notif_type: str = "system"


@router.post("/broadcast")
def broadcast_notification(
    req: BroadcastRequest,
    db: Session = Depends(get_db),
    admin: models.User = Depends(require_admin),
):
    """Send a system notification to specific users or all users."""
    if req.recipient_ids:
        targets = db.query(models.User).filter(models.User.id.in_(req.recipient_ids)).all()
    else:
        targets = db.query(models.User).filter(models.User.is_active == True).all()

    count = 0
    for user in targets:
        if user.id == admin.id:
            continue
        db.add(models.Notification(
            recipient_id=user.id,
            actor_id=admin.id,
            notif_type=req.notif_type,
            body=req.message,
        ))
        count += 1

    db.commit()
    return {"sent": count, "message": req.message}
