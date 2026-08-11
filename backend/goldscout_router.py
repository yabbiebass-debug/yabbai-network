"""
GoldScout surface — real opportunity scanner + real token safety + scam analysis.

Two data planes, both real, neither fabricated:
  · MARKET  — live DEX data from DexScreener (free, no key): trending tokens and pairs
    with real liquidity/volume/price. See core/market.py.
  · NEWS    — Tavily search for airdrops/quests/testnets when TAVILY_API_KEY is set;
    without it that path degrades honestly (no invented findings). See core/scout.py.

Every finding is scam-scored; the top market results get a real on-chain safety check
(Solana RPC authority + GoPlus, free). Findings persist to Mongo (durable across
Emergent publishes) instead of the old JSON file that was wiped on every deploy.

Read/analysis only. Nothing here moves funds — the /approve route records a wallet
message-signature; the user executes any swap themselves in their own wallet.
"""

import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Request, HTTPException, Header
from pydantic import BaseModel

from goldscout.core.scout import run_scout, clear_findings as _legacy_clear
from goldscout.core.scam_analysis import GOLDEN_RULES, analyze_opportunity
from goldscout.core import store
from goldscout.core import market as market_mod
from goldscout.core import safety as safety_mod
from network_db import db
from auth_router import _session_and_user

router = APIRouter(prefix="/api/goldscout", tags=["goldscout"])

_state = {"running": False, "last_run": None, "last_summary": None}


async def _require_user(request: Request, authorization: Optional[str]):
    session, user = await _session_and_user(request, authorization)
    if not user:
        raise HTTPException(401, "Not authenticated")
    if not session.get("mfa_verified"):
        raise HTTPException(403, "2FA required")
    return user


@router.get("/health")
async def health():
    return {"ok": True, "app": "goldscout", "version": "3.0.0",
            "market_source": "dexscreener (live, no key)",
            "safety_sources": ["solana_rpc", "goplus"],
            "news_scout": "tavily" if os.getenv("TAVILY_API_KEY") else "disabled (set TAVILY_API_KEY)",
            "persistence": "mongo",
            "ts": datetime.now(timezone.utc).isoformat()}


# ── real market scan (live DEX data, no key) ──────────────────────────────────
class MarketBody(BaseModel):
    queries: Optional[list[str]] = None
    chain: Optional[str] = None
    deep_safety_top: int = 3
    limit: int = 40


@router.post("/market/scan")
async def market_scan(body: MarketBody, request: Request, authorization: Optional[str] = Header(None)):
    user = await _require_user(request, authorization)
    result = await market_mod.scan_market(
        queries=body.queries, chain=body.chain,
        deep_safety_top=body.deep_safety_top, limit=body.limit)
    added = await store.add_findings(user["user_id"], result["results"])
    await store.record_scan(user["user_id"], "market", {**result["summary"], "new_kept": added})
    result["new_kept"] = added
    return result


# ── real single-token safety (on-chain + GoPlus + heuristics + optional AI) ────
class SafetyBody(BaseModel):
    token_address: str
    chain: str = "solana"
    name: str = ""
    with_ai: bool = False


@router.post("/token/safety")
async def token_safety(body: SafetyBody, request: Request, authorization: Optional[str] = Header(None)):
    await _require_user(request, authorization)
    if not body.token_address.strip():
        raise HTTPException(400, "token_address is required")
    return await safety_mod.check_token(
        body.token_address.strip(), chain=body.chain, name=body.name, with_ai=body.with_ai)


# ── news scout (Tavily) — now persists to Mongo per user ──────────────────────
@router.post("/api/scout/run")
async def scout_run(background_tasks: BackgroundTasks, request: Request,
                    authorization: Optional[str] = Header(None)):
    user = await _require_user(request, authorization)
    if not os.getenv("TAVILY_API_KEY"):
        return {"ok": False, "message": "News scout needs TAVILY_API_KEY. The live MARKET scan "
                                        "(/market/scan) works without it."}
    if _state["running"]:
        return {"ok": False, "message": "Scout already running — check status"}

    uid = user["user_id"]

    async def _run():
        _state["running"] = True
        try:
            result = await run_scout()
            await store.add_findings(uid, result.get("findings", []))
            await store.record_scan(uid, "news", result.get("summary", {}))
            _state["last_summary"] = result.get("summary")
            _state["last_run"] = datetime.now(timezone.utc).isoformat()
        except Exception as e:
            _state["last_summary"] = {"error": str(e)}
        finally:
            _state["running"] = False

    background_tasks.add_task(_run)
    return {"ok": True, "message": "News scout started — results at /api/scout/findings"}


@router.get("/api/scout/status")
async def scout_status():
    return {"running": _state["running"], "last_run": _state["last_run"],
            "last_summary": _state["last_summary"],
            "tavily_configured": bool(os.getenv("TAVILY_API_KEY"))}


@router.get("/api/scout/findings")
async def scout_findings(request: Request, risk: str = "all", category: str = "all",
                         authorization: Optional[str] = Header(None)):
    user = await _require_user(request, authorization)
    findings = await store.load_findings(user["user_id"], risk_filter=risk, category=category)
    return {"total": len(findings), "findings": findings}


@router.get("/scans")
async def scans(request: Request, authorization: Optional[str] = Header(None)):
    user = await _require_user(request, authorization)
    return {"scans": await store.recent_scans(user["user_id"])}


@router.post("/api/scout/clear")
async def scout_clear(request: Request, authorization: Optional[str] = Header(None)):
    user = await _require_user(request, authorization)
    n = await store.clear_findings(user["user_id"])
    return {"ok": True, "cleared": n}


@router.get("/api/scout/rules")
async def scout_rules():
    return {"rules": GOLDEN_RULES}


# ── watchlist ─────────────────────────────────────────────────────────────────
class WatchBody(BaseModel):
    token_address: str = ""
    url: str = ""
    name: str = ""
    note: str = ""


@router.post("/watch")
async def watch_add(body: WatchBody, request: Request, authorization: Optional[str] = Header(None)):
    user = await _require_user(request, authorization)
    if not (body.token_address or body.url):
        raise HTTPException(400, "Provide a token_address or url to watch.")
    return {"ok": True, "watch": await store.add_watch(user["user_id"], body.model_dump())}


@router.get("/watch")
async def watch_list(request: Request, authorization: Optional[str] = Header(None)):
    user = await _require_user(request, authorization)
    return {"watchlist": await store.list_watch(user["user_id"])}


@router.delete("/watch/{watch_id}")
async def watch_remove(watch_id: str, request: Request, authorization: Optional[str] = Header(None)):
    user = await _require_user(request, authorization)
    ok = await store.remove_watch(user["user_id"], watch_id)
    if not ok:
        raise HTTPException(404, "Watch item not found.")
    return {"ok": True}


@router.post("/indexes")
async def ensure_indexes(request: Request, authorization: Optional[str] = Header(None)):
    await _require_user(request, authorization)
    return {"ok": True, "indexed": await store.ensure_indexes()}


# ── opportunities + human-approved, wallet-signed execution ───────────────────
@router.get("/opportunities")
async def opportunities(request: Request, authorization: Optional[str] = Header(None)):
    """Owner-scoped scout findings (scam-filtered). Backed by the Mongo store —
    populated by /market/scan or the news scout."""
    user = await _require_user(request, authorization)
    findings = await store.load_findings(user["user_id"], risk_filter="all", category="all")
    return {"total": len(findings), "findings": findings,
            "tavily_configured": bool(os.getenv("TAVILY_API_KEY"))}


class AnalyzeBody(BaseModel):
    title: str = ""
    url: str = ""
    description: str = ""


@router.post("/analyze")
async def analyze(body: AnalyzeBody):
    """Run any opportunity through the scam engine before you touch it."""
    result = analyze_opportunity({"title": body.title, "url": body.url,
                                  "description": body.description, "snippet": body.description})
    return {"ok": True, "analysis": result}


class ApproveBody(BaseModel):
    opportunity: dict
    chain: str
    address: str
    message: str
    signature: str


@router.post("/approve")
async def approve(body: ApproveBody, request: Request, authorization: Optional[str] = Header(None)):
    """Record a human, wallet-SIGNED approval of an opportunity (message signature only —
    no funds move here; the actual swap is executed by the user in their wallet)."""
    user = await _require_user(request, authorization)
    opp = body.opportunity or {}
    analysis = analyze_opportunity({
        "title": opp.get("title", ""), "url": opp.get("url", ""),
        "description": opp.get("description", ""), "snippet": opp.get("description", "")})
    if analysis["riskScore"] >= 70:
        raise HTTPException(400, "Blocked — this opportunity scored HIGH risk and cannot be approved.")
    doc = {
        "user_id": user["user_id"], "email": user.get("email"),
        "opportunity": opp, "chain": body.chain, "address": body.address,
        "message": body.message, "signature": body.signature,
        "risk_score": analysis["riskScore"], "risk_level": analysis["riskLevel"],
        "approved_at": datetime.now(timezone.utc),
    }
    res = await db.goldscout_approvals.insert_one(doc)
    return {"ok": True, "approval_id": str(res.inserted_id),
            "risk_score": analysis["riskScore"], "risk_level": analysis["riskLevel"]}


@router.get("/approvals")
async def approvals(request: Request, authorization: Optional[str] = Header(None)):
    user = await _require_user(request, authorization)
    rows = await db.goldscout_approvals.find(
        {"user_id": user["user_id"]}, {"_id": 0, "signature": 0}).sort("approved_at", -1).to_list(100)
    for r in rows:
        r["approved_at"] = r["approved_at"].isoformat() if hasattr(r.get("approved_at"), "isoformat") else r.get("approved_at")
    return {"approvals": rows}
