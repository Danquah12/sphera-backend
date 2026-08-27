"""Database engine — supports both SQLite (dev) and PostgreSQL (prod) via DATABASE_URL."""
import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./sphera.db")

# SQLite needs check_same_thread=False; Postgres does not
_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

# For asyncpg connection strings coming from Heroku/Render ("postgres://")
# SQLAlchemy 1.4+ requires "postgresql://"
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(
    DATABASE_URL,
    connect_args=_connect_args,
    pool_pre_ping=True,   # reconnect on stale connections (important for Postgres)
    pool_size=10 if not DATABASE_URL.startswith("sqlite") else 1,
    max_overflow=20 if not DATABASE_URL.startswith("sqlite") else 0,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """FastAPI dependency — yields a DB session and closes it on exit."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
