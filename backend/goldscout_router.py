"""
GoldScout surface — opportunity scanner + scam analysis.

The original server gates everything behind a single-user Google login. In the
unified network that gate lives at the hub level, so this router exposes the
scanner directly. Scout runs use Tavily when TAVILY_API_KEY is set; without it
the endpoint degrades honestly (no fabricated findings).
"""

import os
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks

from goldscout.core.scout import run_scout, get_findings, clear_findings
from goldscout.core.scam_analysis import GOLDEN_RULES, analyze_opportunity

router = APIRouter(prefix="/api/goldscout", tags=["goldscout"])

_state = {"running": False, "last_run": None, "last_summary": None}


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
