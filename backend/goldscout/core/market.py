"""
GoldScout real market scanner.

The original scout only searched the news (Tavily) — it found *articles about*
airdrops, never live on-chain data. This pulls real, current market data straight
from DexScreener (free, no API key): trending/boosted tokens and live pairs with real
liquidity, 24h volume, price and age.

It is deliberately NOT a trading signal. Each result carries the real numbers, a
neutral liquidity tier, the scam-text heuristics, and — for the top few — a real
on-chain safety check. The framing stays: these are RESEARCH LEADS with risk ratings.
You verify and act in your own wallet. Nothing here moves funds.
"""

from datetime import datetime, timezone

import httpx

from .scam_analysis import analyze_opportunity
from . import safety as safety_mod

DS_SEARCH = "https://api.dexscreener.com/latest/dex/search"
DS_BOOSTS = "https://api.dexscreener.com/token-boosts/latest/v1"
DS_TOKENS = "https://api.dexscreener.com/latest/dex/tokens"
TIMEOUT = httpx.Timeout(15.0, connect=6.0)

# Sane floors so a "scan" surfaces real markets, not dust. Tunable per call.
MIN_LIQ_USD = 20_000
MIN_VOL_24H = 10_000


def _now():
    return datetime.now(timezone.utc)


def _liquidity_tier(liq: float) -> str:
    if liq >= 1_000_000:
        return "deep"
    if liq >= 100_000:
        return "moderate"
    return "thin"


def _pair_record(p: dict) -> dict | None:
    try:
        liq = float((p.get("liquidity") or {}).get("usd") or 0)
        vol = float((p.get("volume") or {}).get("h24") or 0)
    except (TypeError, ValueError):
        return None
    if liq < MIN_LIQ_USD or vol < MIN_VOL_24H:
        return None
    base = p.get("baseToken") or {}
    created = p.get("pairCreatedAt")
    age_days = None
    if created:
        try:
            age_days = round((_now().timestamp() * 1000 - float(created)) / 86_400_000, 1)
        except (TypeError, ValueError):
            age_days = None
    return {
        "token_address": base.get("address"),
        "name": base.get("name"),
        "symbol": base.get("symbol"),
        "chain": p.get("chainId"),
        "dex": p.get("dexId"),
        "url": p.get("url"),
        "pair_address": p.get("pairAddress"),
        "price_usd": p.get("priceUsd"),
        "liquidity_usd": round(liq),
        "volume_24h": round(vol),
        "price_change_24h": (p.get("priceChange") or {}).get("h24"),
        "age_days": age_days,
        "liquidity_tier": _liquidity_tier(liq),
        "category": "market",
        "found_at": _now(),
    }


async def _search(client: httpx.AsyncClient, query: str) -> list[dict]:
    try:
        r = await client.get(DS_SEARCH, params={"q": query})
        r.raise_for_status()
        return (r.json() or {}).get("pairs") or []
    except Exception:
        return []


async def _boosted(client: httpx.AsyncClient) -> list[str]:
    """Trending/boosted token addresses (a real 'what's active right now' feed)."""
    try:
        r = await client.get(DS_BOOSTS)
        r.raise_for_status()
        data = r.json()
        items = data if isinstance(data, list) else data.get("data") or []
        return [(i.get("tokenAddress"), i.get("chainId")) for i in items if i.get("tokenAddress")]
    except Exception:
        return []


async def _token_pairs(client: httpx.AsyncClient, address: str) -> list[dict]:
    try:
        r = await client.get(f"{DS_TOKENS}/{address}")
        r.raise_for_status()
        return (r.json() or {}).get("pairs") or []
    except Exception:
        return []


async def scan_market(queries: list[str] | None = None, chain: str | None = None,
                      deep_safety_top: int = 3, limit: int = 40) -> dict:
    """
    Live DEX scan. Returns real pairs (scam-filtered, risk-rated), best-liquidity first.
    `deep_safety_top` runs a real on-chain safety check on the top N results.
    """
    queries = queries or ["SOL", "USDC", "trending"]
    errors: list[str] = []
    seen: set = set()
    records: list[dict] = []

    async with httpx.AsyncClient(timeout=TIMEOUT, headers={"accept": "application/json"}) as client:
        pairs: list[dict] = []

        # 1) trending/boosted tokens → their live pairs
        for addr, _c in (await _boosted(client))[:15]:
            pairs.extend(await _token_pairs(client, addr))

        # 2) ecosystem searches
        for q in queries:
            pairs.extend(await _search(client, q))

        if not pairs:
            errors.append("DexScreener returned no pairs (network blocked or rate-limited).")

        for p in pairs:
            if chain and (p.get("chainId") or "").lower() != chain.lower():
                continue
            rec = _pair_record(p)
            if not rec:
                continue
            key = rec.get("pair_address") or rec.get("token_address")
            if not key or key in seen:
                continue
            seen.add(key)

            # scam-text heuristic on the listing (name/url)
            analysis = analyze_opportunity({
                "title": f"{rec.get('name')} ({rec.get('symbol')})",
                "url": rec.get("url") or "", "snippet": "", "description": ""})
            rec["heuristic_score"] = analysis["riskScore"]
            rec["red_flags"] = analysis["flags"]
            rec["risk_level"] = analysis["riskLevel"]
            rec["is_labor"] = False
            rec["requires_capital"] = True
            rec["status"] = "lead"
            records.append(rec)

    # deepest liquidity first (real signal of an established market, not a buy call)
    records.sort(key=lambda r: r.get("liquidity_usd", 0), reverse=True)
    records = records[:limit]

    # real on-chain safety on the top few (bounded so we don't hammer public RPC)
    for rec in records[:max(0, deep_safety_top)]:
        if not rec.get("token_address"):
            continue
        s = await safety_mod.check_token(
            rec["token_address"], chain=(rec.get("chain") or "solana"),
            name=rec.get("name") or "", heuristic_score=rec.get("heuristic_score", 0))
        rec["safety"] = s
        # promote the real safety verdict to the record's headline risk
        rec["risk_score"] = s["risk_score"]
        rec["risk_level"] = s["risk_level"]
        rec["verdict"] = s["verdict"]

    return {
        "total": len(records),
        "results": records,
        "summary": {
            "returned": len(records),
            "deep_checked": min(deep_safety_top, len(records)),
            "errors": errors,
            "note": ("Live DEX data from DexScreener, scam-filtered and risk-rated. These are "
                     "RESEARCH LEADS, not buy signals. Liquidity/volume are real but say nothing "
                     "about whether a token is a good buy. Deep safety was run on the top few; run "
                     "/token/safety on any other before you touch it. Nothing here moves funds."),
        },
    }
