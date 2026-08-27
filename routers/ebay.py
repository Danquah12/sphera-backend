"""
ebay.py — eBay Marketplace Integration router for SPHERA BAZAAR.

Implements the full eBay listing flow using eBay's REST APIs:
  1. OAuth 2.0 Application Token (client_credentials) — cached & auto-refreshed
  2. PUT  /sell/inventory/v1/inventory_item/{sku}   — create/update inventory
  3. POST /sell/inventory/v1/offer                  — create an offer (listing draft)
  4. POST /sell/inventory/v1/offer/{offerId}/publish — go live on eBay

The router transparently supports both SANDBOX and PRODUCTION environments,
switched via the EBAY_SANDBOX env var (default: true during dev).

Credentials are NEVER hardcoded — always loaded from environment / .env file.
"""

import os
import time
import base64
import asyncio
import logging
from typing import Optional, Any
from dataclasses import dataclass, field

import httpx
from fastapi import APIRouter, HTTPException, Query, Body, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

EBAY_CLIENT_ID     = os.getenv("EBAY_CLIENT_ID", "")
EBAY_CLIENT_SECRET = os.getenv("EBAY_CLIENT_SECRET", "")
EBAY_DEV_ID        = os.getenv("EBAY_DEV_ID", "")
EBAY_SANDBOX              = os.getenv("EBAY_SANDBOX", "true").lower() in ("1", "true", "yes")
EBAY_MARKETPLACE          = os.getenv("EBAY_MARKETPLACE_ID", "EBAY_US")
EBAY_FULFILLMENT_POLICY   = os.getenv("EBAY_FULFILLMENT_POLICY_ID", "")
EBAY_PAYMENT_POLICY       = os.getenv("EBAY_PAYMENT_POLICY_ID", "")
EBAY_RETURN_POLICY        = os.getenv("EBAY_RETURN_POLICY_ID", "")
# Pre-configured user token (from Developer Portal → User Tokens tool)
# Required for Sell APIs — client_credentials only supports buy.* scopes
EBAY_USER_TOKEN           = os.getenv("EBAY_USER_TOKEN", "")
REQUEST_TIMEOUT           = 10  # seconds

# Base URLs
if EBAY_SANDBOX:
    EBAY_API_BASE    = "https://api.sandbox.ebay.com"
    EBAY_AUTH_URL    = "https://api.sandbox.ebay.com/identity/v1/oauth2/token"
    EBAY_SIGNIN_URL  = "https://auth.sandbox.ebay.com/oauth2/authorize"
else:
    EBAY_API_BASE    = "https://api.ebay.com"
    EBAY_AUTH_URL    = "https://api.ebay.com/identity/v1/oauth2/token"
    EBAY_SIGNIN_URL  = "https://auth.ebay.com/oauth2/authorize"

# OAuth redirect URI — must match what's registered in eBay Developer Portal
# For sandbox testing use the server's public URL
EBAY_REDIRECT_URI  = os.getenv("EBAY_REDIRECT_URI", "https://bazaar.expediteconsults.com/api/v1/ebay/oauth/callback")
# RuName from developer portal (Redirecting URL Name)
EBAY_RU_NAME       = os.getenv("EBAY_RU_NAME", "")

# Scopes needed for BAZAAR → eBay listing
EBAY_SCOPES = " ".join([
    "https://api.ebay.com/oauth/api_scope",
    "https://api.ebay.com/oauth/api_scope/sell.inventory",
    "https://api.ebay.com/oauth/api_scope/sell.account",
    "https://api.ebay.com/oauth/api_scope/sell.fulfillment",
])

# In-memory token store (persists across requests, lost on restart)
# Set from env or updated via OAuth callback
# expires_at=0 means unknown — will attempt refresh on first 401
_live_user_token: dict = {
    "access_token":  EBAY_USER_TOKEN,
    "refresh_token": os.getenv("EBAY_REFRESH_TOKEN", ""),
    "expires_at":    0.0,   # 0 = unknown (from .env), will refresh on 401
}
_token_lock = asyncio.Lock()

# In-memory store of successfully listed items (persists across requests)
_listed_items: list = []

# ── Token Cache ───────────────────────────────────────────────────────────────

@dataclass
class _TokenCache:
    access_token: str = ""
    expires_at: float = 0.0          # unix timestamp
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

_token_cache = _TokenCache()


async def _refresh_user_token() -> bool:
    """
    Refresh the eBay user access token using the stored refresh token.
    Returns True on success, False if no refresh token available.
    """
    refresh_token = _live_user_token.get("refresh_token", "")
    if not refresh_token:
        logger.warning("No eBay refresh token available — cannot auto-refresh")
        return False

    credentials = base64.b64encode(
        f"{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}".encode()
    ).decode()

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.post(
                EBAY_AUTH_URL,
                headers={
                    "Authorization": f"Basic {credentials}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={
                    "grant_type":    "refresh_token",
                    "refresh_token": refresh_token,
                    "scope":         EBAY_SCOPES,
                },
            )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.error("eBay token refresh failed: %s", exc)
        return False

    new_access  = data.get("access_token", "")
    new_refresh = data.get("refresh_token", refresh_token)  # may be rotated
    expires_in  = int(data.get("expires_in", 7200))

    if not new_access:
        logger.error("eBay token refresh returned no access_token: %s", data)
        return False

    _live_user_token["access_token"]  = new_access
    _live_user_token["refresh_token"] = new_refresh
    _live_user_token["expires_at"]    = time.time() + expires_in - 60  # 60s buffer
    logger.info("eBay user token auto-refreshed, expires in %ss", expires_in)
    return True


async def _get_app_token() -> str:
    """
    Returns a valid eBay token for Sell APIs.
    Priority: OAuth callback token (auto-refreshed) → app token (public only).
    Automatically refreshes expired OR unknown-age user tokens using the refresh token.
    """
    async with _token_lock:
        now = time.time()
        access  = _live_user_token.get("access_token", "")
        exp_at  = _live_user_token.get("expires_at", 0.0)

        # Token is present and KNOWN not expired (set by previous refresh call)
        if access and exp_at > 0 and exp_at > now:
            return access

        # Token loaded from .env (exp_at==0) OR expired → proactively refresh
        if access and _live_user_token.get("refresh_token"):
            logger.info("eBay token %s — attempting proactive refresh...",
                        "age unknown (from .env)" if exp_at == 0.0 else "expired")
            refreshed = await _refresh_user_token()
            if refreshed:
                return _live_user_token["access_token"]
            # Refresh failed but we still have the token — use it and hope for the best
            if access:
                logger.warning("Token refresh failed, using existing token as fallback")
                _live_user_token["expires_at"] = now + 1800  # assume 30 min left
                return access

        # No refresh token or no access token — fall through to app token
        if access:
            return access

    async with _token_cache.lock:
        now = time.time()
        if _token_cache.access_token and _token_cache.expires_at - 60 > now:
            return _token_cache.access_token

        if not EBAY_CLIENT_ID or not EBAY_CLIENT_SECRET:
            raise HTTPException(
                status_code=503,
                detail="eBay credentials not configured. "
                       "Set EBAY_CLIENT_ID, EBAY_CLIENT_SECRET, or EBAY_USER_TOKEN in .env"
            )

        credentials = base64.b64encode(
            f"{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}".encode()
        ).decode()

        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                resp = await client.post(
                    EBAY_AUTH_URL,
                    headers={
                        "Authorization": f"Basic {credentials}",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    content="grant_type=client_credentials"
                            "&scope=https%3A%2F%2Fapi.ebay.com%2Foauth%2Fapi_scope",
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as exc:
            logger.error("eBay OAuth HTTP error: %s %s", exc.response.status_code, exc.response.text)
            raise HTTPException(status_code=502, detail=f"eBay OAuth failed: {exc.response.text}")
        except Exception as exc:
            logger.error("eBay OAuth connection error: %s", exc)
            raise HTTPException(status_code=503, detail=f"Cannot reach eBay OAuth endpoint: {exc}")

        _token_cache.access_token = data["access_token"]
        _token_cache.expires_at   = now + int(data.get("expires_in", 7200))
        env_label = "SANDBOX" if EBAY_SANDBOX else "PRODUCTION"
        logger.info("eBay %s token refreshed, expires in %ss", env_label, data.get("expires_in"))
        return _token_cache.access_token


def _ebay_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Content-Language": "en-US",
        "X-EBAY-C-MARKETPLACE-ID": EBAY_MARKETPLACE,
    }


async def _ebay_get(path: str, token: str, params: dict = None) -> Any:
    url = f"{EBAY_API_BASE}{path}"
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.get(url, headers=_ebay_headers(token), params=params or {})
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code,
                            detail=exc.response.text)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))


async def _ebay_put(path: str, token: str, body: dict) -> Any:
    url = f"{EBAY_API_BASE}{path}"
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.put(url, headers=_ebay_headers(token), json=body)
        if resp.status_code == 204:       # No Content — success for inventory upserts
            return {"status": "success", "statusCode": 204}
        resp.raise_for_status()
        return resp.json() if resp.content else {"status": "success"}
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code,
                            detail=exc.response.text)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))


async def _ebay_post(path: str, token: str, body: dict) -> Any:
    url = f"{EBAY_API_BASE}{path}"
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.post(url, headers=_ebay_headers(token), json=body)
        if resp.status_code == 204:       # No Content — success for location creates etc.
            return {"status": "success", "statusCode": 204}
        resp.raise_for_status()
        return resp.json() if resp.content else {"status": "success"}
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code,
                            detail=exc.response.text)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))


async def _ebay_delete(path: str, token: str) -> Any:
    url = f"{EBAY_API_BASE}{path}"
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.delete(url, headers=_ebay_headers(token))
        if resp.status_code == 204:
            return {"status": "deleted"}
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code,
                            detail=exc.response.text)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))


# ── Pydantic Models ───────────────────────────────────────────────────────────

class BazaarProduct(BaseModel):
    """Bazaar product fields — sent from frontend when listing to eBay."""
    sku: str                          = Field(..., description="Unique product SKU (URL-safe)")
    name: str                         = Field(..., max_length=80)
    description: str                  = Field(default="")
    price: float                      = Field(..., gt=0)
    currency: str                     = Field(default="USD")
    quantity: int                     = Field(default=1, ge=1)
    condition: str                    = Field(default="NEW",
                                              description="NEW | USED_EXCELLENT | USED_GOOD | USED_ACCEPTABLE")
    category_id: str                  = Field(..., description="eBay category ID (use /ebay/categories to look up)")
    image_url: Optional[str]          = Field(default=None, description="Publicly accessible image URL")
    fulfillment_policy_id: Optional[str] = Field(default=None)
    payment_policy_id: Optional[str]    = Field(default=None)
    return_policy_id: Optional[str]     = Field(default=None)
    merchant_location_key: Optional[str] = Field(default="default")
    item_specifics: Optional[dict]       = Field(default=None, description="Key-value item specifics, e.g. {'Brand':'Samsung','Storage Capacity':'256GB'}")


class WithdrawOfferRequest(BaseModel):
    offer_id: str


# ── Router ────────────────────────────────────────────────────────────────────

router = APIRouter(prefix="/ebay", tags=["eBay Marketplace"])


# ── OAuth 2.0 Authorization Code Flow ─────────────────────────────────────────

@router.get("/oauth/start", summary="Start eBay OAuth — redirects to eBay login")
async def oauth_start():
    """
    Generates the eBay OAuth consent URL and returns it.
    Visit the returned URL in your browser, sign in as your sandbox user,
    and eBay will redirect back to /ebay/oauth/callback with an auth code.
    """
    import urllib.parse
    params = {
        "client_id":     EBAY_CLIENT_ID,
        "redirect_uri":  EBAY_REDIRECT_URI,
        "response_type": "code",
        "scope":         EBAY_SCOPES,
    }
    if EBAY_RU_NAME:
        params["ru_name"] = EBAY_RU_NAME
    consent_url = f"{EBAY_SIGNIN_URL}?{urllib.parse.urlencode(params)}"
    return {
        "status":       "ready",
        "action":       "Visit the consent_url in your browser and sign in as your sandbox user",
        "consent_url":  consent_url,
        "callback_url": EBAY_REDIRECT_URI,
        "scopes":       EBAY_SCOPES.split(),
    }


@router.get("/oauth/callback", summary="eBay OAuth callback — exchanges code for token")
async def oauth_callback(code: str = Query(...), expires_in: int = Query(default=7200)):
    """
    eBay redirects here after the user authorizes. Exchanges the auth code
    for an access token + refresh token and stores it in memory.
    Also logs the token so you can save it to .env as EBAY_USER_TOKEN.
    """
    credentials = base64.b64encode(
        f"{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}".encode()
    ).decode()

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.post(
                EBAY_AUTH_URL,
                headers={
                    "Authorization": f"Basic {credentials}",
                    "Content-Type":  "application/x-www-form-urlencoded",
                },
                content=(
                    f"grant_type=authorization_code"
                    f"&code={code}"
                    f"&redirect_uri={EBAY_REDIRECT_URI}"
                ),
            )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"Token exchange failed: {exc.response.text}")
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    access_token  = data.get("access_token", "")
    refresh_token = data.get("refresh_token", "")
    exp           = int(data.get("expires_in", expires_in))

    # Store in memory for immediate use
    _live_user_token["access_token"]  = access_token
    _live_user_token["refresh_token"] = refresh_token
    _live_user_token["expires_at"]    = time.time() + exp

    # Log to server output so admin can save to .env
    logger.info("=" * 60)
    logger.info("eBay OAuth SUCCESS! Save this token to .env as EBAY_USER_TOKEN:")
    logger.info(access_token)
    logger.info("Refresh token: %s", refresh_token)
    logger.info("=" * 60)

    return {
        "status":         "success",
        "token_type":     data.get("token_type"),
        "expires_in":     exp,
        "access_token":   access_token,
        "refresh_token":  refresh_token,
        "next_step":      "Save access_token to .env as EBAY_USER_TOKEN and restart the container",
    }


# ── Health / Status ───────────────────────────────────────────────────────────

@router.get("/status", summary="eBay integration status + environment info")
async def ebay_status():
    """
    Returns current eBay integration configuration.
    Does NOT reveal credentials — only whether they are set.
    """
    configured = bool(EBAY_CLIENT_ID and EBAY_CLIENT_SECRET)
    user_token_set = bool(EBAY_USER_TOKEN)
    policies_set = all([EBAY_FULFILLMENT_POLICY, EBAY_PAYMENT_POLICY, EBAY_RETURN_POLICY])
    env = "sandbox" if EBAY_SANDBOX else "production"

    result = {
        "environment": env,
        "marketplace": EBAY_MARKETPLACE,
        "api_base": EBAY_API_BASE,
        "credentials_configured": configured,
        "user_token_configured": user_token_set,
        "business_policies_configured": policies_set,
        "publish_ready": configured and user_token_set and policies_set,
        "token_cached": bool(_token_cache.access_token),
        "token_expires_at": _token_cache.expires_at if _token_cache.access_token else None,
    }

    if user_token_set:
        result["auth_mode"] = "user_token (Sell APIs supported)"
    else:
        result["auth_mode"] = "app_token (read-only, Sell APIs blocked)"

    if not configured:
        result["setup_instructions"] = (
            "Add EBAY_CLIENT_ID, EBAY_CLIENT_SECRET to .env — "
            "get them from: https://developer.ebay.com/my/keys"
        )
    if not user_token_set:
        result["user_token_instructions"] = (
            "Generate a User Token at: https://developer.ebay.com/DevZone/account/tokens/ "
            "and set EBAY_USER_TOKEN in .env"
        )
    if not policies_set:
        result["policy_instructions"] = (
            "Create business policies at: https://www.sandbox.ebay.com/seller-center/programs/selling-tools/business-policies "
            "and set EBAY_FULFILLMENT_POLICY_ID, EBAY_PAYMENT_POLICY_ID, EBAY_RETURN_POLICY_ID in .env"
        )

    return result


@router.post("/token/refresh", summary="Force-refresh the eBay OAuth application token")
async def force_refresh_token():
    """Clears the cached token and fetches a fresh one. Useful for debugging auth issues."""
    _token_cache.access_token = ""
    _token_cache.expires_at   = 0.0
    token = await _get_app_token()
    return {
        "status": "refreshed",
        "expires_at": _token_cache.expires_at,
        "environment": "sandbox" if EBAY_SANDBOX else "production",
    }


# ── Inventory Item (SKU) ──────────────────────────────────────────────────────

@router.put("/inventory/{sku}", summary="Create or update an eBay inventory item")
async def upsert_inventory_item(sku: str, product: BazaarProduct):
    """
    Maps Bazaar product fields to the eBay Inventory Item schema and
    PUTs it to /sell/inventory/v1/inventory_item/{sku}.

    A 204 means the item was created/updated successfully.
    """
    token = await _get_app_token()

    # Build eBay inventory_item payload
    payload: dict = {
        "availability": {
            "shipToLocationAvailability": {
                "quantity": product.quantity
            }
        },
        "condition": product.condition,
        "product": {
            "title": product.name[:80],
            "description": product.description or product.name,
        },
    }

    if product.image_url:
        payload["product"]["imageUrls"] = [product.image_url]

    result = await _ebay_put(
        f"/sell/inventory/v1/inventory_item/{sku}",
        token,
        payload,
    )
    return {"sku": sku, "inventory": result}


@router.get("/inventory/{sku}", summary="Get a specific eBay inventory item")
async def get_inventory_item(sku: str):
    token = await _get_app_token()
    return await _ebay_get(f"/sell/inventory/v1/inventory_item/{sku}", token)


@router.get("/inventory", summary="List all eBay inventory items")
async def list_inventory_items(
    limit:  int = Query(25, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    token = await _get_app_token()
    return await _ebay_get(
        "/sell/inventory/v1/inventory_item",
        token,
        {"limit": limit, "offset": offset},
    )


@router.delete("/inventory/{sku}", summary="Delete an eBay inventory item")
async def delete_inventory_item(sku: str):
    token = await _get_app_token()
    return await _ebay_delete(f"/sell/inventory/v1/inventory_item/{sku}", token)


# ── Offers ────────────────────────────────────────────────────────────────────

@router.post("/offers", summary="Create a listing offer for an inventory item")
async def create_offer(product: BazaarProduct):
    """
    Creates an offer (listing draft) linking the inventory SKU to a
    category, price, and marketplace policies.

    Requires fulfillment_policy_id, payment_policy_id, return_policy_id,
    and merchant_location_key to be set for a publishable offer.
    """
    token = await _get_app_token()

    payload: dict = {
        "sku": product.sku,
        "marketplaceId": EBAY_MARKETPLACE,
        "format": "FIXED_PRICE",
        "availableQuantity": product.quantity,
        "categoryId": product.category_id,
        "pricingSummary": {
            "price": {
                "value": str(product.price),
                "currency": product.currency,
            }
        },
        "listingDescription": product.description or product.name,
        "merchantLocationKey": product.merchant_location_key or "default",
    }

    if product.fulfillment_policy_id:
        payload["fulfillmentPolicyId"] = product.fulfillment_policy_id
    if product.payment_policy_id:
        payload["paymentPolicyId"] = product.payment_policy_id
    if product.return_policy_id:
        payload["returnPolicyId"] = product.return_policy_id

    result = await _ebay_post("/sell/inventory/v1/offer", token, payload)
    return result


@router.get("/offers", summary="Get all offers for a SKU")
async def get_offers(
    sku:    str = Query(..., description="Inventory SKU to look up offers for"),
    limit:  int = Query(25, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    token = await _get_app_token()
    return await _ebay_get(
        "/sell/inventory/v1/offer",
        token,
        {"sku": sku, "limit": limit, "offset": offset},
    )


@router.get("/offers/{offer_id}", summary="Get a specific offer")
async def get_offer(offer_id: str):
    token = await _get_app_token()
    return await _ebay_get(f"/sell/inventory/v1/offer/{offer_id}", token)


@router.delete("/offers/{offer_id}", summary="Delete (withdraw) an offer")
async def delete_offer(offer_id: str):
    token = await _get_app_token()
    return await _ebay_delete(f"/sell/inventory/v1/offer/{offer_id}", token)


# ── Publish (Go Live) ─────────────────────────────────────────────────────────

@router.post("/offers/{offer_id}/publish", summary="Publish an offer — makes listing live on eBay")
async def publish_offer(offer_id: str):
    """
    Publishes the offer to eBay. Returns a listingId that can be saved
    against the Bazaar product record.

    The listing becomes immediately visible on eBay (Sandbox or Production).
    """
    token = await _get_app_token()
    result = await _ebay_post(
        f"/sell/inventory/v1/offer/{offer_id}/publish",
        token,
        {},
    )
    listing_id = result.get("listingId") if isinstance(result, dict) else None
    env = "sandbox" if EBAY_SANDBOX else "production"
    response = {
        "status": "published",
        "offer_id": offer_id,
        "listing_id": listing_id,
        "environment": env,
        "ebay_result": result,
    }
    if listing_id:
        base = "https://www.sandbox.ebay.com" if EBAY_SANDBOX else "https://www.ebay.com"
        response["listing_url"] = f"{base}/itm/{listing_id}"
    return response


@router.post("/offers/{offer_id}/withdraw", summary="Withdraw (end) a live eBay listing")
async def withdraw_offer(offer_id: str):
    """Ends the eBay listing without deleting the offer record."""
    token = await _get_app_token()
    return await _ebay_post(
        f"/sell/inventory/v1/offer/{offer_id}/withdraw",
        token,
        {},
    )


async def _upload_to_ebay_eps(token: str, image_url: str) -> str:
    """Upload an image to eBay Picture Services (EPS), return ebayimg.com URL."""
    import re as _re

    # Download the image
    async with httpx.AsyncClient(timeout=15) as client:
        img_resp = await client.get(image_url, follow_redirects=True)
        img_resp.raise_for_status()
    img_bytes = img_resp.content
    content_type = img_resp.headers.get("content-type", "image/jpeg")

    # Upload to eBay EPS via Trading API
    eps_url = f"{EBAY_API_BASE}/ws/api.dll"
    boundary = "EBAY_EPS_BOUNDARY"
    xml_part = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<UploadSiteHostedPicturesRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
        '<PictureName>bazaar_product</PictureName>'
        '</UploadSiteHostedPicturesRequest>'
    )
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="XML Payload"\r\n'
        f"Content-Type: text/xml\r\n\r\n"
        f"{xml_part}\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="image"; filename="product.jpg"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("utf-8") + img_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")

    headers = {
        "X-EBAY-API-SITEID": "0",
        "X-EBAY-API-COMPATIBILITY-LEVEL": "967",
        "X-EBAY-API-CALL-NAME": "UploadSiteHostedPictures",
        "X-EBAY-API-IAF-TOKEN": token,
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(eps_url, headers=headers, content=body)

    full_url_match = _re.search(r"<FullURL>(.*?)</FullURL>", resp.text)
    if full_url_match:
        ebay_url = full_url_match.group(1)
        logger.info("EPS upload success: %s", ebay_url)
        return ebay_url

    logger.warning("EPS upload returned no URL: %s", resp.text[:300])
    raise ValueError("EPS upload did not return a FullURL")


def _build_item_specifics_xml(product) -> str:
    """Build <ItemSpecifics> XML from product data, with smart auto-extraction.
    
    eBay requires different item specifics per category. This function:
    1. Tries to extract values from the product name
    2. Falls back to safe defaults for commonly required fields
    3. Skips fields not relevant to the product type
    """
    import re as _re
    specifics = dict(product.item_specifics or {})
    name = product.name
    name_lower = name.lower()

    # ── Detect product type to choose relevant specifics ──
    is_tech = any(w in name_lower for w in [
        "phone", "laptop", "tablet", "ipad", "iphone", "macbook", "samsung",
        "headphone", "earbuds", "airpods", "speaker", "camera", "drone",
        "tv", "monitor", "console", "playstation", "xbox", "nintendo",
        "watch", "smartwatch", "computer", "pc", "gpu", "ssd", "hdd",
    ])
    is_clothing = any(w in name_lower for w in [
        "shirt", "pants", "jacket", "hoodie", "dress", "shoe", "sneaker",
        "boot", "hat", "cap", "jersey", "jeans", "shorts", "skirt",
        "sock", "glove", "scarf", "coat", "suit", "tie", "blazer",
    ])
    is_home = any(w in name_lower for w in [
        "bottle", "mug", "cup", "plate", "bowl", "towel", "pillow",
        "blanket", "lamp", "chair", "table", "shelf", "mat", "rug",
        "container", "organizer", "basket", "vase", "candle",
    ])

    # ── Brand (always required) ──
    if "Brand" not in specifics:
        brands = ["Samsung", "Apple", "iPhone", "Google", "Sony", "LG",
                  "OnePlus", "Dell", "HP", "Lenovo", "ASUS", "Acer",
                  "Microsoft", "Nintendo", "Canon", "Nikon", "Bose",
                  "Nike", "Adidas", "Puma", "Under Armour", "Jordan",
                  "Supreme", "Gucci", "Louis Vuitton", "Yeezy",
                  "Dyson", "KitchenAid", "Weber", "Traeger", "Hydro Flask",
                  "Nalgene", "Stanley", "Yeti", "CamelBak", "Contigo",
                  "Trek", "DJI", "GoPro", "Fitbit", "Garmin", "Rolex",
                  "Patek Philippe", "Omega", "Casio", "Seiko"]
        for b in brands:
            if b.lower() in name_lower:
                specifics["Brand"] = b
                break
        if "Brand" not in specifics:
            specifics["Brand"] = "Unbranded"

    # ── Color (almost always required) ──
    if "Color" not in specifics:
        colors = ["Black", "White", "Silver", "Gold", "Blue", "Red",
                  "Green", "Gray", "Grey", "Titanium", "Purple", "Pink",
                  "Yellow", "Orange", "Brown", "Navy", "Beige", "Clear",
                  "Transparent", "Rose Gold", "Space Gray"]
        for c in colors:
            if c.lower() in name_lower:
                specifics["Color"] = c
                break
        if "Color" not in specifics:
            specifics["Color"] = "Multicolor"

    # ── Type / Product Type (often required) ──
    if "Type" not in specifics:
        if is_home:
            specifics["Type"] = "Household"
        elif is_clothing:
            specifics["Type"] = "Regular"
        elif is_tech:
            specifics["Type"] = "Consumer Electronics"
        else:
            specifics["Type"] = "Not Specified"

    # ── Material (commonly required — always include) ──
    if "Material" not in specifics:
        materials = {
            "stainless": "Stainless Steel", "steel": "Stainless Steel",
            "plastic": "Plastic", "glass": "Glass", "wood": "Wood",
            "leather": "Leather", "cotton": "Cotton", "polyester": "Polyester",
            "silicone": "Silicone", "aluminum": "Aluminum", "ceramic": "Ceramic",
            "bamboo": "Bamboo", "rubber": "Rubber", "nylon": "Nylon",
            "titanium": "Titanium", "carbon fiber": "Carbon Fiber",
        }
        for keyword, mat_value in materials.items():
            if keyword in name_lower:
                specifics["Material"] = mat_value
                break
        if "Material" not in specifics:
            specifics["Material"] = "Does Not Apply"

    # ── Storage Capacity (ALWAYS include — eBay categories are unpredictable) ──
    if "Storage Capacity" not in specifics:
        cap = _re.search(r"(\d+)\s*(GB|TB)", name, _re.IGNORECASE)
        if cap:
            specifics["Storage Capacity"] = f"{cap.group(1)} {cap.group(2).upper()}"
        else:
            specifics["Storage Capacity"] = "Does Not Apply"

    # ── Model (always safe to include) ──
    if "Model" not in specifics:
        specifics["Model"] = name[:65]

    # ── Size (always include — many categories need it) ──
    if "Size" not in specifics:
        size_match = _re.search(r"\b(XXS|XS|S|M|L|XL|XXL|XXXL|\d{1,2})\b", name, _re.IGNORECASE)
        if size_match:
            specifics["Size"] = size_match.group(1).upper()
        else:
            specifics["Size"] = "One Size"

    # ── Capacity (bottles, containers) ──
    cap_match = _re.search(r"(\d+(?:\.\d+)?)\s*(ml|ML|mL|oz|OZ|L|l|gal)\b", name, _re.IGNORECASE)
    if cap_match and "Capacity" not in specifics:
        specifics["Capacity"] = f"{cap_match.group(1)} {cap_match.group(2)}"

    # ── MPN (always safe) ──
    if "MPN" not in specifics:
        specifics["MPN"] = "Does Not Apply"

    # ── UPC (always safe) ──
    if "UPC" not in specifics:
        specifics["UPC"] = "Does Not Apply"

    if not specifics:
        return ""

    lines = ["<ItemSpecifics>"]
    for k, v in specifics.items():
        # XML-escape the values
        safe_v = str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        safe_k = str(k).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        lines.append(f"  <NameValueList><Name>{safe_k}</Name><Value>{safe_v}</Value></NameValueList>")
    lines.append("</ItemSpecifics>")
    return "\n    ".join(lines)


# ── Full One-Shot Listing Flow ─────────────────────────────────────────────────

# Fallback category for items whose category is rejected
_FALLBACK_CATEGORY = "9355"  # Cell Phones & Smartphones — leaf, accepts NEW

def _build_listing_xml(product, pic_url: str, condition_id: str, category_id: str,
                       extra_specifics: dict = None, include_condition: bool = True) -> str:
    """Build the AddFixedPriceItem XML. Extracted so we can retry with modifications."""
    def _xml_esc(s: str) -> str:
        return (str(s).replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;").replace("'", "&apos;"))

    safe_title = _xml_esc(product.name[:80])
    safe_sku   = _xml_esc(product.sku)
    desc = product.description or product.name
    safe_desc  = desc.replace("]]>", "]] >")
    description_html = f"<div>{safe_desc}</div>"

    condition_xml = f"<ConditionID>{condition_id}</ConditionID>" if include_condition else ""

    # Merge extra specifics into product (for auto-retry)
    if extra_specifics:
        if product.item_specifics is None:
            product.item_specifics = {}
        for k, v in extra_specifics.items():
            if k not in product.item_specifics:
                product.item_specifics[k] = v

    return f"""<?xml version="1.0" encoding="utf-8"?>
<AddFixedPriceItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <ErrorLanguage>en_US</ErrorLanguage>
  <WarningLevel>High</WarningLevel>
  <Item>
    <Title>{safe_title}</Title>
    <Description><![CDATA[{description_html}]]></Description>
    <PrimaryCategory>
      <CategoryID>{category_id}</CategoryID>
    </PrimaryCategory>
    <StartPrice currencyID="{product.currency}">{product.price}</StartPrice>
    {condition_xml}
    <Country>US</Country>
    <Currency>{product.currency}</Currency>
    <DispatchTimeMax>1</DispatchTimeMax>
    <ListingDuration>GTC</ListingDuration>
    <ListingType>FixedPriceItem</ListingType>
    <PictureDetails>
      <PictureURL>{pic_url}</PictureURL>
    </PictureDetails>
    <PostalCode>60601</PostalCode>
    <Quantity>{product.quantity}</Quantity>
    <ShippingDetails>
      <ShippingType>Flat</ShippingType>
      <ShippingServiceOptions>
        <ShippingServicePriority>1</ShippingServicePriority>
        <ShippingService>USPSPriority</ShippingService>
        <FreeShipping>true</FreeShipping>
        <ShippingServiceCost currencyID="{product.currency}">0.00</ShippingServiceCost>
        <ShippingServiceAdditionalCost currencyID="{product.currency}">0.00</ShippingServiceAdditionalCost>
      </ShippingServiceOptions>
    </ShippingDetails>
    <ReturnPolicy>
      <ReturnsAcceptedOption>ReturnsAccepted</ReturnsAcceptedOption>
      <ReturnsWithinOption>Days_30</ReturnsWithinOption>
      <RefundOption>MoneyBack</RefundOption>
      <ShippingCostPaidByOption>Buyer</ShippingCostPaidByOption>
    </ReturnPolicy>
    <Site>US</Site>
    <SKU>{safe_sku}</SKU>
    {_build_item_specifics_xml(product)}
  </Item>
</AddFixedPriceItemRequest>"""


async def _call_ebay_trading(xml_body: str, token: str) -> tuple:
    """Send XML to eBay Trading API. Returns (ack, item_id, error_msg, body_text)."""
    import re
    trading_url = f"{EBAY_API_BASE}/ws/api.dll"
    headers = {
        "X-EBAY-API-SITEID": "0",
        "X-EBAY-API-COMPATIBILITY-LEVEL": "967",
        "X-EBAY-API-CALL-NAME": "AddFixedPriceItem",
        "X-EBAY-API-IAF-TOKEN": token,
        "Content-Type": "text/xml",
    }
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(trading_url, headers=headers, content=xml_body)

    body_text = resp.text
    ack_match = re.search(r"<Ack>(\w+)</Ack>", body_text)
    ack = ack_match.group(1) if ack_match else "Unknown"
    item_id_match = re.search(r"<ItemID>(\d+)</ItemID>", body_text)
    item_id = item_id_match.group(1) if item_id_match else None

    fatal_errors = re.findall(
        r"<Errors>.*?<SeverityCode>Error</SeverityCode>.*?<LongMessage>(.*?)</LongMessage>.*?</Errors>",
        body_text, re.DOTALL
    )
    all_messages = re.findall(r"<LongMessage>(.*?)</LongMessage>", body_text)
    error_msg = " | ".join(fatal_errors) if fatal_errors else (" | ".join(all_messages) if all_messages else None)

    return ack, item_id, error_msg, body_text


def _parse_ebay_errors(error_msg: str) -> dict:
    """Parse eBay error message to determine what auto-fix to apply.

    Returns dict with keys:
      - remove_condition: True if condition not applicable
      - missing_specifics: list of field names eBay is requesting
      - replace_category: new category ID if eBay suggests a replacement
      - not_leaf: True if category is not a leaf
    """
    import re
    fixes = {
        "remove_condition": False,
        "missing_specifics": [],
        "replace_category": None,
        "not_leaf": False,
    }
    if not error_msg:
        return fixes

    # "Condition is not applicable for this category"
    if "Condition is not applicable" in error_msg or "condition value submitted has been dropped" in error_msg.lower():
        fixes["remove_condition"] = True

    # "The item specific X is missing. Add X to this listing"
    missing = re.findall(r"item specific ([A-Za-z\s]+?) is missing", error_msg)
    fixes["missing_specifics"] = [m.strip() for m in missing]

    # "Old category XXXXX replaced with new category YYYYY"
    cat_replace = re.search(r"replaced with new category (\d+)", error_msg)
    if cat_replace:
        fixes["replace_category"] = cat_replace.group(1)

    # "not a leaf category"
    if "not a leaf category" in error_msg:
        fixes["not_leaf"] = True

    return fixes


# Default values for commonly missing item specifics
_SPECIFIC_DEFAULTS = {
    "Department": "Unisex Adult",
    "Size": "One Size",
    "Size Type": "Regular",
    "Style": "Modern",
    "Type": "Not Specified",
    "Band Material": "Silicone",
    "Band Color": "Black",
    "Ring Size": "7",
    "Connectivity": "Bluetooth",
    "Compatible Operating System": "Android",
    "Exterior Color": "Multicolor",
    "Frame Color": "Black",
    "Lens Color": "Black",
    "Case Size": "44 mm",
    "Item Height": "10 in",
    "Item Length": "5 in",
    "Item Width": "4 in",
    "Main Stone": "Does Not Apply",
    "Metal": "Does Not Apply",
    "Base Metal": "Does Not Apply",
    "Body Jewelry Type": "Navel Ring",
    "Gauge": "14g",
    "Skin Type": "All Skin Types",
    "Activity": "General",
    "Voice Assistant": "Not Applicable",
}


@router.post("/list", summary="⚡ List a Bazaar product on eBay in one call (auto-healing)")
async def list_product_on_ebay(product: BazaarProduct):
    """
    The all-in-one endpoint that powers the 'List on eBay ↗' button in Bazaar.

    Uses the eBay Trading API (AddFixedPriceItem) with **auto-healing**:
    - If condition is not applicable → retries without ConditionID
    - If item specifics are missing → auto-fills with smart defaults and retries
    - If category is not a leaf → falls back to a known-good category
    - If category was replaced → uses the new category eBay suggests
    Up to 3 automatic retries before giving up.
    """
    token = await _get_app_token()

    condition_map = {
        "NEW": "1000", "LIKE_NEW": "3000", "VERY_GOOD": "4000",
        "GOOD": "5000", "ACCEPTABLE": "6000", "FOR_PARTS_OR_NOT_WORKING": "7000",
    }
    condition_id = condition_map.get(product.condition, "1000")

    # ── Prepare image ──
    pic_url = None
    if product.image_url:
        try:
            async with httpx.AsyncClient(timeout=10) as img_client:
                img_check = await img_client.head(product.image_url, follow_redirects=True)
                if img_check.status_code >= 400:
                    product.image_url = None
        except Exception:
            product.image_url = None

    if product.image_url:
        try:
            pic_url = await _upload_to_ebay_eps(token, product.image_url)
        except Exception as exc:
            print(f"[EBAY] EPS upload failed: {exc}")
    if not pic_url:
        pic_url = "https://i.ebayimg.com/images/g/~bEAAOSwBLlU3GR0/s-l400.jpg"

    # ── Auto-healing retry loop (up to 3 retries) ──
    include_condition = True
    category_id = product.category_id
    extra_specifics: dict = {}
    max_retries = 3

    for attempt in range(max_retries + 1):
        xml_body = _build_listing_xml(
            product, pic_url, condition_id, category_id,
            extra_specifics=extra_specifics,
            include_condition=include_condition,
        )

        try:
            ack, item_id, error_msg, body_text = await _call_ebay_trading(xml_body, token)
        except Exception as exc:
            if attempt < max_retries:
                print(f"[EBAY] Attempt {attempt+1} network error, retrying: {exc}")
                await asyncio.sleep(2)
                continue
            raise HTTPException(status_code=503, detail=f"eBay Trading API request failed: {exc}")

        print(f"[EBAY] Attempt {attempt+1} | {product.name} | Ack: {ack} | ItemID: {item_id}")
        if error_msg:
            print(f"[EBAY] Errors: {error_msg[:300]}")

        # ── SUCCESS ──
        if item_id and ack in ("Success", "Warning"):
            env = "sandbox" if EBAY_SANDBOX else "production"
            base = "https://www.sandbox.ebay.com" if EBAY_SANDBOX else "https://www.ebay.com"
            healed = " (auto-healed)" if attempt > 0 else ""
            print(f"[EBAY] ✅ LISTED{healed}: {product.name} → {base}/itm/{item_id}")
            listing_record = {
                "status": "success",
                "sku": product.sku,
                "item_id": item_id,
                "title": product.name,
                "price": float(product.price),
                "image": product.image_url,
                "listing_url": f"{base}/itm/{item_id}",
                "environment": env,
                "ack": ack,
                "attempts": attempt + 1,
            }
            existing_ids = {i["item_id"] for i in _listed_items}
            if item_id not in existing_ids:
                _listed_items.append(listing_record)
            return listing_record

        # ── FAILURE — attempt to auto-heal ──
        if attempt < max_retries:
            fixes = _parse_ebay_errors(error_msg or "")
            applied_any = False

            if fixes["remove_condition"]:
                include_condition = False
                print(f"[EBAY] 🔧 Auto-fix: removing ConditionID (not applicable for category {category_id})")
                applied_any = True

            if fixes["missing_specifics"]:
                for spec_name in fixes["missing_specifics"]:
                    default_val = _SPECIFIC_DEFAULTS.get(spec_name, "Does Not Apply")
                    extra_specifics[spec_name] = default_val
                    print(f"[EBAY] 🔧 Auto-fix: adding {spec_name} = '{default_val}'")
                applied_any = True

            if fixes["replace_category"]:
                category_id = fixes["replace_category"]
                print(f"[EBAY] 🔧 Auto-fix: using replacement category {category_id}")
                applied_any = True

            if fixes["not_leaf"]:
                category_id = _FALLBACK_CATEGORY
                print(f"[EBAY] 🔧 Auto-fix: category not leaf → fallback to {_FALLBACK_CATEGORY}")
                applied_any = True

            if applied_any:
                print(f"[EBAY] ↻ Retrying with fixes (attempt {attempt+2}/{max_retries+1})...")
                await asyncio.sleep(1)
                continue
            else:
                # No fixes detected — don't retry blindly
                break

    # ── All retries exhausted ──
    print(f"[EBAY] ❌ FAILED after {max_retries+1} attempts: {product.name}")
    raise HTTPException(
        status_code=400,
        detail=error_msg or body_text[:800],
    )


# ── Active Listings (query eBay for all live items) ────────────────────────────

@router.get("/active", summary="📋 List all active eBay listings for this account")
async def list_active_ebay_listings():
    """Return all successfully listed items from this session."""
    base = "https://www.sandbox.ebay.com" if EBAY_SANDBOX else "https://www.ebay.com"
    return {
        "total": len(_listed_items),
        "environment": "sandbox" if EBAY_SANDBOX else "production",
        "listings": _listed_items,
    }

# ── Policies (account-level, needed for offers) ───────────────────────────────

@router.get("/policies/fulfillment", summary="List eBay fulfillment policies on this account")
async def list_fulfillment_policies():
    token = await _get_app_token()
    return await _ebay_get(
        "/sell/account/v1/fulfillment_policy",
        token,
        {"marketplace_id": EBAY_MARKETPLACE},
    )


@router.get("/policies/payment", summary="List eBay payment policies on this account")
async def list_payment_policies():
    token = await _get_app_token()
    return await _ebay_get(
        "/sell/account/v1/payment_policy",
        token,
        {"marketplace_id": EBAY_MARKETPLACE},
    )


@router.get("/policies/return", summary="List eBay return policies on this account")
async def list_return_policies():
    token = await _get_app_token()
    return await _ebay_get(
        "/sell/account/v1/return_policy",
        token,
        {"marketplace_id": EBAY_MARKETPLACE},
    )


# ── Merchant Location ─────────────────────────────────────────────────────────

@router.get("/locations", summary="List merchant locations (needed for offers)")
async def list_merchant_locations():
    token = await _get_app_token()
    return await _ebay_get("/sell/inventory/v1/location", token)


@router.post("/locations", summary="Create a merchant location")
async def create_merchant_location(
    merchant_location_key: str = Body(...),
    name: str                  = Body(...),
    address_line1: str         = Body(...),
    city: str                  = Body(...),
    state_or_province: str     = Body(...),
    country: str               = Body(default="US"),
    postal_code: str           = Body(...),
):
    """Creates a merchant location — required before creating offers."""
    token = await _get_app_token()
    payload = {
        "name": name,
        "merchantLocationStatus": "ENABLED",
        "location": {
            "address": {
                "addressLine1": address_line1,
                "city": city,
                "stateOrProvince": state_or_province,
                "country": country,
                "postalCode": postal_code,
            }
        },
    }
    return await _ebay_post(
        f"/sell/inventory/v1/location/{merchant_location_key}",
        token,
        payload,
    )


# ── Taxonomy / Category Lookup ────────────────────────────────────────────────

@router.get("/categories/suggest", summary="Suggest eBay category IDs from a product title")
async def suggest_categories(
    q: str = Query(..., description="Product title or keywords"),
):
    """
    Uses eBay's Taxonomy API to suggest the best category for a product.
    Use the returned categoryId when creating offers.
    """
    token = await _get_app_token()
    return await _ebay_get(
        "/commerce/taxonomy/v1/category_tree/0/get_category_suggestions",
        token,
        {"q": q},
    )


@router.get("/categories/{category_id}/aspects", summary="Get required item aspects for a category")
async def get_category_aspects(category_id: str):
    """Returns required/recommended item specifics for a given eBay category."""
    token = await _get_app_token()
    return await _ebay_get(
        f"/commerce/taxonomy/v1/category_tree/0/get_item_aspects_for_category",
        token,
        {"category_id": category_id},
    )


# ── Bazaar Category → eBay Category Mapping ───────────────────────────────────
# Pre-seeded common mappings so Bazaar categories can auto-select an eBay ID.
# Users can always override by passing a custom category_id.

BAZAAR_TO_EBAY_CATEGORY: dict[str, dict] = {
    "techvault": {"id": "9355",   "name": "Cell Phones & Smartphones"},
    "threads":   {"id": "11450",  "name": "Men's Clothing"},
    "garage":    {"id": "6001",   "name": "Cars & Trucks"},
    "homebase":  {"id": "11700",  "name": "Home & Garden"},
    "icebox":    {"id": "281",    "name": "Jewelry & Watches"},
    "vault":     {"id": "64482",  "name": "Sports Trading Cards"},
    "arena":     {"id": "888",    "name": "Sporting Goods"},
    "glow":      {"id": "26395",  "name": "Health & Beauty"},
    "pantry":    {"id": "174422", "name": "Specialty Food Market"},
    "pantry_african":   {"id": "174422", "name": "Specialty Food Market — African"},
    "pantry_caribbean": {"id": "174422", "name": "Specialty Food Market — Caribbean"},
    "pantry_spices":    {"id": "11696",  "name": "Condiments, Sauces & Spices"},
    "pantry_grains":    {"id": "181004", "name": "Grains, Rice & Cereal"},
    "pantry_halal":     {"id": "174422", "name": "Specialty Food Market — Halal"},
    "lightning": {"id": "9355",   "name": "Cell Phones & Smartphones"},  # fallback
}


@router.get("/categories/bazaar-map", summary="Bazaar section → eBay category ID mapping")
async def bazaar_category_map():
    """
    Returns the pre-seeded mapping of Bazaar section slugs to eBay category IDs.
    The frontend uses this to auto-populate the categoryId when listing a product.
    """
    return {"mapping": BAZAAR_TO_EBAY_CATEGORY}
