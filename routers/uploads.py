"""File upload router — POST /upload for images, videos, avatars."""
import os
import uuid
import mimetypes
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from database import get_db
from auth import get_current_user
from config import settings
import models

router = APIRouter(prefix="/upload", tags=["uploads"])

# Allowed MIME types
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
ALLOWED_VIDEO_TYPES = {"video/mp4", "video/webm", "video/quicktime"}
ALLOWED_TYPES = ALLOWED_IMAGE_TYPES | ALLOWED_VIDEO_TYPES


def _ensure_upload_dir(subdir: str = "") -> Path:
    """Create upload directory if it doesn't exist and return path."""
    path = Path(settings.upload_dir) / subdir
    path.mkdir(parents=True, exist_ok=True)
    return path


def _unique_filename(original: str) -> str:
    """Generate a unique filename preserving the extension."""
    ext = Path(original).suffix.lower() or ".bin"
    return f"{uuid.uuid4().hex}{ext}"


@router.post("")
async def upload_file(
    file: UploadFile = File(...),
    purpose: str = Form(default="post"),  # "post" | "avatar" | "cover" | "reel"
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Upload a media file. Returns the public URL path.

    - **purpose**: "post" | "avatar" | "cover" | "reel"
    - Max size: 50 MB (configurable via MAX_UPLOAD_BYTES)
    """
    # Validate MIME type
    content_type = file.content_type or ""
    if content_type not in ALLOWED_TYPES:
        raise HTTPException(
            400,
            f"File type '{content_type}' not allowed. Accepted: images (JPEG/PNG/GIF/WebP) and videos (MP4/WebM/MOV)"
        )

    # Read and size-check
    data = await file.read()
    if len(data) > settings.max_upload_bytes:
        max_mb = settings.max_upload_bytes // (1024 * 1024)
        raise HTTPException(413, f"File too large. Maximum size is {max_mb} MB.")

    # Determine subdirectory by purpose
    subdir_map = {
        "avatar": "avatars",
        "cover":  "covers",
        "reel":   "reels",
        "post":   "posts",
    }
    subdir = subdir_map.get(purpose, "misc")
    upload_path = _ensure_upload_dir(subdir)
    filename = _unique_filename(file.filename or "upload")
    file_path = upload_path / filename

    # Write file
    with open(file_path, "wb") as f:
        f.write(data)

    # Build public URL (served via /static/uploads/)
    public_url = f"/static/uploads/{subdir}/{filename}"

    # For avatar/cover: update user record immediately
    if purpose == "avatar":
        db.query(models.User).filter(models.User.id == current_user.id).update(
            {"avatar_url": public_url}
        )
        db.commit()
    elif purpose == "cover":
        db.query(models.User).filter(models.User.id == current_user.id).update(
            {"cover_url": public_url}
        )
        db.commit()

    return JSONResponse({
        "url": public_url,
        "filename": filename,
        "content_type": content_type,
        "size_bytes": len(data),
        "purpose": purpose,
    })


@router.delete("/{subdir}/{filename}")
async def delete_file(
    subdir: str,
    filename: str,
    current_user: models.User = Depends(get_current_user),
):
    """Delete an uploaded file (only admins or the uploader via matching filename pattern)."""
    if current_user.role != "admin":
        # Non-admins can only delete files that contain their user ID prefix (future feature)
        raise HTTPException(403, "Admin only")

    file_path = Path(settings.upload_dir) / subdir / filename
    if not file_path.exists():
        raise HTTPException(404, "File not found")

    file_path.unlink()
    return {"deleted": True, "path": f"/static/uploads/{subdir}/{filename}"}
