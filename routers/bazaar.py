"""
bazaar.py — BAZAAR Marketplace router for SPHERA backend.

Wraps the API2Cart v1.1 unified commerce API so the Bazaar frontend
can pull live product feeds, categories, orders, and store data
from any connected eCommerce platform (Shopify, WooCommerce, etc.)
without ever exposing the raw API2Cart key to the browser.

Falls back to curated demo products instantly when API2Cart is
unreachable (VPS firewall / egress blocks). No timeouts for users.
"""

import os
import httpx
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Body
from fastapi.responses import JSONResponse

# ── Config ────────────────────────────────────────────────────────────────────
API2CART_KEY  = os.getenv("API2CART_KEY",  "498b0988a8da23545abf30ed0a8276e9")
API2CART_BASE = os.getenv("API2CART_BASE_URL", "https://app.api2cart.com/v1.1")
REQUEST_TIMEOUT = 4   # seconds — fail fast, fall back to demo

router = APIRouter(prefix="/bazaar", tags=["Bazaar Marketplace"])

# ── Demo product catalog (shown when API2Cart is unreachable) ─────────────────
DEMO_PRODUCTS = [
    # ── Threads & Fits (Fashion / Sneakers) ──
    {"id":"1",  "name":"Nike Air Jordan 1 Red And Black",    "price":{"value":149.99,"currency_id":"USD"}, "special_price":None,             "img":"https://cdn.dummyjson.com/product-images/mens-shoes/nike-air-jordan-1-red-and-black/thumbnail.webp",         "cat":"Threads & Fits", "badge":"hot",  "quantity":12},
    {"id":"2",  "name":"Puma Future Rider Trainers",          "price":{"value":89.99, "currency_id":"USD"}, "special_price":{"value":119.99}, "img":"https://cdn.dummyjson.com/product-images/mens-shoes/puma-future-rider-trainers/thumbnail.webp",               "cat":"Threads & Fits", "badge":"deal", "quantity":22},
    {"id":"3",  "name":"Man Plaid Shirt",                     "price":{"value":34.99, "currency_id":"USD"}, "special_price":None,             "img":"https://cdn.dummyjson.com/product-images/mens-shirts/man-plaid-shirt/thumbnail.webp",                         "cat":"Threads & Fits", "badge":"new",  "quantity":45},
    {"id":"4",  "name":"Blue & Black Check Shirt",            "price":{"value":29.99, "currency_id":"USD"}, "special_price":{"value":39.99},  "img":"https://cdn.dummyjson.com/product-images/mens-shirts/blue-&-black-check-shirt/thumbnail.webp",                "cat":"Threads & Fits", "badge":None,   "quantity":30},
    {"id":"5",  "name":"Blue Frock",                          "price":{"value":29.99, "currency_id":"USD"}, "special_price":None,             "img":"https://cdn.dummyjson.com/product-images/tops/blue-frock/thumbnail.webp",                                      "cat":"Threads & Fits", "badge":"new",  "quantity":18},
    {"id":"6",  "name":"Prada Women Bag",                     "price":{"value":599.99,"currency_id":"USD"}, "special_price":None,             "img":"https://cdn.dummyjson.com/product-images/womens-bags/prada-women-bag/thumbnail.webp",                          "cat":"Threads & Fits", "badge":"new",  "quantity":5},
    {"id":"7",  "name":"Heshe Leather Bag",                   "price":{"value":129.99,"currency_id":"USD"}, "special_price":{"value":179.99}, "img":"https://cdn.dummyjson.com/product-images/womens-bags/heshe-women-leather-bag/thumbnail.webp",                  "cat":"Threads & Fits", "badge":"deal", "quantity":14},
    # ── TechVault (Electronics) ──
    {"id":"8",  "name":"Apple MacBook Pro 14-inch Space Grey","price":{"value":1999.99,"currency_id":"USD"},"special_price":{"value":2299.99},"img":"https://cdn.dummyjson.com/product-images/laptops/apple-macbook-pro-14-inch-space-grey/thumbnail.webp",         "cat":"TechVault",      "badge":"deal", "quantity":8},
    {"id":"9",  "name":"Asus Zenbook Pro Dual Screen",        "price":{"value":1799.99,"currency_id":"USD"},"special_price":None,             "img":"https://cdn.dummyjson.com/product-images/laptops/asus-zenbook-pro-dual-screen-laptop/thumbnail.webp",           "cat":"TechVault",      "badge":"new",  "quantity":6},
    {"id":"10", "name":"iPhone 13 Pro",                       "price":{"value":1099.99,"currency_id":"USD"},"special_price":{"value":1299.99},"img":"https://cdn.dummyjson.com/product-images/smartphones/iphone-13-pro/thumbnail.webp",                            "cat":"TechVault",      "badge":"hot",  "quantity":15},
    {"id":"11", "name":"Samsung Galaxy S10",                  "price":{"value":699.99,"currency_id":"USD"}, "special_price":{"value":899.99}, "img":"https://cdn.dummyjson.com/product-images/smartphones/samsung-galaxy-s10/thumbnail.webp",                        "cat":"TechVault",      "badge":"deal", "quantity":9},
    {"id":"12", "name":"iPad Mini 2021 Starlight",            "price":{"value":499.99,"currency_id":"USD"}, "special_price":None,             "img":"https://cdn.dummyjson.com/product-images/tablets/ipad-mini-2021-starlight/thumbnail.webp",                     "cat":"TechVault",      "badge":"new",  "quantity":12},
    {"id":"13", "name":"Samsung Galaxy Tab S8 Plus",          "price":{"value":599.99,"currency_id":"USD"}, "special_price":{"value":749.99}, "img":"https://cdn.dummyjson.com/product-images/tablets/samsung-galaxy-tab-s8-plus-grey/thumbnail.webp",               "cat":"TechVault",      "badge":"deal", "quantity":7},
    {"id":"14", "name":"Apple AirPods Max Silver",            "price":{"value":549.99,"currency_id":"USD"}, "special_price":None,             "img":"https://cdn.dummyjson.com/product-images/mobile-accessories/apple-airpods-max-silver/thumbnail.webp",           "cat":"TechVault",      "badge":"hot",  "quantity":20},
    {"id":"15", "name":"Lenovo Yoga 920",                     "price":{"value":1099.99,"currency_id":"USD"},"special_price":{"value":1299.99},"img":"https://cdn.dummyjson.com/product-images/laptops/lenovo-yoga-920/thumbnail.webp",                               "cat":"TechVault",      "badge":"deal", "quantity":11},
    # ── The IceBox (Watches / Jewelry) ──
    {"id":"16", "name":"Rolex Submariner Watch",              "price":{"value":13999.99,"currency_id":"USD"},"special_price":None,            "img":"https://cdn.dummyjson.com/product-images/mens-watches/rolex-submariner-watch/thumbnail.webp",                   "cat":"The IceBox",     "badge":"new",  "quantity":2},
    {"id":"17", "name":"Longines Master Collection",          "price":{"value":1499.99,"currency_id":"USD"},"special_price":None,             "img":"https://cdn.dummyjson.com/product-images/mens-watches/longines-master-collection/thumbnail.webp",                "cat":"The IceBox",     "badge":"new",  "quantity":6},
    {"id":"18", "name":"Rolex Cellini Moonphase",             "price":{"value":12999.99,"currency_id":"USD"},"special_price":None,            "img":"https://cdn.dummyjson.com/product-images/mens-watches/rolex-cellini-moonphase/thumbnail.webp",                  "cat":"The IceBox",     "badge":"hot",  "quantity":1},
    {"id":"19", "name":"Green Crystal Earring",               "price":{"value":29.99, "currency_id":"USD"}, "special_price":None,             "img":"https://cdn.dummyjson.com/product-images/womens-jewellery/green-crystal-earring/thumbnail.webp",                 "cat":"The IceBox",     "badge":"new",  "quantity":40},
    {"id":"20", "name":"IWC Ingenieur Automatic Steel",       "price":{"value":4999.99,"currency_id":"USD"},"special_price":None,             "img":"https://cdn.dummyjson.com/product-images/womens-watches/iwc-ingenieur-automatic-steel/thumbnail.webp",           "cat":"The IceBox",     "badge":"new",  "quantity":3},
    # ── The Garage (Vehicles) ──
    {"id":"21", "name":"Charger SXT RWD",                     "price":{"value":32999.99,"currency_id":"USD"},"special_price":None,            "img":"https://cdn.dummyjson.com/product-images/vehicle/charger-sxt-rwd/thumbnail.webp",                               "cat":"The Garage",     "badge":"hot",  "quantity":2},
    {"id":"22", "name":"Dodge Hornet GT Plus",                "price":{"value":24999.99,"currency_id":"USD"},"special_price":None,            "img":"https://cdn.dummyjson.com/product-images/vehicle/dodge-hornet-gt-plus/thumbnail.webp",                          "cat":"The Garage",     "badge":"new",  "quantity":3},
    {"id":"23", "name":"Kawasaki Z800",                       "price":{"value":8999.99,"currency_id":"USD"}, "special_price":None,             "img":"https://cdn.dummyjson.com/product-images/motorcycle/kawasaki-z800/thumbnail.webp",                              "cat":"The Garage",     "badge":"hot",  "quantity":1},
    # ── HomeBase (Furniture / Kitchen) ──
    {"id":"24", "name":"Annibale Colombo Sofa",               "price":{"value":2499.99,"currency_id":"USD"},"special_price":None,             "img":"https://cdn.dummyjson.com/product-images/furniture/annibale-colombo-sofa/thumbnail.webp",                       "cat":"HomeBase",       "badge":"deal", "quantity":5},
    {"id":"25", "name":"Knoll Saarinen Executive Chair",       "price":{"value":499.99,"currency_id":"USD"}, "special_price":{"value":649.99}, "img":"https://cdn.dummyjson.com/product-images/furniture/knoll-saarinen-executive-conference-chair/thumbnail.webp",   "cat":"HomeBase",       "badge":"deal", "quantity":8},
    {"id":"26", "name":"Wooden Bathroom Sink With Mirror",     "price":{"value":799.99,"currency_id":"USD"}, "special_price":None,             "img":"https://cdn.dummyjson.com/product-images/furniture/wooden-bathroom-sink-with-mirror/thumbnail.webp",             "cat":"HomeBase",       "badge":"new",  "quantity":4},
    {"id":"27", "name":"Silver Pot With Glass Cap",            "price":{"value":39.99, "currency_id":"USD"}, "special_price":{"value":54.99},  "img":"https://cdn.dummyjson.com/product-images/kitchen-accessories/silver-pot-with-glass-cap/thumbnail.webp",         "cat":"HomeBase",       "badge":"deal", "quantity":60},
    # ── Glow Up (Beauty / Fragrance) ──
    {"id":"28", "name":"Essence Mascara Lash Princess",        "price":{"value":9.99,  "currency_id":"USD"}, "special_price":None,             "img":"https://cdn.dummyjson.com/product-images/beauty/essence-mascara-lash-princess/thumbnail.webp",                  "cat":"Glow Up",        "badge":"hot",  "quantity":200},
    {"id":"29", "name":"Chanel Coco Noir Eau De",              "price":{"value":129.99,"currency_id":"USD"}, "special_price":None,             "img":"https://cdn.dummyjson.com/product-images/fragrances/chanel-coco-noir-eau-de/thumbnail.webp",                     "cat":"Glow Up",        "badge":"new",  "quantity":45},
    {"id":"30", "name":"Dolce Shine Eau De",                   "price":{"value":69.99, "currency_id":"USD"}, "special_price":{"value":89.99},  "img":"https://cdn.dummyjson.com/product-images/fragrances/dolce-shine-eau-de/thumbnail.webp",                         "cat":"Glow Up",        "badge":"deal", "quantity":80},
    # ── The Pantry (Groceries) ──
    {"id":"31", "name":"Honey Jar",                            "price":{"value":6.99,  "currency_id":"USD"}, "special_price":None,             "img":"https://cdn.dummyjson.com/product-images/groceries/honey-jar/thumbnail.webp",                                   "cat":"The Pantry",     "badge":"new",  "quantity":500},
    {"id":"32", "name":"Cooking Oil",                          "price":{"value":4.99,  "currency_id":"USD"}, "special_price":{"value":6.99},   "img":"https://cdn.dummyjson.com/product-images/groceries/cooking-oil/thumbnail.webp",                                  "cat":"The Pantry",     "badge":"deal", "quantity":350},
    # ── Sports (The Arena) ──
    {"id":"33", "name":"Basketball",                           "price":{"value":14.99, "currency_id":"USD"}, "special_price":None,             "img":"https://cdn.dummyjson.com/product-images/sports-accessories/basketball/thumbnail.webp",                          "cat":"The Arena",      "badge":None,   "quantity":120},
    {"id":"34", "name":"Football",                             "price":{"value":17.99, "currency_id":"USD"}, "special_price":{"value":24.99},  "img":"https://cdn.dummyjson.com/product-images/sports-accessories/football/thumbnail.webp",                            "cat":"The Arena",      "badge":"deal", "quantity":90},
    {"id":"35", "name":"Tennis Racket",                        "price":{"value":49.99, "currency_id":"USD"}, "special_price":None,             "img":"https://cdn.dummyjson.com/product-images/sports-accessories/tennis-racket/thumbnail.webp",                       "cat":"The Arena",      "badge":"new",  "quantity":55},
    # ── The Vault (Sunglasses / Luxury) ──
    {"id":"36", "name":"Black Sun Glasses",                    "price":{"value":29.99, "currency_id":"USD"}, "special_price":None,             "img":"https://cdn.dummyjson.com/product-images/sunglasses/black-sun-glasses/thumbnail.webp",                           "cat":"The Vault",      "badge":None,   "quantity":75},
    {"id":"37", "name":"Classic Sun Glasses",                  "price":{"value":24.99, "currency_id":"USD"}, "special_price":{"value":34.99},  "img":"https://cdn.dummyjson.com/product-images/sunglasses/classic-sun-glasses/thumbnail.webp",                         "cat":"The Vault",      "badge":"deal", "quantity":60},
]

DEMO_CATEGORIES = [
    {"id":"1","name":"The Garage"},  {"id":"2","name":"Threads & Fits"},
    {"id":"3","name":"The Arena"},   {"id":"4","name":"TechVault"},
    {"id":"5","name":"The IceBox"},  {"id":"6","name":"The Vault"},
    {"id":"7","name":"HomeBase"},    {"id":"8","name":"Glow Up"},
    {"id":"9","name":"The Pantry"},  {"id":"10","name":"Lightning Deals"},
]

# ── Helpers ───────────────────────────────────────────────────────────────────

def _base_params(store_id: Optional[str] = None) -> dict:
    p = {"api_key": API2CART_KEY}
    if store_id:
        p["store_id"] = store_id
    return p


async def _a2c_get(path: str, params: dict) -> dict:
    """GET from API2Cart. Returns None on any connection failure."""
    url = f"{API2CART_BASE}/{path}.json"
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        if data.get("return_code", 0) != 0:
            return None
        return data.get("result", data)
    except Exception:
        return None


async def _a2c_post(path: str, params: dict, body: dict = None) -> dict:
    """POST to API2Cart. Returns None on any connection failure."""
    url = f"{API2CART_BASE}/{path}.json"
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.post(url, params=params, json=body or {})
        resp.raise_for_status()
        data = resp.json()
        if data.get("return_code", 0) != 0:
            return None
        return data.get("result", data)
    except Exception:
        return None


# ── Health ─────────────────────────────────────────────────────────────────────

@router.get("/health", summary="Check API2Cart connectivity + router status")
async def bazaar_health():
    """Always responds immediately. Reports API2Cart reachability."""
    data = await _a2c_get("account.info", {"api_key": API2CART_KEY})
    if data:
        return {"status": "connected", "api2cart": "reachable", "account": data}
    return {
        "status": "demo_mode",
        "api2cart": "unreachable",
        "router": "registered",
        "routes": 15,
        "demo_products": len(DEMO_PRODUCTS),
        "message": "API2Cart is blocked from this server. Serving demo products. Connect via VPN or whitelist api2cart.com egress."
    }


# ── Products ───────────────────────────────────────────────────────────────────

@router.get("/products", summary="Paginated product feed")
async def list_products(
    store_id:    Optional[str] = Query(None),
    start:       int           = Query(0,   ge=0),
    count:       int           = Query(20,  ge=1, le=100),
    category_id: Optional[str] = Query(None),
    sort_by:     Optional[str] = Query("id"),
    sort_dir:    Optional[str] = Query("asc"),
    avail:       Optional[bool] = Query(None),
):
    """Returns live products from API2Cart, merged with supplier products, or demo fallback."""
    params = _base_params(store_id)
    params.update({"start": start, "count": count, "sort_by": sort_by, "sort_dir": sort_dir})
    if category_id: params["category_id"] = category_id
    if avail is not None: params["available_for_sale"] = str(avail).lower()

    data = await _a2c_get("product.list", params)
    if data:
        return data

    # Merge demo products with synced supplier products
    from routers.suppliers import _synced_products

    # Convert supplier products to BAZAAR format
    supplier_items = []
    for sp in _synced_products:
        img_url = sp.get("image", "")
        is_url = img_url.startswith("http") if img_url else False
        supplier_items.append({
            "id":       sp.get("id", ""),
            "name":     sp.get("title", "Untitled"),
            "price":    {"value": float(sp.get("price", 0)), "currency_id": "USD"},
            "special_price": None,
            "img":      img_url if is_url else "📦",
            "image":    img_url if is_url else "",
            "cat":      sp.get("category", "Printful"),
            "badge":    "new",
            "quantity": int(sp.get("stock", 10)),
            "supplier": sp.get("supplier", ""),
            "platform": sp.get("platform", ""),
            "source":   "supplier",
        })

    # Combine: supplier products first, then demo
    all_products = supplier_items + DEMO_PRODUCTS
    sliced = all_products[start: start + count]
    return {
        "products": sliced,
        "products_count": len(all_products),
        "source": "merged" if supplier_items else "demo",
        "supplier_count": len(supplier_items),
    }


@router.get("/products/count", summary="Total product count")
async def count_products(
    store_id:    Optional[str] = Query(None),
    category_id: Optional[str] = Query(None),
):
    params = _base_params(store_id)
    if category_id: params["category_id"] = category_id
    data = await _a2c_get("product.count", params)
    if data:
        return data
    return {"products_count": len(DEMO_PRODUCTS), "source": "demo"}


@router.get("/products/{product_id}", summary="Single product detail")
async def get_product(product_id: str, store_id: Optional[str] = Query(None)):
    params = _base_params(store_id)
    params["id"] = product_id
    data = await _a2c_get("product.info", params)
    if data:
        return data
    # Fallback: find in demo
    match = next((p for p in DEMO_PRODUCTS if p["id"] == product_id), None)
    if match:
        return match
    raise HTTPException(status_code=404, detail="Product not found")


@router.get("/search", summary="Full-text product search")
async def search_products(
    q:        str           = Query(..., min_length=1),
    store_id: Optional[str] = Query(None),
    start:    int           = Query(0, ge=0),
    count:    int           = Query(20, ge=1, le=100),
):
    params = _base_params(store_id)
    params.update({"find_value": q, "find_where": "name", "start": start, "count": count})
    data = await _a2c_get("product.find", params)
    if data:
        return data
    # Fallback: search demo products
    q_lower = q.lower()
    results = [p for p in DEMO_PRODUCTS if q_lower in p["name"].lower() or q_lower in p["cat"].lower()]
    return {"products": results[start: start + count], "products_count": len(results), "source": "demo"}


# ── Categories ─────────────────────────────────────────────────────────────────

@router.get("/categories", summary="Category tree")
async def list_categories(
    store_id:  Optional[str] = Query(None),
    parent_id: Optional[str] = Query(None),
):
    params = _base_params(store_id)
    params["count"] = 250
    if parent_id: params["parent_id"] = parent_id
    data = await _a2c_get("category.list", params)
    if data:
        return data
    return {"categories": DEMO_CATEGORIES, "categories_count": len(DEMO_CATEGORIES), "source": "demo"}


# ── Store Management ───────────────────────────────────────────────────────────

@router.get("/stores", summary="List all connected stores")
async def list_stores():
    data = await _a2c_get("store.list", _base_params())
    if data:
        return data
    return {"stores": [], "source": "demo", "message": "API2Cart unreachable — no stores connected yet"}


@router.post("/stores/connect", summary="Connect a new store")
async def connect_store(
    cart_id:          str = Body(...),
    store_url:        str = Body(...),
    bridge_url:       str = Body(None),
    store_key:        str = Body(None),
    store_root_login: str = Body(None),
    store_root_passwd:str = Body(None),
):
    params = _base_params()
    body = {"cart_id": cart_id, "store_url": store_url}
    if bridge_url:        body["bridge_url"]        = bridge_url
    if store_key:         body["key"]               = store_key
    if store_root_login:  body["store_root_login"]  = store_root_login
    if store_root_passwd: body["store_root_passwd"] = store_root_passwd

    data = await _a2c_post("store.add", params, body)
    if data:
        return {"status": "connected", "result": data}
    return JSONResponse(
        status_code=503,
        content={"status": "error", "message": "API2Cart is currently unreachable from this server. Please contact support to enable outbound access to app.api2cart.com:443"}
    )


@router.delete("/stores/{store_id}", summary="Disconnect a store")
async def disconnect_store(store_id: str):
    params = _base_params()
    params["store_id"] = store_id
    data = await _a2c_get("store.delete", params)
    if data:
        return data
    raise HTTPException(status_code=503, detail="API2Cart unreachable")


# ── Orders ─────────────────────────────────────────────────────────────────────

@router.get("/orders", summary="List orders")
async def list_orders(
    store_id:       Optional[str] = Query(None),
    start:          int           = Query(0, ge=0),
    count:          int           = Query(20, ge=1, le=100),
    status:         Optional[str] = Query(None),
    customer_email: Optional[str] = Query(None),
):
    params = _base_params(store_id)
    params.update({"start": start, "count": count})
    if status:         params["status"]         = status
    if customer_email: params["customer_email"] = customer_email
    data = await _a2c_get("order.list", params)
    if data:
        return data
    return {"orders": [], "orders_count": 0, "source": "demo"}


@router.get("/orders/{order_id}", summary="Single order detail")
async def get_order(order_id: str, store_id: Optional[str] = Query(None)):
    params = _base_params(store_id)
    params["order_id"] = order_id
    data = await _a2c_get("order.info", params)
    if data:
        return data
    raise HTTPException(status_code=404, detail="Order not found")


# ── Cart Info ──────────────────────────────────────────────────────────────────

@router.get("/cart/info", summary="Connected store/cart platform info")
async def cart_info(store_id: Optional[str] = Query(None)):
    data = await _a2c_get("cart.info", _base_params(store_id))
    if data:
        return data
    return {"cart_id": "demo", "store_url": "bazaar.expediteconsults.com", "source": "demo"}


@router.get("/cart/config", summary="Store configuration")
async def cart_config(store_id: Optional[str] = Query(None)):
    data = await _a2c_get("cart.config", _base_params(store_id))
    if data:
        return data
    return {"currency": "USD", "language": "en", "timezone": "UTC", "source": "demo"}


@router.post("/sync", summary="Sync supplier products to storefront cache")
async def sync_products(payload: dict):
    import json, pathlib, time
    products = payload.get("products", [])
    if not isinstance(products, list):
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="products must be an array")
    cache_path = pathlib.Path("/tmp/sphera_bazaar_sync.json")
    cache_path.write_text(json.dumps({"synced_at": time.time(), "count": len(products), "products": products[:500]}))
    return {"ok": True, "synced": len(products), "message": f"Synced {len(products)} products"}

@router.get("/sync/status", summary="Get last sync status")
async def sync_status():
    import json, pathlib, time
    cache_path = pathlib.Path("/tmp/sphera_bazaar_sync.json")
    if not cache_path.exists():
        return {"ok": False, "message": "No sync yet"}
    data = json.loads(cache_path.read_text())
    age = int(time.time() - data.get("synced_at", 0))
    return {"ok": True, "count": data.get("count", 0), "age_seconds": age}
