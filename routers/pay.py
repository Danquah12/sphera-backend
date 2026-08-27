"""Sphera Pay router — wallet balance, top-up, send money."""
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from auth import get_current_user
import models
import schemas

router = APIRouter(prefix="/pay", tags=["pay"])


def _ensure_wallet(db: Session, user_id: int) -> models.Wallet:
    wallet = db.query(models.Wallet).filter(models.Wallet.user_id == user_id).first()
    if not wallet:
        wallet = models.Wallet(user_id=user_id, balance=0.0)
        db.add(wallet)
        db.commit()
        db.refresh(wallet)
    return wallet


@router.get("/wallet", response_model=schemas.WalletOut)
def get_wallet(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    wallet = _ensure_wallet(db, current_user.id)
    return wallet


@router.post("/topup", response_model=schemas.WalletOut)
def top_up(
    req: schemas.TopUpRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    if req.amount <= 0:
        raise HTTPException(400, "Amount must be positive")

    wallet = _ensure_wallet(db, current_user.id)
    wallet.balance += req.amount

    db.add(models.Transaction(
        from_user_id=None,
        to_user_id=current_user.id,
        amount=req.amount,
        note=f"Top-up via {req.method}",
        tx_type="topup",
    ))
    db.commit()
    db.refresh(wallet)
    return wallet


@router.post("/send", response_model=schemas.TransactionOut, status_code=201)
def send_money(
    req: schemas.SendMoneyRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    if req.amount <= 0:
        raise HTTPException(400, "Amount must be positive")

    recipient = db.query(models.User).filter(
        models.User.username == req.recipient_username.lower()
    ).first()
    if not recipient:
        raise HTTPException(404, "Recipient not found")
    if recipient.id == current_user.id:
        raise HTTPException(400, "Cannot send money to yourself")

    sender_wallet = _ensure_wallet(db, current_user.id)
    if sender_wallet.balance < req.amount:
        raise HTTPException(400, "Insufficient balance")

    recipient_wallet = _ensure_wallet(db, recipient.id)
    sender_wallet.balance -= req.amount
    recipient_wallet.balance += req.amount

    tx = models.Transaction(
        from_user_id=current_user.id,
        to_user_id=recipient.id,
        amount=req.amount,
        note=req.note,
        tx_type="transfer",
    )
    db.add(tx)
    db.commit()
    db.refresh(tx)

    return schemas.TransactionOut(
        id=tx.id,
        amount=tx.amount,
        note=tx.note or "",
        tx_type=tx.tx_type,
        created_at=tx.created_at,
    )


@router.get("/transactions")
def get_transactions(
    page: int = 1,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Return paginated wallet transaction history for the current user."""
    from sqlalchemy import desc, or_
    PAGE = 20
    txs = (
        db.query(models.Transaction)
        .filter(
            or_(
                models.Transaction.from_user_id == current_user.id,
                models.Transaction.to_user_id == current_user.id,
            )
        )
        .order_by(desc(models.Transaction.created_at))
        .offset((page - 1) * PAGE)
        .limit(PAGE)
        .all()
    )

    result = []
    for tx in txs:
        direction = "in" if tx.to_user_id == current_user.id else "out"
        other_id = tx.from_user_id if direction == "in" else tx.to_user_id
        other_user = db.query(models.User).filter(models.User.id == other_id).first() if other_id else None
        result.append({
            "id": tx.id,
            "amount": tx.amount,
            "direction": direction,
            "tx_type": tx.tx_type,
            "note": tx.note or "",
            "other_party": other_user.username if other_user else "SPHERA",
            "created_at": tx.created_at.isoformat(),
        })
    return {"transactions": result, "page": page}
