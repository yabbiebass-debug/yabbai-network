"""
Harvester — candidate discovery pipeline. Writes Mongo and STOPS.

Pipeline: token search + DeFiLlama → shield (failures DROPPED, never down-ranked)
→ real quote at the intended size → simulation → rank. Candidates carry NAMED,
SOURCED components only — no blended score, no projected return (N5).
This module has no path to any execution or relay route and imports none.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from network_db import db
from . import config, simulation
from .jupiter import token_search, shield, shield_verdict, prices, swap_order, llama_pools
from .authz import require_user

logger = logging.getLogger("defi.harvester")
router = APIRouter(prefix="/api/defi/harvest", tags=["defi-harvest"])

SOL_MINT = "So11111111111111111111111111111111111111112"


class ScanBody(BaseModel):
    queries: List[str] = ["SOL", "USDC", "JUP"]
    size_usd: float = 10.0


@router.post("/scan")
async def scan(body: ScanBody, request: Request, authorization: Optional[str] = Header(None)):
    if not config.harvest_enabled():
        raise HTTPException(403, "HARVEST_ENABLED=false — harvester is dark")
    user = await require_user(request, authorization)
    size_usd = min(max(0.5, body.size_usd), config.max_trade_usd())
    run_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    # 1) token search + DeFiLlama context
    raw: Dict[str, Dict] = {}
    for q in body.queries[:6]:
        for t in await token_search(q):
            if t.get("mint") and t["mint"] != SOL_MINT:
                raw.setdefault(t["mint"], t)
    pools = await llama_pools()
    llama_by_symbol = {}
    for p in pools:
        if (p.get("chain") or "").lower() == "solana" and p.get("symbol"):
            llama_by_symbol.setdefault(p["symbol"].upper(), p)

    dropped = {"shield_failed": [], "no_quote": [], "sim_failed": []}
    candidates: List[Dict] = []
    mints = list(raw.keys())[:config.harvest_max_candidates() * 2]

    # 2) shield — a failed/flagged mint is DROPPED, not down-ranked
    sh = await shield(mints)
    warnings = sh.get("warnings") or {}
    if sh.get("error"):
        raise HTTPException(503, f"shield unreachable — refusing to scan without safety data: {sh['error']}")

    sol_price_map = await prices([SOL_MINT])
    sol_price = (sol_price_map.get(SOL_MINT) or {}).get("usdPrice")
    if not sol_price:
        raise HTTPException(503, "no SOL price — cannot size quotes, fail closed")
    in_amount = int(size_usd / sol_price * 1e9)      # SOL base units for the intended size

    for mint in mints:
        t = raw[mint]
        verdict = shield_verdict(warnings.get(mint, []))
        if verdict in ("caution", "unknown"):
            dropped["shield_failed"].append(mint)
            continue
        # 3) real quote at intended size — executable price including impact
        order = await swap_order(SOL_MINT, mint, in_amount, 100)
        if order.get("error") or not order.get("outAmount"):
            dropped["no_quote"].append(mint)
            continue
        impact_bps = abs(float(order.get("priceImpactPct") or 0)) * 10_000
        # 4) simulation dry-run
        pm = await prices([SOL_MINT, mint])
        sim = simulation.evaluate_quote(
            in_amount, int(order["outAmount"]),
            float(order.get("priceImpactPct") or 0),
            (pm.get(SOL_MINT) or {}).get("usdPrice"), (pm.get(mint) or {}).get("usdPrice"),
            9, (pm.get(mint) or {}).get("decimals"))
        if not sim.get("ok"):
            dropped["sim_failed"].append(mint)
            continue
        llama = llama_by_symbol.get((t.get("symbol") or "").upper())
        candidates.append({
            "id": str(uuid.uuid4()), "run_id": run_id, "owner": user["user_id"],
            "mint": mint, "symbol": t.get("symbol"), "name": t.get("name"),
            # named, sourced components ONLY — no blended score, no projected return
            "shield_verdict": verdict,
            "price_impact_bps": round(impact_bps, 1),
            "liquidity_usd": t.get("liquidity_usd"),
            "age_days": None,     # tokens/v2 does not expose listing age — never invented
            "holder_concentration": ((t.get("audit") or {}).get("topHoldersPercentage")
                                     if isinstance(t.get("audit"), dict) else None),
            "holder_count": t.get("holder_count"),
            "organic_score": t.get("organic_score"),
            "llama_tvl_usd": (llama or {}).get("tvlUsd"),
            "quote_size_usd": size_usd,
            "simulated_divergence_bps": sim.get("divergence_bps"),
            "found_at": now,
        })
        if len(candidates) >= config.harvest_max_candidates():
            break

    # 5) rank — deterministic sort on named components (liquidity desc, impact asc)
    candidates.sort(key=lambda c: (-(c.get("liquidity_usd") or 0), c.get("price_impact_bps") or 1e9))
    if candidates:
        await db.defi_harvest_candidates.insert_many([dict(c) for c in candidates])
    logger.info(f"harvest run {run_id[:8]}: {len(candidates)} candidates, dropped "
                f"shield={len(dropped['shield_failed'])} quote={len(dropped['no_quote'])}")
    for c in candidates:
        c["found_at"] = c["found_at"].isoformat()
        c.pop("_id", None)
    # harvester writes Mongo and STOPS — no execution path exists here
    return {"ok": True, "run_id": run_id, "candidates": candidates, "dropped": dropped}


@router.get("/candidates")
async def candidates(request: Request, authorization: Optional[str] = Header(None), limit: int = 50):
    user = await require_user(request, authorization)
    rows = await db.defi_harvest_candidates.find(
        {"owner": user["user_id"]}, {"_id": 0}).sort("found_at", -1).to_list(min(limit, 200))
    for r in rows:
        if hasattr(r.get("found_at"), "isoformat"):
            r["found_at"] = r["found_at"].isoformat()
    return {"candidates": rows}
