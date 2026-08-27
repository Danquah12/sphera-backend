"""JWT utilities, password hashing, and FastAPI auth dependency."""
from datetime import datetime, timedelta, timezone
from typing import Optional

from argon2 import PasswordHasher as Argon2Hasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from config import settings
from database import get_db
import models

# ── Config (loaded from .env via config.py) ───────────────────────
SECRET_KEY = settings.jwt_secret_key
ALGORITHM  = settings.jwt_algorithm
ACCESS_TOKEN_EXPIRE_MINUTES  = settings.access_expire_min
REFRESH_TOKEN_EXPIRE_DAYS    = settings.refresh_expire_days

# ── Password hashing ──────────────────────────────────────────────
_argon2 = Argon2Hasher()


def hash_password(plain: str) -> str:
    return _argon2.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _argon2.verify(hashed, plain)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


# ── Token creation ────────────────────────────────────────────────
def _make_token(data: dict, expires_delta: timedelta) -> str:
    payload = data.copy()
    payload["exp"] = datetime.now(timezone.utc) + expires_delta
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def create_access_token(user_id: int) -> str:
    return _make_token({"sub": str(user_id), "type": "access"},
                       timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))


def create_refresh_token(user_id: int) -> str:
    return _make_token({"sub": str(user_id), "type": "refresh"},
                       timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS))


# ── Token decoding ────────────────────────────────────────────────
def _decode_token(token: str, expected_type: str = "access") -> Optional[int]:
    """Returns user_id or None if invalid."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("type") != expected_type:
            return None
        sub = payload.get("sub")
        return int(sub) if sub else None
    except JWTError:
        return None


# ── FastAPI dependency ────────────────────────────────────────────
bearer_scheme = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> models.User:
    """Dependency — returns the authenticated User or raises 401."""
    cred_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if not credentials:
        raise cred_exc
    user_id = _decode_token(credentials.credentials, "access")
    if not user_id:
        raise cred_exc
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user or not user.is_active:
        raise cred_exc
    return user


def get_current_user_from_token(token: str, db: Session) -> Optional[models.User]:
    """Utility for WebSocket auth (token passed as query param)."""
    user_id = _decode_token(token, "access")
    if not user_id:
        return None
    return db.query(models.User).filter(models.User.id == user_id).first()


def decode_refresh_token(token: str) -> Optional[int]:
    return _decode_token(token, "refresh")
