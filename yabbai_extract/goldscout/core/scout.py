"""
GoldScout -- Scout Engine (Python port of goldScout.js)

Runs Tavily searches, filters every result through scam analysis before storing.
Drops guaranteed-theft hits, flags risky ones, sorts safest-first.
Findings persist to a local JSON file -- no database required.
"""

import os
import json
import httpx
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict

from .scam_analysis import analyze_opportunity, label_type

TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
TAVILY_URL     = "https://api.tavily.com/search"
DATA_FILE      = Path(__file__).resolve().parent.parent / "data" / "findings.json"

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


def _load_findings() -> List[Dict]:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text())
        except Exception:
            pass
    return []


def _save_findings(findings: List[Dict]):
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(findings, indent=2))


def _tavily_search(query: str, max_results: int = 5) -> List[Dict]:
    if not TAVILY_API_KEY:
        raise RuntimeError("TAVILY_API_KEY not set")
    resp = httpx.post(TAVILY_URL, json={
        "api_key": TAVILY_API_KEY,
        "query": query,
        "max_results": max_results,
        "search_depth": "basic",
        "include_answer": False,
    }, timeout=20)
    resp.raise_for_status()
    return resp.json().get("results", [])


def run_scout(max_per_search: int = 5) -> Dict:
    """Run the full scout -- search --> analyze --> persist --> return summary."""
    findings = _load_findings()
    # Deduplicate by URL
    seen_urls = {f["url"] for f in findings}

    scanned = 0
    flagged = 0
    new_kept = 0
    errors   = []

    for search in SEARCHES:
        try:
            results = _tavily_search(search["q"], max_per_search)
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
    _save_findings(findings)

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


def get_findings(risk_filter: str = "all", category: str = "all") -> List[Dict]:
    findings = _load_findings()
    if risk_filter == "safe-ish":
        findings = [f for f in findings if f.get("risk_level") == "low"]
    elif risk_filter == "flagged":
        findings = [f for f in findings if f.get("risk_level") in ("high", "critical")]
    elif risk_filter != "all":
        findings = [f for f in findings if f.get("risk_level") == risk_filter]
    if category != "all":
        findings = [f for f in findings if f.get("category") == category]
    return findings


def clear_findings():
    _save_findings([])
