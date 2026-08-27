"""Stories router — short-lived post-like content (24h expiry)."""
import asyncio
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import desc
from sqlalchemy.orm import Session

from database import get_db
from auth import get_current_user
import models

router = APIRouter(prefix="/stories", tags=["stories"])


# ── Schemas ───────────────────────────────────────────────────────
class StoryCreate(BaseModel):
    content: str = ""
    media_url: str = ""
    bg_color: str = "#1a1a2e"   # background colour for text-only stories
    story_type: str = "text"    # "text" | "image" | "video"


class StoryOut(BaseModel):
    id: int
    author_id: int
    author_name: str
    author_avatar: str
    content: str
    media_url: str
    bg_color: str
    story_type: str
    view_count: int
    expires_at: str
    created_at: str
    is_expired: bool

    model_config = {"from_attributes": True}


def _story_out(s: models.Story, now: datetime) -> StoryOut:
    return StoryOut(
        id=s.id,
        author_id=s.author_id,
        author_name=s.author.username if s.author else "unknown",
        author_avatar=s.author.avatar_url if s.author else "",
        content=s.content,
        media_url=s.media_url or "",
        bg_color=s.bg_color or "#1a1a2e",
        story_type=s.story_type or "text",
        view_count=s.view_count,
        expires_at=s.expires_at.isoformat(),
        created_at=s.created_at.isoformat(),
        is_expired=s.expires_at < now,
    )


# ── Endpoints ─────────────────────────────────────────────────────
@router.post("", response_model=StoryOut, status_code=201)
def create_story(
    req: StoryCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    if not req.content and not req.media_url:
        raise HTTPException(400, "Story must have content or a media URL")

    expires = datetime.now(timezone.utc) + timedelta(hours=24)
    story = models.Story(
        author_id=current_user.id,
        content=req.content,
        media_url=req.media_url,
        bg_color=req.bg_color,
        story_type=req.story_type,
        expires_at=expires,
    )
    db.add(story)
    db.commit()
    db.refresh(story)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return _story_out(story, now)


@router.get("", response_model=List[StoryOut])
def list_active_stories(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Return all active (non-expired) stories, ordered by newest first."""
    now_dt = datetime.now(timezone.utc)
    now_naive = now_dt.replace(tzinfo=None)
    stories = (
        db.query(models.Story)
        .filter(models.Story.expires_at > now_naive)
        .order_by(desc(models.Story.created_at))
        .limit(100)
        .all()
    )
    return [_story_out(s, now_naive) for s in stories]


@router.get("/mine", response_model=List[StoryOut])
def my_stories(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Return the current user's own stories (including expired)."""
    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    stories = (
        db.query(models.Story)
        .filter(models.Story.author_id == current_user.id)
        .order_by(desc(models.Story.created_at))
        .all()
    )
    return [_story_out(s, now_naive) for s in stories]


@router.post("/{story_id}/view")
def view_story(
    story_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Increment view count on a story."""
    story = db.query(models.Story).filter(models.Story.id == story_id).first()
    if not story:
        raise HTTPException(404, "Story not found")
    story.view_count += 1
    db.commit()
    return {"viewed": True, "view_count": story.view_count}


@router.delete("/{story_id}")
def delete_story(
    story_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    story = db.query(models.Story).filter(models.Story.id == story_id).first()
    if not story:
        raise HTTPException(404, "Story not found")
    if story.author_id != current_user.id and current_user.role != "admin":
        raise HTTPException(403, "Cannot delete someone else's story")
    db.delete(story)
    db.commit()
    return {"deleted": True}
