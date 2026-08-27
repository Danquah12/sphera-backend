"""Auth router — register, login, refresh, /me, email verification."""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from database import get_db
import models
import schemas
import auth as auth_utils
from rate_limit import limiter
from email_utils import (
    generate_verification_token,
    send_email,
    verification_email,
    welcome_email,
)

router = APIRouter(prefix="/auth", tags=["auth"])

TOKEN_EXPIRE_HOURS = 24


# ── Register ──────────────────────────────────────────────────────
@router.post("/register", response_model=schemas.TokenResponse, status_code=201)
@limiter.limit("5/minute")
def register(request: Request, req: schemas.RegisterRequest, db: Session = Depends(get_db)):
    if db.query(models.User).filter(models.User.email == req.email.lower()).first():
        raise HTTPException(400, "Email already registered")
    if db.query(models.User).filter(models.User.username == req.username.lower()).first():
        raise HTTPException(400, "Username already taken")

    # Generate email verification token
    token = generate_verification_token()
    expires = datetime.now(timezone.utc) + timedelta(hours=TOKEN_EXPIRE_HOURS)

    user = models.User(
        username=req.username.lower(),
        email=req.email.lower(),
        password_hash=auth_utils.hash_password(req.password),
        display_name=req.display_name or req.username,
        email_token=token,
        email_token_expires=expires,
        email_verified=False,
    )
    db.add(user)
    db.flush()

    # Create wallet
    db.add(models.Wallet(user_id=user.id, balance=0.0))
    db.commit()
    db.refresh(user)

    # Send verification email (console print if SMTP not configured)
    subject, html, text = verification_email(user.display_name, token)
    send_email(user.email, subject, html, text)

    return schemas.TokenResponse(
        access_token=auth_utils.create_access_token(user.id),
        refresh_token=auth_utils.create_refresh_token(user.id),
    )


# ── Login ─────────────────────────────────────────────────────────
@router.post("/login", response_model=schemas.TokenResponse)
@limiter.limit("10/minute")
def login(request: Request, req: schemas.LoginRequest, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.email == req.email.lower()).first()
    if not user or not auth_utils.verify_password(req.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Account suspended")

    return schemas.TokenResponse(
        access_token=auth_utils.create_access_token(user.id),
        refresh_token=auth_utils.create_refresh_token(user.id),
    )


# ── Refresh token ─────────────────────────────────────────────────
@router.post("/refresh", response_model=schemas.TokenResponse)
@limiter.limit("20/minute")
def refresh(request: Request, credentials=Depends(auth_utils.bearer_scheme), db: Session = Depends(get_db)):
    if not credentials:
        raise HTTPException(401, "Missing token")
    user_id = auth_utils.decode_refresh_token(credentials.credentials)
    if not user_id:
        raise HTTPException(401, "Invalid refresh token")
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(401, "User not found")

    return schemas.TokenResponse(
        access_token=auth_utils.create_access_token(user.id),
        refresh_token=auth_utils.create_refresh_token(user.id),
    )


# ── Me ────────────────────────────────────────────────────────────
@router.get("/me", response_model=schemas.UserOut)
def get_me(current_user: models.User = Depends(auth_utils.get_current_user)):
    return current_user


# ── Email Verification ────────────────────────────────────────────
@router.get("/verify-email")
def verify_email(token: str, db: Session = Depends(get_db)):
    """
    Clicked from the email link.
    GET /api/v1/auth/verify-email?token=<token>
    """
    user = db.query(models.User).filter(models.User.email_token == token).first()
    if not user:
        raise HTTPException(400, "Invalid or expired verification token")

    # Check expiry (compare tz-aware to the naive DB value)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if user.email_token_expires and user.email_token_expires < now:
        raise HTTPException(400, "Verification token has expired — please register again or request a new link")

    user.email_verified = True
    user.email_token = None
    user.email_token_expires = None
    db.commit()

    # Send welcome email
    subject, html, text = welcome_email(user.display_name)
    send_email(user.email, subject, html, text)

    return {
        "verified": True,
        "username": user.username,
        "message": f"Email verified! Welcome to SPHERA, {user.display_name} 🎉",
    }


@router.post("/resend-verification")
@limiter.limit("3/minute")
def resend_verification(
    request: Request,
    current_user: models.User = Depends(auth_utils.get_current_user),
    db: Session = Depends(get_db),
):
    """Resend the verification email if the user hasn't verified yet."""
    if current_user.email_verified:
        raise HTTPException(400, "Email already verified")

    token = generate_verification_token()
    expires = datetime.now(timezone.utc) + timedelta(hours=TOKEN_EXPIRE_HOURS)
    current_user.email_token = token
    current_user.email_token_expires = expires
    db.commit()

    subject, html, text = verification_email(current_user.display_name, token)
    send_email(current_user.email, subject, html, text)

    return {"sent": True, "message": "Verification email resent"}
