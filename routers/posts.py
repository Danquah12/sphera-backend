"""Posts router — feed, explore, reels, create, like, comment, hashtags, user posts."""
import re
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import desc
from sqlalchemy.orm import Session

from database import get_db
from auth import get_current_user
import models
import schemas
from routers.notifications import create_notification

router = APIRouter(prefix="/posts", tags=["posts"])
PAGE_SIZE = 20

_HASHTAG_RE = re.compile(r"#(\w+)", re.UNICODE)


def _post_out(post: models.Post) -> schemas.PostOut:
    return schemas.PostOut(
        id=post.id,
        author_id=post.author_id,
        author_name=post.author.username if post.author else "unknown",
        content=post.content,
        media_urls=post.media_urls,
        post_type=post.post_type,
        likes_count=post.likes_count,
        comments_count=post.comments_count,
        shares_count=post.shares_count,
        created_at=post.created_at,
    )


def _extract_and_save_hashtags(db: Session, post_id: int, content: str):
    tags = {m.lower() for m in _HASHTAG_RE.findall(content)}
    for tag in tags:
        try:
            db.add(models.PostHashtag(post_id=post_id, tag=tag))
            db.flush()
        except Exception:
            db.rollback()


@router.get("/feed", response_model=schemas.FeedResponse)
def feed(
    page: int = 1,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    followed = db.query(models.Follow.followee_id).filter(
        models.Follow.follower_id == current_user.id
    ).subquery()
    q = (
        db.query(models.Post)
        .filter(
            (models.Post.author_id == current_user.id) |
            models.Post.author_id.in_(followed)
        )
        .filter(models.Post.post_type == "feed")
        .order_by(desc(models.Post.created_at))
    )
    total = q.count()
    posts = q.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all()
    return schemas.FeedResponse(
        posts=[_post_out(p) for p in posts],
        page=page,
        has_more=(page * PAGE_SIZE) < total,
    )


@router.get("/explore", response_model=schemas.FeedResponse)
def explore(
    page: int = 1,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    q = (
        db.query(models.Post)
        .filter(models.Post.post_type == "feed")
        .order_by(desc(models.Post.created_at))
    )
    total = q.count()
    posts = q.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all()
    return schemas.FeedResponse(
        posts=[_post_out(p) for p in posts],
        page=page,
        has_more=(page * PAGE_SIZE) < total,
    )


@router.get("/reels", response_model=schemas.FeedResponse)
def reels(
    page: int = 1,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    q = (
        db.query(models.Post)
        .filter(models.Post.post_type == "reel")
        .order_by(desc(models.Post.created_at))
    )
    total = q.count()
    posts = q.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all()
    return schemas.FeedResponse(
        posts=[_post_out(p) for p in posts],
        page=page,
        has_more=(page * PAGE_SIZE) < total,
    )


@router.get("/hashtag/{tag}", response_model=schemas.FeedResponse)
def posts_by_hashtag(
    tag: str,
    page: int = 1,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Return all posts tagged with #tag."""
    tag = tag.lower().lstrip("#")
    tagged_post_ids = (
        db.query(models.PostHashtag.post_id)
        .filter(models.PostHashtag.tag == tag)
        .subquery()
    )
    q = (
        db.query(models.Post)
        .filter(models.Post.id.in_(tagged_post_ids))
        .order_by(desc(models.Post.likes_count))
    )
    total = q.count()
    posts = q.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all()
    return schemas.FeedResponse(
        posts=[_post_out(p) for p in posts],
        page=page,
        has_more=(page * PAGE_SIZE) < total,
    )


@router.get("/{post_id}", response_model=schemas.PostOut)
def get_post(
    post_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    post = db.query(models.Post).filter(models.Post.id == post_id).first()
    if not post:
        raise HTTPException(404, "Post not found")
    return _post_out(post)


@router.post("/", response_model=schemas.PostOut, status_code=201)
def create_post(
    req: schemas.PostCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    post = models.Post(
        author_id=current_user.id,
        content=req.content,
        post_type=req.post_type,
    )
    post.media_urls = req.media_urls
    db.add(post)
    db.flush()
    _extract_and_save_hashtags(db, post.id, req.content)
    db.commit()
    db.refresh(post)
    return _post_out(post)


@router.post("/{post_id}/like", response_model=schemas.LikeResponse)
def toggle_like(
    post_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    post = db.query(models.Post).filter(models.Post.id == post_id).first()
    if not post:
        raise HTTPException(404, "Post not found")

    existing = db.query(models.PostLike).filter(
        models.PostLike.user_id == current_user.id,
        models.PostLike.post_id == post_id,
    ).first()

    if existing:
        db.delete(existing)
        post.likes_count = max(0, post.likes_count - 1)
        liked = False
    else:
        db.add(models.PostLike(user_id=current_user.id, post_id=post_id))
        post.likes_count += 1
        liked = True
        if post.author_id != current_user.id:
            create_notification(
                db,
                recipient_id=post.author_id,
                actor_id=current_user.id,
                notif_type="like",
                body=f"@{current_user.username} liked your post",
                entity_id=post_id,
                entity_type="post",
            )

    db.commit()
    return schemas.LikeResponse(liked=liked, likes_count=post.likes_count)


@router.get("/{post_id}/comments", response_model=List[schemas.CommentOut])
def list_comments(
    post_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    post = db.query(models.Post).filter(models.Post.id == post_id).first()
    if not post:
        raise HTTPException(404, "Post not found")
    comments = (
        db.query(models.Comment)
        .filter(models.Comment.post_id == post_id)
        .order_by(models.Comment.created_at)
        .all()
    )
    return [
        schemas.CommentOut(
            id=c.id,
            post_id=c.post_id,
            author_id=c.author_id,
            author_name=c.author.username if c.author else "?",
            content=c.content,
            created_at=c.created_at,
        )
        for c in comments
    ]


@router.post("/{post_id}/comment", response_model=schemas.CommentOut, status_code=201)
def add_comment(
    post_id: int,
    req: schemas.CommentCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    post = db.query(models.Post).filter(models.Post.id == post_id).first()
    if not post:
        raise HTTPException(404, "Post not found")

    comment = models.Comment(
        post_id=post_id,
        author_id=current_user.id,
        content=req.content,
    )
    db.add(comment)
    post.comments_count += 1
    db.commit()
    db.refresh(comment)

    if post.author_id != current_user.id:
        create_notification(
            db,
            recipient_id=post.author_id,
            actor_id=current_user.id,
            notif_type="comment",
            body=f"@{current_user.username} commented: {req.content[:80]}",
            entity_id=post_id,
            entity_type="post",
        )

    return schemas.CommentOut(
        id=comment.id,
        post_id=comment.post_id,
        author_id=comment.author_id,
        author_name=current_user.username,
        content=comment.content,
        created_at=comment.created_at,
    )


@router.get("/feed", response_model=schemas.FeedResponse)
def feed(
    page: int = 1,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    # IDs of users the current user follows + themselves
    followed = db.query(models.Follow.followee_id).filter(
        models.Follow.follower_id == current_user.id
    ).subquery()

    q = (
        db.query(models.Post)
        .filter(
            (models.Post.author_id == current_user.id) |
            models.Post.author_id.in_(followed)
        )
        .filter(models.Post.post_type == "feed")
        .order_by(desc(models.Post.created_at))
    )
    total = q.count()
    posts = q.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all()
    return schemas.FeedResponse(
        posts=[_post_out(p) for p in posts],
        page=page,
        has_more=(page * PAGE_SIZE) < total,
    )


@router.get("/explore", response_model=schemas.FeedResponse)
def explore(
    page: int = 1,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    q = (
        db.query(models.Post)
        .filter(models.Post.post_type == "feed")
        .order_by(desc(models.Post.created_at))
    )
    total = q.count()
    posts = q.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all()
    return schemas.FeedResponse(
        posts=[_post_out(p) for p in posts],
        page=page,
        has_more=(page * PAGE_SIZE) < total,
    )


@router.get("/reels", response_model=schemas.FeedResponse)
def reels(
    page: int = 1,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    q = (
        db.query(models.Post)
        .filter(models.Post.post_type == "reel")
        .order_by(desc(models.Post.created_at))
    )
    total = q.count()
    posts = q.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all()
    return schemas.FeedResponse(
        posts=[_post_out(p) for p in posts],
        page=page,
        has_more=(page * PAGE_SIZE) < total,
    )


@router.post("/", response_model=schemas.PostOut, status_code=201)
def create_post(
    req: schemas.PostCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    post = models.Post(
        author_id=current_user.id,
        content=req.content,
        post_type=req.post_type,
    )
    post.media_urls = req.media_urls
    db.add(post)
    db.commit()
    db.refresh(post)
    return _post_out(post)


@router.post("/{post_id}/like", response_model=schemas.LikeResponse)
def toggle_like(
    post_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    post = db.query(models.Post).filter(models.Post.id == post_id).first()
    if not post:
        raise HTTPException(404, "Post not found")

    existing = db.query(models.PostLike).filter(
        models.PostLike.user_id == current_user.id,
        models.PostLike.post_id == post_id,
    ).first()

    if existing:
        db.delete(existing)
        post.likes_count = max(0, post.likes_count - 1)
        liked = False
    else:
        db.add(models.PostLike(user_id=current_user.id, post_id=post_id))
        post.likes_count += 1
        liked = True
        # Notify post author
        if post.author_id != current_user.id:
            create_notification(
                db,
                recipient_id=post.author_id,
                actor_id=current_user.id,
                notif_type="like",
                body=f"@{current_user.username} liked your post",
                entity_id=post_id,
                entity_type="post",
            )

    db.commit()
    return schemas.LikeResponse(liked=liked, likes_count=post.likes_count)


@router.post("/{post_id}/comment", response_model=schemas.CommentOut, status_code=201)
def add_comment(
    post_id: int,
    req: schemas.CommentCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    post = db.query(models.Post).filter(models.Post.id == post_id).first()
    if not post:
        raise HTTPException(404, "Post not found")

    comment = models.Comment(
        post_id=post_id,
        author_id=current_user.id,
        content=req.content,
    )
    db.add(comment)
    post.comments_count += 1
    db.commit()
    db.refresh(comment)

    # Notify post author
    if post.author_id != current_user.id:
        create_notification(
            db,
            recipient_id=post.author_id,
            actor_id=current_user.id,
            notif_type="comment",
            body=f"@{current_user.username} commented: {req.content[:80]}",
            entity_id=post_id,
            entity_type="post",
        )

    return schemas.CommentOut(
        id=comment.id,
        post_id=comment.post_id,
        author_id=comment.author_id,
        author_name=current_user.username,
        content=comment.content,
        created_at=comment.created_at,
    )
