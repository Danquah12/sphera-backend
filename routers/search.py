"""Search router — unified full-text search across posts and users."""
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, or_
from sqlalchemy.orm import Session

from database import get_db
from auth import get_current_user
import models
import schemas

router = APIRouter(prefix="/search", tags=["search"])

MAX_RESULTS = 20


@router.get("")
def unified_search(
    q: str = Query(..., min_length=1, description="Search query"),
    category: str = Query("all", description="Filter: all | posts | users | hashtags"),
    page: int = 1,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Unified search across posts and users.

    - **all**: returns both users and posts
    - **posts**: only post content
    - **users**: only usernames / display names
    - **hashtags**: posts containing #tag
    """
    if not q.strip():
        raise HTTPException(400, "Search query cannot be empty")

    offset = (page - 1) * MAX_RESULTS
    pattern = f"%{q.strip().lower()}%"
    result = {}

    if category in ("all", "users"):
        users = (
            db.query(models.User)
            .filter(
                models.User.is_active == True,
                or_(
                    models.User.username.ilike(pattern),
                    models.User.display_name.ilike(pattern),
                    models.User.bio.ilike(pattern),
                )
            )
            .order_by(desc(models.User.score))
            .offset(offset)
            .limit(MAX_RESULTS)
            .all()
        )
        result["users"] = [
            schemas.UserSearchOut(
                id=u.id,
                username=u.username,
                display_name=u.display_name,
                avatar_url=u.avatar_url,
            )
            for u in users
        ]

    if category in ("all", "posts"):
        posts = (
            db.query(models.Post)
            .filter(models.Post.content.ilike(pattern))
            .order_by(desc(models.Post.likes_count))
            .offset(offset)
            .limit(MAX_RESULTS)
            .all()
        )
        result["posts"] = [
            {
                "id": p.id,
                "author": p.author.username if p.author else "?",
                "content": p.content[:200],
                "post_type": p.post_type,
                "likes_count": p.likes_count,
                "created_at": p.created_at.isoformat(),
            }
            for p in posts
        ]

    if category == "hashtags":
        # Search for posts containing the tag (with or without #)
        tag = q.lstrip("#").lower()
        tag_pattern = f"%#{tag}%"
        posts = (
            db.query(models.Post)
            .filter(models.Post.content.ilike(tag_pattern))
            .order_by(desc(models.Post.likes_count))
            .offset(offset)
            .limit(MAX_RESULTS)
            .all()
        )
        result["hashtag"] = f"#{tag}"
        result["posts"] = [
            {
                "id": p.id,
                "author": p.author.username if p.author else "?",
                "content": p.content[:200],
                "post_type": p.post_type,
                "likes_count": p.likes_count,
                "created_at": p.created_at.isoformat(),
            }
            for p in posts
        ]

    result["query"] = q
    result["page"] = page
    result["category"] = category
    return result
