"""
routers/video.py — Remotion story-to-video API endpoints
POST /api/v1/story-to-video    → kick off render, return job_id
GET  /api/v1/render-status/{job_id} → poll progress / get result URL

The rendering is done by a Node.js HTTP server running on the HOST machine
at port 8001 (accessible from Docker via the bridge IP 172.18.0.1).
"""
import json
import re
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(tags=["video"])

# Docker bridge gateway — host's IP from inside the container
RENDER_SERVER = "http://172.18.0.1:8001"

# ── Slide accent colours per style ────────────────────────────────
STYLE_ACCENTS = {
    "cinematic":   ["#7c3aed", "#ec4899", "#0ea5e9", "#10b981", "#f59e0b"],
    "news":        ["#ffcc00", "#ff4444", "#ffffff", "#ffcc00", "#ff4444"],
    "magazine":    ["#c0392b", "#2c3e50", "#8e44ad", "#16a085", "#d35400"],
    "documentary": ["#10b981", "#0ea5e9", "#a78bfa", "#34d399", "#60a5fa"],
}


# ── Request model ─────────────────────────────────────────────────
class StoryVideoRequest(BaseModel):
    title: str = "My Story"
    story_text: str
    style: str = "cinematic"
    brand_name: str = "SPHERA"
    slides_per_minute: int = 8
    audio_track: str = "none"      # none | cinematic | inspire | upbeat | documentary
    audio_volume: float = 0.25
    composition: str = "StoryVideo"  # StoryVideo | StoryAnchor
    anchor_style: str = "newsroom"   # newsroom | morning | outdoor | studio


# ── Audio track map ───────────────────────────────────────────────
AUDIO_TRACKS = {
    "cinematic":   "file:///opt/sphera/static/audio/cinematic.mp3",
    "inspire":     "file:///opt/sphera/static/audio/inspire.mp3",
    "upbeat":      "file:///opt/sphera/static/audio/upbeat.mp3",
    "documentary": "file:///opt/sphera/static/audio/documentary.mp3",
}

# ── Story → Slides parser ─────────────────────────────────────────
def parse_story_to_slides(title: str, story_text: str, style: str, slides_per_minute: int) -> list[dict]:
    """Split story text into slides each with a headline + body."""
    accents = STYLE_ACCENTS.get(style, STYLE_ACCENTS["cinematic"])

    # Split on double-newlines first, then on single newlines
    raw_paragraphs = [p.strip() for p in re.split(r"\n{2,}", story_text) if p.strip()]
    if len(raw_paragraphs) < 2:
        raw_paragraphs = [p.strip() for p in story_text.split("\n") if p.strip()]

    paragraphs = raw_paragraphs[:12]  # max 12 slides

    slides = []
    for i, para in enumerate(paragraphs):
        sentences = re.split(r"(?<=[.!?])\s+", para.strip())
        if len(para) > 120 and len(sentences) > 1:
            headline = sentences[0][:80]
            body = " ".join(sentences[1:])[:300]
        else:
            headline = para[:80]
            body = para[80:] if len(para) > 80 else ""

        slides.append({
            "headline": headline,
            "body": body,
            "accent": accents[i % len(accents)],
        })

    if not slides:
        slides = [{"headline": title, "body": story_text[:280], "accent": accents[0]}]

    return slides


def _http_post(url: str, payload: dict, timeout: int = 10) -> dict:
    """Synchronous HTTP POST using Python built-in urllib."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_get(url: str, timeout: int = 5) -> dict:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# Thread pool for blocking urllib calls
_pool = ThreadPoolExecutor(max_workers=4)


# ── Endpoints ─────────────────────────────────────────────────────
@router.post("/story-to-video")
async def story_to_video(req: StoryVideoRequest):
    """Parse story and kick off a Remotion render job via host render server."""
    import asyncio

    if not req.story_text.strip():
        raise HTTPException(status_code=400, detail="story_text is required")

    slides = parse_story_to_slides(req.title, req.story_text, req.style, req.slides_per_minute)

    props = {
        "title": req.title,
        "slides": slides,
        "style": req.style,
        "brandName": req.brand_name,
        "composition": req.composition,   # StoryVideo | StoryAnchor
        "anchorStyle": req.anchor_style,  # newsroom | morning | outdoor | studio
    }

    # Attach audio track if selected
    if req.audio_track and req.audio_track != "none":
        audio_path = AUDIO_TRACKS.get(req.audio_track)
        if audio_path:
            props["audioSrc"] = audio_path
            props["audioVolume"] = req.audio_volume

    try:
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(
            _pool, lambda: _http_post(f"{RENDER_SERVER}/render", props, timeout=10)
        )
        job_id = data["jobId"]
    except urllib.error.URLError as e:
        raise HTTPException(status_code=503, detail=f"Render server unavailable: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Render error: {str(e)}")

    return {
        "job_id": job_id,
        "slide_count": len(slides),
        "estimated_duration_s": round((len(slides) + 1) * 3.5),
        "slides_preview": [{"headline": s["headline"]} for s in slides],
    }


@router.get("/render-status/{job_id}")
async def render_status(job_id: str):
    """Poll the status of an ongoing render job."""
    import asyncio

    try:
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(
            _pool, lambda: _http_get(f"{RENDER_SERVER}/status/{job_id}", timeout=5)
        )
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise HTTPException(status_code=404, detail="Job not found")
        raise HTTPException(status_code=503, detail=f"Render server error: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Render server unavailable: {str(e)}")

    return {
        "job_id": job_id,
        "status": data.get("status", "unknown"),
        "progress": data.get("progress", 0),
        "video_url": data.get("videoUrl"),
        "error": data.get("error"),
    }
