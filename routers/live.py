"""
SpheraCut Live Integrations API
"""
import os, json, time, secrets, requests
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from typing import Optional

router = APIRouter(prefix="/api/live", tags=["live"])

DAILY_API_KEY = os.getenv("DAILY_API_KEY", "")
DAILY_DOMAIN = os.getenv("DAILY_DOMAIN", "spheracut")
AGORA_APP_ID = os.getenv("AGORA_APP_ID", "")
AGORA_APP_CERT = os.getenv("AGORA_APP_CERTIFICATE", "")
MUX_TOKEN_ID = os.getenv("MUX_TOKEN_ID", "")
MUX_TOKEN_SECRET = os.getenv("MUX_TOKEN_SECRET", "")

class DailyRoomRequest(BaseModel):
    name: Optional[str] = None
    privacy: str = "public"
    max_participants: int = 20

@router.post("/daily/rooms")
async def create_daily_room(req: DailyRoomRequest):
    if not DAILY_API_KEY:
        room_name = req.name or f"sphera-{secrets.token_hex(4)}"
        return {"url": f"https://{DAILY_DOMAIN}.daily.co/{room_name}", "name": room_name, "demo": True, "message": "Set DAILY_API_KEY in .env"}
    room_name = req.name or f"sphera-{secrets.token_hex(4)}"
    resp = requests.post("https://api.daily.co/v1/rooms", headers={"Authorization": f"Bearer {DAILY_API_KEY}", "Content-Type": "application/json"}, json={"name": room_name, "privacy": req.privacy, "properties": {"max_participants": req.max_participants, "enable_screenshare": True, "enable_chat": True, "exp": int(time.time()) + 86400}})
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    data = resp.json()
    return {"url": data.get("url"), "name": data.get("name"), "id": data.get("id"), "demo": False}

@router.get("/daily/rooms")
async def list_daily_rooms():
    if not DAILY_API_KEY:
        return {"rooms": [], "demo": True}
    resp = requests.get("https://api.daily.co/v1/rooms", headers={"Authorization": f"Bearer {DAILY_API_KEY}"})
    return {"rooms": resp.json().get("data", [])[:20]} if resp.status_code == 200 else {"rooms": []}

class AgoraTokenRequest(BaseModel):
    channel: str = "sphera-live"
    uid: int = 0
    role: str = "host"

@router.post("/agora/token")
async def generate_agora_token(req: AgoraTokenRequest):
    return {"appId": AGORA_APP_ID or "DEMO_APP_ID", "token": None, "channel": req.channel, "demo": not bool(AGORA_APP_ID), "message": "Set AGORA_APP_ID in .env"}

@router.get("/agora/config")
async def get_agora_config():
    return {"appId": AGORA_APP_ID or "DEMO_APP_ID", "configured": bool(AGORA_APP_ID)}

@router.post("/mux/upload")
async def create_mux_upload():
    if not MUX_TOKEN_ID or not MUX_TOKEN_SECRET:
        return {"upload_url": None, "demo": True, "message": "Set MUX_TOKEN_ID in .env"}
    resp = requests.post("https://api.mux.com/video/v1/uploads", auth=(MUX_TOKEN_ID, MUX_TOKEN_SECRET), json={"new_asset_settings": {"playback_policy": ["public"]}, "cors_origin": "*"})
    if resp.status_code not in (200, 201):
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    data = resp.json()["data"]
    return {"upload_url": data.get("url"), "upload_id": data.get("id"), "demo": False}

@router.get("/mux/assets")
async def list_mux_assets():
    if not MUX_TOKEN_ID:
        return {"assets": [{"playback_id": "EcHgOK9coz5K4rjSwOkoE7Y7O01201YMIC200RI6lNxnhs", "title": "Cape Coast Kitchen"}, {"playback_id": "qxb01i02T02SOCIkYJgpNDCelMhq3AI2hB", "title": "BAZAAR Eats Tour"}, {"playback_id": "DS00Spx1CV902MCtPj5WknGlR102V5HFkDe", "title": "SpheraCut Demo"}], "demo": True}
    resp = requests.get("https://api.mux.com/video/v1/assets", auth=(MUX_TOKEN_ID, MUX_TOKEN_SECRET))
    if resp.status_code == 200:
        assets = [{"playback_id": a.get("playback_ids", [{}])[0].get("id") if a.get("playback_ids") else None, "title": a.get("id", "")[:16], "status": a.get("status")} for a in resp.json().get("data", [])[:50]]
        return {"assets": assets, "demo": False}
    return {"assets": [], "demo": True}

_webrtc_peers = {}

@router.websocket("/ws/signal/{peer_id}")
async def webrtc_signaling(websocket: WebSocket, peer_id: str):
    await websocket.accept()
    _webrtc_peers[peer_id] = websocket
    for pid, ws in list(_webrtc_peers.items()):
        if pid != peer_id:
            try:
                await ws.send_json({"type": "peer-joined", "peerId": peer_id, "peers": list(_webrtc_peers.keys())})
            except: pass
    try:
        while True:
            data = await websocket.receive_json()
            target = data.get("target")
            if data.get("type") in ("offer", "answer", "ice-candidate") and target and target in _webrtc_peers:
                await _webrtc_peers[target].send_json({"type": data["type"], "from": peer_id, "payload": data.get("payload", {})})
    except WebSocketDisconnect: pass
    finally:
        _webrtc_peers.pop(peer_id, None)
        for pid, ws in list(_webrtc_peers.items()):
            try:
                await ws.send_json({"type": "peer-left", "peerId": peer_id})
            except: pass

@router.get("/status")
async def live_status():
    return {"daily": {"configured": bool(DAILY_API_KEY)}, "agora": {"configured": bool(AGORA_APP_ID)}, "mux": {"configured": bool(MUX_TOKEN_ID)}, "webrtc": {"signaling": True, "peers": len(_webrtc_peers)}}
