"""
BAZAAR Eats API Router
Endpoints:
  GET  /api/v1/eats/restaurants               — all restaurants (paginated)
  GET  /api/v1/eats/restaurants/zip/{zip}     — restaurants serving a ZIP code
  GET  /api/v1/eats/restaurants/{id}          — single restaurant with full menu
  GET  /api/v1/eats/restaurants/search        — free-text + category search
  POST /api/v1/eats/restaurants               — create (admin)
  PUT  /api/v1/eats/restaurants/{id}          — update (admin)
  DELETE /api/v1/eats/restaurants/{id}        — delete (admin)
  GET  /api/v1/eats/categories                — distinct categories
  GET  /api/v1/eats/zips                      — all registered ZIP codes
"""

from __future__ import annotations

import json
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from models import EatsRestaurant, EatsServiceZip

router = APIRouter(prefix="/eats", tags=["BAZAAR Eats"])

# ─────────────────────────────────────────────────────────────────────────────
# Pydantic schemas
# ─────────────────────────────────────────────────────────────────────────────

class MenuItemSchema(BaseModel):
    n: str
    d: str
    p: float
    img: str = ""

class MenuSectionSchema(BaseModel):
    cat: str
    items: List[MenuItemSchema]

class RestaurantBase(BaseModel):
    name: str
    addr: str
    phone: Optional[str] = None
    type: str = "restaurant"          # "restaurant" | "grocery"
    cats: List[str] = []
    rating: float = 4.5
    reviews: int = 0
    time: str = "30-45"
    fee: str = "$2.99"
    min_order: float = 15.0
    img: str = ""
    promo: str = ""
    tags: List[str] = []
    featured: bool = False
    desc: str = ""
    menu: List[MenuSectionSchema] = []
    service_zips: List[str] = []

class RestaurantCreate(RestaurantBase):
    pass

class RestaurantOut(RestaurantBase):
    id: int

    class Config:
        from_attributes = True

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _row_to_dict(r: EatsRestaurant) -> dict:
    return {
        "id":         r.id,
        "name":       r.name,
        "addr":       r.addr,
        "phone":      r.phone,
        "type":       r.type,
        "cats":       json.loads(r.cats  or "[]"),
        "rating":     r.rating,
        "reviews":    r.reviews,
        "time":       r.time,
        "fee":        r.fee,
        "minOrder":   r.min_order,
        "img":        r.img,
        "promo":      r.promo,
        "tags":       json.loads(r.tags  or "[]"),
        "featured":   r.featured,
        "desc":       r.desc,
        "menu":       json.loads(r.menu  or "[]"),
        "serviceZips": [z.zip_code for z in r.service_zips],
    }

# ─────────────────────────────────────────────────────────────────────────────
# Read endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/restaurants", summary="All restaurants / markets")
def list_restaurants(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, le=500),
    type: Optional[str] = Query(None, description="restaurant | grocery"),
    featured: Optional[bool] = None,
    db: Session = Depends(get_db),
):
    q = db.query(EatsRestaurant)
    if type:
        q = q.filter(EatsRestaurant.type == type)
    if featured is not None:
        q = q.filter(EatsRestaurant.featured == featured)
    total = q.count()
    rows  = q.offset(skip).limit(limit).all()
    return {"total": total, "results": [_row_to_dict(r) for r in rows]}


@router.get("/restaurants/zip/{zip_code}", summary="Restaurants serving a ZIP code")
def restaurants_by_zip(
    zip_code: str,
    type: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    z = zip_code.strip().zfill(5)
    q = (
        db.query(EatsRestaurant)
        .join(EatsServiceZip)
        .filter(EatsServiceZip.zip_code == z)
    )
    if type:
        q = q.filter(EatsRestaurant.type == type)
    rows = q.order_by(EatsRestaurant.featured.desc(), EatsRestaurant.rating.desc()).all()
    return {
        "zip":     z,
        "count":   len(rows),
        "results": [_row_to_dict(r) for r in rows],
    }


@router.get("/restaurants/search", summary="Search restaurants by name / tag / category")
def search_restaurants(
    q: str = Query(..., min_length=1),
    zip_code: Optional[str] = None,
    db: Session = Depends(get_db),
):
    term  = f"%{q.lower()}%"
    query = db.query(EatsRestaurant).filter(
        (EatsRestaurant.name.ilike(term)) |
        (EatsRestaurant.desc.ilike(term)) |
        (EatsRestaurant.tags.ilike(term)) |
        (EatsRestaurant.cats.ilike(term))
    )
    if zip_code:
        z = zip_code.strip().zfill(5)
        query = query.join(EatsServiceZip).filter(EatsServiceZip.zip_code == z)
    rows = query.limit(50).all()
    return {"query": q, "count": len(rows), "results": [_row_to_dict(r) for r in rows]}


@router.get("/restaurants/{restaurant_id}", summary="Single restaurant detail")
def get_restaurant(restaurant_id: int, db: Session = Depends(get_db)):
    r = db.query(EatsRestaurant).filter(EatsRestaurant.id == restaurant_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="Restaurant not found")
    return _row_to_dict(r)


@router.get("/categories", summary="All distinct categories")
def list_categories(db: Session = Depends(get_db)):
    rows = db.query(EatsRestaurant.cats).all()
    cats = set()
    for (raw,) in rows:
        for c in json.loads(raw or "[]"):
            cats.add(c)
    return {"categories": sorted(cats)}


@router.get("/zips", summary="All registered service ZIP codes")
def list_zips(db: Session = Depends(get_db)):
    rows = db.query(EatsServiceZip.zip_code).distinct().all()
    return {"zips": sorted(set(z for (z,) in rows))}

# ─────────────────────────────────────────────────────────────────────────────
# Write endpoints (admin — no auth guard here, add as needed)
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/restaurants", status_code=status.HTTP_201_CREATED, summary="Create restaurant")
def create_restaurant(payload: RestaurantCreate, db: Session = Depends(get_db)):
    r = EatsRestaurant(
        name=payload.name, addr=payload.addr, phone=payload.phone,
        type=payload.type,
        cats=json.dumps(payload.cats), rating=payload.rating,
        reviews=payload.reviews, time=payload.time,
        fee=payload.fee, min_order=payload.min_order,
        img=payload.img, promo=payload.promo,
        tags=json.dumps(payload.tags), featured=payload.featured,
        desc=payload.desc, menu=json.dumps([s.dict() for s in payload.menu]),
    )
    db.add(r)
    db.flush()
    for z in payload.service_zips:
        db.add(EatsServiceZip(restaurant_id=r.id, zip_code=z.strip().zfill(5)))
    db.commit()
    db.refresh(r)
    return _row_to_dict(r)


@router.put("/restaurants/{restaurant_id}", summary="Update restaurant")
def update_restaurant(restaurant_id: int, payload: RestaurantCreate, db: Session = Depends(get_db)):
    r = db.query(EatsRestaurant).filter(EatsRestaurant.id == restaurant_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="Not found")
    r.name       = payload.name
    r.addr       = payload.addr
    r.phone      = payload.phone
    r.type       = payload.type
    r.cats       = json.dumps(payload.cats)
    r.rating     = payload.rating
    r.reviews    = payload.reviews
    r.time       = payload.time
    r.fee        = payload.fee
    r.min_order  = payload.min_order
    r.img        = payload.img
    r.promo      = payload.promo
    r.tags       = json.dumps(payload.tags)
    r.featured   = payload.featured
    r.desc       = payload.desc
    r.menu       = json.dumps([s.dict() for s in payload.menu])
    # Replace service zips
    db.query(EatsServiceZip).filter(EatsServiceZip.restaurant_id == r.id).delete()
    for z in payload.service_zips:
        db.add(EatsServiceZip(restaurant_id=r.id, zip_code=z.strip().zfill(5)))
    db.commit()
    db.refresh(r)
    return _row_to_dict(r)


@router.delete("/restaurants/{restaurant_id}", summary="Delete restaurant")
def delete_restaurant(restaurant_id: int, db: Session = Depends(get_db)):
    r = db.query(EatsRestaurant).filter(EatsRestaurant.id == restaurant_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="Not found")
    db.query(EatsServiceZip).filter(EatsServiceZip.restaurant_id == r.id).delete()
    db.delete(r)
    db.commit()
    return {"deleted": restaurant_id}
