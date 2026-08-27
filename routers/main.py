"""FastAPI application entry point — SpheraChat backend."""
import json
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy.orm import Session

from config import settings
from database import engine, SessionLocal
import models
from rate_limit import limiter
from routers import auth, posts, users, messages, linkedup, pay
from routers import uploads, notifications, admin, stories, search
from routers import password_reset
from routers import ai as ai_router
from routers import video as video_router
from routers import bazaar as bazaar_router
from auth import get_current_user_from_token
from ws import manager

# ── Create all tables ──────────────────────────────────────────────
models.Base.metadata.create_all(bind=engine)

# ── Ensure upload directory exists ────────────────────────────────
Path(settings.upload_dir).mkdir(parents=True, exist_ok=True)

# ── App ────────────────────────────────────────────────────────────
app = FastAPI(
    title="SpheraChat API",
    version="5.0.0",
    description="Backend for the SPHERA social platform — with AI features powered by OpenAI",
)

# ── Rate limiter ───────────────────────────────────────────────────
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ── CORS ───────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Static files (uploaded media) ─────────────────────────────────
app.mount("/static", StaticFiles(directory="static", html=False), name="static")

# ── Mount routers ──────────────────────────────────────────────────
PREFIX = "/api/v1"
app.include_router(auth.router,           prefix=PREFIX)
app.include_router(posts.router,          prefix=PREFIX)
app.include_router(users.router,          prefix=PREFIX)
app.include_router(messages.router,       prefix=PREFIX)
app.include_router(linkedup.router,       prefix=PREFIX)
app.include_router(pay.router,            prefix=PREFIX)
app.include_router(uploads.router,        prefix=PREFIX)
app.include_router(notifications.router,  prefix=PREFIX)
app.include_router(admin.router,          prefix=PREFIX)
app.include_router(stories.router,        prefix=PREFIX)
app.include_router(search.router,         prefix=PREFIX)
app.include_router(password_reset.router, prefix=PREFIX)
app.include_router(ai_router.router,      prefix=PREFIX)
app.include_router(video_router.router,   prefix=PREFIX)
app.include_router(bazaar_router.router,  prefix=PREFIX)  # BAZAAR marketplace (API2Cart)


# ── WebSocket endpoint ────────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, token: str = Query(default=None)):
    db: Session = SessionLocal()
    try:
        user = get_current_user_from_token(token or "", db) if token else None
        if not user:
            await ws.close(code=4001)
            return

        await manager.connect(user.id, ws)
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    data = json.loads(raw)
                except Exception:
                    continue

                # Heartbeat ping → pong
                if data.get("type") == "ping":
                    await ws.send_text(json.dumps({"type": "pong"}))

        except WebSocketDisconnect:
            manager.disconnect(user.id)
    finally:
        db.close()


# ── Health check ──────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "service": "SpheraChat API"}


# ── Swagger UI root redirect ──────────────────────────────────────
@app.get("/")
def root():
    return {"message": "SpheraChat API is running. Visit /docs for Swagger UI."}
