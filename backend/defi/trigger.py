"""
Trigger V2 — USD-priced trigger orders (client-signed relay).

Docs: https://developers.jup.ag/docs/trigger — Trigger V2 ONLY (not V1, not
Recurring V1). Verified live 2026-08-12: https://api.jup.ag/trigger/v2/orders/price
and /deposit/craft answer 401 without a Portal key → a JUPITER_API_KEY is
REQUIRED for every route here; without it every call degrades to a clean
"key not set" error. Funds are VAULT-LOCKED while an order is open — the
confirmation states this plainly. Caps enforced server-side from open orders.
"""

import logging
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from network_db import db
from . import config, jupiter
from .authz import require_user

logger = logging.getLogger("defi.trigger")
router = APIRouter(prefix="/api/defi/trigger", tags=["defi-trigger"])

# per-type minimums / defaults surfaced from the live docs (developers.jup.ag/docs/trigger)
DOC_MINIMUM_USD = 10.0            # "$10 minimum" validation rule (Trigger V2 docs)
DOC_DEFAULT_SLIPPAGE_BPS = 100


async def _key_or_503() -> str:
    key = await config.jupiter_key()
    if not key:
        raise HTTPException(503, "Jupiter API key not set — Trigger V2 requires a Portal key "
                                 "(portal.jup.ag). Paste it in /settings → DeFi · Jupiter or "
                                 "set JUPITER_API_KEY.")
    return key


async def _open_orders(wallet: str, key: str) -> List[dict]:
    res = await jupiter.trigger_get("getTriggerOrders",
                                    {"user": wallet, "orderStatus": "active"}, key)
    if res.get("http_status") != 200:
        raise HTTPException(503, f"cannot verify open orders (HTTP {res.get('http_status')}) — "
                                 f"fail closed, no new order")
    return res.get("orders") or res.get("data") or []


class TriggerBuildBody(BaseModel):
    wallet: str
    inputMint: str
    outputMint: str
    makingAmount: str            # base units of inputMint to lock
    triggerPriceUsd: float
    triggerCondition: str = "above"     # above | below
    orderSubType: str = "single"        # single | oco | otoco
    slippageBps: int = DOC_DEFAULT_SLIPPAGE_BPS
    expiresAt: Optional[str] = None     # ISO — docs require a future expiry


@router.post("/build")
async def build(body: TriggerBuildBody, request: Request,
                authorization: Optional[str] = Header(None)):
    if not config.trigger_enabled():
        raise HTTPException(403, "TRIGGER_ENABLED=false — trigger orders are dark")
    await require_user(request, authorization)
    key = await _key_or_503()

    allow = config.allowed_mints()
    if not allow:
        raise HTTPException(403, "DEFI_ALLOWED_MINTS is empty — nothing is tradeable")
    for m in (body.inputMint, body.outputMint):
        if m not in allow:
            raise HTTPException(403, f"mint {m[:12]}… is not on the allowlist")
    if body.inputMint == body.outputMint:
        raise HTTPException(400, "mints must be distinct (Trigger V2 validation rule)")
    if body.triggerPriceUsd <= 0:
        raise HTTPException(400, "triggerPriceUsd must be positive")

    # USD sizing from independent prices; fail closed when unknown
    pm = await jupiter.prices([body.inputMint])
    p = (pm.get(body.inputMint) or {})
    if not p.get("usdPrice") or p.get("decimals") is None:
        raise HTTPException(503, "no independent price for the input mint — fail closed")
    usd_locked = int(body.makingAmount) / (10 ** p["decimals"]) * p["usdPrice"]
    if usd_locked < DOC_MINIMUM_USD:
        raise HTTPException(400, f"order ${usd_locked:.2f} is below the documented "
                                 f"${DOC_MINIMUM_USD:.0f} Trigger V2 minimum")

    open_orders = await _open_orders(body.wallet, key)
    if len(open_orders) >= config.trigger_max_open_orders():
        raise HTTPException(403, f"TRIGGER_MAX_OPEN_ORDERS ({config.trigger_max_open_orders()}) reached")
    locked = 0.0
    for o in open_orders:
        try:
            locked += float(o.get("makingAmountUsd") or o.get("lockedUsd") or 0)
        except (TypeError, ValueError):
            pass
    if locked + usd_locked > config.trigger_max_locked_usd():
        raise HTTPException(403, f"locked-value ceiling: ${locked:.2f} open + ${usd_locked:.2f} "
                                 f"new exceeds TRIGGER_MAX_LOCKED_USD ${config.trigger_max_locked_usd():.2f}")

    # V2 flow: deposit/craft (vault pre-step) — returns the unsigned tx the user signs.
    # docs: https://developers.jup.ag/docs/trigger (deposit/craft requires explicit
    # orderType metadata since the V2 changelog update)
    payload = {"user": body.wallet, "inputMint": body.inputMint, "outputMint": body.outputMint,
               "makingAmount": body.makingAmount,
               "orderType": "price", "orderSubType": body.orderSubType,
               "triggerPriceUsd": str(body.triggerPriceUsd),
               "triggerCondition": body.triggerCondition,
               "slippageBps": body.slippageBps}
    if body.expiresAt:
        payload["expiresAt"] = body.expiresAt
    crafted = await jupiter.trigger_post("deposit/craft", payload, key)
    if crafted.get("http_status") != 200:
        raise HTTPException(503, f"trigger deposit/craft failed (HTTP {crafted.get('http_status')}): "
                                 f"{crafted.get('error') or crafted}")
    await db.defi_trigger_builds.insert_one({
        "wallet": body.wallet, "payload": payload, "usd_locked": round(usd_locked, 2),
        "request_id": crafted.get("requestId"), "built_at": datetime.now(timezone.utc)})
    return {"ok": True,
            "transaction": crafted.get("transaction"),
            "requestId": crafted.get("requestId"),
            "confirmation": {
                "usd_locked": round(usd_locked, 2),
                "trigger_price_usd": body.triggerPriceUsd,
                "condition": body.triggerCondition,
                "slippage_bps": body.slippageBps,
                "minimum_usd": DOC_MINIMUM_USD,
                "plain_language": "Your funds LOCK IN THE JUPITER VAULT while this order is "
                                  "open. They are not spendable until the order fills, "
                                  "expires or you cancel it. Cancelling returns the funds."},
            "raw": {k: v for k, v in crafted.items() if k not in ("transaction",)}}


class TriggerSubmitBody(BaseModel):
    signedTransaction: str
    requestId: str


@router.post("/submit")
async def submit(body: TriggerSubmitBody, request: Request,
                 authorization: Optional[str] = Header(None)):
    if not config.trigger_enabled():
        raise HTTPException(403, "TRIGGER_ENABLED=false — trigger orders are dark")
    await require_user(request, authorization)
    key = await _key_or_503()
    built = await db.defi_trigger_builds.find_one_and_update(
        {"request_id": body.requestId, "submitted": {"$ne": True}},
        {"$set": {"submitted": True}})
    if not built:
        exists = await db.defi_trigger_builds.find_one({"request_id": body.requestId})
        if exists:
            raise HTTPException(409, "this trigger build was already submitted")
        raise HTTPException(400, "unknown requestId — trigger builds must originate from this backend")
    res = await jupiter.trigger_post("execute", {
        "signedTransaction": body.signedTransaction, "requestId": body.requestId}, key)
    await db.defi_trigger_events.insert_one({
        "request_id": body.requestId, "result_status": res.get("status"),
        "http_status": res.get("http_status"), "signature": res.get("signature"),
        "ts": datetime.now(timezone.utc)})
    if res.get("http_status", 500) >= 400:
        raise HTTPException(503, f"trigger execute failed: {res.get('error') or res}")
    return {"ok": True, "status": res.get("status"), "signature": res.get("signature")}


@router.get("/orders")
async def orders(wallet: str, status: str = "active", request: Request = None,
                 authorization: Optional[str] = Header(None)):
    await require_user(request, authorization)
    key = await _key_or_503()
    res = await jupiter.trigger_get("getTriggerOrders", {"user": wallet, "orderStatus": status}, key)
    if res.get("http_status") != 200:
        return {"ok": False, "error": res.get("error") or f"HTTP {res.get('http_status')}", "orders": None}
    return {"ok": True, "orders": res.get("orders") or res.get("data") or [],
            "locked_note": "funds stay vault-locked while an order is open"}


class CancelBody(BaseModel):
    wallet: str
    orderKey: str


@router.post("/cancel")
async def cancel(body: CancelBody, request: Request, authorization: Optional[str] = Header(None)):
    """Cancel = an EXIT — never gated behind TRIGGER_ENABLED (N4: exits are never gated)."""
    await require_user(request, authorization)
    key = await _key_or_503()
    res = await jupiter.trigger_post("cancelOrder", {"user": body.wallet, "order": body.orderKey}, key)
    if res.get("http_status", 500) >= 400:
        raise HTTPException(503, f"cancel craft failed: {res.get('error') or res}")
    return {"ok": True, "transaction": res.get("transaction"), "requestId": res.get("requestId"),
            "note": "sign in Phantom to cancel; the vault returns your funds"}
