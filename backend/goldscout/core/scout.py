"""
GoldScout -- Scout Engine (Python port of goldScout.js)

Runs Tavily searches, filters every result through scam analysis before storing.
Drops guaranteed-theft hits, flags risky ones, sorts safest-first.
Findings persist to a local JSON file -- no database required.
"""

import os
import time
import httpx
from datetime import datetime, timezone
from typing import List, Dict

from .scam_analysis import analyze_opportunity, label_type

TAVILY_URL = "https://api.tavily.com/search"
TAVILY_USAGE_URL = "https://api.tavily.com/usage"


def _key(explicit: str = "") -> str:
    return (explicit or os.getenv("TAVILY_API_KEY", "") or "").strip()

# Honest search queries -- target legitimate activity types, not "free money" bait
SEARCHES = [
    {"q": "legitimate crypto airdrops this month verified projects",  "category": "airdrop",  "chain": "multi"},
    {"q": "active testnet incentive programs ethereum arbitrum 2026", "category": "testnet",  "chain": "ethereum"},
    {"q": "Solana ecosystem airdrop eligibility checker official",    "category": "airdrop",  "chain": "solana"},
    {"q": "Layer3 Galxe active quests rewards",                       "category": "quest",    "chain": "multi"},
    {"q": "learn to earn crypto programs official 2026",              "category": "quest",    "chain": "multi"},
    {"q": "liquid staking SOL JitoSOL mSOL official rates",           "category": "staking",  "chain": "solana"},
    {"q": "crypto points programs active 2026 official",              "category": "points",   "chain": "multi"},
    {"q": "DePIN projects earn rewards hardware official",            "category": "depin",    "chain": "multi"},
]


async def _tavily_search(client: httpx.AsyncClient, query: str, max_results: int = 5,
                          api_key: str = "") -> List[Dict]:
    key = _key(api_key)
    if not key:
        raise RuntimeError("TAVILY_API_KEY not set")
    resp = await client.post(TAVILY_URL, json={
        "api_key": key,
        "query": query,
        "max_results": max_results,
        "search_depth": "basic",
        "include_answer": False,
    })
    resp.raise_for_status()
    return resp.json().get("results", [])


async def run_scout(max_per_search: int = 5, api_key: str = "") -> Dict:
    """Run the full news scout -- search --> analyze --> return findings + summary.

    ``api_key`` (optional) lets the caller pass the current Tavily key from Mongo
    settings; falls back to the ``TAVILY_API_KEY`` env var when blank.

    Persistence is the caller's job (the router writes findings to Mongo). This
    function holds no local state, so nothing is lost when Emergent wipes the FS.
    """
    findings: List[Dict] = []
    seen_urls: set = set()

    scanned = 0
    flagged = 0
    new_kept = 0
    errors   = []

    async with httpx.AsyncClient(timeout=20) as client:
      for search in SEARCHES:
        try:
            results = await _tavily_search(client, search["q"], max_per_search, api_key)
        except Exception as e:
            errors.append(f"{search['category']}: {e}")
            continue

        for r in results:
            scanned += 1
            url = r.get("url", "")
            if url in seen_urls:
                continue

            opp = {
                "title":    r.get("title", ""),
                "url":      url,
                "snippet":  r.get("content", r.get("snippet", "")),
                "category": search["category"],
                "chain":    search["chain"],
            }
            analysis = analyze_opportunity(opp)

            # Drop worst -- don't even surface guaranteed-theft results
            if analysis["riskScore"] >= 85:
                flagged += 1
                continue
            if analysis["riskLevel"] in ("critical", "high"):
                flagged += 1

            record = {
                **opp,
                "risk_score":       analysis["riskScore"],
                "risk_level":       analysis["riskLevel"],
                "red_flags":        analysis["flags"],
                "domain_trust":     analysis["domainTrust"],
                "verdict":          analysis["verdict"],
                "opportunity_type": label_type(search["category"]),
                "requires_capital": search["category"] in ("staking", "lending", "lp"),
                "is_labor":         search["category"] in ("airdrop", "testnet", "quest", "points", "depin"),
                "found_at":         datetime.now(timezone.utc).isoformat(),
                "status":           "lead",
            }
            findings.append(record)
            seen_urls.add(url)
            new_kept += 1

    # Sort safest-looking first, newest within same score
    findings.sort(key=lambda f: (f["risk_score"], f.get("found_at", "")))

    return {
        "summary": {
            "scanned":           scanned,
            "new_kept":          new_kept,
            "flagged_as_risky":  flagged,
            "total_findings":    len(findings),
            "errors":            errors,
            "note": (
                f"Scanned {scanned} web results. {flagged} carried scam red flags. "
                "These are RESEARCH LEADS with risk ratings -- verify each independently and "
                "act manually in your own wallet. No auto-execution. Absence of flags ≠ safe."
            ),
        },
        "findings": findings,
    }


# Findings now persist in Mongo (see core/store.py). These remain only so older
# imports don't break; the router uses the store directly.
def clear_findings():
    return None


# ── Tavily /usage — real remaining credits ─────────────────────────────────────
_usage_cache = {"ts": 0.0, "key_hash": None, "data": None}


async def tavily_usage(api_key: str = "") -> Dict:
    """Ping Tavily's /usage endpoint for real usage/limit numbers.

    Cached 5 minutes per key (Tavily throttles /usage to 10 req / 10 min).
    Never a fabricated 0 — ``ok=False`` on any failure with ``error`` populated.
    """
    key = _key(api_key)
    if not key:
        return {"configured": False, "ok": False, "error": "no key set"}
    key_hash = hash(key)
    if (time.time() - _usage_cache["ts"] < 300
            and _usage_cache["key_hash"] == key_hash
            and _usage_cache["data"] is not None):
        return _usage_cache["data"]
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            r = await client.get(TAVILY_USAGE_URL,
                                 headers={"Authorization": f"Bearer {key}"})
    except Exception as e:
        return {"configured": True, "ok": False, "error": f"network: {e}"}
    if r.status_code in (401, 403):
        return {"configured": True, "ok": False, "error": f"auth {r.status_code}"}
    if r.status_code != 200:
        return {"configured": True, "ok": False, "error": f"http {r.status_code}"}
    try:
        d = r.json() or {}
    except Exception as e:
        return {"configured": True, "ok": False, "error": f"json: {e}"}
    # Tavily may wrap in {"data": {...}} or return flat; accept either.
    src = d.get("data") if isinstance(d.get("data"), dict) else d
    usage = src.get("usage")
    limit = src.get("limit")
    remaining = None
    if isinstance(limit, (int, float)) and isinstance(usage, (int, float)):
        remaining = max(0, int(limit) - int(usage))
    out = {
        "configured": True, "ok": True,
        "usage": usage, "limit": limit, "remaining": remaining,
        "plan": src.get("plan"),
        "search_usage": src.get("search_usage"),
        "extract_usage": src.get("extract_usage"),
    }
    _usage_cache["ts"] = time.time()
    _usage_cache["key_hash"] = key_hash
    _usage_cache["data"] = out
    return out
