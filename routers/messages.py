"""Messages router — conversations, message history, send."""
import asyncio
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import desc
from sqlalchemy.orm import Session

from database import get_db
from auth import get_current_user
import models
import schemas
from ws import manager

router = APIRouter(prefix="/messages", tags=["messages"])


def _msg_out(msg: models.Message) -> schemas.MessageOut:
    return schemas.MessageOut(
        id=msg.id,
        conversation_id=msg.conversation_id,
        sender_id=msg.sender_id,
        sender_name=msg.sender.username if msg.sender else "unknown",
        content=msg.content,
        is_read=msg.is_read,
        created_at=msg.created_at,
    )


@router.post("/conversations", response_model=schemas.ConvoOut, status_code=201)
def start_or_get_conversation(
    req: schemas.StartConvoRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    recipient = db.query(models.User).filter(
        models.User.username == req.recipient_username.lower()
    ).first()
    if not recipient:
        raise HTTPException(404, "User not found")
    if recipient.id == current_user.id:
        raise HTTPException(400, "Cannot message yourself")

    # Find existing DM between the two users
    my_convos = (
        db.query(models.ConversationMember.conversation_id)
        .filter(models.ConversationMember.user_id == current_user.id)
        .subquery()
    )
    their_convos = (
        db.query(models.ConversationMember.conversation_id)
        .filter(models.ConversationMember.user_id == recipient.id)
        .subquery()
    )
    existing = (
        db.query(models.Conversation)
        .filter(models.Conversation.id.in_(my_convos))
        .filter(models.Conversation.id.in_(their_convos))
        .first()
    )
    if existing:
        return existing

    # Create new conversation
    convo = models.Conversation()
    db.add(convo)
    db.flush()
    db.add(models.ConversationMember(conversation_id=convo.id, user_id=current_user.id))
    db.add(models.ConversationMember(conversation_id=convo.id, user_id=recipient.id))
    db.commit()
    db.refresh(convo)
    return convo


@router.get("/{convo_id}", response_model=List[schemas.MessageOut])
def get_messages(
    convo_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    # Verify membership
    member = db.query(models.ConversationMember).filter(
        models.ConversationMember.conversation_id == convo_id,
        models.ConversationMember.user_id == current_user.id,
    ).first()
    if not member:
        raise HTTPException(403, "Not a member of this conversation")

    messages = (
        db.query(models.Message)
        .filter(models.Message.conversation_id == convo_id)
        .order_by(models.Message.created_at)
        .all()
    )
    return [_msg_out(m) for m in messages]


@router.post("/send", response_model=schemas.MessageOut, status_code=201)
def send_message(
    req: schemas.SendMessageRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    # Verify membership
    member = db.query(models.ConversationMember).filter(
        models.ConversationMember.conversation_id == req.conversation_id,
        models.ConversationMember.user_id == current_user.id,
    ).first()
    if not member:
        raise HTTPException(403, "Not a member of this conversation")

    msg = models.Message(
        conversation_id=req.conversation_id,
        sender_id=current_user.id,
        content=req.content,
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)

    # Push real-time event to all other members
    other_members = (
        db.query(models.ConversationMember)
        .filter(
            models.ConversationMember.conversation_id == req.conversation_id,
            models.ConversationMember.user_id != current_user.id,
        )
        .all()
    )
    event = {
        "event": "new_message",
        "conversation_id": req.conversation_id,
        "sender": current_user.username,
        "content": req.content[:120],
    }
    for om in other_members:
        asyncio.create_task(manager.send_to(om.user_id, event))

    return _msg_out(msg)


@router.get("/conversations/list")
def list_conversations(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Return all conversations the current user is part of, with last message preview."""
    memberships = (
        db.query(models.ConversationMember)
        .filter(models.ConversationMember.user_id == current_user.id)
        .all()
    )
    result = []
    for m in memberships:
        convo = db.query(models.Conversation).filter(models.Conversation.id == m.conversation_id).first()
        if not convo:
            continue
        last_msg = (
            db.query(models.Message)
            .filter(models.Message.conversation_id == convo.id)
            .order_by(desc(models.Message.created_at))
            .first()
        )
        # Find the other participant(s)
        other_members = (
            db.query(models.ConversationMember)
            .filter(
                models.ConversationMember.conversation_id == convo.id,
                models.ConversationMember.user_id != current_user.id,
            )
            .all()
        )
        other_users = []
        for om in other_members:
            u = db.query(models.User).filter(models.User.id == om.user_id).first()
            if u:
                other_users.append({"id": u.id, "username": u.username, "avatar_url": u.avatar_url})

        unread = (
            db.query(models.Message)
            .filter(
                models.Message.conversation_id == convo.id,
                models.Message.sender_id != current_user.id,
                models.Message.is_read == False,
            )
            .count()
        )

        result.append({
            "id": convo.id,
            "participants": other_users,
            "last_message": {
                "content": last_msg.content[:100] if last_msg else None,
                "sender_id": last_msg.sender_id if last_msg else None,
                "created_at": last_msg.created_at.isoformat() if last_msg else None,
            } if last_msg else None,
            "unread_count": unread,
            "created_at": convo.created_at.isoformat(),
        })

    # Sort by most recent activity
    result.sort(key=lambda x: x["last_message"]["created_at"] if x.get("last_message") else x["created_at"], reverse=True)
    return {"conversations": result}


@router.post("/{convo_id}/read")
def mark_messages_read(
    convo_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Mark all messages in a conversation as read for the current user."""
    member = db.query(models.ConversationMember).filter(
        models.ConversationMember.conversation_id == convo_id,
        models.ConversationMember.user_id == current_user.id,
    ).first()
    if not member:
        raise HTTPException(403, "Not a member of this conversation")

    updated = (
        db.query(models.Message)
        .filter(
            models.Message.conversation_id == convo_id,
            models.Message.sender_id != current_user.id,
            models.Message.is_read == False,
        )
        .update({"is_read": True})
    )
    db.commit()
    return {"marked_read": updated}
