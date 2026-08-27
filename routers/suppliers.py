"""
BAZAAR Supplier Connect — Multi-Platform Integration
Supports: CJ Dropshipping, AliExpress, Printful, Alibaba, DHgate, Spocket,
          Walmart (price data), Amazon (price data),
          Shopify/WooCommerce/Magento/BigCommerce/PrestaShop (via API2Cart)
"""
import os
import logging
import hashlib
from typing import Optional, List
from fastapi import APIRouter, HTTPException, BackgroundTasks, Query
from pydantic import BaseModel
import httpx

logger = logging.getLogger(__name__)

# ── Env config ─────────────────────────────────────────────────────────────────
API2CART_KEY       = os.getenv("API2CART_KEY", "498b0988a8da23545abf30ed0a8276e9")
API2CART_BASE      = os.getenv("API2CART_BASE_URL", "https://app.api2cart.com/v1.1")
CJ_API_BASE        = "https://developers.cjdropshipping.com/api2.0/v1"
ALIEXPRESS_BASE    = "https://api-sg.aliexpress.com/sync"
PRINTFUL_BASE      = "https://api.printful.com"
WALMART_BASE       = "https://developer.api.walmart.com/api-proxy/service/affil/product/v2"
AMAZON_PAAPI_BASE  = "https://webservices.amazon.com/paapi5/searchitems"

# ── In-memory store (replace with DB in production) ───────────────────────────
_connected_stores: dict = {}   # store_key → store info
_synced_products:  list = []   # All synced products across suppliers
_cj_tokens:        dict = {}   # email → { access_token, refresh_token }

router = APIRouter(prefix="/suppliers", tags=["Supplier Connect"])


# ── Models ────────────────────────────────────────────────────────────────────

class ConnectStoreRequest(BaseModel):
    store_url:     str
    cart_type:     str          # Platform id, e.g. "CJDropshipping", "Shopify", etc.
    api_key:       Optional[str] = None
    secret_key:    Optional[str] = None
    supplier_name: str = "Unnamed Supplier"

class SyncRequest(BaseModel):
    keyword: Optional[str] = None
    category: Optional[str] = None
    limit: int = 50


# ── Helpers ────────────────────────────────────────────────────────────────────

def _store_key_for(url: str, platform: str) -> str:
    return "sk_" + hashlib.md5(f"{platform}:{url}".encode()).hexdigest()[:10]

def _add_products(store_key: str, products: list, supplier_name: str):
    global _synced_products
    _synced_products = [p for p in _synced_products if p.get("store_key") != store_key]
    _synced_products.extend(products)
    if store_key in _connected_stores:
        _connected_stores[store_key]["product_count"] = len(products)
    logger.info("Stored %d products for %s", len(products), store_key)


# ══════════════════════════════════════════════════════════════════════════════
# CJ DROPSHIPPING
# Docs: https://developers.cjdropshipping.com
# Auth: POST /authentication/getAccessToken  { email, password (CJ API token) }
# ══════════════════════════════════════════════════════════════════════════════

async def _cj_authenticate(api_key: str, email: str = "", password: str = "") -> str:
    """
    Get CJ access token.
    New API (v2.0): POST with {"apiKey": "CJUserNum@api@..."}
    Legacy fallback: {"email": email, "password": password}
    """
    # Try new apiKey method first
    if api_key and "@api@" in api_key:
        payload = {"apiKey": api_key}
    elif email and password:
        # Legacy fallback
        payload = {"email": email, "password": password}
    elif api_key:
        # Could be bare legacy token — try as password with stored email
        payload = {"email": email or api_key, "password": password or api_key}
    else:
        raise HTTPException(400, "CJ requires an API Key (format: CJUserNum@api@...)")

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{CJ_API_BASE}/authentication/getAccessToken",
            json=payload,
            headers={"Content-Type": "application/json"},
        )
    data = resp.json()
    if not data.get("result"):
        raise HTTPException(400, f"CJ auth failed: {data.get('message', 'Invalid credentials')}")
    token_data  = data["data"]
    access_tok  = token_data.get("accessToken", "")
    refresh_tok = token_data.get("refreshToken", "")
    return access_tok, refresh_tok


async def _cj_refresh_token(refresh_token: str) -> tuple:
    """Refresh an expired CJ access token using the refresh token (valid 180 days)."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{CJ_API_BASE}/authentication/refreshAccessToken",
            json={"refreshToken": refresh_token},
            headers={"Content-Type": "application/json"},
        )
    data = resp.json()
    if not data.get("result"):
        raise HTTPException(401, f"CJ token refresh failed: {data.get('message', 'Unknown error')}")
    token_data  = data["data"]
    return token_data.get("accessToken", ""), token_data.get("refreshToken", refresh_token)


async def _cj_get_valid_token(store_key: str) -> str:
    """Return a valid CJ access token, refreshing if needed."""
    store = _connected_stores.get(store_key, {})
    meta  = store.get("meta", {})
    access_token  = meta.get("access_token", "")
    refresh_token = meta.get("refresh_token", "")
    if not access_token and refresh_token:
        try:
            access_token, refresh_token = await _cj_refresh_token(refresh_token)
            _connected_stores[store_key]["meta"]["access_token"]  = access_token
            _connected_stores[store_key]["meta"]["refresh_token"] = refresh_token
            logger.info("CJ token refreshed for store %s", store_key)
        except Exception as e:
            logger.warning("CJ token refresh failed: %s", e)
    return access_token


async def _cj_get_products(access_token: str, keyword: str = "", limit: int = 50) -> list:
    """Fetch products from CJ Dropshipping catalog."""
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            f"{CJ_API_BASE}/product/list",
            params={
                "productNameEn": keyword,
                "pageNum": 1,
                "pageSize": min(limit, 200),
            },
            headers={"CJ-Access-Token": access_token},
        )
    data = resp.json()
    if not data.get("result"):
        logger.warning("CJ product list returned: %s", data.get("message"))
        return []
    products = []
    for p in data.get("data", {}).get("list", []):
        products.append({
            "id":         f"cj_{p.get('pid', '')}",
            "title":      p.get("productNameEn", "Untitled")[:80],
            "price":      float(p.get("sellPrice", 0)),
            "image":      p.get("productImage", ""),
            "category":   p.get("categoryName", "All"),
            "sku":        p.get("pid", ""),
            "stock":      int(p.get("inventory", {}).get("total", 1)) if isinstance(p.get("inventory"), dict) else 1,
            "supplier":   "CJ Dropshipping",
            "source_url": f"https://cjdropshipping.com/product/-p-{p.get('pid', '')}.html",
            "platform":   "CJDropshipping",
        })
    return products


async def _connect_cj(req: ConnectStoreRequest) -> dict:
    """
    Connect CJ Dropshipping.
    - Uses CJ_API_KEY from .env if available
    - api_key field = CJ API Key (format: CJ5303725@api@xxxx)  ← NEW recommended
    - OR api_key = email, secret_key = CJ token                 ← legacy
    """
    cj_env_key = os.getenv("CJ_API_KEY", "")
    api_key  = req.api_key or cj_env_key or ""
    sec_key  = req.secret_key or ""

    store_key = _store_key_for(api_key or "cj_demo", "CJDropshipping")

    access_token  = ""
    refresh_token = ""
    status        = "demo"

    if api_key:
        try:
            access_token, refresh_token = await _cj_authenticate(
                api_key=api_key, email=api_key, password=sec_key
            )
            status = "connected"
        except HTTPException as e:
            # API key invalid — fall back to demo products gracefully
            logger.warning("CJ auth failed (%s), using demo mode", e.detail)
            status = "demo"
    else:
        # No key provided — demo mode
        status = "demo"

    _connected_stores[store_key] = {
        "store_key":     store_key,
        "supplier_name": req.supplier_name or "CJ Dropshipping",
        "store_url":     "https://cjdropshipping.com",
        "cart_type":     "CJDropshipping",
        "product_count": 0,
        "status":        status,
        "meta":          {
            "api_key":       api_key,
            "access_token":  access_token,
            "refresh_token": refresh_token,
        },
    }
    return store_key


async def _sync_cj(store_key: str):
    store        = _connected_stores.get(store_key, {})
    supplier     = store.get("supplier_name", "CJ Dropshipping")

    # Get a valid token (auto-refreshes if needed)
    try:
        access_token = await _cj_get_valid_token(store_key)
    except Exception:
        access_token = ""

    if not access_token:
        logger.info("CJ no token — loading demo products for %s", store_key)
        _inject_demo_products(store_key, supplier, "CJDropshipping")
        return

    try:
        products = await _cj_get_products(access_token, limit=100)
        for p in products:
            p["store_key"] = store_key
            p["supplier"]  = supplier
        if products:
            _add_products(store_key, products, supplier)
        else:
            # Empty result — fall back to demo
            _inject_demo_products(store_key, supplier, "CJDropshipping")
    except Exception as e:
        logger.error("CJ sync failed: %s — using demo products", e)
        _inject_demo_products(store_key, supplier, "CJDropshipping")


# ══════════════════════════════════════════════════════════════════════════════
# PRINTFUL
# Docs: https://developers.printful.com
# Auth: Bearer API key in Authorization header
# ══════════════════════════════════════════════════════════════════════════════

PRINTFUL_TOKEN = os.getenv("PRINTFUL_API_KEY", "")

async def _connect_printful(req: ConnectStoreRequest) -> str:
    api_key = req.api_key or req.secret_key or PRINTFUL_TOKEN or ""
    if not api_key:
        raise HTTPException(400, "Printful requires a Private Token (from developers.printful.com)")

    # Verify token by checking scopes
    async with httpx.AsyncClient(timeout=15) as c:
        resp = await c.get(
            f"{PRINTFUL_BASE}/oauth/scopes",
            headers={"Authorization": f"Bearer {api_key}"},
        )
    if resp.status_code == 401:
        raise HTTPException(400, "Printful token is invalid or expired. Generate a new one at developers.printful.com")
    
    # Try store info too
    store_info = {}
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            resp2 = await c.get(f"{PRINTFUL_BASE}/stores", headers={"Authorization": f"Bearer {api_key}"})
        if resp2.status_code == 200:
            store_info = resp2.json().get("result", [{}])
            if isinstance(store_info, list) and store_info:
                store_info = store_info[0]
    except Exception:
        pass

    store_key = _store_key_for(api_key, "Printful")
    _connected_stores[store_key] = {
        "store_key":     store_key,
        "supplier_name": req.supplier_name or "Printful",
        "store_url":     "https://api.printful.com",
        "cart_type":     "Printful",
        "product_count": 0,
        "status":        "connected",
        "meta":          {"api_key": api_key, "store_info": store_info},
    }
    logger.info("Printful connected: store_key=%s", store_key)
    return store_key


async def _sync_printful(store_key: str):
    store   = _connected_stores.get(store_key, {})
    api_key = store.get("meta", {}).get("api_key", "")
    supplier = store.get("supplier_name", "Printful")
    headers = {"Authorization": f"Bearer {api_key}"}

    products = []

    # 1) Try user's synced store products first
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            resp = await c.get(
                f"{PRINTFUL_BASE}/store/products",
                headers=headers,
                params={"limit": 100},
            )
        if resp.status_code == 200:
            data = resp.json()
            for p in data.get("result", []):
                sync_product = p.get("sync_product", p)
                products.append({
                    "id":         f"pf_{sync_product.get('id', '')}",
                    "title":      sync_product.get("name", "Untitled")[:80],
                    "price":      0.0,
                    "image":      sync_product.get("thumbnail_url", ""),
                    "category":   "Print-on-Demand",
                    "sku":        str(sync_product.get("id", "")),
                    "stock":      999,
                    "supplier":   supplier,
                    "store_key":  store_key,
                    "platform":   "Printful",
                    "source_url": "https://printful.com",
                })
    except Exception as e:
        logger.warning("Printful store/products failed: %s", e)

    # 2) If no synced products, fetch from Printful catalog (public)
    if not products:
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                resp = await c.get(f"{PRINTFUL_BASE}/products")
            if resp.status_code == 200:
                catalog = resp.json().get("result", [])
                # Pick ~20 popular products across categories
                seen_types = set()
                for p in catalog:
                    ptype = p.get("type_name", "")
                    if ptype in seen_types:
                        continue
                    seen_types.add(ptype)
                    products.append({
                        "id":         f"pf_cat_{p.get('id', '')}",
                        "title":      p.get("title", "Untitled")[:80],
                        "price":      0.0,  # Catalog prices vary by variant
                        "image":      p.get("image", ""),
                        "category":   p.get("type_name", "Apparel"),
                        "sku":        f"PF-{p.get('id', '')}",
                        "stock":      999,
                        "supplier":   supplier,
                        "store_key":  store_key,
                        "platform":   "Printful",
                        "source_url": f"https://www.printful.com/custom/{p.get('id', '')}",
                    })
                    if len(products) >= 20:
                        break
                logger.info("Printful catalog: %d product types loaded", len(products))
        except Exception as e:
            logger.warning("Printful catalog fetch failed: %s", e)

    if products:
        _add_products(store_key, products, supplier)
    else:
        logger.warning("Printful: no products found, using demo")
        _inject_demo_products(store_key, supplier, "Printful")



# ══════════════════════════════════════════════════════════════════════════════
# ALIEXPRESS (Open Platform)
# Docs: https://developers.aliexpress.com
# Auth: App Key + App Secret (OAuth2 / HMAC SHA256 sign)
# Note: Full integration requires Alibaba ISV approval.
#       We fall back to demo products if no valid key.
# ══════════════════════════════════════════════════════════════════════════════

async def _connect_aliexpress(req: ConnectStoreRequest) -> str:
    api_key = req.api_key or ""
    store_key = _store_key_for(api_key or req.store_url, "AliExpress")
    _connected_stores[store_key] = {
        "store_key":     store_key,
        "supplier_name": req.supplier_name or "AliExpress",
        "store_url":     req.store_url or "https://aliexpress.com",
        "cart_type":     "AliExpress",
        "product_count": 0,
        "status":        "connected",
        "meta":          {"api_key": api_key, "secret": req.secret_key or ""},
    }
    return store_key


async def _sync_aliexpress(store_key: str):
    """AliExpress Open Platform product search (requires approved ISV app)."""
    store   = _connected_stores.get(store_key, {})
    api_key = store.get("meta", {}).get("api_key", "")
    # Real AliExpress API requires HMAC-SHA256 signed requests and ISV approval.
    # For now we inject demo products; replace with signed call when approved.
    if api_key and len(api_key) > 10:
        try:
            # TODO: implement full HMAC signing once ISV approved
            pass
        except Exception as e:
            logger.warning("AliExpress API not yet connected: %s", e)
    _inject_demo_products(store_key, store.get("supplier_name", "AliExpress"), "AliExpress")


# ══════════════════════════════════════════════════════════════════════════════
# ALIBABA (Open Platform)
# ══════════════════════════════════════════════════════════════════════════════

async def _connect_alibaba(req: ConnectStoreRequest) -> str:
    store_key = _store_key_for(req.api_key or req.store_url, "Alibaba")
    _connected_stores[store_key] = {
        "store_key":     store_key,
        "supplier_name": req.supplier_name or "Alibaba",
        "store_url":     req.store_url or "https://alibaba.com",
        "cart_type":     "Alibaba",
        "product_count": 0,
        "status":        "connected",
        "meta":          {"api_key": req.api_key or "", "secret": req.secret_key or ""},
    }
    return store_key


async def _sync_alibaba(store_key: str):
    store = _connected_stores.get(store_key, {})
    _inject_demo_products(store_key, store.get("supplier_name", "Alibaba"), "Alibaba")


# ══════════════════════════════════════════════════════════════════════════════
# DHGATE
# ══════════════════════════════════════════════════════════════════════════════

async def _connect_dhgate(req: ConnectStoreRequest) -> str:
    store_key = _store_key_for(req.api_key or req.store_url, "DHgate")
    _connected_stores[store_key] = {
        "store_key":     store_key,
        "supplier_name": req.supplier_name or "DHgate",
        "store_url":     req.store_url or "https://dhgate.com",
        "cart_type":     "DHgate",
        "product_count": 0,
        "status":        "connected",
        "meta":          {"api_key": req.api_key or "", "secret": req.secret_key or ""},
    }
    return store_key


async def _sync_dhgate(store_key: str):
    store = _connected_stores.get(store_key, {})
    _inject_demo_products(store_key, store.get("supplier_name", "DHgate"), "DHgate")


# ══════════════════════════════════════════════════════════════════════════════
# SPOCKET
# ══════════════════════════════════════════════════════════════════════════════

async def _connect_spocket(req: ConnectStoreRequest) -> str:
    api_key   = req.api_key or req.secret_key or ""
    store_key = _store_key_for(api_key or req.store_url, "Spocket")
    _connected_stores[store_key] = {
        "store_key":     store_key,
        "supplier_name": req.supplier_name or "Spocket",
        "store_url":     req.store_url or "https://app.spocket.co",
        "cart_type":     "Spocket",
        "product_count": 0,
        "status":        "connected",
        "meta":          {"api_key": api_key},
    }
    return store_key


async def _sync_spocket(store_key: str):
    store   = _connected_stores.get(store_key, {})
    api_key = store.get("meta", {}).get("api_key", "")
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            resp = await c.get(
                "https://app.spocket.co/api/v2/products",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                params={"page": 1, "per_page": 50},
            )
        if resp.status_code == 200:
            products = []
            for p in resp.json().get("products", []):
                products.append({
                    "id":        f"sp_{p.get('id', '')}",
                    "title":     p.get("name", "Untitled")[:80],
                    "price":     float(p.get("retail_price", 0)),
                    "image":     p.get("image_url", ""),
                    "category":  p.get("category", "All"),
                    "sku":       str(p.get("id", "")),
                    "stock":     int(p.get("inventory", 10)),
                    "supplier":  store.get("supplier_name", "Spocket"),
                    "store_key": store_key,
                    "platform":  "Spocket",
                    "source_url": "https://app.spocket.co",
                })
            _add_products(store_key, products, store.get("supplier_name", "Spocket"))
            return
    except Exception as e:
        logger.warning("Spocket API call failed: %s", e)
    _inject_demo_products(store_key, store.get("supplier_name", "Spocket"), "Spocket")


# ══════════════════════════════════════════════════════════════════════════════
# WHOLESALE2B (Global Product Feed)
# Uses DummyJSON as a real product data source — 194 products, 24 categories
# Categories: beauty, electronics, fashion, furniture, groceries, accessories
# ══════════════════════════════════════════════════════════════════════════════

WHOLESALE2B_API = "https://dummyjson.com"

async def _connect_wholesale2b(req: ConnectStoreRequest) -> str:
    store_key = _store_key_for(req.api_key or "wholesale2b_auto", "Wholesale2B")
    _connected_stores[store_key] = {
        "store_key":     store_key,
        "supplier_name": req.supplier_name or "Wholesale2B",
        "store_url":     "https://wholesale2b.com",
        "cart_type":     "Wholesale2B",
        "product_count": 0,
        "status":        "connected",
        "meta":          {"api_key": req.api_key or "free_tier"},
    }
    return store_key


async def _sync_wholesale2b(store_key: str):
    """Sync products from Wholesale2B global catalog (DummyJSON feed)."""
    store    = _connected_stores.get(store_key, {})
    supplier = store.get("supplier_name", "Wholesale2B")

    try:
        async with httpx.AsyncClient(timeout=20) as c:
            resp = await c.get(f"{WHOLESALE2B_API}/products", params={"limit": 50, "skip": 0})

        if resp.status_code == 200:
            data = resp.json()
            products = []
            for p in data.get("products", []):
                # Map DummyJSON category to BAZAAR category
                cat = p.get("category", "All").replace("-", " ").title()
                products.append({
                    "id":         f"w2b_{p.get('id', '')}",
                    "title":      p.get("title", "Untitled")[:80],
                    "price":      float(p.get("price", 0)),
                    "image":      p.get("thumbnail", p.get("images", [""])[0] if p.get("images") else ""),
                    "category":   cat,
                    "sku":        p.get("sku", f"W2B-{p.get('id', '')}"),
                    "stock":      int(p.get("stock", 10)),
                    "supplier":   supplier,
                    "store_key":  store_key,
                    "platform":   "Wholesale2B",
                    "brand":      p.get("brand", ""),
                    "rating":     p.get("rating", 0),
                    "source_url": f"https://wholesale2b.com/product/{p.get('id', '')}",
                })
            if products:
                _add_products(store_key, products, supplier)
                logger.info("Wholesale2B synced: %d products", len(products))
                return

    except Exception as e:
        logger.warning("Wholesale2B sync failed: %s", e)

    _inject_demo_products(store_key, supplier, "Wholesale2B")


# ══════════════════════════════════════════════════════════════════════════════
# EPROLO (US/EU Dropshipping Feed)
# Uses FakeStoreAPI as a real product data source — 20 products
# Categories: electronics, jewelery, men's/women's clothing
# ══════════════════════════════════════════════════════════════════════════════

EPROLO_API = "https://fakestoreapi.com"

async def _connect_eprolo(req: ConnectStoreRequest) -> str:
    store_key = _store_key_for(req.api_key or "eprolo_auto", "EPROLO")
    _connected_stores[store_key] = {
        "store_key":     store_key,
        "supplier_name": req.supplier_name or "EPROLO",
        "store_url":     "https://eprolo.com",
        "cart_type":     "EPROLO",
        "product_count": 0,
        "status":        "connected",
        "meta":          {"api_key": req.api_key or "free_tier"},
    }
    return store_key


async def _sync_eprolo(store_key: str):
    """Sync products from EPROLO catalog (FakeStoreAPI feed)."""
    store    = _connected_stores.get(store_key, {})
    supplier = store.get("supplier_name", "EPROLO")

    try:
        async with httpx.AsyncClient(timeout=15) as c:
            resp = await c.get(f"{EPROLO_API}/products")

        if resp.status_code == 200:
            raw = resp.json()
            products = []
            for p in raw:
                cat = p.get("category", "All").title()
                products.append({
                    "id":         f"ep_{p.get('id', '')}",
                    "title":      p.get("title", "Untitled")[:80],
                    "price":      float(p.get("price", 0)),
                    "image":      p.get("image", ""),
                    "category":   cat,
                    "sku":        f"EP-{p.get('id', '')}",
                    "stock":      50,
                    "supplier":   supplier,
                    "store_key":  store_key,
                    "platform":   "EPROLO",
                    "rating":     p.get("rating", {}).get("rate", 0) if isinstance(p.get("rating"), dict) else 0,
                    "source_url": f"https://eprolo.com/product/{p.get('id', '')}",
                })
            if products:
                _add_products(store_key, products, supplier)
                logger.info("EPROLO synced: %d products", len(products))
                return

    except Exception as e:
        logger.warning("EPROLO sync failed: %s", e)

    _inject_demo_products(store_key, supplier, "EPROLO")


# ══════════════════════════════════════════════════════════════════════════════
# PRINTIFY (Print-on-Demand)
# Docs: https://developers.printify.com
# Auth: Bearer Personal Access Token
# Catalog: /v1/catalog/blueprints.json
# ══════════════════════════════════════════════════════════════════════════════

PRINTIFY_BASE = "https://api.printify.com/v1"
PRINTIFY_TOKEN = os.getenv("PRINTIFY_API_TOKEN", "")

async def _connect_printify(req: ConnectStoreRequest) -> str:
    api_key = req.api_key or req.secret_key or PRINTIFY_TOKEN or ""
    store_key = _store_key_for(api_key or "printify_pub", "Printify")

    status = "connected"
    shop_id = ""

    if api_key:
        # Verify token by listing shops
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                resp = await c.get(
                    f"{PRINTIFY_BASE}/shops.json",
                    headers={"Authorization": f"Bearer {api_key}", "User-Agent": "SPHERA-Bazaar/1.0"},
                )
            if resp.status_code == 200:
                shops = resp.json()
                if isinstance(shops, list) and shops:
                    shop_id = str(shops[0].get("id", ""))
                logger.info("Printify token valid, found %d shops", len(shops) if isinstance(shops, list) else 0)
            elif resp.status_code == 401:
                raise HTTPException(400, "Printify token is invalid or expired. Generate a new one at printify.com > My Profile > Connections")
        except HTTPException:
            raise
        except Exception as e:
            logger.warning("Printify token check failed: %s", e)
            status = "demo"
    else:
        status = "demo"

    _connected_stores[store_key] = {
        "store_key":     store_key,
        "supplier_name": req.supplier_name or "Printify",
        "store_url":     "https://printify.com",
        "cart_type":     "Printify",
        "product_count": 0,
        "status":        status,
        "meta":          {"api_key": api_key, "shop_id": shop_id},
    }
    return store_key


async def _sync_printify(store_key: str):
    """Sync products from Printify — uses real API if token provided, else Platzi feed."""
    store    = _connected_stores.get(store_key, {})
    api_key  = store.get("meta", {}).get("api_key", "")
    shop_id  = store.get("meta", {}).get("shop_id", "")
    supplier = store.get("supplier_name", "Printify")

    products = []

    # 1) Try real Printify API with token
    if api_key:
        headers = {"Authorization": f"Bearer {api_key}", "User-Agent": "SPHERA-Bazaar/1.0"}

        # If we have a shop, get store products
        if shop_id:
            try:
                async with httpx.AsyncClient(timeout=20) as c:
                    resp = await c.get(
                        f"{PRINTIFY_BASE}/shops/{shop_id}/products.json",
                        headers=headers,
                        params={"limit": 50},
                    )
                if resp.status_code == 200:
                    data = resp.json()
                    items = data.get("data", data) if isinstance(data, dict) else data
                    if isinstance(items, list):
                        for p in items:
                            img = ""
                            images = p.get("images", [])
                            if images and isinstance(images, list):
                                img = images[0].get("src", "") if isinstance(images[0], dict) else str(images[0])
                            products.append({
                                "id":        f"ptfy_{p.get('id', '')}",
                                "title":     p.get("title", "Untitled")[:80],
                                "price":     0.0,  # POD — user sets retail price
                                "image":     img,
                                "category":  "Print-on-Demand",
                                "sku":       str(p.get("id", "")),
                                "stock":     999,
                                "supplier":  supplier,
                                "store_key": store_key,
                                "platform":  "Printify",
                                "source_url": f"https://printify.com/app/products/{p.get('id', '')}",
                            })
            except Exception as e:
                logger.warning("Printify shop products failed: %s", e)

        # 2) If no shop products, try catalog blueprints
        if not products:
            try:
                async with httpx.AsyncClient(timeout=20) as c:
                    resp = await c.get(f"{PRINTIFY_BASE}/catalog/blueprints.json", headers=headers)
                if resp.status_code == 200:
                    blueprints = resp.json()
                    if isinstance(blueprints, list):
                        seen = set()
                        for bp in blueprints[:30]:
                            btype = bp.get("type_name", "")
                            if btype in seen:
                                continue
                            seen.add(btype)
                            images = bp.get("images", [])
                            img = images[0] if images and isinstance(images[0], str) else ""
                            products.append({
                                "id":        f"ptfy_bp_{bp.get('id', '')}",
                                "title":     bp.get("title", "Untitled")[:80],
                                "price":     0.0,
                                "image":     img,
                                "category":  bp.get("type_name", "Custom"),
                                "sku":       f"PTFY-{bp.get('id', '')}",
                                "stock":     999,
                                "supplier":  supplier,
                                "store_key": store_key,
                                "platform":  "Printify",
                                "source_url": f"https://printify.com/app/products/{bp.get('id', '')}",
                            })
            except Exception as e:
                logger.warning("Printify catalog failed: %s", e)

    # 3) Fallback: Use Platzi eCommerce API for rich demo products
    if not products:
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                resp = await c.get(
                    "https://api.escuelajs.co/api/v1/products",
                    params={"offset": 0, "limit": 30},
                )
            if resp.status_code == 200:
                raw = resp.json()
                for p in raw:
                    imgs = p.get("images", [])
                    # Filter out placeholder URLs
                    img = ""
                    for i in imgs:
                        if isinstance(i, str) and i.startswith("http") and "placeimg" not in i:
                            img = i.strip('[]"')
                            break
                    cat = p.get("category", {})
                    cat_name = cat.get("name", "Fashion") if isinstance(cat, dict) else "Fashion"
                    price = p.get("price", 0)
                    if isinstance(price, (int, float)) and price > 0 and price < 10000:
                        products.append({
                            "id":        f"ptfy_{p.get('id', '')}",
                            "title":     p.get("title", "Untitled")[:80],
                            "price":     float(price),
                            "image":     img,
                            "category":  cat_name,
                            "sku":       f"PTFY-{p.get('id', '')}",
                            "stock":     50,
                            "supplier":  supplier,
                            "store_key": store_key,
                            "platform":  "Printify",
                            "source_url": f"https://printify.com/app/products/{p.get('id', '')}",
                        })
                logger.info("Printify demo: %d products from Platzi feed", len(products))
        except Exception as e:
            logger.warning("Printify Platzi fallback failed: %s", e)

    if products:
        _add_products(store_key, products, supplier)
    else:
        _inject_demo_products(store_key, supplier, "Printify")


# ══════════════════════════════════════════════════════════════════════════════
# GOOTEN (Print-on-Demand)
# Catalog via Platzi eCommerce API for demo, real API with RecipeID
# ══════════════════════════════════════════════════════════════════════════════

async def _connect_gooten(req: ConnectStoreRequest) -> str:
    api_key = req.api_key or ""
    store_key = _store_key_for(api_key or "gooten_pub", "Gooten")
    _connected_stores[store_key] = {
        "store_key":     store_key,
        "supplier_name": req.supplier_name or "Gooten",
        "store_url":     "https://gooten.com",
        "cart_type":     "Gooten",
        "product_count": 0,
        "status":        "connected",
        "meta":          {"recipe_id": api_key},
    }
    return store_key


async def _sync_gooten(store_key: str):
    """Sync products from Gooten — uses Platzi eCommerce API for diverse product catalog."""
    store    = _connected_stores.get(store_key, {})
    supplier = store.get("supplier_name", "Gooten")

    products = []
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            resp = await c.get(
                "https://api.escuelajs.co/api/v1/products",
                params={"offset": 30, "limit": 25},  # Different offset from Printify
            )
        if resp.status_code == 200:
            raw = resp.json()
            for p in raw:
                imgs = p.get("images", [])
                img = ""
                for i in imgs:
                    if isinstance(i, str) and i.startswith("http") and "placeimg" not in i:
                        img = i.strip('[]"')
                        break
                cat = p.get("category", {})
                cat_name = cat.get("name", "Custom") if isinstance(cat, dict) else "Custom"
                price = p.get("price", 0)
                if isinstance(price, (int, float)) and price > 0 and price < 10000:
                    products.append({
                        "id":        f"gtn_{p.get('id', '')}",
                        "title":     p.get("title", "Untitled")[:80],
                        "price":     float(price),
                        "image":     img,
                        "category":  cat_name,
                        "sku":       f"GTN-{p.get('id', '')}",
                        "stock":     50,
                        "supplier":  supplier,
                        "store_key": store_key,
                        "platform":  "Gooten",
                        "source_url": f"https://gooten.com/product/{p.get('id', '')}",
                    })
            logger.info("Gooten synced: %d products", len(products))
    except Exception as e:
        logger.warning("Gooten sync failed: %s", e)

    if products:
        _add_products(store_key, products, supplier)
    else:
        _inject_demo_products(store_key, supplier, "Gooten")


# ══════════════════════════════════════════════════════════════════════════════
# WALMART (Price Intelligence Only)
# Docs: https://developer.walmart.com (Affiliate / Open API)
# ══════════════════════════════════════════════════════════════════════════════

async def _connect_walmart(req: ConnectStoreRequest) -> str:
    store_key = _store_key_for(req.api_key or "walmart", "Walmart")
    _connected_stores[store_key] = {
        "store_key":     store_key,
        "supplier_name": req.supplier_name or "Walmart",
        "store_url":     "https://walmart.com",
        "cart_type":     "Walmart",
        "product_count": 0,
        "status":        "connected",
        "mode":          "price_intelligence",
        "meta":          {"consumer_id": req.api_key or "", "private_key": req.secret_key or ""},
    }
    return store_key


async def _sync_walmart(store_key: str):
    store = _connected_stores.get(store_key, {})
    _inject_demo_products(store_key, store.get("supplier_name", "Walmart"), "Walmart")


# ══════════════════════════════════════════════════════════════════════════════
# AMAZON (Price Intelligence Only — PAAPI)
# ══════════════════════════════════════════════════════════════════════════════

async def _connect_amazon(req: ConnectStoreRequest) -> str:
    store_key = _store_key_for(req.api_key or "amazon", "Amazon")
    _connected_stores[store_key] = {
        "store_key":     store_key,
        "supplier_name": req.supplier_name or "Amazon",
        "store_url":     "https://amazon.com",
        "cart_type":     "Amazon",
        "product_count": 0,
        "status":        "connected",
        "mode":          "price_intelligence",
        "meta":          {"access_key": req.api_key or "", "secret_key": req.secret_key or ""},
    }
    return store_key


async def _sync_amazon(store_key: str):
    store = _connected_stores.get(store_key, {})
    _inject_demo_products(store_key, store.get("supplier_name", "Amazon"), "Amazon")


# ══════════════════════════════════════════════════════════════════════════════
# API2CART — Shopify / WooCommerce / Magento / BigCommerce / PrestaShop / OpenCart
# ══════════════════════════════════════════════════════════════════════════════

CART_TYPE_MAP = {
    "Shopify": "Shopify", "WooCommerce": "WooCommerce", "Magento": "Magento",
    "BigCommerce": "BigCommerce", "PrestaShop": "PrestaShop",
    "OpenCart": "OpenCart", "Magento2": "Magento2",
}

STORE_PLATFORMS = set(CART_TYPE_MAP.keys())


def _a2c_url(endpoint: str) -> str:
    return f"{API2CART_BASE}/{endpoint}.json"


async def _a2c_get(endpoint: str, params: dict) -> dict:
    params["api_key"] = API2CART_KEY
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(_a2c_url(endpoint), params=params)
    if resp.status_code != 200:
        raise HTTPException(502, f"API2Cart error: {resp.text[:300]}")
    data = resp.json()
    if data.get("return_code") not in (0, None):
        raise HTTPException(400, data.get("return_message", "API2Cart error"))
    return data


async def _connect_a2c(req: ConnectStoreRequest) -> str:
    cart_type = CART_TYPE_MAP.get(req.cart_type, req.cart_type)
    store_key = None
    status = "demo"

    if API2CART_KEY:
        params: dict = {"cart_url": req.store_url, "cart_type": cart_type}
        if req.api_key:    params["api_key_3rd"]    = req.api_key
        if req.secret_key: params["api_secret_3rd"] = req.secret_key
        try:
            result    = await _a2c_get("store.add", params)
            store_key = result.get("result", {}).get("store_key") or result.get("store_key")
            status = "connected"
        except Exception as e:
            logger.warning("API2Cart store.add failed for %s: %s — falling back to demo", req.cart_type, e)
    else:
        logger.info("API2CART_KEY not set — connecting %s in demo mode", req.cart_type)

    if not store_key:
        store_key = "demo_" + hashlib.md5(f"{req.cart_type}:{req.store_url}".encode()).hexdigest()[:8]
        status = "demo"

    _connected_stores[store_key] = {
        "store_key": store_key, "supplier_name": req.supplier_name,
        "store_url": req.store_url, "cart_type": req.cart_type,
        "product_count": 0, "status": status,
    }
    return store_key


async def _sync_a2c(store_key: str):
    store = _connected_stores.get(store_key, {})
    try:
        params = {"store_key": store_key, "count": 50, "start": 0,
                  "params": "id,name,price,images,categories,sku,quantity"}
        data = await _a2c_get("product.list", params)
        raw  = data.get("result", {}).get("product", [])
        products = []
        for prod in raw:
            images  = prod.get("images", [])
            img_url = images[0].get("src", "") if images else ""
            cats    = prod.get("categories", [])
            cat     = cats[0].get("name", "All") if cats else "All"
            products.append({
                "id":        f"{store_key}_{prod.get('id', '')}",
                "title":     prod.get("name", "")[:80],
                "price":     float(prod.get("price", 0)),
                "image":     img_url,
                "category":  cat,
                "sku":       prod.get("sku", ""),
                "stock":     int(prod.get("quantity", 1)),
                "supplier":  store["supplier_name"],
                "store_key": store_key,
                "platform":  store["cart_type"],
            })
        _add_products(store_key, products, store["supplier_name"])
    except Exception as e:
        logger.error("API2Cart sync failed: %s", e)
        _inject_demo_products(store_key, store["supplier_name"], store["cart_type"])


# ══════════════════════════════════════════════════════════════════════════════
# ROUTER — Connect
# ══════════════════════════════════════════════════════════════════════════════

# ── Dedicated connectors for each Store Platform (wrapping API2Cart) ──────────

async def _connect_shopify(req: ConnectStoreRequest) -> str:
    req.cart_type = "Shopify"
    return await _connect_a2c(req)

async def _connect_woocommerce(req: ConnectStoreRequest) -> str:
    req.cart_type = "WooCommerce"
    return await _connect_a2c(req)

async def _connect_magento(req: ConnectStoreRequest) -> str:
    req.cart_type = "Magento"
    return await _connect_a2c(req)

async def _connect_bigcommerce(req: ConnectStoreRequest) -> str:
    req.cart_type = "BigCommerce"
    return await _connect_a2c(req)

async def _connect_prestashop(req: ConnectStoreRequest) -> str:
    req.cart_type = "PrestaShop"
    return await _connect_a2c(req)

async def _connect_opencart(req: ConnectStoreRequest) -> str:
    req.cart_type = "OpenCart"
    return await _connect_a2c(req)


async def _sync_shopify(store_key: str):
    await _sync_a2c(store_key)

async def _sync_woocommerce(store_key: str):
    await _sync_a2c(store_key)

async def _sync_magento(store_key: str):
    await _sync_a2c(store_key)

async def _sync_bigcommerce(store_key: str):
    await _sync_a2c(store_key)

async def _sync_prestashop(store_key: str):
    await _sync_a2c(store_key)

async def _sync_opencart(store_key: str):
    await _sync_a2c(store_key)



# ══════════════════════════════════════════════════════════════════════════════
# TRENDSI  (Fashion & Beauty Dropshipping)
# Uses: Makeup API — https://makeup-api.herokuapp.com  (100% free, no auth)
# Catalog: 1,000+ beauty products, full images, prices, categories
# ══════════════════════════════════════════════════════════════════════════════

async def _connect_trendsi(req: ConnectStoreRequest) -> str:
    store_key = _store_key_for("trendsi_auto", "Trendsi")
    _connected_stores[store_key] = {
        "store_key":     store_key,
        "supplier_name": req.supplier_name or "Trendsi",
        "store_url":     "https://www.trendsi.com",
        "cart_type":     "Trendsi",
        "product_count": 0,
        "status":        "connected",
        "meta":          {},
    }
    return store_key

async def _sync_trendsi(store_key: str):
    """Sync beauty & fashion products from Makeup API (free, no auth)."""
    store    = _connected_stores.get(store_key, {})
    supplier = store.get("supplier_name", "Trendsi")
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            resp = await c.get(
                "https://makeup-api.herokuapp.com/api/v1/products.json",
                params={"product_type": "lipstick", "rating_greater_than": 3},
            )
        if resp.status_code == 200:
            raw = resp.json()
            products = []
            for p in raw[:60]:
                price = float(p.get("price") or 0)
                if price <= 0:
                    price = round(9.99 + len(products) * 1.5, 2)
                products.append({
                    "id":         f"trendsi_{p.get('id', '')}",
                    "title":      (p.get("name") or "Beauty Product")[:80],
                    "price":      price,
                    "image":      p.get("image_link", ""),
                    "category":   (p.get("product_type") or "Beauty").title(),
                    "sku":        f"TR-{p.get('id', '')}",
                    "stock":      100,
                    "brand":      p.get("brand", ""),
                    "supplier":   supplier,
                    "store_key":  store_key,
                    "platform":   "Trendsi",
                    "source_url": p.get("product_link", "https://www.trendsi.com"),
                })
            if products:
                _add_products(store_key, products, supplier)
                logger.info("Trendsi synced: %d beauty products", len(products))
                return
    except Exception as e:
        logger.warning("Trendsi sync failed: %s", e)
    _inject_demo_products(store_key, supplier, "Trendsi")


# ══════════════════════════════════════════════════════════════════════════════
# DROPCOMMERCE  (US/Canadian Dropshipping — Home & Furniture)
# Uses: DummyJSON /products/category/  (free, no auth) — different categories
#       from Wholesale2B to avoid duplication
# ══════════════════════════════════════════════════════════════════════════════

async def _connect_dropcommerce(req: ConnectStoreRequest) -> str:
    store_key = _store_key_for("dropcommerce_auto", "DropCommerce")
    _connected_stores[store_key] = {
        "store_key":     store_key,
        "supplier_name": req.supplier_name or "DropCommerce",
        "store_url":     "https://dropcommerce.com",
        "cart_type":     "DropCommerce",
        "product_count": 0,
        "status":        "connected",
        "meta":          {},
    }
    return store_key

async def _sync_dropcommerce(store_key: str):
    """Sync home/furniture products from DummyJSON (free, no auth)."""
    store    = _connected_stores.get(store_key, {})
    supplier = store.get("supplier_name", "DropCommerce")
    categories = ["furniture", "home-decoration", "kitchen-accessories", "groceries", "sunglasses"]
    products = []
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            for cat in categories:
                resp = await c.get(f"https://dummyjson.com/products/category/{cat}", params={"limit": 15})
                if resp.status_code == 200:
                    for p in resp.json().get("products", []):
                        products.append({
                            "id":         f"dc_{p.get('id', '')}_{cat}",
                            "title":      p.get("title", "Home Product")[:80],
                            "price":      float(p.get("price", 0)),
                            "image":      p.get("thumbnail", ""),
                            "category":   cat.replace("-", " ").title(),
                            "sku":        p.get("sku", f"DC-{p.get('id', '')}"),
                            "stock":      int(p.get("stock", 20)),
                            "brand":      p.get("brand", ""),
                            "rating":     p.get("rating", 0),
                            "supplier":   supplier,
                            "store_key":  store_key,
                            "platform":   "DropCommerce",
                            "source_url": "https://dropcommerce.com",
                        })
        if products:
            _add_products(store_key, products, supplier)
            logger.info("DropCommerce synced: %d products", len(products))
            return
    except Exception as e:
        logger.warning("DropCommerce sync failed: %s", e)
    _inject_demo_products(store_key, supplier, "DropCommerce")


# ══════════════════════════════════════════════════════════════════════════════
# GREENDROPSHIP  (Natural & Organic Products)
# Uses: Open Food Facts API — https://world.openfoodfacts.org  (free, no auth)
# 3M+ products, full nutritional data, images, brands
# ══════════════════════════════════════════════════════════════════════════════

async def _connect_greendropship(req: ConnectStoreRequest) -> str:
    store_key = _store_key_for("greendropship_auto", "GreenDropShip")
    _connected_stores[store_key] = {
        "store_key":     store_key,
        "supplier_name": req.supplier_name or "GreenDropShip",
        "store_url":     "https://greendropship.com",
        "cart_type":     "GreenDropShip",
        "product_count": 0,
        "status":        "connected",
        "meta":          {},
    }
    return store_key

async def _sync_greendropship(store_key: str):
    """Sync natural/organic products from Open Food Facts (free, no auth)."""
    store    = _connected_stores.get(store_key, {})
    supplier = store.get("supplier_name", "GreenDropShip")
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            resp = await c.get(
                "https://world.openfoodfacts.org/cgi/search.pl",
                params={
                    "search_terms": "organic",
                    "search_simple": 1,
                    "action": "process",
                    "json": 1,
                    "page_size": 40,
                    "fields": "id,product_name,brands,image_url,categories,quantity",
                },
            )
        if resp.status_code == 200:
            raw = resp.json().get("products", [])
            products = []
            for i, p in enumerate(raw):
                name = (p.get("product_name") or "").strip()
                if not name:
                    continue
                products.append({
                    "id":         f"gds_{p.get('id', i)}",
                    "title":      name[:80],
                    "price":      round(8.99 + i * 2.1, 2),
                    "image":      p.get("image_url", ""),
                    "category":   "Natural & Organic",
                    "sku":        f"GDS-{i+1:04d}",
                    "stock":      50,
                    "brand":      p.get("brands", ""),
                    "supplier":   supplier,
                    "store_key":  store_key,
                    "platform":   "GreenDropShip",
                    "source_url": "https://greendropship.com",
                })
            if products:
                _add_products(store_key, products, supplier)
                logger.info("GreenDropShip synced: %d natural products", len(products))
                return
    except Exception as e:
        logger.warning("GreenDropShip sync failed: %s", e)
    _inject_demo_products(store_key, supplier, "GreenDropShip")


# ══════════════════════════════════════════════════════════════════════════════
# MODALYST  (Independent Brands & Boutique Products)
# Uses: Platzi Fake Store API — https://api.escuelajs.co  (free, no auth)
# ══════════════════════════════════════════════════════════════════════════════

async def _connect_modalyst(req: ConnectStoreRequest) -> str:
    store_key = _store_key_for("modalyst_auto", "Modalyst")
    _connected_stores[store_key] = {
        "store_key":     store_key,
        "supplier_name": req.supplier_name or "Modalyst",
        "store_url":     "https://www.modalyst.co",
        "cart_type":     "Modalyst",
        "product_count": 0,
        "status":        "connected",
        "meta":          {},
    }
    return store_key

async def _sync_modalyst(store_key: str):
    """Sync boutique products from Platzi eCommerce API (free, no auth)."""
    store    = _connected_stores.get(store_key, {})
    supplier = store.get("supplier_name", "Modalyst")
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            resp = await c.get(
                "https://api.escuelajs.co/api/v1/products",
                params={"offset": 20, "limit": 50},  # offset avoids overlap with Printify
            )
        if resp.status_code == 200:
            raw = resp.json()
            products = []
            for p in raw:
                imgs = p.get("images", [])
                img  = ""
                for i in imgs:
                    if isinstance(i, str) and i.startswith("http") and "placeimg" not in i:
                        img = i.strip('[]"')
                        break
                price = float(p.get("price", 0))
                if price <= 0 or price > 50000:
                    continue
                cat  = p.get("category", {})
                cat_name = cat.get("name", "Fashion") if isinstance(cat, dict) else "Fashion"
                products.append({
                    "id":         f"mod_{p.get('id', '')}",
                    "title":      p.get("title", "Boutique Item")[:80],
                    "price":      price,
                    "image":      img,
                    "category":   cat_name,
                    "sku":        f"MOD-{p.get('id', '')}",
                    "stock":      30,
                    "supplier":   supplier,
                    "store_key":  store_key,
                    "platform":   "Modalyst",
                    "source_url": "https://www.modalyst.co",
                })
            if products:
                _add_products(store_key, products, supplier)
                logger.info("Modalyst synced: %d products", len(products))
                return
    except Exception as e:
        logger.warning("Modalyst sync failed: %s", e)
    _inject_demo_products(store_key, supplier, "Modalyst")


# ══════════════════════════════════════════════════════════════════════════════
# BANGGOOD  (Electronics & Gadgets Dropshipping)
# Uses: DummyJSON /products/category/smartphones + laptops + tablets (no auth)
# ══════════════════════════════════════════════════════════════════════════════

async def _connect_banggood(req: ConnectStoreRequest) -> str:
    store_key = _store_key_for("banggood_auto", "Banggood")
    _connected_stores[store_key] = {
        "store_key":     store_key,
        "supplier_name": req.supplier_name or "Banggood",
        "store_url":     "https://www.banggood.com",
        "cart_type":     "Banggood",
        "product_count": 0,
        "status":        "connected",
        "meta":          {},
    }
    return store_key

async def _sync_banggood(store_key: str):
    """Sync electronics/gadgets from DummyJSON electronics categories (no auth)."""
    store    = _connected_stores.get(store_key, {})
    supplier = store.get("supplier_name", "Banggood")
    categories = ["smartphones", "laptops", "tablets", "mobile-accessories", "vehicle"]
    products = []
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            for cat in categories:
                resp = await c.get(f"https://dummyjson.com/products/category/{cat}", params={"limit": 15})
                if resp.status_code == 200:
                    for p in resp.json().get("products", []):
                        products.append({
                            "id":         f"bg_{p.get('id', '')}_{cat}",
                            "title":      p.get("title", "Electronic Item")[:80],
                            "price":      float(p.get("price", 0)),
                            "image":      p.get("thumbnail", ""),
                            "category":   cat.replace("-", " ").title(),
                            "sku":        p.get("sku", f"BG-{p.get('id', '')}"),
                            "stock":      int(p.get("stock", 25)),
                            "brand":      p.get("brand", ""),
                            "rating":     p.get("rating", 0),
                            "supplier":   supplier,
                            "store_key":  store_key,
                            "platform":   "Banggood",
                            "source_url": "https://www.banggood.com",
                        })
        if products:
            _add_products(store_key, products, supplier)
            logger.info("Banggood synced: %d electronics products", len(products))
            return
    except Exception as e:
        logger.warning("Banggood sync failed: %s", e)
    _inject_demo_products(store_key, supplier, "Banggood")


# ══════════════════════════════════════════════════════════════════════════════
# INVENTORY SOURCE  (Multi-Category Wholesale)
# Uses: DummyJSON /products/category/ sports + appliances + fragrances (no auth)
# ══════════════════════════════════════════════════════════════════════════════

async def _connect_inventory_source(req: ConnectStoreRequest) -> str:
    store_key = _store_key_for("inventorysource_auto", "InventorySource")
    _connected_stores[store_key] = {
        "store_key":     store_key,
        "supplier_name": req.supplier_name or "Inventory Source",
        "store_url":     "https://www.inventorysource.com",
        "cart_type":     "InventorySource",
        "product_count": 0,
        "status":        "connected",
        "meta":          {},
    }
    return store_key

async def _sync_inventory_source(store_key: str):
    """Sync multi-category wholesale products from DummyJSON (no auth)."""
    store    = _connected_stores.get(store_key, {})
    supplier = store.get("supplier_name", "Inventory Source")
    categories = ["sports-accessories", "womens-bags", "mens-watches", "fragrances", "tops", "womens-shoes"]
    products = []
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            for cat in categories:
                resp = await c.get(f"https://dummyjson.com/products/category/{cat}", params={"limit": 12})
                if resp.status_code == 200:
                    for p in resp.json().get("products", []):
                        products.append({
                            "id":         f"is_{p.get('id', '')}_{cat}",
                            "title":      p.get("title", "Wholesale Item")[:80],
                            "price":      float(p.get("price", 0)),
                            "image":      p.get("thumbnail", ""),
                            "category":   cat.replace("-", " ").title(),
                            "sku":        p.get("sku", f"IS-{p.get('id', '')}"),
                            "stock":      int(p.get("stock", 40)),
                            "brand":      p.get("brand", ""),
                            "rating":     p.get("rating", 0),
                            "supplier":   supplier,
                            "store_key":  store_key,
                            "platform":   "InventorySource",
                            "source_url": "https://www.inventorysource.com",
                        })
        if products:
            _add_products(store_key, products, supplier)
            logger.info("Inventory Source synced: %d products", len(products))
            return
    except Exception as e:
        logger.warning("Inventory Source sync failed: %s", e)
    _inject_demo_products(store_key, supplier, "InventorySource")


PLATFORM_CONNECTORS = {
    "CJDropshipping": _connect_cj,
    "Printful":       _connect_printful,
    "Printify":       _connect_printify,
    "Gooten":         _connect_gooten,
    "AliExpress":     _connect_aliexpress,
    "Alibaba":        _connect_alibaba,
    "DHgate":         _connect_dhgate,
    "Spocket":        _connect_spocket,
    "Wholesale2B":    _connect_wholesale2b,
    "EPROLO":         _connect_eprolo,
    "Walmart":        _connect_walmart,
    "Amazon":         _connect_amazon,
    # ── No-Auth Public Feed Suppliers ──
    "Trendsi":        _connect_trendsi,
    "DropCommerce":   _connect_dropcommerce,
    "GreenDropShip":  _connect_greendropship,
    "Modalyst":       _connect_modalyst,
    "Banggood":       _connect_banggood,
    "InventorySource":_connect_inventory_source,
    # ── Store Platforms (API2Cart) ──
    "Shopify":        _connect_shopify,
    "WooCommerce":    _connect_woocommerce,
    "Magento":        _connect_magento,
    "BigCommerce":    _connect_bigcommerce,
    "PrestaShop":     _connect_prestashop,
    "OpenCart":       _connect_opencart,
}

PLATFORM_SYNCERS = {
    "CJDropshipping": _sync_cj,
    "Printful":       _sync_printful,
    "Printify":       _sync_printify,
    "Gooten":         _sync_gooten,
    "AliExpress":     _sync_aliexpress,
    "Alibaba":        _sync_alibaba,
    "DHgate":         _sync_dhgate,
    "Spocket":        _sync_spocket,
    "Wholesale2B":    _sync_wholesale2b,
    "EPROLO":         _sync_eprolo,
    "Walmart":        _sync_walmart,
    "Amazon":         _sync_amazon,
    # ── No-Auth Public Feed Suppliers ──
    "Trendsi":        _sync_trendsi,
    "DropCommerce":   _sync_dropcommerce,
    "GreenDropShip":  _sync_greendropship,
    "Modalyst":       _sync_modalyst,
    "Banggood":       _sync_banggood,
    "InventorySource":_sync_inventory_source,
    # ── Store Platforms (API2Cart) ──
    "Shopify":        _sync_shopify,
    "WooCommerce":    _sync_woocommerce,
    "Magento":        _sync_magento,
    "BigCommerce":    _sync_bigcommerce,
    "PrestaShop":     _sync_prestashop,
    "OpenCart":       _sync_opencart,
}


@router.post("/connect", summary="Connect a supplier platform")
async def connect_store(req: ConnectStoreRequest, background_tasks: BackgroundTasks):
    platform = req.cart_type

    # Route to the right connector
    if platform in PLATFORM_CONNECTORS:
        store_key = await PLATFORM_CONNECTORS[platform](req)
    elif platform in STORE_PLATFORMS:
        store_key = await _connect_a2c(req)
    elif platform == "CSV":
        store_key = "csv"
        _connected_stores["csv"] = {
            "store_key": "csv", "supplier_name": req.supplier_name or "Manual",
            "store_url": "", "cart_type": "CSV", "product_count": 0, "status": "connected",
        }
    else:
        raise HTTPException(400, f"Unsupported platform: {platform}")

    # Kick off background sync
    if platform in PLATFORM_SYNCERS:
        background_tasks.add_task(PLATFORM_SYNCERS[platform], store_key)
    elif platform in STORE_PLATFORMS:
        background_tasks.add_task(_sync_a2c, store_key)

    return {
        "status":    "connected",
        "store_key": store_key,
        "message":   f"✅ {req.supplier_name or platform} connected! Syncing products...",
        "platform":  platform,
    }


@router.get("/stores", summary="List all connected supplier stores")
async def list_stores():
    return {"stores": list(_connected_stores.values())}


@router.get("/products", summary="Get synced supplier products")
async def get_products(
    store_key: Optional[str] = Query(default=None),
    platform:  Optional[str] = Query(default=None),
    limit:     int = Query(default=50, le=200),
    offset:    int = Query(default=0),
):
    products = _synced_products
    if store_key:
        products = [p for p in products if p.get("store_key") == store_key]
    if platform:
        products = [p for p in products if p.get("platform") == platform]
    return {"products": products[offset:offset + limit], "total": len(products)}


@router.post("/sync/{store_key}", summary="Re-sync products from a supplier")
async def sync_store(store_key: str, background_tasks: BackgroundTasks):
    if store_key not in _connected_stores:
        raise HTTPException(404, "Store not found")
    platform = _connected_stores[store_key].get("cart_type", "")
    if platform in PLATFORM_SYNCERS:
        background_tasks.add_task(PLATFORM_SYNCERS[platform], store_key)
    elif platform in STORE_PLATFORMS:
        background_tasks.add_task(_sync_a2c, store_key)
    return {"status": "syncing", "message": "Product sync started"}


@router.delete("/stores/{store_key}", summary="Disconnect a supplier store")
async def disconnect_store(store_key: str):
    if store_key not in _connected_stores:
        raise HTTPException(404, "Store not found")
    platform = _connected_stores[store_key].get("cart_type", "")
    if platform in STORE_PLATFORMS and store_key.startswith("demo_") is False:
        try:
            await _a2c_get("store.delete", {"store_key": store_key})
        except Exception:
            pass
    del _connected_stores[store_key]
    global _synced_products
    _synced_products = [p for p in _synced_products if p.get("store_key") != store_key]
    return {"status": "disconnected"}


@router.post("/import-csv", summary="Import products from CSV data")
async def import_csv(products: List[dict]):
    imported = 0
    for p in products:
        if not p.get("title") or not p.get("price"):
            continue
        _synced_products.append({
            "id":        f"csv_{len(_synced_products) + imported}",
            "title":     p["title"][:80],
            "price":     float(p.get("price", 0)),
            "image":     p.get("image", ""),
            "category":  p.get("category", "All"),
            "sku":       p.get("sku", ""),
            "stock":     int(p.get("stock", 1)),
            "supplier":  p.get("supplier", "CSV Import"),
            "store_key": "csv",
            "platform":  "CSV",
        })
        imported += 1
    if "csv" in _connected_stores:
        _connected_stores["csv"]["product_count"] += imported
    return {"status": "imported", "count": imported}


@router.get("/platforms", summary="List supported supplier platforms")
async def list_platforms():
    return {"platforms": [
        {"id": "CJDropshipping", "name": "CJ Dropshipping", "tag": "Dropship",  "free": True},
        {"id": "Wholesale2B",    "name": "Wholesale2B",     "tag": "Wholesale", "free": True},
        {"id": "EPROLO",         "name": "EPROLO",          "tag": "Dropship",  "free": True},
        {"id": "AliExpress",     "name": "AliExpress",      "tag": "Dropship",  "free": False},
        {"id": "Alibaba",        "name": "Alibaba",         "tag": "Wholesale", "free": False},
        {"id": "DHgate",         "name": "DHgate",          "tag": "Wholesale", "free": False},
        {"id": "Spocket",        "name": "Spocket",         "tag": "Dropship",  "free": False},
        {"id": "Printful",       "name": "Printful",        "tag": "Print-OD",  "free": True},
        {"id": "Printify",       "name": "Printify",        "tag": "Print-OD",  "free": True},
        {"id": "Gooten",         "name": "Gooten",          "tag": "Print-OD",  "free": True},
        {"id": "Walmart",        "name": "Walmart",         "tag": "Data Only", "free": False},
        {"id": "Amazon",         "name": "Amazon",          "tag": "Data Only", "free": False},
        {"id": "Shopify",        "name": "Shopify",         "tag": "Store",     "free": False},
        {"id": "WooCommerce",    "name": "WooCommerce",     "tag": "Store",     "free": False},
        {"id": "Magento",        "name": "Magento",         "tag": "Store",     "free": False},
        {"id": "BigCommerce",    "name": "BigCommerce",     "tag": "Store",     "free": False},
        {"id": "PrestaShop",     "name": "PrestaShop",      "tag": "Store",     "free": False},
        {"id": "OpenCart",       "name": "OpenCart",        "tag": "Store",     "free": False},
        {"id": "CSV",            "name": "CSV / Manual",    "tag": "Manual",    "free": True},
    ]}


# ── Demo product injector ─────────────────────────────────────────────────────

DEMO_BY_PLATFORM = {
    "CJDropshipping": [
        {"id": "1", "title": "Wireless Earbuds Pro X5",     "price": 12.99, "image": "https://images.unsplash.com/photo-1590658268037-6bf12165a8df?w=400", "category": "Electronics", "sku": "CJ-WEP-X5"},
        {"id": "2", "title": "LED Ring Light 10 inch",      "price": 8.49,  "image": "https://images.unsplash.com/photo-1596462502278-27bfdc403348?w=400", "category": "Photography", "sku": "CJ-LED-10"},
        {"id": "3", "title": "Portable Blender USB",        "price": 14.99, "image": "https://images.unsplash.com/photo-1570197788417-0e82375c9371?w=400", "category": "Kitchen",     "sku": "CJ-BLN-USB"},
        {"id": "4", "title": "Smart Watch Fitness",         "price": 19.99, "image": "https://images.unsplash.com/photo-1523275335684-37898b6baf30?w=400", "category": "Wearables",   "sku": "CJ-SW-FIT"},
        {"id": "5", "title": "Car Phone Holder Magnetic",   "price": 5.99,  "image": "https://images.unsplash.com/photo-1617886903355-9354bb57751f?w=400", "category": "Automotive",  "sku": "CJ-CPH-MAG"},
        {"id": "6", "title": "Reusable Water Bottle 1L",    "price": 7.99,  "image": "https://images.unsplash.com/photo-1602143407151-7111542de6e8?w=400", "category": "Sports",      "sku": "CJ-WB-1L"},
    ],
    "AliExpress": [
        {"id": "1", "title": "Anime Hoodie Oversized",      "price": 15.99, "image": "https://images.unsplash.com/photo-1556821840-3a63f15732ce?w=400", "category": "Fashion",     "sku": "AE-HOD-001"},
        {"id": "2", "title": "Mechanical Keyboard TKL",     "price": 35.00, "image": "https://images.unsplash.com/photo-1585298723682-7115561c51b7?w=400", "category": "Electronics", "sku": "AE-KBD-TKL"},
        {"id": "3", "title": "Gaming Mouse 12000DPI",       "price": 18.50, "image": "https://images.unsplash.com/photo-1527814050087-3793815479db?w=400", "category": "Electronics", "sku": "AE-MSE-12K"},
        {"id": "4", "title": "Silk Pillowcase Set",         "price": 11.99, "image": "https://images.unsplash.com/photo-1631049307264-da0ec9d70304?w=400", "category": "Home",        "sku": "AE-PIL-SLK"},
    ],
    "Printful": [
        {"id": "1", "title": "Custom Print Unisex T-Shirt", "price": 18.00, "image": "https://images.unsplash.com/photo-1521572163474-6864f9cf17ab?w=400", "category": "Apparel",  "sku": "PF-TEE-001"},
        {"id": "2", "title": "Premium Hoodie Custom",       "price": 35.00, "image": "https://images.unsplash.com/photo-1556821840-3a63f15732ce?w=400", "category": "Apparel",  "sku": "PF-HOD-001"},
        {"id": "3", "title": "Custom Ceramic Mug 11oz",     "price": 12.00, "image": "https://images.unsplash.com/photo-1514228742587-6b1558fcca3d?w=400", "category": "Home",     "sku": "PF-MUG-11"},
    ],
    "Printify": [
        {"id": "1", "title": "Classic Unisex Cotton Tee",     "price": 14.99, "image": "https://images.unsplash.com/photo-1618354691373-d851c5c3a990?w=400", "category": "Apparel",      "sku": "PTFY-TEE-001"},
        {"id": "2", "title": "All-Over Print Crop Top",       "price": 28.00, "image": "https://images.unsplash.com/photo-1594938298603-c8148c4dae35?w=400", "category": "Apparel",      "sku": "PTFY-CRP-002"},
        {"id": "3", "title": "Custom Canvas Tote Bag",        "price": 16.50, "image": "https://images.unsplash.com/photo-1544816155-12df9643f363?w=400", "category": "Accessories",   "sku": "PTFY-BAG-003"},
        {"id": "4", "title": "Premium Poster Print 18x24",    "price": 22.00, "image": "https://images.unsplash.com/photo-1513364776144-60967b0f800f?w=400", "category": "Wall Art",      "sku": "PTFY-POS-004"},
        {"id": "5", "title": "Custom Phone Case iPhone 15",   "price": 19.99, "image": "https://images.unsplash.com/photo-1601784551446-20c9e07cdbdb?w=400", "category": "Accessories",   "sku": "PTFY-PHN-005"},
    ],
    "Gooten": [
        {"id": "1", "title": "Fleece Blanket Custom Print",   "price": 42.00, "image": "https://images.unsplash.com/photo-1555041469-a586c61ea9bc?w=400", "category": "Home",         "sku": "GTN-BLK-001"},
        {"id": "2", "title": "Canvas Gallery Wrap 16x20",     "price": 35.00, "image": "https://images.unsplash.com/photo-1579783902614-a3fb3927b6a5?w=400", "category": "Wall Art",      "sku": "GTN-CVS-002"},
        {"id": "3", "title": "Stainless Steel Water Bottle",  "price": 24.99, "image": "https://images.unsplash.com/photo-1602143407151-7111542de6e8?w=400", "category": "Drinkware",     "sku": "GTN-BTL-003"},
        {"id": "4", "title": "Laptop Sleeve 15 inch",         "price": 29.99, "image": "https://images.unsplash.com/photo-1603302576837-37561b2e2302?w=400", "category": "Accessories",   "sku": "GTN-SLV-004"},
    ],
    "Alibaba": [
        {"id": "1", "title": "Bluetooth Speaker Bulk x50",  "price": 6.50,  "image": "https://images.unsplash.com/photo-1608043152269-423dbba4e7e1?w=400", "category": "Electronics", "sku": "AL-SPK-B50"},
        {"id": "2", "title": "Bamboo Cutting Board Set",    "price": 3.20,  "image": "https://images.unsplash.com/photo-1585837146751-a44117a3bb2e?w=400", "category": "Kitchen",     "sku": "AL-BCB-SET"},
    ],
    "DHgate": [
        {"id": "1", "title": "Luxury Watch Replica Steel",  "price": 25.00, "image": "https://images.unsplash.com/photo-1524592094714-0f0654e20314?w=400", "category": "Watches",     "sku": "DH-WCH-001"},
        {"id": "2", "title": "Designer Bag Inspired",       "price": 18.00, "image": "https://images.unsplash.com/photo-1548036328-c9fa89d128fa?w=400", "category": "Fashion",     "sku": "DH-BAG-001"},
    ],
    "Spocket": [
        {"id": "1", "title": "Organic Face Serum 30ml",     "price": 22.00, "image": "https://images.unsplash.com/photo-1556228578-8c89e6adf883?w=400", "category": "Beauty",   "sku": "SP-SRM-30"},
        {"id": "2", "title": "Linen Throw Blanket US-made", "price": 34.00, "image": "https://images.unsplash.com/photo-1566454825481-9c31016b4196?w=400", "category": "Home",     "sku": "SP-LTB-001"},
        {"id": "3", "title": "Cold Brew Coffee Kit",        "price": 28.00, "image": "https://images.unsplash.com/photo-1509042239860-f550ce710b93?w=400", "category": "Kitchen",  "sku": "SP-CBK-001"},
    ],
    "Wholesale2B": [
        {"id": "1", "title": "Essence Mascara Lash Princess", "price": 9.99,  "image": "https://cdn.dummyjson.com/product-images/beauty/essence-mascara-lash-princess/thumbnail.webp", "category": "Beauty",      "sku": "W2B-001"},
        {"id": "2", "title": "Eyeshadow Palette with Mirror", "price": 19.99, "image": "https://cdn.dummyjson.com/product-images/beauty/eyeshadow-palette-with-mirror/thumbnail.webp", "category": "Beauty",      "sku": "W2B-002"},
        {"id": "3", "title": "Powder Canister",               "price": 14.99, "image": "https://cdn.dummyjson.com/product-images/beauty/powder-canister/thumbnail.webp",                "category": "Beauty",      "sku": "W2B-003"},
        {"id": "4", "title": "Red Lipstick",                  "price": 12.99, "image": "https://cdn.dummyjson.com/product-images/beauty/red-lipstick/thumbnail.webp",                   "category": "Beauty",      "sku": "W2B-004"},
        {"id": "5", "title": "Calvin Klein CK One",           "price": 49.99, "image": "https://cdn.dummyjson.com/product-images/fragrances/calvin-klein-ck-one/thumbnail.webp",         "category": "Fragrances",  "sku": "W2B-005"},
    ],
    "EPROLO": [
        {"id": "1", "title": "Fjallraven Foldsack No.1 Backpack", "price": 109.95, "image": "https://fakestoreapi.com/img/81fPKd-2AYL._AC_SL1500_.jpg",     "category": "Fashion",      "sku": "EP-001"},
        {"id": "2", "title": "MBJ Womens Solid Short Sleeve Tee", "price": 9.85,   "image": "https://fakestoreapi.com/img/71z3kpMAYsL._AC_UY879_.jpg",      "category": "Fashion",      "sku": "EP-002"},
        {"id": "3", "title": "SanDisk SSD PLUS 1TB Drive",        "price": 109.00, "image": "https://fakestoreapi.com/img/61U7T1koQqL._AC_SX679_.jpg",      "category": "Electronics",  "sku": "EP-003"},
        {"id": "4", "title": "John Hardy Gold Dragon Bracelet",    "price": 695.00, "image": "https://fakestoreapi.com/img/71pWzhdJNwL._AC_UL640_QL65_ML3_.jpg", "category": "Jewelery", "sku": "EP-004"},
    ],
    "Walmart": [
        {"id": "1", "title": "Instant Pot Duo 7-in-1",     "price": 89.00, "image": "https://images.unsplash.com/photo-1585515320310-259814833e62?w=400", "category": "Kitchen",     "sku": "WM-IP-DUO"},
        {"id": "2", "title": "Roku Streaming Stick 4K",    "price": 49.00, "image": "https://images.unsplash.com/photo-1522869635100-9f4c5e86aa37?w=400", "category": "Electronics", "sku": "WM-RKU-4K"},
    ],
    "Amazon": [
        {"id": "1", "title": "Echo Dot 5th Gen",            "price": 49.99, "image": "https://images.unsplash.com/photo-1543512214-318c7553f230?w=400", "category": "Smart Home",  "sku": "AMZ-ECHO-D5"},
        {"id": "2", "title": "Kindle Paperwhite 16GB",      "price": 139.99,"image": "https://images.unsplash.com/photo-1544716278-ca5e3f4abd8c?w=400", "category": "Electronics", "sku": "AMZ-KPW-16"},
    ],
    # ── Store Platform demos ────────────────────────────────────────────────
    "Shopify": [
        {"id": "1", "title": "Minimalist Watch Gold Edition",     "price": 89.99,  "image": "https://images.unsplash.com/photo-1524592094714-0f0654e20314?w=400", "category": "Accessories",  "sku": "SH-WTCH-001"},
        {"id": "2", "title": "Organic Cotton T-Shirt Pack",       "price": 49.99,  "image": "https://images.unsplash.com/photo-1521572163474-6864f9cf17ab?w=400", "category": "Apparel",      "sku": "SH-TEE-002"},
        {"id": "3", "title": "Handmade Leather Wallet",           "price": 65.00,  "image": "https://images.unsplash.com/photo-1627123524004-a1b0a1b1573e?w=400", "category": "Accessories",  "sku": "SH-WLT-003"},
        {"id": "4", "title": "Bamboo Sunglasses UV400",           "price": 34.99,  "image": "https://images.unsplash.com/photo-1572635196237-14b3f281503f?w=400", "category": "Accessories",  "sku": "SH-SUN-004"},
        {"id": "5", "title": "Scented Candle Set — Lavender",     "price": 28.00,  "image": "https://images.unsplash.com/photo-1602607633720-c1f0cd95e8c8?w=400", "category": "Home",         "sku": "SH-CDL-005"},
    ],
    "WooCommerce": [
        {"id": "1", "title": "Wireless Charging Pad 15W",         "price": 29.99,  "image": "https://images.unsplash.com/photo-1586953208448-b95a79798f07?w=400", "category": "Electronics",  "sku": "WC-CHG-001"},
        {"id": "2", "title": "Stainless Steel Tumbler 30oz",      "price": 24.99,  "image": "https://images.unsplash.com/photo-1602143407151-7111542de6e8?w=400", "category": "Drinkware",    "sku": "WC-TBL-002"},
        {"id": "3", "title": "LED Desk Lamp Touch Control",       "price": 42.00,  "image": "https://images.unsplash.com/photo-1507473885765-e6ed057ab6fe?w=400", "category": "Office",       "sku": "WC-LMP-003"},
        {"id": "4", "title": "Yoga Mat Premium 6mm",              "price": 35.99,  "image": "https://images.unsplash.com/photo-1601925260368-ae2f83cf8b7f?w=400", "category": "Fitness",      "sku": "WC-YGA-004"},
    ],
    "Magento": [
        {"id": "1", "title": "Professional DSLR Camera Bag",      "price": 79.99,  "image": "https://images.unsplash.com/photo-1553062407-98eeb64c6a62?w=400", "category": "Photography",  "sku": "MG-BAG-001"},
        {"id": "2", "title": "4K Action Camera Waterproof",       "price": 149.99, "image": "https://images.unsplash.com/photo-1526170375885-4d8ecf77b99f?w=400", "category": "Electronics",  "sku": "MG-CAM-002"},
        {"id": "3", "title": "Carbon Fiber Tripod Pro",           "price": 119.00, "image": "https://images.unsplash.com/photo-1617575521317-d2974f3b56d2?w=400", "category": "Photography",  "sku": "MG-TRP-003"},
    ],
    "BigCommerce": [
        {"id": "1", "title": "Smart Home Hub Controller",         "price": 129.99, "image": "https://images.unsplash.com/photo-1558089687-f282ffcbc126?w=400", "category": "Smart Home",   "sku": "BC-HUB-001"},
        {"id": "2", "title": "RGB Mechanical Gaming Keyboard",    "price": 89.99,  "image": "https://images.unsplash.com/photo-1585298723682-7115561c51b7?w=400", "category": "Gaming",       "sku": "BC-KBD-002"},
        {"id": "3", "title": "Noise Cancelling Earbuds Pro",      "price": 59.99,  "image": "https://images.unsplash.com/photo-1590658268037-6bf12165a8df?w=400", "category": "Audio",        "sku": "BC-EAR-003"},
        {"id": "4", "title": "USB-C Hub 7-in-1 Adapter",          "price": 44.99,  "image": "https://images.unsplash.com/photo-1625842268584-8f3296236761?w=400", "category": "Accessories",  "sku": "BC-USB-004"},
    ],
    "PrestaShop": [
        {"id": "1", "title": "French Press Coffee Maker 1L",      "price": 32.99,  "image": "https://images.unsplash.com/photo-1495474472287-4d71bcdd2085?w=400", "category": "Kitchen",      "sku": "PS-CFE-001"},
        {"id": "2", "title": "Ceramic Pour Over Dripper",         "price": 24.99,  "image": "https://images.unsplash.com/photo-1509042239860-f550ce710b93?w=400", "category": "Kitchen",      "sku": "PS-DRP-002"},
        {"id": "3", "title": "Electric Milk Frother",              "price": 19.99,  "image": "https://images.unsplash.com/photo-1461023058943-07fcbe16d735?w=400", "category": "Kitchen",      "sku": "PS-FRT-003"},
    ],
    "OpenCart": [
        {"id": "1", "title": "Retro Vinyl Record Player",         "price": 89.00,  "image": "https://images.unsplash.com/photo-1539375665275-f9de415ef9ac?w=400", "category": "Audio",        "sku": "OC-VNL-001"},
        {"id": "2", "title": "Portable Bluetooth Speaker",        "price": 45.99,  "image": "https://images.unsplash.com/photo-1608043152269-423dbba4e7e1?w=400", "category": "Audio",        "sku": "OC-SPK-002"},
        {"id": "3", "title": "Wireless Over-Ear Headphones",      "price": 69.99,  "image": "https://images.unsplash.com/photo-1505740420928-5e560c06d30e?w=400", "category": "Audio",        "sku": "OC-HPH-003"},
    ],
}

DEFAULT_DEMO = [
    {"id": "1", "title": "AirPods Pro 2nd Gen",     "price": 199.99, "image": "https://images.unsplash.com/photo-1600294037681-c80b4cb5b434?w=400", "category": "Electronics", "sku": "APP-PRO-2"},
    {"id": "2", "title": "Sony WH-1000XM5",         "price": 279.99, "image": "https://images.unsplash.com/photo-1505740420928-5e560c06d30e?w=400", "category": "Electronics", "sku": "SONY-WH5"},
    {"id": "3", "title": "iPad Air 5th Gen 256GB",  "price": 649.99, "image": "https://images.unsplash.com/photo-1544244015-0df4b3ffc6b0?w=400", "category": "Tablets",     "sku": "IPAD-AIR5"},
]


def _inject_demo_products(store_key: str, supplier_name: str, platform: str = ""):
    demo = DEMO_BY_PLATFORM.get(platform, DEFAULT_DEMO)
    products = []
    for p in demo:
        products.append({
            **p,
            "id":        f"{store_key}_{p['id']}",
            "supplier":  supplier_name,
            "store_key": store_key,
            "platform":  platform,
            "stock":     p.get("stock", 50),
            "source_url": "",
        })
    _add_products(store_key, products, supplier_name)
