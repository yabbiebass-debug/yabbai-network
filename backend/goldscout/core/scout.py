"""
GoldScout -- Scout Engine (Python port of goldScout.js)

Runs Tavily searches, filters every result through scam analysis before storing.
Drops guaranteed-theft hits, flags risky ones, sorts safest-first.
Findings persist to a local JSON file -- no database required.
"""

import os
import time
import hashlib
import httpx
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Tuple

from .scam_analysis import analyze_opportunity, label_type
from network_db import db, get_raw_settings

TAVILY_URL = "https://api.tavily.com/search"
TAVILY_USAGE_URL = "https://api.tavily.com/usage"

# Tavily list-price credit cost by depth. Basic = 1 credit / call, advanced = 2.
DEPTH_COST = {"basic": 1, "advanced": 2}
DEFAULT_CACHE_TTL_HOURS = 24
DEFAULT_MONTHLY_LIMIT = 100


class BudgetExceeded(Exception):
    """Raised when a Tavily call would push the month past its credit ceiling."""


def _key(explicit: str = "") -> str:
    return (explicit or os.getenv("TAVILY_API_KEY", "") or "").strip()


def _month_id(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y-%m")


def _cache_id(query: str, depth: str) -> str:
    return hashlib.sha1(f"{depth}::{query}".encode("utf-8")).hexdigest()

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


async def _month_usage() -> Dict:
    """Current month's Tavily credit ledger (local, authoritative for enforcement)."""
    mid = _month_id()
    doc = await db.tavily_credits.find_one({"_id": mid}) or {}
    return {
        "month": mid,
        "used": int(doc.get("used", 0)),
        "calls": int(doc.get("calls", 0)),
        "cache_hits": int(doc.get("cache_hits", 0)),
        "last_call_at": doc.get("last_call_at"),
    }


async def _budget(monthly_limit: int) -> Dict:
    u = await _month_usage()
    used = u["used"]
    remaining = max(0, monthly_limit - used)
    pct = round((used / monthly_limit) * 100, 1) if monthly_limit > 0 else 0
    return {**u, "limit": monthly_limit, "remaining": remaining, "pct_used": pct,
            "warn_80pct": pct >= 80, "exhausted": remaining == 0}


async def _record_call(depth: str, ok: bool, credits: int) -> None:
    now = datetime.now(timezone.utc)
    upd = {"$set": {"last_call_at": now.isoformat()},
           "$inc": {"calls": 1, "used": credits if ok else 0}}
    await db.tavily_credits.update_one({"_id": _month_id(now)}, upd, upsert=True)


async def _record_cache_hit() -> None:
    await db.tavily_credits.update_one(
        {"_id": _month_id()}, {"$inc": {"cache_hits": 1}}, upsert=True)


async def _cache_get(query: str, depth: str, ttl_hours: int):
    doc = await db.tavily_search_cache.find_one({"_id": _cache_id(query, depth)})
    if not doc:
        return None
    cached_at = doc.get("cached_at")
    if not isinstance(cached_at, datetime):
        return None
    age = datetime.now(timezone.utc) - cached_at.replace(tzinfo=timezone.utc)
    if age > timedelta(hours=ttl_hours):
        return None
    return {"results": doc.get("results", []), "cached_at": cached_at.isoformat(),
            "age_seconds": int(age.total_seconds())}


async def _cache_put(query: str, depth: str, results: List[Dict]) -> None:
    await db.tavily_search_cache.update_one(
        {"_id": _cache_id(query, depth)},
        {"$set": {"query": query, "depth": depth, "results": results,
                  "cached_at": datetime.now(timezone.utc)}},
        upsert=True)


async def _tavily_search(client: httpx.AsyncClient, query: str, max_results: int = 5,
                          api_key: str = "", depth: str = "basic",
                          cache_ttl_hours: int = DEFAULT_CACHE_TTL_HOURS,
                          monthly_limit: int = DEFAULT_MONTHLY_LIMIT,
                          ) -> Tuple[List[Dict], Dict]:
    """Return ``(results, meta)`` where meta describes credit + cache handling.

    meta = {"from_cache": bool, "credits": int, "cached_at": iso|None,
            "age_seconds": int|None, "depth": str}
    Raises ``BudgetExceeded`` when the month's credit ceiling would be crossed.
    """
    key = _key(api_key)
    if not key:
        raise RuntimeError("TAVILY_API_KEY not set")
    depth = depth if depth in DEPTH_COST else "basic"
    # 1) cache
    hit = await _cache_get(query, depth, cache_ttl_hours)
    if hit is not None:
        await _record_cache_hit()
        return hit["results"], {"from_cache": True, "credits": 0,
                                 "cached_at": hit["cached_at"],
                                 "age_seconds": hit["age_seconds"], "depth": depth}
    # 2) budget guard — check BEFORE spending
    cost = DEPTH_COST[depth]
    b = await _budget(monthly_limit)
    if b["remaining"] < cost:
        raise BudgetExceeded(
            f"monthly Tavily budget exhausted ({b['used']}/{monthly_limit} used, "
            f"resets {_month_id()} → next month)")
    # 3) live call
    resp = await client.post(TAVILY_URL, json={
        "api_key": key,
        "query": query,
        "max_results": max_results,
        "search_depth": depth,
        "include_answer": False,
    })
    ok = 200 <= resp.status_code < 300
    await _record_call(depth, ok, cost)
    resp.raise_for_status()
    results = resp.json().get("results", [])
    await _cache_put(query, depth, results)
    return results, {"from_cache": False, "credits": cost,
                     "cached_at": None, "age_seconds": None, "depth": depth}


async def run_scout(max_per_search: int = 5, api_key: str = "",
                    depth: str = "basic",
                    cache_ttl_hours: int = DEFAULT_CACHE_TTL_HOURS,
                    monthly_limit: int = DEFAULT_MONTHLY_LIMIT) -> Dict:
    """Run the full news scout — search --> analyze --> return findings + summary.

    Cache-first, budget-guarded. When the monthly ceiling is hit mid-run the loop
    stops and the summary says so plainly — never falls back to stale results
    presented as fresh.
    """
    findings: List[Dict] = []
    seen_urls: set = set()

    scanned = 0
    flagged = 0
    new_kept = 0
    errors: List[str] = []
    credits_used = 0
    cache_hits = 0
    live_calls = 0
    budget_stopped = False

    async with httpx.AsyncClient(timeout=20) as client:
      for search in SEARCHES:
        try:
            results, meta = await _tavily_search(
                client, search["q"], max_per_search, api_key,
                depth=depth, cache_ttl_hours=cache_ttl_hours,
                monthly_limit=monthly_limit)
        except BudgetExceeded as e:
            errors.append(f"budget: {e}")
            budget_stopped = True
            break
        except Exception as e:
            errors.append(f"{search['category']}: {e}")
            continue
        if meta["from_cache"]:
            cache_hits += 1
        else:
            live_calls += 1
            credits_used += meta["credits"]

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
                "cached":           meta["from_cache"],
                "cached_at":        meta["cached_at"],
                "cached_age_secs":  meta["age_seconds"],
                "status":           "lead",
            }
            findings.append(record)
            seen_urls.add(url)
            new_kept += 1

    findings.sort(key=lambda f: (f["risk_score"], f.get("found_at", "")))

    note_parts = [
        f"Scanned {scanned} web results. {flagged} carried scam red flags.",
        f"Tavily spend this run: {credits_used} credits ({live_calls} live calls, "
        f"{cache_hits} served from cache, depth={depth}).",
    ]
    if budget_stopped:
        note_parts.append(
            "STOPPED EARLY — monthly Tavily budget exhausted. No stale fallback: "
            "these are only the queries that fit inside the remaining credits.")
    note_parts.append(
        "These are RESEARCH LEADS with risk ratings — verify each independently and "
        "act manually in your own wallet. No auto-execution. Absence of flags ≠ safe.")

    return {
        "summary": {
            "scanned":           scanned,
            "new_kept":          new_kept,
            "flagged_as_risky":  flagged,
            "total_findings":    len(findings),
            "credits_used":      credits_used,
            "cache_hits":        cache_hits,
            "live_calls":        live_calls,
            "depth":             depth,
            "budget_stopped":    budget_stopped,
            "errors":            errors,
            "note": " ".join(note_parts),
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


# ── combined status: local ledger (authoritative) + live Tavily numbers ────────
def scan_cost(depth: str = "basic") -> Dict:
    """Static cost profile of one full news scout run."""
    d = depth if depth in DEPTH_COST else "basic"
    q = len(SEARCHES)
    return {"queries_per_scan": q, "depth": d, "credits_per_call": DEPTH_COST[d],
            "credits_per_scan": q * DEPTH_COST[d]}


def project_monthly_credits(interval_secs: int, depth: str = "basic") -> Dict:
    """How many credits per calendar month a schedule at ``interval_secs`` costs.

    A month averages ~30.44 days; we surface the exact math so the user sees it.
    """
    per_scan = scan_cost(depth)["credits_per_scan"]
    if interval_secs <= 0:
        return {"interval_secs": interval_secs, "depth": depth,
                "scans_per_month": 0, "credits_per_month": 0,
                "note": "scheduler disabled — on-demand only"}
    seconds_per_month = 30.44 * 24 * 3600
    scans = seconds_per_month / interval_secs
    return {
        "interval_secs": interval_secs, "depth": depth,
        "scans_per_month": round(scans, 2),
        "credits_per_scan": per_scan,
        "credits_per_month": round(scans * per_scan, 1),
    }


async def credit_status(api_key: str = "", monthly_limit: int = DEFAULT_MONTHLY_LIMIT,
                        depth: str = "basic",
                        cache_ttl_hours: int = DEFAULT_CACHE_TTL_HOURS) -> Dict:
    """One combined view: local monthly ledger + live Tavily /usage + scan cost.

    ``monthly`` is enforcement (local, per calendar month, ceiling = monthly_limit).
    ``live`` is Tavily's own counter, cached 5min — surfaces divergence but is not
    used for enforcement (Tavily /usage is rate-limited).
    """
    monthly = await _budget(monthly_limit)
    live = await tavily_usage(api_key) if _key(api_key) else {
        "configured": False, "ok": False, "error": "no key set"}
    cost = scan_cost(depth)
    scans_remaining = (monthly["remaining"] // cost["credits_per_scan"]
                       if cost["credits_per_scan"] > 0 else None)
    return {
        "configured": bool(_key(api_key)),
        "monthly": monthly,                # local ledger (authoritative)
        "live": live,                       # Tavily's own /usage
        "scan_cost": cost,
        "scans_remaining_this_month": scans_remaining,
        "cache_ttl_hours": cache_ttl_hours,
        "depth_default": depth,
    }
