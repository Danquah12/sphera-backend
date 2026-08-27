"""LinkedUp router — discover, swipe, matches."""
import asyncio
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from auth import get_current_user
import models
import schemas
from ws import manager

router = APIRouter(prefix="/linkedup", tags=["linkedup"])


@router.get("/discover", response_model=List[schemas.UserSearchOut])
def discover(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Return users not yet swiped by the current user."""
    already_swiped = (
        db.query(models.LinkedUpSwipe.swiped_id)
        .filter(models.LinkedUpSwipe.swiper_id == current_user.id)
        .subquery()
    )
    candidates = (
        db.query(models.User)
        .filter(models.User.id != current_user.id)
        .filter(~models.User.id.in_(already_swiped))
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
        for u in candidates
    ]


@router.post("/swipe", response_model=schemas.SwipeResponse)
def swipe(
    req: schemas.SwipeRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    target = db.query(models.User).filter(models.User.id == req.swiped_user_id).first()
    if not target:
        raise HTTPException(404, "User not found")

    # Upsert swipe
    existing = db.query(models.LinkedUpSwipe).filter(
        models.LinkedUpSwipe.swiper_id == current_user.id,
        models.LinkedUpSwipe.swiped_id == req.swiped_user_id,
    ).first()
    if not existing:
        db.add(models.LinkedUpSwipe(
            swiper_id=current_user.id,
            swiped_id=req.swiped_user_id,
            direction=req.direction,
        ))
        db.commit()

    # Check for mutual right swipe (match)
    matched = False
    if req.direction == "right":
        mutual = db.query(models.LinkedUpSwipe).filter(
            models.LinkedUpSwipe.swiper_id == req.swiped_user_id,
            models.LinkedUpSwipe.swiped_id == current_user.id,
            models.LinkedUpSwipe.direction == "right",
        ).first()
        if mutual:
            matched = True
            event = {"event": "linkedup_match", "matched_with": current_user.username}
            asyncio.create_task(manager.send_to(target.id, event))

    return schemas.SwipeResponse(
        matched=matched,
        matched_with=target.username if matched else None,
    )


@router.get("/matches", response_model=List[schemas.UserSearchOut])
def get_matches(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Return all users who mutually swiped right."""
    my_rights = (
        db.query(models.LinkedUpSwipe.swiped_id)
        .filter(
            models.LinkedUpSwipe.swiper_id == current_user.id,
            models.LinkedUpSwipe.direction == "right",
        )
        .subquery()
    )
    their_rights = (
        db.query(models.LinkedUpSwipe.swiper_id)
        .filter(
            models.LinkedUpSwipe.swiped_id == current_user.id,
            models.LinkedUpSwipe.direction == "right",
        )
        .subquery()
    )
    matched_ids = db.query(models.User).filter(
        models.User.id.in_(my_rights),
        models.User.id.in_(their_rights),
    ).all()

    return [
        schemas.UserSearchOut(
            id=u.id,
            username=u.username,
            display_name=u.display_name,
            avatar_url=u.avatar_url,
        )
        for u in matched_ids
    ]
