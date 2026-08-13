"""Jupiter + DeFiLlama HTTP wrappers. Read/quote/relay only — no keys with
signing authority live anywhere near this module (N1).

Doc references (fetched + curled live 2026-08-12, per R2/R3):
  Swap V2 order/execute — https://developers.jup.ag/docs/swap/order-and-execute
                          GET  https://api.jup.ag/swap/v2/order      (200 keyless, quote only)
                          POST https://api.jup.ag/swap/v2/execute
  Shield              — https://developers.jup.ag/docs/ultra/get-shield
                          GET https://lite-api.jup.ag/ultra/v1/shield?mints=… (200 keyless; the
                          ultra/v1 namespace stays live for shield; Portal keys don't apply)
  Tokens V2           — https://dev.jup.ag/docs/tokens
                          GET https://lite-api.jup.ag/tokens/v2/search?query=… (200 keyless)
  Price V3            — https://dev.jup.ag/docs/price
                          GET https://lite-api.jup.ag/price/v3?ids=… (200 keyless)
  Trigger V2          — https://developers.jup.ag/docs/trigger
                          https://api.jup.ag/trigger/v2/* (verified live: orders/price +
                          deposit/craft answer 401 without a Portal key — key REQUIRED)
  Lend (Earn)         — https://dev.jup.ag/api-reference (lend/earn)
                          https://lite-api.jup.ag/lend/v1/earn/{tokens,positions,
                          mint-instructions,redeem-instructions} (verified live 2026-08-12)
  Yields              — DeFiLlama public API https://yields.llama.fi/pools
"""

import time
from typing import Dict, List, Optional

import httpx

SWAP_ORDER_URL = "https://api.jup.ag/swap/v2/order"
SWAP_EXECUTE_URL = "https://api.jup.ag/swap/v2/execute"
SHIELD_URL = "https://lite-api.jup.ag/ultra/v1/shield"
TOKENS_SEARCH_URL = "https://lite-api.jup.ag/tokens/v2/search"
PRICE_URL = "https://lite-api.jup.ag/price/v3"
TRIGGER_BASE = "https://api.jup.ag/trigger/v2"
LEND_BASE = "https://lite-api.jup.ag/lend/v1"
LLAMA_POOLS_URL = "https://yields.llama.fi/pools"

TIMEOUT = httpx.Timeout(20.0, connect=8.0)


def _headers(api_key: str = "") -> Dict:
    h = {"accept": "application/json"}
    if api_key:
        h["x-api-key"] = api_key
    return h


async def swap_order(input_mint: str, output_mint: str, amount: int,
                     slippage_bps: int, taker: str = "", api_key: str = "") -> Dict:
    params = {"inputMint": input_mint, "outputMint": output_mint,
              "amount": str(int(amount)), "slippageBps": int(slippage_bps)}
    if taker:
        params["taker"] = taker
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(SWAP_ORDER_URL, params=params, headers=_headers(api_key))
    if r.status_code in (401, 403):
        return {"error": "jupiter API key not set or invalid — add it in /settings → DeFi · Jupiter "
                         "or the JUPITER_API_KEY prod secret", "http_status": r.status_code}
    try:
        body = r.json()
    except Exception:
        return {"error": f"jupiter order returned non-JSON (HTTP {r.status_code})",
                "http_status": r.status_code}
    body["http_status"] = r.status_code
    return body


async def swap_execute(signed_transaction: str, request_id: str, api_key: str = "") -> Dict:
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=8.0)) as c:
        r = await c.post(SWAP_EXECUTE_URL,
                         json={"signedTransaction": signed_transaction, "requestId": request_id},
                         headers=_headers(api_key))
    try:
        body = r.json()
    except Exception:
        body = {"error": f"non-JSON execute response (HTTP {r.status_code})"}
    body["http_status"] = r.status_code
    return body


async def shield(mints: List[str]) -> Dict:
    """Token risk warnings. Returns {mint: [warnings]} or {'error': ...}."""
    if not mints:
        return {"warnings": {}}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.get(SHIELD_URL, params={"mints": ",".join(mints[:50])})
            r.raise_for_status()
            return r.json() or {"warnings": {}}
    except Exception as e:
        return {"error": f"shield unreachable: {e}"}


def shield_verdict(warnings: Optional[List[Dict]]) -> str:
    """clear = no warnings · caution = warning-severity flags · info = info only.
    None (source unreachable) → 'unknown', never 'clear'."""
    if warnings is None:
        return "unknown"
    if any((w.get("severity") or "").lower() in ("warning", "critical", "danger") for w in warnings):
        return "caution"
    if warnings:
        return "info"
    return "clear"


async def token_search(query: str, limit: int = 20) -> List[Dict]:
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.get(TOKENS_SEARCH_URL, params={"query": query})
            r.raise_for_status()
            rows = r.json() or []
    except Exception:
        return []
    out = []
    for t in rows[:limit]:
        out.append({"mint": t.get("id"), "name": t.get("name"), "symbol": t.get("symbol"),
                    "decimals": t.get("decimals"), "icon": t.get("icon"),
                    "usd_price": t.get("usdPrice"), "mcap": t.get("mcap"),
                    "liquidity_usd": t.get("liquidity"), "holder_count": t.get("holderCount"),
                    "organic_score": t.get("organicScore"),
                    "audit": t.get("audit"), "is_verified": t.get("isVerified")})
    return out


async def prices(mints: List[str]) -> Dict:
    """{mint: {usdPrice, decimals, ...}} — a mint absent from the answer has NO price
    (render —, never 0)."""
    if not mints:
        return {}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.get(PRICE_URL, params={"ids": ",".join(mints[:50])})
            r.raise_for_status()
            return r.json() or {}
    except Exception:
        return {}


_llama_cache = {"ts": 0.0, "data": None}


async def llama_pools() -> List[Dict]:
    """DeFiLlama pools, cached 10 min (large payload, public API courtesy)."""
    if _llama_cache["data"] is not None and time.time() - _llama_cache["ts"] < 600:
        return _llama_cache["data"]
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(40.0, connect=10.0)) as c:
            r = await c.get(LLAMA_POOLS_URL)
            r.raise_for_status()
            data = (r.json() or {}).get("data") or []
    except Exception:
        return _llama_cache["data"] or []
    _llama_cache["ts"] = time.time()
    _llama_cache["data"] = data
    return data


async def trigger_get(path: str, params: Dict, api_key: str) -> Dict:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(f"{TRIGGER_BASE}/{path}", params=params, headers=_headers(api_key))
    try:
        body = r.json()
    except Exception:
        body = {"error": f"non-JSON (HTTP {r.status_code})"}
    if isinstance(body, dict):
        body["http_status"] = r.status_code
    return body if isinstance(body, dict) else {"data": body, "http_status": r.status_code}


async def trigger_post(path: str, payload: Dict, api_key: str) -> Dict:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.post(f"{TRIGGER_BASE}/{path}", json=payload, headers=_headers(api_key))
    try:
        body = r.json()
    except Exception:
        body = {"error": f"non-JSON (HTTP {r.status_code})"}
    body["http_status"] = r.status_code
    return body


async def lend_get(path: str, params: Dict) -> Dict:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(f"{LEND_BASE}/{path}", params=params)
    try:
        return {"data": r.json(), "http_status": r.status_code}
    except Exception:
        return {"error": f"non-JSON (HTTP {r.status_code})", "http_status": r.status_code}


async def lend_post(path: str, payload: Dict) -> Dict:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.post(f"{LEND_BASE}/{path}", json=payload)
    try:
        body = r.json()
    except Exception:
        return {"error": f"non-JSON (HTTP {r.status_code})", "http_status": r.status_code}
    return {"data": body, "http_status": r.status_code}
