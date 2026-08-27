"""Pydantic schemas for request/response validation."""
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, EmailStr


# ── Auth ──────────────────────────────────────────────────────────
class RegisterRequest(BaseModel):
    username: str
    email: EmailStr
    password: str
    display_name: str = ""


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    id: int
    username: str
    email: str
    display_name: str
    bio: str
    avatar_url: str
    cover_url: str
    role: str
    score: int
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Posts ─────────────────────────────────────────────────────────
class PostCreate(BaseModel):
    content: str
    media_urls: List[str] = []
    post_type: str = "feed"


class PostOut(BaseModel):
    id: int
    author_id: int
    author_name: str
    content: str
    media_urls: List[str]
    post_type: str
    likes_count: int
    comments_count: int
    shares_count: int
    created_at: datetime

    model_config = {"from_attributes": True}


class FeedResponse(BaseModel):
    posts: List[PostOut]
    page: int
    has_more: bool


class LikeResponse(BaseModel):
    liked: bool
    likes_count: int


class CommentCreate(BaseModel):
    content: str


class CommentOut(BaseModel):
    id: int
    post_id: int
    author_id: int
    author_name: str
    content: str
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Users ─────────────────────────────────────────────────────────
class ProfileOut(BaseModel):
    id: int
    username: str
    display_name: str
    bio: str
    avatar_url: str
    cover_url: str
    role: str
    score: int
    followers_count: int
    following_count: int
    posts_count: int
    is_following: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class FollowResponse(BaseModel):
    following: bool
    followers_count: int


class UserSearchOut(BaseModel):
    id: int
    username: str
    display_name: str
    avatar_url: str

    model_config = {"from_attributes": True}


# ── Messages ──────────────────────────────────────────────────────
class StartConvoRequest(BaseModel):
    recipient_username: str


class ConvoOut(BaseModel):
    id: int
    created_at: datetime

    model_config = {"from_attributes": True}


class SendMessageRequest(BaseModel):
    conversation_id: int
    content: str


class MessageOut(BaseModel):
    id: int
    conversation_id: int
    sender_id: int
    sender_name: str
    content: str
    is_read: bool
    created_at: datetime

    model_config = {"from_attributes": True}


# ── LinkedUp ──────────────────────────────────────────────────────
class SwipeRequest(BaseModel):
    swiped_user_id: int
    direction: str  # "right" | "left"


class SwipeResponse(BaseModel):
    matched: bool
    matched_with: Optional[str] = None


# ── Sphera Pay ────────────────────────────────────────────────────
class WalletOut(BaseModel):
    balance: float

    model_config = {"from_attributes": True}


class TopUpRequest(BaseModel):
    amount: float
    method: str = "card"


class SendMoneyRequest(BaseModel):
    recipient_username: str
    amount: float
    note: str = ""


class TransactionOut(BaseModel):
    id: int
    amount: float
    note: str
    tx_type: str
    created_at: datetime

    model_config = {"from_attributes": True}
