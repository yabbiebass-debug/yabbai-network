"""
Sentinel — READ-ONLY position + risk monitor. Constructs NO transactions,
imports no execution paths. Poll-driven (the page polls; no scheduler — N3).
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

import httpx
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from network_db import db
from . import config, jupiter
from .authz import require_user, rpc_call

logger = logging.getLogger("defi.sentinel")
router = APIRouter(prefix="/api/defi/sentinel", tags=["defi-sentinel"])

DS_TOKENS = "https://api.dexscreener.com/latest/dex/tokens"
SOL_MINT = "So11111111111111111111111111111111111111112"
FEE_LAMPORTS_PER_ORDER = 10_000     # conservative fee reserve per pending order


def _guard():
    if not config.sentinel_enabled():
        raise HTTPException(403, "SENTINEL_ENABLED=false")


async def _held_mints(wallet: str) -> List[Dict]:
    out = []
    for program in ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"):
        res = await rpc_call("getTokenAccountsByOwner",
                             [wallet, {"programId": program}, {"encoding": "jsonParsed"}])
        for acc in (res or {}).get("value", []):
            info = acc["account"]["data"]["parsed"]["info"]
            ui = (info.get("tokenAmount") or {}).get("uiAmount")
            if ui:
                out.append({"mint": info["mint"], "amount": ui})
    return out


async def _open_orders(wallet: str) -> Optional[List[Dict]]:
    """Trigger V2 open orders — None (unknown) without a key, never []-as-fact."""
    key = await config.jupiter_key()
    if not key:
        return None
    res = await jupiter.trigger_get("getTriggerOrders",
                                    {"user": wallet, "orderStatus": "active"}, key)
    if res.get("http_status") != 200:
        return None
    return res.get("orders") or res.get("data") or []


@router.get("/positions")
async def positions(wallet: str, request: Request, authorization: Optional[str] = Header(None)):
    _guard()
    await require_user(request, authorization)
    held = await _held_mints(wallet)
    lam = await rpc_call("getBalance", [wallet])
    sol = (lam or {}).get("value", 0) / 1e9
    orders = await _open_orders(wallet)
    locked_usd = None
    if orders is not None:
        locked_usd = 0.0
        for o in orders:
            try:
                locked_usd += float(o.get("makingAmountUsd") or o.get("lockedUsd") or 0)
            except (TypeError, ValueError):
                pass
    price_map = await jupiter.prices([SOL_MINT] + [h["mint"] for h in held])
    for h in held:
        p = (price_map.get(h["mint"]) or {}).get("usdPrice")
        h["usd"] = round(h["amount"] * p, 2) if p else None
    sol_price = (price_map.get(SOL_MINT) or {}).get("usdPrice")
    return {"wallet": wallet,
            "spendable": {"sol": sol, "sol_usd": round(sol * sol_price, 2) if sol_price else None,
                          "tokens": held},
            "locked": {"usd": locked_usd, "open_orders": orders,
                       "note": "vault-locked while trigger orders are open" if orders else
                               "open orders unknown — Jupiter key not set" if orders is None else None}}


class WatchBody(BaseModel):
    mint: str
    note: str = ""


@router.post("/watch")
async def watch_add(body: WatchBody, request: Request, authorization: Optional[str] = Header(None)):
    _guard()
    user = await require_user(request, authorization)
    doc = {"id": str(uuid.uuid4()), "owner": user["user_id"], "mint": body.mint.strip(),
           "note": body.note[:200], "added_at": datetime.now(timezone.utc)}
    await db.defi_sentinel_watch.update_one(
        {"owner": user["user_id"], "mint": doc["mint"]}, {"$set": doc}, upsert=True)
    return {"ok": True, "mint": doc["mint"]}


@router.get("/watch")
async def watch_list(request: Request, authorization: Optional[str] = Header(None)):
    _guard()
    user = await require_user(request, authorization)
    rows = await db.defi_sentinel_watch.find({"owner": user["user_id"]}, {"_id": 0}).to_list(200)
    for r in rows:
        if hasattr(r.get("added_at"), "isoformat"):
            r["added_at"] = r["added_at"].isoformat()
    return {"watch": rows}


async def _liquidity_usd(mint: str) -> Optional[float]:
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=6.0)) as c:
            r = await c.get(f"{DS_TOKENS}/{mint}")
            r.raise_for_status()
            pairs = (r.json() or {}).get("pairs") or []
    except Exception:
        return None
    best = 0.0
    for p in pairs:
        try:
            best = max(best, float((p.get("liquidity") or {}).get("usd") or 0))
        except (TypeError, ValueError):
            pass
    return best or None


@router.get("/alerts")
async def alerts(wallet: str, request: Request, authorization: Optional[str] = Header(None)):
    """Each poll re-runs shield on HELD + WATCHED tokens, checks liquidity drops,
    fee headroom and trigger-order fills/expiries. Read-only."""
    _guard()
    user = await require_user(request, authorization)
    now = datetime.now(timezone.utc)
    held = await _held_mints(wallet)
    watch_rows = await db.defi_sentinel_watch.find({"owner": user["user_id"]}).to_list(200)
    mints = sorted({h["mint"] for h in held} | {w["mint"] for w in watch_rows})
    out: List[Dict] = []

    # 1) shield degradation — re-run EVERY poll, diff against the stored last state
    shield_res = await jupiter.shield(mints) if mints else {"warnings": {}}
    if not shield_res.get("error"):
        warnings = shield_res.get("warnings") or {}
        for m in mints:
            cur = sorted(w.get("type", "") for w in warnings.get(m, []))
            prev_doc = await db.defi_shield_state.find_one({"owner": user["user_id"], "mint": m})
            prev = sorted(prev_doc.get("types", [])) if prev_doc else None
            if prev is not None and set(cur) - set(prev):
                out.append({"kind": "shield_degradation", "mint": m,
                            "new_flags": sorted(set(cur) - set(prev)),
                            "message": f"shield added flags on a held/watched token: {sorted(set(cur) - set(prev))}"})
            await db.defi_shield_state.update_one(
                {"owner": user["user_id"], "mint": m},
                {"$set": {"types": cur, "checked_at": now}}, upsert=True)
    else:
        out.append({"kind": "source_unreachable", "message": "shield unreachable this poll — "
                    "no verdicts assumed (unknown ≠ safe)"})

    # 2) liquidity drop > SENTINEL_LIQ_DROP_PCT vs the newest snapshot OLDER than 24h
    #    (poll-to-poll noise never trips it; <24h of history = no verdict, not a false one)
    day_cutoff = now - timedelta(hours=24)
    for m in mints:
        liq = await _liquidity_usd(m)
        if liq is None:
            continue
        baseline = await db.defi_liq_snapshots.find_one(
            {"mint": m, "ts": {"$lte": day_cutoff}}, sort=[("ts", -1)])
        await db.defi_liq_snapshots.insert_one({"mint": m, "liquidity_usd": liq, "ts": now})
        if baseline and baseline.get("liquidity_usd"):
            drop = (baseline["liquidity_usd"] - liq) / baseline["liquidity_usd"] * 100
            if drop > config.sentinel_liq_drop_pct():
                out.append({"kind": "liquidity_drop", "mint": m,
                            "drop_pct": round(drop, 1),
                            "message": f"liquidity down {drop:.0f}% vs the 24h-old snapshot"})
    # retention: snapshots older than 8 days serve no baseline — prune
    await db.defi_liq_snapshots.delete_many({"ts": {"$lt": now - timedelta(days=8)}})

    # 3) trigger fills / expiries + fee headroom
    orders = await _open_orders(wallet)
    if orders is not None:
        prev = await db.defi_order_state.find_one({"owner": user["user_id"], "wallet": wallet})
        prev_ids = set((prev or {}).get("order_ids", []))
        cur_ids = {str(o.get("orderKey") or o.get("id") or "") for o in orders}
        for gone in prev_ids - cur_ids:
            out.append({"kind": "order_closed", "order": gone,
                        "message": "a trigger order left the active set (filled, cancelled or expired)"})
        await db.defi_order_state.update_one(
            {"owner": user["user_id"], "wallet": wallet},
            {"$set": {"order_ids": sorted(cur_ids), "checked_at": now}}, upsert=True)
        lam = await rpc_call("getBalance", [wallet])
        need = len(orders) * FEE_LAMPORTS_PER_ORDER
        if (lam or {}).get("value", 0) < need:
            out.append({"kind": "low_sol_for_fees",
                        "message": f"SOL balance below the fee reserve needed for "
                                   f"{len(orders)} pending orders"})
    return {"wallet": wallet, "alerts": out, "checked_mints": mints,
            "polled_at": now.isoformat()}
