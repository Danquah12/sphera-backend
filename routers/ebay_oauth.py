"""
ebay_oauth.py — eBay OAuth 2.0 Authorization Code Flow

SETUP: In eBay Developer Portal (developer.ebay.com/my/keys):
  1. Edit your Sandbox keyset
  2. Add redirect URL: https://bazaar.expediteconsults.com/api/v1/ebay/oauth/callback
  3. Copy the RuName (e.g. ExpediteCon-SpheraBa-SBX-xxxxxxxx-xxxxxxxx)
  4. Set EBAY_RU_NAME=<runame> in backend/.env

USAGE:
  1. Visit https://bazaar.expediteconsults.com/api/v1/ebay/oauth/start
  2. Copy the consent_url, visit it in browser
  3. Sign in as TESTUSER_kdanquah / BazaarEbay1!
  4. eBay redirects to /oauth/callback — token is auto-captured
  5. Copy the access_token from the response, save to .env as EBAY_USER_TOKEN
"""
import os
import time
import base64
import urllib.parse
import logging

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ebay", tags=["eBay OAuth"])

# ── Config ─────────────────────────────────────────────────────────────────────
CLIENT_ID     = os.getenv("EBAY_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("EBAY_CLIENT_SECRET", "")
SANDBOX       = os.getenv("EBAY_SANDBOX", "true").lower() in ("1", "true", "yes")

# The RuName from developer.ebay.com/my/keys (required for OAuth)
# e.g. ExpediteCon-SpheraBa-SBX-xxxxxxxx-xxxxxxxx
EBAY_RU_NAME  = os.getenv("EBAY_RU_NAME", "")

# The actual callback URL (must match what's registered in portal)
REDIRECT_URI  = "https://bazaar.expediteconsults.com/api/v1/ebay/oauth/callback"

AUTH_URL   = ("https://api.sandbox.ebay.com/identity/v1/oauth2/token"
              if SANDBOX else "https://api.ebay.com/identity/v1/oauth2/token")
SIGNIN_URL = ("https://auth.sandbox.ebay.com/oauth2/authorize"
              if SANDBOX else "https://auth.ebay.com/oauth2/authorize")

SCOPES = " ".join([
    "https://api.ebay.com/oauth/api_scope",
    "https://api.ebay.com/oauth/api_scope/sell.inventory",
    "https://api.ebay.com/oauth/api_scope/sell.account",
    "https://api.ebay.com/oauth/api_scope/sell.fulfillment",
])

# In-memory token store (cleared on restart — use .env for persistence)
_token_store: dict = {
    "access_token":  os.getenv("EBAY_USER_TOKEN", ""),
    "refresh_token": os.getenv("EBAY_REFRESH_TOKEN", ""),
    "expires_at":    0.0,
}


def get_stored_token() -> str:
    """Returns the current live access token (used by ebay.py _get_app_token)."""
    return _token_store.get("access_token", "")


# ── OAuth Routes ───────────────────────────────────────────────────────────────

@router.get("/oauth/start", summary="Generate eBay OAuth consent URL")
async def oauth_start():
    """
    Returns the eBay OAuth consent URL.
    Visit it in your browser and sign in as your sandbox user.
    eBay will redirect to /ebay/oauth/callback with an auth code.
    """
    if not EBAY_RU_NAME:
        return {
            "status":  "NOT_CONFIGURED",
            "error":   "EBAY_RU_NAME not set in .env",
            "fix":     (
                "1. Go to developer.ebay.com/my/keys → Edit Sandbox keyset\n"
                "2. Add redirect URL: https://bazaar.expediteconsults.com/api/v1/ebay/oauth/callback\n"
                "3. Copy the generated RuName\n"
                "4. Set EBAY_RU_NAME=<runame> in /opt/sphera/backend/.env\n"
                "5. Rebuild: docker compose -f docker-compose.prod.yml up -d --build backend"
            ),
        }

    params = {
        "client_id":     CLIENT_ID,
        "redirect_uri":  EBAY_RU_NAME,   # eBay OAuth requires RuName here, not the actual URL
        "response_type": "code",
        "scope":         SCOPES,
    }
    consent_url = f"{SIGNIN_URL}?{urllib.parse.urlencode(params)}"

    return {
        "status":       "ready",
        "action":       "Visit consent_url in browser, sign in as TESTUSER_kdanquah / BazaarEbay1!",
        "consent_url":  consent_url,
        "ru_name":      EBAY_RU_NAME,
        "callback_url": REDIRECT_URI,
        "scopes":       SCOPES.split(),
        "env":          "sandbox" if SANDBOX else "production",
    }


@router.get("/oauth/callback", summary="eBay OAuth callback — exchanges code for token")
async def oauth_callback(code: str = Query(...), expires_in: int = Query(default=7200)):
    """
    eBay redirects here after user authorizes.
    Exchanges the auth code for an access + refresh token.
    Token is stored in memory AND logged for saving to .env.
    """
    if not CLIENT_ID or not CLIENT_SECRET:
        raise HTTPException(500, "EBAY_CLIENT_ID / EBAY_CLIENT_SECRET not configured")

    credentials = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                AUTH_URL,
                headers={
                    "Authorization": f"Basic {credentials}",
                    "Content-Type":  "application/x-www-form-urlencoded",
                },
                content=(
                    f"grant_type=authorization_code"
                    f"&code={urllib.parse.quote(code)}"
                    f"&redirect_uri={urllib.parse.quote(EBAY_RU_NAME or REDIRECT_URI)}"
                ),
            )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPStatusError as exc:
        logger.error("Token exchange failed: %s", exc.response.text)
        raise HTTPException(502, detail=f"Token exchange failed: {exc.response.text}")
    except Exception as exc:
        raise HTTPException(503, detail=str(exc))

    access_token  = data.get("access_token", "")
    refresh_token = data.get("refresh_token", "")
    exp           = int(data.get("expires_in", expires_in))

    # Store in memory for immediate use
    _token_store["access_token"]  = access_token
    _token_store["refresh_token"] = refresh_token
    _token_store["expires_at"]    = time.time() + exp

    # Log prominently so admin can save to .env
    logger.info("=" * 70)
    logger.info("✅ eBay OAuth SUCCESS — Save to /opt/sphera/backend/.env:")
    logger.info("EBAY_USER_TOKEN=%s", access_token)
    logger.info("EBAY_REFRESH_TOKEN=%s", refresh_token)
    logger.info("=" * 70)

    return HTMLResponse(f"""
    <html><body style="font-family:monospace;padding:20px;background:#0a0a0a;color:#00ff88">
    <h2>✅ eBay OAuth Success!</h2>
    <p>Token is now active in memory. Save it permanently:</p>
    <p>On the server run:</p>
    <pre style="background:#111;padding:15px;border-radius:8px;word-break:break-all">
echo 'EBAY_USER_TOKEN={access_token}' >> /opt/sphera/backend/.env
echo 'EBAY_REFRESH_TOKEN={refresh_token}' >> /opt/sphera/backend/.env
    </pre>
    <p>Then rebuild: <code>docker compose -f docker-compose.prod.yml up -d --build backend</code></p>
    <p>Expires in: {exp // 3600}h {(exp % 3600) // 60}m</p>
    </body></html>
    """)
