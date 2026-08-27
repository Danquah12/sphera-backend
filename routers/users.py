"""Users router — profile, follow, search."""
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from auth import get_current_user
import models
import schemas
from routers.notifications import create_notification

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/search", response_model=List[schemas.UserSearchOut])
def search_users(
    q: str = "",
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    if not q:
        return []
    pattern = f"%{q.lower()}%"
    users = (
        db.query(models.User)
        .filter(
            (models.User.username.ilike(pattern)) |
            (models.User.display_name.ilike(pattern))
        )
        .filter(models.User.id != current_user.id)
        .limit(20)
        .all()
    )
    return [
        schemas.UserSearchOut(
            id=u.id,
            username=u.username,
            display_name=u.display_name,
            avatar_url=u.avatar_url,
        )
        for u in users
    ]


@router.get("/{username}", response_model=schemas.ProfileOut)
def get_profile(
    username: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    user = db.query(models.User).filter(models.User.username == username.lower()).first()
    if not user:
        raise HTTPException(404, "User not found")

    followers_count = db.query(models.Follow).filter(models.Follow.followee_id == user.id).count()
    following_count = db.query(models.Follow).filter(models.Follow.follower_id == user.id).count()
    posts_count = db.query(models.Post).filter(models.Post.author_id == user.id).count()
    is_following = bool(
        db.query(models.Follow).filter(
            models.Follow.follower_id == current_user.id,
            models.Follow.followee_id == user.id,
        ).first()
    )

    return schemas.ProfileOut(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        bio=user.bio,
        avatar_url=user.avatar_url,
        cover_url=user.cover_url,
        role=user.role,
        score=user.score,
        followers_count=followers_count,
        following_count=following_count,
        posts_count=posts_count,
        is_following=is_following,
        created_at=user.created_at,
    )


@router.post("/{username}/follow", response_model=schemas.FollowResponse)
def toggle_follow(
    username: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    target = db.query(models.User).filter(models.User.username == username.lower()).first()
    if not target:
        raise HTTPException(404, "User not found")
    if target.id == current_user.id:
        raise HTTPException(400, "You cannot follow yourself")

    existing = db.query(models.Follow).filter(
        models.Follow.follower_id == current_user.id,
        models.Follow.followee_id == target.id,
    ).first()

    if existing:
        db.delete(existing)
        following = False
    else:
        db.add(models.Follow(follower_id=current_user.id, followee_id=target.id))
        following = True
        # Notify the person being followed
        create_notification(
            db,
            recipient_id=target.id,
            actor_id=current_user.id,
            notif_type="follow",
            body=f"@{current_user.username} started following you",
            entity_id=current_user.id,
            entity_type="user",
        )

    db.commit()
    followers_count = db.query(models.Follow).filter(models.Follow.followee_id == target.id).count()

    return schemas.FollowResponse(following=following, followers_count=followers_count)


# ── Profile Update ────────────────────────────────────────────────
from pydantic import BaseModel
from typing import Optional as Opt


class ProfileUpdateRequest(BaseModel):
    display_name: Opt[str] = None
    bio: Opt[str] = None
    avatar_url: Opt[str] = None
    cover_url: Opt[str] = None


@router.patch("/me", response_model=schemas.ProfileOut)
def update_profile(
    req: ProfileUpdateRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Update the current user's own profile."""
    if req.display_name is not None:
        current_user.display_name = req.display_name[:80]
    if req.bio is not None:
        current_user.bio = req.bio[:500]
    if req.avatar_url is not None:
        current_user.avatar_url = req.avatar_url
    if req.cover_url is not None:
        current_user.cover_url = req.cover_url

    db.commit()
    db.refresh(current_user)

    followers_count = db.query(models.Follow).filter(models.Follow.followee_id == current_user.id).count()
    following_count = db.query(models.Follow).filter(models.Follow.follower_id == current_user.id).count()
    posts_count = db.query(models.Post).filter(models.Post.author_id == current_user.id).count()

    return schemas.ProfileOut(
        id=current_user.id,
        username=current_user.username,
        display_name=current_user.display_name,
        bio=current_user.bio,
        avatar_url=current_user.avatar_url,
        cover_url=current_user.cover_url,
        role=current_user.role,
        score=current_user.score,
        followers_count=followers_count,
        following_count=following_count,
        posts_count=posts_count,
        is_following=False,
        created_at=current_user.created_at,
    )


@router.get("/{username}/posts", response_model=schemas.FeedResponse)
def get_user_posts(
    username: str,
    page: int = 1,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Return paginated posts by a specific user."""
    PAGE_SIZE = 20
    user = db.query(models.User).filter(models.User.username == username.lower()).first()
    if not user:
        raise HTTPException(404, "User not found")

    from sqlalchemy import desc as _desc
    q = (
        db.query(models.Post)
        .filter(models.Post.author_id == user.id)
        .order_by(_desc(models.Post.created_at))
    )
    total = q.count()
    posts = q.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all()

    from routers.posts import _post_out
    return schemas.FeedResponse(
        posts=[_post_out(p) for p in posts],
        page=page,
        has_more=(page * PAGE_SIZE) < total,
    )
