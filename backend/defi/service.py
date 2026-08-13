"""
Live DeFi core service — /api/defi (REPLACES the paper simulator mount).

Data plane: portfolio / tokens / shield / yields (read-only, keyless).
Quote plane: Swap V2 order + MANDATORY simulation dry-run + guardrails.
Execution plane: relays CLIENT-SIGNED transactions to Swap V2 /execute.
The backend never holds a private key (N1). All guardrails fail closed (N4).
"""

import logging
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from network_db import db
from . import config, jupiter, simulation
from .authz import require_user, rpc_call

logger = logging.getLogger("defi")
router = APIRouter(prefix="/api/defi", tags=["defi"])

VERSION = "1.0.0"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
SOL_MINT = "So11111111111111111111111111111111111111112"


@router.get("/health")
async def health():
    key = await config.jupiter_key()
    return {"ok": True, "app": "yabbai-defi", "version": VERSION,
            "live_enabled": config.live_enabled(),
            "jupiter_key_configured": bool(key),
            "allowed_mints_count": len(config.allowed_mints()),
            "caps": {"max_trade_usd": config.max_trade_usd(),
                     "daily_cap_usd": config.daily_cap_usd(),
                     "max_price_impact_bps": config.max_price_impact_bps(),
                     "max_quote_divergence_bps": config.max_quote_divergence_bps()},
            "custodial": False, "server_side_signing": False,
            "ts": datetime.now(timezone.utc).isoformat()}


@router.get("/jupiter/status")
async def jupiter_status(request: Request, authorization: Optional[str] = Header(None)):
    """Key test for the /settings card — a tiny keyed quote probe (no taker, no tx)."""
    await require_user(request, authorization)
    key = await config.jupiter_key()
    probe = await jupiter.swap_order(SOL_MINT, "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                                     1_000_000, 50, api_key=key)
    ok = probe.get("http_status") == 200 and not probe.get("error")
    return {"configured": bool(key), "probe_ok": ok,
            "http_status": probe.get("http_status"), "error": probe.get("error")}


# ── C1 data plane ──────────────────────────────────────────────────────────────
@router.get("/portfolio")
async def portfolio(wallet: str):
    wallet = (wallet or "").strip()
    if not wallet:
        raise HTTPException(400, "wallet is required")
    lam = await rpc_call("getBalance", [wallet])
    sol_amount = (lam or {}).get("value", 0) / 1e9

    tokens = []
    for program in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
        res = await rpc_call("getTokenAccountsByOwner",
                             [wallet, {"programId": program}, {"encoding": "jsonParsed"}])
        for acc in (res or {}).get("value", []):
            info = acc["account"]["data"]["parsed"]["info"]
            amt = info.get("tokenAmount") or {}
            ui = amt.get("uiAmount")
            if ui:
                tokens.append({"mint": info.get("mint"), "amount": ui,
                               "decimals": amt.get("decimals"),
                               "account": acc.get("pubkey"),
                               "program": "token-2022" if program == TOKEN_2022_PROGRAM else "spl-token"})

    price_map = await jupiter.prices([SOL_MINT] + [t["mint"] for t in tokens])
    sol_price = (price_map.get(SOL_MINT) or {}).get("usdPrice")
    sol_usd = round(sol_amount * sol_price, 2) if sol_price else None
    total = sol_usd if sol_usd is not None else None
    unpriced = []
    for t in tokens:
        p = (price_map.get(t["mint"]) or {}).get("usdPrice")
        if p:
            t["usd"] = round(t["amount"] * p, 2)
            total = (total or 0) + t["usd"]
        else:
            t["usd"] = None      # no price → —, never 0
            unpriced.append(t["mint"])
    return {"wallet": wallet,
            "sol": {"amount": round(sol_amount, 9), "usd": sol_usd},
            "tokens": sorted(tokens, key=lambda t: (t["usd"] is None, -(t["usd"] or 0))),
            "total_usd": round(total, 2) if total is not None else None,
            "unpriced_mints": unpriced,
            "ts": datetime.now(timezone.utc).isoformat()}


@router.get("/tokens")
async def tokens(q: str):
    if not (q or "").strip():
        raise HTTPException(400, "q is required")
    return {"query": q, "tokens": await jupiter.token_search(q.strip())}


@router.get("/shield")
async def shield_route(mint: str):
    mints = [m.strip() for m in (mint or "").split(",") if m.strip()]
    if not mints:
        raise HTTPException(400, "mint is required")
    res = await jupiter.shield(mints)
    if res.get("error"):
        return {"ok": False, "error": res["error"],
                "verdicts": {m: "unknown" for m in mints}}
    warnings = res.get("warnings") or {}
    return {"ok": True,
            "warnings": {m: warnings.get(m, []) for m in mints},
            "verdicts": {m: jupiter.shield_verdict(warnings.get(m, [])) for m in mints},
            "source": "jupiter shield (lite-api)"}


@router.get("/yields")
async def yields(symbol: str = "", chain: str = "Solana", limit: int = 20):
    pools = await jupiter.llama_pools()
    if not pools:
        return {"ok": False, "error": "DeFiLlama unreachable", "pools": []}
    sym = (symbol or "").upper().strip()
    out = []
    for p in pools:
        if chain and (p.get("chain") or "").lower() != chain.lower():
            continue
        if sym and sym not in (p.get("symbol") or "").upper():
            continue
        out.append({"project": p.get("project"), "symbol": p.get("symbol"),
                    "chain": p.get("chain"), "tvl_usd": p.get("tvlUsd"),
                    "apy_pct": p.get("apy"), "apy_base_pct": p.get("apyBase"),
                    "apy_reward_pct": p.get("apyReward"), "il_risk": p.get("ilRisk"),
                    "stablecoin": p.get("stablecoin"), "pool_id": p.get("pool")})
    out.sort(key=lambda x: x.get("tvl_usd") or 0, reverse=True)
    return {"ok": True, "count": len(out[:limit]), "pools": out[:limit],
            "source": "DeFiLlama public API",
            "note": "APY is variable — not guaranteed. Historical, third-party data."}


# ── C2 quote plane — every quote passes the simulation dry-run ────────────────
@router.get("/quote")
async def quote(inputMint: str, outputMint: str, amount: int,
                slippageBps: int = 50, taker: str = "",
                request: Request = None, authorization: Optional[str] = Header(None)):
    await require_user(request, authorization)
    allow = config.allowed_mints()
    if not allow:
        raise HTTPException(403, "DEFI_ALLOWED_MINTS is empty — nothing is tradeable (empty ≠ allow-all)")
    for m in (inputMint, outputMint):
        if m not in allow:
            raise HTTPException(403, f"mint {m[:12]}… is not on the allowlist")
    if amount <= 0:
        raise HTTPException(400, "amount must be positive (base units)")

    price_map = await jupiter.prices([inputMint, outputMint])
    pin, pout = price_map.get(inputMint) or {}, price_map.get(outputMint) or {}
    in_price, out_price = pin.get("usdPrice"), pout.get("usdPrice")
    in_dec, out_dec = pin.get("decimals"), pout.get("decimals")
    if not in_price or in_dec is None:
        raise HTTPException(503, "no independent price for the input mint — fail closed, no quote")
    usd_in = amount / (10 ** in_dec) * in_price
    if usd_in > config.max_trade_usd():
        raise HTTPException(403, f"trade ${usd_in:.2f} exceeds DEFI_MAX_TRADE_USD "
                                 f"${config.max_trade_usd():.2f}")

    key = await config.jupiter_key()
    order = await jupiter.swap_order(inputMint, outputMint, amount, slippageBps,
                                     taker=taker, api_key=key)
    if order.get("error"):
        return {"ok": False, "error": order["error"],
                "jupiter_key_configured": bool(key),
                "requestId": order.get("requestId")}

    impact_pct = abs(float(order.get("priceImpactPct") or 0.0))
    impact_bps = impact_pct * 10_000.0
    sim = simulation.evaluate_quote(amount, int(order.get("outAmount") or 0), impact_pct,
                                    in_price, out_price, in_dec, out_dec)
    shield_res = await jupiter.shield([outputMint])
    out_warnings = (shield_res.get("warnings") or {}).get(outputMint) if not shield_res.get("error") else None
    verdict = jupiter.shield_verdict(out_warnings)

    blocked, reason = False, None
    if impact_bps > config.max_price_impact_bps():
        blocked, reason = True, (f"price impact {impact_bps:.0f}bps exceeds cap "
                                 f"{config.max_price_impact_bps()}bps")
    elif not sim.get("ok") or sim.get("divergence_bps") is None:
        blocked, reason = True, sim.get("reason", "simulation could not run — fail closed")
    elif sim["divergence_bps"] > config.max_quote_divergence_bps():
        blocked, reason = True, (f"simulated vs quoted divergence {sim['divergence_bps']:.0f}bps "
                                 f"exceeds cap {config.max_quote_divergence_bps()}bps")

    request_id = order.get("requestId") or str(uuid.uuid4())
    await db.defi_quotes.insert_one({
        "request_id": request_id, "created_at": datetime.now(timezone.utc),
        "taker": taker, "input_mint": inputMint, "output_mint": outputMint,
        "in_amount": str(amount), "out_amount": order.get("outAmount"),
        "usd_in": round(usd_in, 4), "price_impact_bps": round(impact_bps, 1),
        "slippage_bps": slippageBps, "simulation": sim, "shield_verdict": verdict,
        "blocked": blocked, "block_reason": reason, "executed": False,
    })
    logger.info(f"defi quote {request_id[:13]} {inputMint[:6]}→{outputMint[:6]} "
                f"${usd_in:.2f} impact={impact_bps:.0f}bps blocked={blocked}")

    resp = {"ok": True, "requestId": request_id, "blocked": blocked, "block_reason": reason,
            "quote": {"in_amount": str(amount), "out_amount": order.get("outAmount"),
                      "other_amount_threshold": order.get("otherAmountThreshold"),
                      "price_impact_pct": order.get("priceImpactPct"),
                      "price_impact_bps": round(impact_bps, 1),
                      "usd_in": round(usd_in, 4), "route_plan": order.get("routePlan"),
                      "slippage_bps": order.get("slippageBps", slippageBps),
                      "fee_bps": order.get("feeBps")},
            "simulation": sim,
            "shield": {"verdict": verdict, "warnings": out_warnings},
            "live_enabled": config.live_enabled(),
            "jupiter_key_configured": bool(key)}
    if not blocked and order.get("transaction"):
        resp["transaction"] = order["transaction"]     # signable only when clean
    return resp


# ── C3 execution plane ─────────────────────────────────────────────────────────
class ExecuteBody(BaseModel):
    signedTransaction: str
    requestId: str
    wallet: str = ""


@router.post("/execute")
async def execute(body: ExecuteBody, request: Request,
                  authorization: Optional[str] = Header(None)):
    # Master switch FIRST — unconditional 403 while dark. This is the shipping default.
    if not config.live_enabled():
        raise HTTPException(403, "DEFI_LIVE_ENABLED=false — live execution is OFF. "
                                 "Quotes and simulation still work.")
    await require_user(request, authorization)

    # ATOMIC claim: flips executed False→True before the relay so a concurrent
    # duplicate requestId loses the race and gets 409 (no double relay).
    q = await db.defi_quotes.find_one_and_update(
        {"request_id": body.requestId, "executed": False},
        {"$set": {"executed": True}})
    if not q:
        exists = await db.defi_quotes.find_one({"request_id": body.requestId})
        if not exists:
            raise HTTPException(400, "unknown requestId — quotes must originate from this backend")
        raise HTTPException(409, "this quote was already executed")
    if q.get("blocked"):
        raise HTTPException(403, f"quote was blocked: {q.get('block_reason')}")
    created = q.get("created_at")
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - created > timedelta(seconds=60):
        raise HTTPException(410, "quote expired (>60s) — request a fresh quote")

    # server-side rolling 24h notional cap, computed from PERSISTED history
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    agg = await db.defi_trades.aggregate([
        {"$match": {"ts": {"$gte": since}, "outcome": {"$ne": "failed"}}},
        {"$group": {"_id": None, "usd": {"$sum": "$usd_in"}}}]).to_list(1)
    spent = (agg[0]["usd"] if agg else 0) or 0
    if spent + (q.get("usd_in") or 0) > config.daily_cap_usd():
        raise HTTPException(403, f"daily cap: ${spent:.2f} traded in 24h + this trade "
                                 f"exceeds DEFI_DAILY_CAP_USD ${config.daily_cap_usd():.2f}")

    key = await config.jupiter_key()
    result = await jupiter.swap_execute(body.signedTransaction, body.requestId, api_key=key)
    outcome = "success" if (result.get("status") or "").lower() == "success" else \
              ("failed" if result.get("error") or result.get("http_status", 500) >= 400 else "submitted")
    trade = {"ts": datetime.now(timezone.utc), "wallet": body.wallet or q.get("taker"),             "request_id": body.requestId,
             "pair": f"{q.get('input_mint')}→{q.get('output_mint')}",
             "in_amount": q.get("in_amount"), "out_amount_quoted": q.get("out_amount"),
             "usd_in": q.get("usd_in"), "price_impact_bps": q.get("price_impact_bps"),
             "simulated_out": (q.get("simulation") or {}).get("simulated_out_base"),
             "realized_out": (result.get("events") or {}).get("outAmount") or result.get("outAmount"),
             "signature": result.get("signature"), "outcome": outcome,
             "jupiter_status": result.get("status"), "jupiter_error": result.get("error")}
    await db.defi_trades.insert_one(trade)
    await db.defi_quotes.update_one({"request_id": body.requestId}, {"$set": {"executed": True}})
    logger.warning(f"DEFI EXECUTE relayed: {trade['pair']} ${trade['usd_in']} outcome={outcome} "
                   f"sig={str(trade.get('signature'))[:20]}")
    return {"ok": outcome != "failed", "outcome": outcome,
            "signature": result.get("signature"), "status": result.get("status"),
            "error": result.get("error")}


@router.get("/history")
async def history(request: Request, authorization: Optional[str] = Header(None), limit: int = 50):
    await require_user(request, authorization)
    rows = await db.defi_trades.find({}, {"_id": 0}).sort("ts", -1).to_list(min(limit, 200))
    for r in rows:
        if hasattr(r.get("ts"), "isoformat"):
            r["ts"] = r["ts"].isoformat()
    return {"trades": rows}
