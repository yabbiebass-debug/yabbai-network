"""
GoldScout -- Scout Engine (Python port of goldScout.js)

Runs Tavily searches, filters every result through scam analysis before storing.
Drops guaranteed-theft hits, flags risky ones, sorts safest-first.
Findings persist to a local JSON file -- no database required.
"""

import os
import httpx
from datetime import datetime, timezone
from typing import List, Dict

from .scam_analysis import analyze_opportunity, label_type

TAVILY_URL = "https://api.tavily.com/search"


def _key() -> str:
    return os.getenv("TAVILY_API_KEY", "")

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


async def _tavily_search(client: httpx.AsyncClient, query: str, max_results: int = 5) -> List[Dict]:
    if not _key():
        raise RuntimeError("TAVILY_API_KEY not set")
    resp = await client.post(TAVILY_URL, json={
        "api_key": _key(),
        "query": query,
        "max_results": max_results,
        "search_depth": "basic",
        "include_answer": False,
    })
    resp.raise_for_status()
    return resp.json().get("results", [])


async def run_scout(max_per_search: int = 5) -> Dict:
    """Run the full news scout -- search --> analyze --> return findings + summary.

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
            results = await _tavily_search(client, search["q"], max_per_search)
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
