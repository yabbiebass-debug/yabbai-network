"""
GoldScout-real smoke test. Mocks every external call (DexScreener, Solana RPC,
GoPlus, AI) and uses an in-memory Mongo, so it needs no keys and no network — which
also means it verifies the LOGIC, not the live endpoints. The live calls are exercised
only when deployed; this proves the parsing, scoring, persistence and degradation.

Run: python3 smoke_goldscout.py
"""
import asyncio, os, sys, json, types

sys.path.insert(0, "/home/claude/src/backend")
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "smoke")
os.environ.setdefault("APP_ENC_KEY", "fXK4hE2Uu9m0i8vJq3yQpZ7nR1sT6wB5cD8eG0aH2kM=")
os.environ.setdefault("RECOVERY_CODE_PEPPER", "smoke-pepper")

from mongomock_motor import AsyncMongoMockClient
import network_db
network_db._client = AsyncMongoMockClient()
network_db._db = network_db._client["smoke"]
network_db.db = network_db._db

# point the modules' `db` at the mock (they bound it at import)
from goldscout.core import store
store.F = network_db.db.goldscout_findings
store.H = network_db.db.goldscout_scans
store.W = network_db.db.goldscout_watchlist

from goldscout.core import safety as sf
from goldscout.core import market as mk

PASS, FAIL = [], []
def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {extra}" if extra and not cond else ""))

# ── fake HTTP layer ───────────────────────────────────────────────────────────
class FakeResp:
    def __init__(self, payload, status=200):
        self._p = payload; self.status_code = status
    def json(self): return self._p
    def raise_for_status(self):
        if self.status_code >= 400: raise RuntimeError(f"HTTP {self.status_code}")

# canned upstream data
SOL_RENOUNCED = {"result": {"value": {"data": {"parsed": {"info": {
    "mintAuthority": None, "freezeAuthority": None, "decimals": 6}}}}}}
SOL_FREEZE_LIVE = {"result": {"value": {"data": {"parsed": {"info": {
    "mintAuthority": None, "freezeAuthority": "SoMeAuth1111111111111111111111111111111111", "decimals": 9}}}}}}
GOPLUS_CLEAN = {"result": {"MINT111": {"is_honeypot": "0", "transfer_pausable": "0", "is_mintable": "0"}}}
GOPLUS_HONEYPOT = {"result": {"MINT666": {"is_honeypot": "1", "buy_tax": "0", "sell_tax": "0.25"}}}
DEXSCREENER_PAIRS = {"pairs": [
    {"chainId": "solana", "dexId": "raydium", "url": "https://dexscreener.com/solana/abc",
     "pairAddress": "PAIRabc", "baseToken": {"address": "MINT111", "name": "Good Token", "symbol": "GOOD"},
     "priceUsd": "1.23", "liquidity": {"usd": 2_500_000}, "volume": {"h24": 800_000},
     "priceChange": {"h24": 4.2}, "pairCreatedAt": 1_600_000_000_000},
    {"chainId": "solana", "dexId": "raydium", "url": "https://dexscreener.com/solana/dust",
     "pairAddress": "PAIRdust", "baseToken": {"address": "MINTdust", "name": "Dust", "symbol": "DUST"},
     "priceUsd": "0.0001", "liquidity": {"usd": 500}, "volume": {"h24": 50}},  # below floor → dropped
]}
BOOSTS = [{"tokenAddress": "MINT111", "chainId": "solana"}]

class FakeClient:
    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def post(self, url, json=None, **k):
        if "solana.com" in url:
            mint = json["params"][0]
            return FakeResp(SOL_FREEZE_LIVE if mint == "MINTfreeze" else SOL_RENOUNCED)
        return FakeResp({})
    async def get(self, url, params=None, **k):
        params = params or {}
        if "token-boosts" in url:
            return FakeResp(BOOSTS)
        if "dex/tokens" in url:
            return FakeResp(DEXSCREENER_PAIRS)
        if "dex/search" in url:
            return FakeResp({"pairs": []})
        if "gopluslabs" in url:
            addr = params.get("contract_addresses", "")
            if addr == "MINT666":
                return FakeResp(GOPLUS_HONEYPOT)
            return FakeResp(GOPLUS_CLEAN)
        return FakeResp({})

# a client that fails every call (to prove honest degradation)
class DeadClient(FakeClient):
    async def post(self, *a, **k): raise RuntimeError("network down")
    async def get(self, *a, **k): raise RuntimeError("network down")


async def main():
    import httpx
    real_client = httpx.AsyncClient
    httpx.AsyncClient = FakeClient
    sf.httpx.AsyncClient = FakeClient
    mk.httpx.AsyncClient = FakeClient

    print("\n── real safety: clean Solana token ──")
    s = await sf.check_token("MINT111", chain="solana", name="Good Token")
    check("clean token scores low", s["risk_level"] == "low", s)
    check("both sources reported ok", s["sources"] == {"onchain": "ok", "goplus": "ok"}, s["sources"])
    check("no false 'safe' claim", "not 'safe'" in s["disclaimer"].lower() or "not a safety guarantee" in s["verdict"].lower())

    print("\n── real safety: live freeze authority is a hard flag ──")
    s = await sf.check_token("MINTfreeze", chain="solana", name="Freezer")
    check("freeze authority pushes risk up", s["risk_score"] >= 45, s)
    check("freeze reason surfaced", any("freeze" in r.lower() for r in s["reasons"]), s["reasons"])

    print("\n── real safety: GoPlus honeypot ──")
    s = await sf.check_token("MINT666", chain="solana", name="Trap")
    check("honeypot → critical", s["risk_level"] == "critical", s)
    check("honeypot reason surfaced", any("honeypot" in r.lower() for r in s["reasons"]), s["reasons"])

    print("\n── honest degradation: sources unreachable are 'unknown', never 'safe' ──")
    sf.httpx.AsyncClient = DeadClient
    s = await sf.check_token("MINTx", chain="solana", name="Unknown")
    check("unreachable sources listed as unchecked", len(s["unchecked"]) >= 1, s)
    check("does NOT claim safe when it couldn't check", s["sources"]["onchain"] == "unknown", s["sources"])
    sf.httpx.AsyncClient = FakeClient

    print("\n── real market scan (live DEX shape) ──")
    r = await mk.scan_market(queries=["SOL"], deep_safety_top=1, limit=10)
    check("returns real pairs", r["total"] == 1, r["total"])
    rec = r["results"][0]
    check("dust filtered out (below liquidity floor)", rec["symbol"] == "GOOD", rec.get("symbol"))
    check("real numbers carried", rec["liquidity_usd"] == 2_500_000 and rec["volume_24h"] == 800_000, rec)
    check("liquidity tier computed", rec["liquidity_tier"] == "deep", rec.get("liquidity_tier"))
    check("deep safety attached to top result", "safety" in rec and rec["safety"]["sources"]["onchain"] == "ok")
    check("framed as leads not signals", "not buy signals" in r["summary"]["note"].lower())

    print("\n── persistence to Mongo (survives 'publish') ──")
    added = await store.add_findings("dir-1", r["results"])
    check("findings persisted", added == 1, added)
    again = await store.add_findings("dir-1", r["results"])
    check("re-scan dedupes (no dupes)", again == 0, again)
    loaded = await store.load_findings("dir-1")
    check("owner reads their findings back", len(loaded) == 1, loaded)
    other = await store.load_findings("dir-2")
    check("owner-scoped (other director sees none)", other == [], other)

    print("\n── scan history + watchlist ──")
    await store.record_scan("dir-1", "market", {"returned": 1})
    check("scan recorded", len(await store.recent_scans("dir-1")) == 1)
    w = await store.add_watch("dir-1", {"token_address": "MINT111", "name": "Good Token", "note": "watch"})
    check("watch added", w["token_address"] == "MINT111", w)
    check("watch listed", len(await store.list_watch("dir-1")) == 1)
    check("watch removed", await store.remove_watch("dir-1", w["id"]) is True)

    print("\n── indexes bootstrap ──")
    idx = await store.ensure_indexes()
    check("indexes created", set(idx) == {"goldscout_findings", "goldscout_scans", "goldscout_watchlist"}, idx)

    httpx.AsyncClient = real_client
    print(f"\n{'='*54}\n{len(PASS)} passed · {len(FAIL)} failed")
    if FAIL:
        print("failed: " + ", ".join(FAIL)); sys.exit(1)

asyncio.run(main())
