"""SQLAlchemy ORM models for SpheraChat."""
import json
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey,
    Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import relationship

from database import Base


def now_utc():
    return datetime.now(timezone.utc)


# ───────────────────────────── Users ──────────────────────────────
class User(Base):
    __tablename__ = "users"

    id            = Column(Integer, primary_key=True, index=True)
    username      = Column(String(40), unique=True, index=True, nullable=False)
    email         = Column(String(120), unique=True, index=True, nullable=False)
    password_hash = Column(String(200), nullable=False)
    display_name  = Column(String(80), nullable=False, default="")
    bio           = Column(Text, default="")
    avatar_url    = Column(String(300), default="")
    cover_url     = Column(String(300), default="")
    role          = Column(String(20), default="user")   # "admin" | "user"
    score         = Column(Integer, default=0)
    is_active     = Column(Boolean, default=True)
    # ── Email verification ──────────────────────────────
    email_verified      = Column(Boolean, default=False)
    email_token         = Column(String(100), nullable=True)    # one-time token
    email_token_expires = Column(DateTime,    nullable=True)    # expiry timestamp
    created_at    = Column(DateTime, default=now_utc)

    posts         = relationship("Post",    back_populates="author",  cascade="all, delete")
    comments      = relationship("Comment", back_populates="author",  cascade="all, delete")
    wallet        = relationship("Wallet",  back_populates="owner",   uselist=False, cascade="all, delete")
    sent_messages = relationship("Message", back_populates="sender",  cascade="all, delete")


# ───────────────────────────── Follows ────────────────────────────
class Follow(Base):
    __tablename__ = "follows"
    __table_args__ = (UniqueConstraint("follower_id", "followee_id"),)

    id          = Column(Integer, primary_key=True)
    follower_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    followee_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    created_at  = Column(DateTime, default=now_utc)


# ───────────────────────────── Posts ──────────────────────────────
class Post(Base):
    __tablename__ = "posts"

    id             = Column(Integer, primary_key=True, index=True)
    author_id      = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    content        = Column(Text, default="")
    _media_urls    = Column("media_urls", Text, default="[]")   # JSON list
    post_type      = Column(String(20), default="feed")          # "feed" | "reel"
    likes_count    = Column(Integer, default=0)
    comments_count = Column(Integer, default=0)
    shares_count   = Column(Integer, default=0)
    created_at     = Column(DateTime, default=now_utc)

    author   = relationship("User", back_populates="posts")
    likes    = relationship("PostLike", back_populates="post", cascade="all, delete")
    comments = relationship("Comment", back_populates="post", cascade="all, delete")

    @property
    def media_urls(self):
        try:
            return json.loads(self._media_urls or "[]")
        except Exception:
            return []

    @media_urls.setter
    def media_urls(self, value):
        self._media_urls = json.dumps(value or [])


class PostLike(Base):
    __tablename__ = "post_likes"
    __table_args__ = (UniqueConstraint("user_id", "post_id"),)

    id      = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    post_id = Column(Integer, ForeignKey("posts.id", ondelete="CASCADE"), nullable=False)

    post = relationship("Post", back_populates="likes")


class Comment(Base):
    __tablename__ = "comments"

    id         = Column(Integer, primary_key=True)
    post_id    = Column(Integer, ForeignKey("posts.id", ondelete="CASCADE"), nullable=False)
    author_id  = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    content    = Column(Text, nullable=False)
    created_at = Column(DateTime, default=now_utc)

    post   = relationship("Post", back_populates="comments")
    author = relationship("User", back_populates="comments")


# ───────────────────────────── Messaging ──────────────────────────
class Conversation(Base):
    __tablename__ = "conversations"

    id         = Column(Integer, primary_key=True)
    created_at = Column(DateTime, default=now_utc)

    members  = relationship("ConversationMember", back_populates="conversation", cascade="all, delete")
    messages = relationship("Message", back_populates="conversation", cascade="all, delete")


class ConversationMember(Base):
    __tablename__ = "conversation_members"
    __table_args__ = (UniqueConstraint("conversation_id", "user_id"),)

    id              = Column(Integer, primary_key=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id", ondelete="CASCADE"))
    user_id         = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"))

    conversation = relationship("Conversation", back_populates="members")


class Message(Base):
    __tablename__ = "messages"

    id              = Column(Integer, primary_key=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id", ondelete="CASCADE"))
    sender_id       = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"))
    content         = Column(Text, nullable=False)
    is_read         = Column(Boolean, default=False)
    created_at      = Column(DateTime, default=now_utc)

    conversation = relationship("Conversation", back_populates="messages")
    sender       = relationship("User", back_populates="sent_messages")


# ───────────────────────────── LinkedUp ───────────────────────────
class LinkedUpSwipe(Base):
    __tablename__ = "linkedup_swipes"
    __table_args__ = (UniqueConstraint("swiper_id", "swiped_id"),)

    id        = Column(Integer, primary_key=True)
    swiper_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    swiped_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    direction = Column(String(10), nullable=False)  # "right" | "left"
    created_at = Column(DateTime, default=now_utc)


# ───────────────────────────── Sphera Pay ─────────────────────────
class Wallet(Base):
    __tablename__ = "wallet"

    id      = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    balance = Column(Float, default=0.0)

    owner = relationship("User", back_populates="wallet")


class Transaction(Base):
    __tablename__ = "transactions"

    id           = Column(Integer, primary_key=True)
    from_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    to_user_id   = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    amount       = Column(Float, nullable=False)
    note         = Column(String(200), default="")
    tx_type      = Column(String(20), default="transfer")  # "topup" | "transfer"
    created_at   = Column(DateTime, default=now_utc)


# ───────────────────────────── Notifications ──────────────────────
class Notification(Base):
    """
    Persisted notification for a user.

    notif_type:
        "like"       — actor liked recipient's post
        "comment"    — actor commented on recipient's post
        "follow"     — actor followed recipient
        "match"      — LinkedUp mutual match
        "message"    — new DM received
        "mention"    — actor mentioned recipient in a post/comment
    """
    __tablename__ = "notifications"

    id          = Column(Integer, primary_key=True, index=True)
    recipient_id= Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    actor_id    = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    notif_type  = Column(String(30), nullable=False)   # see docstring above
    entity_id   = Column(Integer, nullable=True)       # post_id / convo_id / etc.
    entity_type = Column(String(30), default="")       # "post" | "conversation" | "user"
    body        = Column(String(300), default="")      # human-readable summary
    is_read     = Column(Boolean, default=False)
    created_at  = Column(DateTime, default=now_utc)


# ───────────────────────────── Stories ────────────────────────────
class Story(Base):
    """Short-lived content that expires after 24 hours."""
    __tablename__ = "stories"

    id         = Column(Integer, primary_key=True, index=True)
    author_id  = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    content    = Column(Text, default="")
    media_url  = Column(String(300), default="")
    bg_color   = Column(String(20), default="#1a1a2e")
    story_type = Column(String(20), default="text")   # "text" | "image" | "video"
    view_count = Column(Integer, default=0)
    expires_at = Column(DateTime, nullable=False)     # now + 24h
    created_at = Column(DateTime, default=now_utc)

    author = relationship("User", foreign_keys=[author_id])


# ───────────────────────────── PostHashtag ─────────────────────────
class PostHashtag(Base):
    """Links a post to a hashtag string for fast lookups."""
    __tablename__ = "post_hashtags"
    __table_args__ = (UniqueConstraint("post_id", "tag"),)

    id      = Column(Integer, primary_key=True)
    post_id = Column(Integer, ForeignKey("posts.id", ondelete="CASCADE"), nullable=False)
    tag     = Column(String(100), index=True, nullable=False)  # lowercase, no '#'


# ───────────────────── BAZAAR Eats ────────────────────────────────
class EatsRestaurant(Base):
    """
    A restaurant OR grocery/market store in the BAZAAR Eats ecosystem.
    Cats, tags, and menu are stored as JSON text.
    Service ZIP codes are stored in EatsServiceZip (one-to-many).
    """
    __tablename__ = "eats_restaurants"

    id          = Column(Integer, primary_key=True, index=True)
    name        = Column(String(200), nullable=False, index=True)
    addr        = Column(String(300), default="")
    phone       = Column(String(30),  default="")
    type        = Column(String(20),  default="restaurant")   # "restaurant" | "grocery"
    cats        = Column(Text,        default="[]")            # JSON list of strings
    rating      = Column(Float,       default=4.5)
    reviews     = Column(Integer,     default=0)
    time        = Column(String(30),  default="30-45")         # delivery ETA or "Open Today"
    fee         = Column(String(30),  default="$2.99")
    min_order   = Column(Float,       default=15.0)
    img         = Column(String(500), default="")
    promo       = Column(String(200), default="")
    tags        = Column(Text,        default="[]")            # JSON list of strings
    featured    = Column(Boolean,     default=False)
    desc        = Column(Text,        default="")
    menu        = Column(Text,        default="[]")            # JSON array of {cat, items[]}
    created_at  = Column(DateTime,    default=now_utc)
    updated_at  = Column(DateTime,    default=now_utc, onupdate=now_utc)

    service_zips = relationship(
        "EatsServiceZip",
        back_populates="restaurant",
        cascade="all, delete-orphan",
        lazy="joined",
    )


class EatsServiceZip(Base):
    """Maps a restaurant to a ZIP code it delivers to / is discoverable from."""
    __tablename__ = "eats_service_zips"
    __table_args__ = (UniqueConstraint("restaurant_id", "zip_code"),)

    id            = Column(Integer, primary_key=True)
    restaurant_id = Column(Integer, ForeignKey("eats_restaurants.id", ondelete="CASCADE"), nullable=False, index=True)
    zip_code      = Column(String(10), nullable=False, index=True)

    restaurant = relationship("EatsRestaurant", back_populates="service_zips")

