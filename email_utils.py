"""Email utilities — SMTP send helper and HTML templates."""
import asyncio
import os
import secrets
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

import aiosmtplib

# ── Config from .env ──────────────────────────────────────────────
SMTP_HOST     = os.getenv("SMTP_HOST",     "smtp.gmail.com")
SMTP_PORT     = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER     = os.getenv("SMTP_USER",     "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM     = os.getenv("SMTP_FROM",     "noreply@sphera.io")
APP_BASE_URL  = os.getenv("APP_BASE_URL",  "http://localhost:8000")
EMAIL_ENABLED = bool(SMTP_USER and SMTP_PASSWORD)


def generate_verification_token() -> str:
    """Generate a cryptographically secure 48-char URL-safe token."""
    return secrets.token_urlsafe(36)


async def _send_email_async(to: str, subject: str, html: str, text: str):
    """Internal async SMTP sender."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SMTP_FROM
    msg["To"]      = to
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html,  "html"))

    await aiosmtplib.send(
        msg,
        hostname=SMTP_HOST,
        port=SMTP_PORT,
        username=SMTP_USER,
        password=SMTP_PASSWORD,
        start_tls=True,
    )


def send_email(to: str, subject: str, html: str, text: str):
    """
    Send an email.
    - If SMTP credentials are configured → fires real email.
    - Otherwise → prints to console (dev mode).
    """
    if not EMAIL_ENABLED:
        print(f"\n{'='*60}")
        print(f"[DEV] EMAIL (not sent — SMTP not configured)")
        print(f"  To:      {to}")
        print(f"  Subject: {subject}")
        print(f"  Body:    {text[:200]}")
        print(f"{'='*60}\n")
        return

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_send_email_async(to, subject, html, text))
    except RuntimeError:
        # Synchronous context — run in a new event loop
        asyncio.run(_send_email_async(to, subject, html, text))


# ── Email Templates ───────────────────────────────────────────────

def verification_email(display_name: str, token: str) -> tuple[str, str, str]:
    """Returns (subject, html, text) for the email verification email."""
    verify_url = f"{APP_BASE_URL}/api/v1/auth/verify-email?token={token}"

    subject = "Verify your SPHERA account"

    html = f"""
<!DOCTYPE html>
<html>
<body style="font-family:sans-serif;background:#0a0a0f;color:#e2e8f0;padding:40px;max-width:600px;margin:0 auto">
  <div style="background:linear-gradient(135deg,#7c3aed,#0ea5e9);padding:3px;border-radius:16px">
    <div style="background:#0f0f1a;border-radius:14px;padding:40px">
      <h1 style="font-size:32px;margin:0 0 8px">◎ SPHERA</h1>
      <p style="color:#94a3b8;margin:0 0 32px">Your Universe of Connection</p>

      <h2 style="font-size:20px;margin:0 0 16px">Welcome, {display_name}! 🎉</h2>
      <p style="line-height:1.7;color:#cbd5e1">
        You're almost in. Click the button below to verify your email and activate your SPHERA account.
      </p>

      <a href="{verify_url}"
         style="display:inline-block;margin:24px 0;padding:14px 32px;
                background:linear-gradient(135deg,#7c3aed,#0ea5e9);
                color:#fff;text-decoration:none;border-radius:10px;
                font-weight:700;font-size:15px">
        ✦ Verify My Account
      </a>

      <p style="color:#64748b;font-size:13px;margin:24px 0 0">
        This link expires in <strong>24 hours</strong>. If you didn't create a SPHERA account, ignore this email.
      </p>

      <hr style="border:none;border-top:1px solid #1e1e2e;margin:32px 0"/>
      <p style="color:#475569;font-size:12px;margin:0">
        Or paste this URL: <br/>
        <a href="{verify_url}" style="color:#7c3aed;word-break:break-all">{verify_url}</a>
      </p>
    </div>
  </div>
</body>
</html>"""

    text = f"""Welcome to SPHERA, {display_name}!

Verify your email by visiting:
{verify_url}

This link expires in 24 hours.
If you didn't create a SPHERA account, ignore this email.
"""
    return subject, html, text


def welcome_email(display_name: str) -> tuple[str, str, str]:
    """Returns (subject, html, text) for the post-verification welcome email."""
    subject = "You're in — Welcome to SPHERA ◎"
    html = f"""
<!DOCTYPE html>
<html>
<body style="font-family:sans-serif;background:#0a0a0f;color:#e2e8f0;padding:40px;max-width:600px;margin:0 auto">
  <h1 style="background:linear-gradient(135deg,#7c3aed,#0ea5e9);-webkit-background-clip:text;-webkit-text-fill-color:transparent">
    ◎ Welcome to SPHERA, {display_name}!
  </h1>
  <p style="color:#cbd5e1;line-height:1.7">
    Your account is verified. Start exploring your universe:
    connect with people, share your story, discover communities,
    and build your orbit. 🚀
  </p>
  <p style="color:#64748b;font-size:13px">— The SPHERA Team</p>
</body>
</html>"""
    text = f"Welcome to SPHERA, {display_name}! Your account is verified. Start exploring."
    return subject, html, text
