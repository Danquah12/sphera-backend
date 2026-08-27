"""Centralised configuration — reads from .env file."""
import os
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


class Settings:
    # JWT
    jwt_secret_key: str      = os.getenv("JWT_SECRET_KEY", "sphera-dev-secret-CHANGE-ME")
    jwt_algorithm: str       = os.getenv("JWT_ALGORITHM", "HS256")
    access_expire_min: int   = int(os.getenv("JWT_ACCESS_EXPIRE_MINUTES", "30"))
    refresh_expire_days: int = int(os.getenv("JWT_REFRESH_EXPIRE_DAYS", "7"))

    # Database
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./sphera.db")

    # Uploads
    upload_dir: str       = os.getenv("UPLOAD_DIR", "./static/uploads")
    max_upload_bytes: int = int(os.getenv("MAX_UPLOAD_BYTES", str(50 * 1024 * 1024)))

    # CORS
    allowed_origins: list = os.getenv("ALLOWED_ORIGINS", "*").split(",")

    # App
    app_env: str      = os.getenv("APP_ENV", "development")
    app_base_url: str = os.getenv("APP_BASE_URL", "http://localhost:8000")

    # Email (SMTP)
    smtp_host: str     = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port: int     = int(os.getenv("SMTP_PORT", "587"))
    smtp_user: str     = os.getenv("SMTP_USER", "")
    smtp_password: str = os.getenv("SMTP_PASSWORD", "")
    smtp_from: str     = os.getenv("SMTP_FROM", "noreply@sphera.io")

    # eBay API (BAZAAR marketplace cross-listing)
    ebay_client_id: str     = os.getenv("EBAY_CLIENT_ID", "")
    ebay_client_secret: str = os.getenv("EBAY_CLIENT_SECRET", "")
    ebay_dev_id: str        = os.getenv("EBAY_DEV_ID", "")
    ebay_sandbox: bool      = os.getenv("EBAY_SANDBOX", "true").lower() in ("1", "true", "yes")
    ebay_marketplace: str   = os.getenv("EBAY_MARKETPLACE_ID", "EBAY_US")

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgresql") or self.database_url.startswith("postgres")


@lru_cache
def get_settings() -> Settings:
    return Settings()


# Convenience singleton
settings = get_settings()
