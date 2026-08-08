"""
GoldScout surface — opportunity scanner + scam analysis.

The original server gates everything behind a single-user Google login. In the
unified network that gate lives at the hub level, so this router exposes the
scanner directly. Scout runs use Tavily when TAVILY_API_KEY is set; without it
the endpoint degrades honestly (no fabricated findings).
"""

import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Request, HTTPException, Header
from pydantic import BaseModel

from goldscout.core.scout import run_scout, get_findings, clear_findings
from goldscout.core.scam_analysis import GOLDEN_RULES, analyze_opportunity
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
    return {"ok": True, "app": "goldscout", "version": "2.0.0",
            "tavily_configured": bool(os.getenv("TAVILY_API_KEY")),
            "ts": datetime.now(timezone.utc).isoformat()}


@router.post("/api/scout/run")
async def scout_run(background_tasks: BackgroundTasks):
    if not os.getenv("TAVILY_API_KEY"):
        return {"ok": False, "message": "Scout needs TAVILY_API_KEY. Configure it in Settings to enable live scanning."}
    if _state["running"]:
        return {"ok": False, "message": "Scout already running — check status"}

    def _run():
        _state["running"] = True
        try:
            result = run_scout()
            _state["last_summary"] = result.get("summary")
            _state["last_run"] = datetime.now(timezone.utc).isoformat()
        except Exception as e:
            _state["last_summary"] = {"error": str(e)}
        finally:
            _state["running"] = False

    background_tasks.add_task(_run)
    return {"ok": True, "message": "Scout started — results at /api/scout/findings"}


@router.get("/api/scout/status")
async def scout_status():
    return {"running": _state["running"], "last_run": _state["last_run"],
            "last_summary": _state["last_summary"],
            "tavily_configured": bool(os.getenv("TAVILY_API_KEY"))}


@router.get("/api/scout/findings")
async def scout_findings(risk: str = "all", category: str = "all"):
    findings = get_findings(risk_filter=risk, category=category)
    return {"total": len(findings), "findings": findings}


@router.post("/api/scout/clear")
async def scout_clear():
    clear_findings()
    return {"ok": True, "message": "All findings cleared"}


@router.get("/api/scout/rules")
async def scout_rules():
    return {"rules": GOLDEN_RULES}


# ── opportunities + human-approved, wallet-signed execution ───────────────────
@router.get("/opportunities")
async def opportunities():
    """Live scout findings (scam-filtered). Empty until a Tavily-backed scan runs."""
    findings = get_findings(risk_filter="all", category="all")
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
