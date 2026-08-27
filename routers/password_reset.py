"""Password reset router — forgot/reset endpoints wired to email_utils."""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from database import get_db
from rate_limit import limiter
from email_utils import generate_verification_token, send_email
import models

router = APIRouter(prefix="/auth", tags=["password-reset"])

RESET_EXPIRE_HOURS = 2


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str


@router.post("/forgot-password")
@limiter.limit("5/minute")
def forgot_password(
    request: Request,
    req: ForgotPasswordRequest,
    db: Session = Depends(get_db),
):
    """
    Send a password reset email.
    Always returns 200 to avoid user enumeration attacks.
    """
    user = db.query(models.User).filter(
        models.User.email == req.email.lower()
    ).first()

    if user:
        token = generate_verification_token()
        expires = datetime.now(timezone.utc) + timedelta(hours=RESET_EXPIRE_HOURS)
        user.email_token = token
        user.email_token_expires = expires
        db.commit()

        from config import settings
        reset_url = f"{settings.app_base_url}/api/v1/auth/reset-password?token={token}"

        subject = "Reset your SPHERA password"
        html = f"""
<!DOCTYPE html>
<html>
<body style="font-family:sans-serif;background:#0a0a0f;color:#e2e8f0;padding:40px;max-width:600px;margin:0 auto">
  <div style="background:linear-gradient(135deg,#7c3aed,#0ea5e9);padding:3px;border-radius:16px">
    <div style="background:#0f0f1a;border-radius:14px;padding:40px">
      <h1 style="margin:0 0 8px">◎ SPHERA</h1>
      <h2 style="font-size:18px;margin:0 0 16px;color:#f1f5f9">Password Reset Requested</h2>
      <p style="color:#cbd5e1;line-height:1.7">
        Hi {user.display_name},<br><br>
        We received a request to reset your password. Click below to set a new one.
        This link expires in <strong>2 hours</strong>.
      </p>
      <a href="{reset_url}"
         style="display:inline-block;margin:24px 0;padding:14px 32px;
                background:linear-gradient(135deg,#7c3aed,#0ea5e9);
                color:#fff;text-decoration:none;border-radius:10px;font-weight:700">
        🔑 Reset My Password
      </a>
      <p style="color:#64748b;font-size:12px;margin:16px 0 0">
        If you didn't request this, ignore this email. Your password won't change.<br/>
        Or paste: <a href="{reset_url}" style="color:#7c3aed">{reset_url}</a>
      </p>
    </div>
  </div>
</body>
</html>"""
        text = f"Reset your SPHERA password: {reset_url} (expires in 2 hours)"
        send_email(user.email, subject, html, text)

    return {"sent": True, "message": "If that email exists, a reset link has been sent"}


@router.post("/reset-password")
@limiter.limit("10/minute")
def reset_password(
    request: Request,
    req: ResetPasswordRequest,
    db: Session = Depends(get_db),
):
    """Apply the new password using the reset token."""
    user = db.query(models.User).filter(
        models.User.email_token == req.token
    ).first()

    if not user:
        raise HTTPException(400, "Invalid or expired reset token")

    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    if user.email_token_expires and user.email_token_expires < now_naive:
        raise HTTPException(400, "Reset token has expired — request a new one")

    if len(req.new_password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")

    from auth import hash_password
    user.password_hash = hash_password(req.new_password)
    user.email_token = None
    user.email_token_expires = None
    db.commit()

    return {"reset": True, "message": "Password updated successfully"}


@router.get("/reset-password")
def reset_password_page(token: str):
    """
    Handles the GET link from the email.
    In a real deployment, this would serve an HTML form.
    Here we return JSON instructions.
    """
    return {
        "token": token,
        "instructions": "POST to /api/v1/auth/reset-password with {token, new_password}",
        "example": {"token": token, "new_password": "your-new-password"},
    }
