"""Storefront: Stripe Checkout -> signature-verified webhook -> entitlement -> timed download.
Docs: https://docs.stripe.com/api/checkout/sessions/create
      https://docs.stripe.com/webhooks/signature
Income books ONLY via revenue_system.defi_backend_patched.settlement.record_settled_income
(rail="stripe"), which re-verifies the PaymentIntent and refuses duplicates (N2).
"""
import os, json, hmac, hashlib, time, secrets
from pathlib import Path
from datetime import datetime, timezone, timedelta
import httpx
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import FileResponse

from network_db import db, get_raw_settings  # same shared handle the rest of the gateway uses
from revenue_system.defi_backend_patched.settlement import (
    record_settled_income, SettlementVerificationError)

router = APIRouter(prefix="/api/store", tags=["store"])

_CFG      = Path(__file__).with_name("products.json")
TTL_HOURS = int(os.environ.get("DOWNLOAD_TTL_HOURS", "72") or 72)
SK        = os.environ.get("STRIPE_SECRET_KEY", "")
WH        = os.environ.get("STRIPE_WEBHOOK_SECRET", "")


async def assets_dir() -> Path:
    """STORE_ASSETS_DIR env wins; /settings (Mongo) fallback; default /app/store_assets."""
    env = (os.environ.get("STORE_ASSETS_DIR", "") or "").strip()
    if env:
        return Path(env)
    s = await get_raw_settings()
    return Path((s.get("store_assets_dir") or "").strip() or "/app/store_assets")


async def base_url() -> str:
    """PUBLIC_BASE_URL env wins; /settings (Mongo) fallback; default https://yabbai.network."""
    env = (os.environ.get("PUBLIC_BASE_URL", "") or "").strip()
    if env:
        return env.rstrip("/")
    s = await get_raw_settings()
    return ((s.get("public_base_url") or "").strip() or "https://yabbai.network").rstrip("/")


def _catalog(assets: Path):
    cfg = json.loads(_CFG.read_text())
    out = {}
    for sku, p in cfg["skus"].items():
        priced = isinstance(p.get("price_cents"), int) and p["price_cents"] > 0
        asset  = (assets / p["file"]).is_file() if p.get("file") else False
        out[sku] = {**p, "listed": priced and asset,
                    "unlisted_reason": None if (priced and asset)
                    else ("price not set" if not priced else "asset file missing")}
    return cfg["currency"], out


@router.get("/health")
async def health():
    cur, cat = _catalog(await assets_dir())
    return {"ok": True, "app": "yabbai-store", "currency": cur,
            "stripe_key_configured": bool(SK), "webhook_secret_configured": bool(WH),
            "listed": [s for s, p in cat.items() if p["listed"]],
            "unlisted": {s: p["unlisted_reason"] for s, p in cat.items() if not p["listed"]}}


@router.get("/products")
async def products():
    cur, cat = _catalog(await assets_dir())
    return {"currency": cur,
            "products": [{"sku": s, "name": p["name"], "price_cents": p["price_cents"]}
                         for s, p in cat.items() if p["listed"]]}


@router.post("/checkout")
async def checkout(body: dict):
    if not SK:
        raise HTTPException(503, "store not configured: STRIPE_SECRET_KEY unset")
    sku = (body or {}).get("sku", "")
    cur, cat = _catalog(await assets_dir())
    p = cat.get(sku)
    if not p or not p["listed"]:
        raise HTTPException(404, f"sku '{sku}' not available")
    base = await base_url()
    form = {
        "mode": "payment",
        "success_url": f"{base}/store/index.html?session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url":  f"{base}/store/index.html",
        "line_items[0][quantity]": "1",
        "line_items[0][price_data][currency]": cur,
        "line_items[0][price_data][unit_amount]": str(p["price_cents"]),
        "line_items[0][price_data][product_data][name]": p["name"],
        "metadata[sku]": sku,
    }
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post("https://api.stripe.com/v1/checkout/sessions",
                         data=form, auth=(SK, ""))
    if r.status_code != 200:
        raise HTTPException(502, f"stripe checkout error {r.status_code}")
    s = r.json()
    return {"checkout_url": s["url"], "session_id": s["id"]}


def _verify_sig(raw: bytes, header: str) -> bool:
    if not (WH and header):
        return False
    kv = dict(x.split("=", 1) for x in header.split(",") if "=" in x)
    try:
        if abs(time.time() - int(kv.get("t", "0"))) > 300:
            return False
    except ValueError:
        return False
    expected = hmac.new(WH.encode(), f"{kv.get('t')}.".encode() + raw,
                        hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, v)
               for k, v in (x.split("=", 1) for x in header.split(",") if "=" in x)
               if k == "v1")


@router.post("/webhook")
async def webhook(request: Request):
    raw = await request.body()
    if not _verify_sig(raw, request.headers.get("stripe-signature", "")):
        raise HTTPException(400, "bad signature")
    event = json.loads(raw)
    if event.get("type") != "checkout.session.completed":
        return {"received": True, "ignored": event.get("type")}
    obj = event["data"]["object"]
    if obj.get("payment_status") != "paid":
        return {"received": True, "ignored": "not paid"}
    sku = (obj.get("metadata") or {}).get("sku", "")
    pi  = obj.get("payment_intent") or ""
    amt = (obj.get("amount_total") or 0) / 100.0
    cur = (obj.get("currency") or "aud").upper()
    email = ((obj.get("customer_details") or {}).get("email")) or ""
    # Settlement first (N2). A repeat raises SettlementVerificationError ("already booked")
    # which we treat as success (Stripe retry). A real failure blocks the grant.
    try:
        await record_settled_income(amt, cur, "stripe", pi,
                                    source="store", notes=f"sku={sku}")
    except SettlementVerificationError as e:
        if "already booked" not in str(e):
            raise HTTPException(409, f"settlement verification failed: {e}")
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(hours=TTL_HOURS)).isoformat()
    await db.entitlements.update_one(
        {"session_id": obj["id"]},
        {"$setOnInsert": {"session_id": obj["id"], "sku": sku, "email": email,
                          "token": token, "expires_at": expires, "payment_intent": pi,
                          "created_at": datetime.now(timezone.utc).isoformat()}},
        upsert=True)
    return {"received": True}


@router.get("/entitlement/{session_id}")
async def entitlement(session_id: str):
    e = await db.entitlements.find_one({"session_id": session_id}, {"_id": 0})
    if not e:
        return {"ready": False}
    return {"ready": True, "sku": e["sku"], "token": e["token"], "expires_at": e["expires_at"]}


@router.get("/download/{token}")
async def download(token: str):
    e = await db.entitlements.find_one({"token": token})
    if not e:
        raise HTTPException(404, "unknown or expired link")
    if datetime.fromisoformat(e["expires_at"]) < datetime.now(timezone.utc):
        raise HTTPException(410, "link expired — contact support for a refresh")
    assets = await assets_dir()
    _, cat = _catalog(assets)
    p = cat.get(e["sku"]) or {}
    f = assets / p.get("file", "")
    if not f.is_file():
        raise HTTPException(503, "asset not uploaded yet — contact support (no placeholder downloads)")
    return FileResponse(f, filename=f.name)
