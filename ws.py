"""WebSocket connection manager for SpheraChat real-time events."""
import asyncio
import json
from typing import Dict, Optional

from fastapi import WebSocket


class ConnectionManager:
    """Manages active WebSocket connections keyed by user_id."""

    def __init__(self):
        self._connections: Dict[int, WebSocket] = {}

    async def connect(self, user_id: int, ws: WebSocket):
        await ws.accept()
        self._connections[user_id] = ws

    def disconnect(self, user_id: int):
        self._connections.pop(user_id, None)

    async def send_to(self, user_id: int, event: dict):
        """Send a JSON event to a specific user (no-op if offline)."""
        ws = self._connections.get(user_id)
        if ws:
            try:
                await ws.send_text(json.dumps(event))
            except Exception:
                self.disconnect(user_id)

    async def broadcast(self, event: dict, exclude: Optional[int] = None):
        """Broadcast to all connected users."""
        dead = []
        for uid, ws in self._connections.items():
            if uid == exclude:
                continue
            try:
                await ws.send_text(json.dumps(event))
            except Exception:
                dead.append(uid)
        for uid in dead:
            self.disconnect(uid)


# Singleton used across routers
manager = ConnectionManager()
