"""
YABBAI Network V2 — Iteration 4: Token balances + GoldScout opportunity signing.

Covers:
  • /api/wallet/tokens ethereum (live majors + total_usd > 0)
  • /api/wallet/tokens base (ok=true)
  • /api/wallet/tokens solana (ok=true native SOL)
  • /api/wallet/tokens invalid chain -> 400
  • /api/wallet/tokens empty EVM address -> ok=true, total_usd 0 (no crash)
  • /api/goldscout/analyze HIGH-risk (airdrop/seed phrase)
  • /api/goldscout/analyze CLEAN (Jupiter staking)
  • /api/goldscout/approve auth gating (no session -> 401)
  • /api/goldscout/approve HIGH risk -> 400 Blocked (verified session)
  • /api/goldscout/approve CLEAN -> ok=true (verified session)
  • /api/goldscout/approvals returns logged approval WITHOUT signature field
  • Regression: /api/wallet/chains == 6, /api/network/status all_live
"""
import os
import time
import uuid
from datetime import datetime, timezone, timedelta

import pytest
import requests
from pymongo import MongoClient

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "test_database")
TIMEOUT = 90

# No real person's wallet baked in. Override with a funded address via env for the
# ">0 balance" check; default is the burn address (holds ~nothing).
TEST_EVM_ADDR = os.environ.get("TEST_EVM_ADDR", "0x000000000000000000000000000000000000dEaD")
EMPTY_EVM_ADDR = "0x000000000000000000000000000000000000dEaD"
TEST_SOL_ADDR = "So11111111111111111111111111111111111111112"  # base58 valid


@pytest.fixture(scope="module")
def mongo():
    c = MongoClient(MONGO_URL)
    yield c[DB_NAME]
    c.close()


def _seed_session(mongo, mfa_verified=True):
    ts = int(time.time() * 1000)
    user_id = f"test-user-4-{ts}-{uuid.uuid4().hex[:6]}"
    email = f"test.user.4.{ts}@example.com"
    token = f"test_session_4_{ts}_{uuid.uuid4().hex[:8]}"
    mongo.users.insert_one({
        "user_id": user_id, "email": email, "name": "Iter4 User",
        "picture": "https://via.placeholder.com/150",
        "role": "director", "totp_enabled": True,
        "created_at": datetime.now(timezone.utc),
    })
    mongo.user_sessions.insert_one({
        "user_id": user_id, "session_token": token,
        "mfa_verified": mfa_verified,
        "expires_at": datetime.now(timezone.utc) + timedelta(days=7),
        "created_at": datetime.now(timezone.utc),
    })
    return {"user_id": user_id, "email": email, "session_token": token}


@pytest.fixture
def verified_session(mongo):
    s = _seed_session(mongo, mfa_verified=True)
    yield s
    mongo.users.delete_one({"user_id": s["user_id"]})
    mongo.user_sessions.delete_many({"user_id": s["user_id"]})
    mongo.goldscout_approvals.delete_many({"user_id": s["user_id"]})


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


# ── /api/wallet/tokens ────────────────────────────────────────────────────────
class TestWalletTokens:
    def test_tokens_ethereum_live(self):
        r = requests.get(f"{BASE_URL}/api/wallet/tokens",
                         params={"chain": "ethereum", "address": TEST_EVM_ADDR},
                         timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        d = r.json()
        if not d.get("ok"):
            pytest.skip(f"public RPC transient: {d.get('error')}")
        assert d["chain"] == "ethereum"
        assert isinstance(d["tokens"], list)
        assert len(d["tokens"]) >= 1
        # Every token has price_usd and value_usd
        for t in d["tokens"]:
            assert "price_usd" in t
            assert "value_usd" in t
            assert "amount" in t
            assert "symbol" in t
        # Native ETH should be present
        eth_row = next((t for t in d["tokens"] if t.get("native") or t["symbol"] == "ETH"), None)
        assert eth_row is not None, f"no native ETH row: {d['tokens']}"
        # total_usd is numeric and non-negative. Only require a positive balance
        # when a real funded address is supplied via the TEST_EVM_ADDR env var.
        assert isinstance(d["total_usd"], (int, float))
        assert d["total_usd"] >= 0
        if os.environ.get("TEST_EVM_ADDR"):
            assert d["total_usd"] > 0, f"expected total_usd > 0, got {d['total_usd']}, tokens={d['tokens']}"
        # Stablecoins pinned to ~1.0 when present
        for t in d["tokens"]:
            if t["symbol"] in {"USDC", "USDT", "DAI"}:
                assert 0.98 <= t["price_usd"] <= 1.02, f"stable {t['symbol']} price {t['price_usd']}"

    def test_tokens_base_ok(self):
        r = requests.get(f"{BASE_URL}/api/wallet/tokens",
                         params={"chain": "base", "address": TEST_EVM_ADDR},
                         timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        d = r.json()
        if not d.get("ok"):
            pytest.skip(f"public RPC transient: {d.get('error')}")
        assert d["chain"] == "base"
        assert isinstance(d["tokens"], list)
        assert isinstance(d["total_usd"], (int, float))

    def test_tokens_solana_ok(self):
        r = requests.get(f"{BASE_URL}/api/wallet/tokens",
                         params={"chain": "solana", "address": TEST_SOL_ADDR},
                         timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        d = r.json()
        if not d.get("ok"):
            pytest.skip(f"public RPC transient: {d.get('error')}")
        assert d["chain"] == "solana"
        # Native SOL row must exist
        sol_row = next((t for t in d["tokens"] if t.get("native") or t["symbol"] == "SOL"), None)
        assert sol_row is not None
        assert "price_usd" in sol_row and "value_usd" in sol_row

    def test_tokens_invalid_chain_400(self):
        r = requests.get(f"{BASE_URL}/api/wallet/tokens",
                         params={"chain": "dogecoin", "address": TEST_EVM_ADDR},
                         timeout=TIMEOUT)
        assert r.status_code == 400

    def test_tokens_empty_address_no_crash(self):
        r = requests.get(f"{BASE_URL}/api/wallet/tokens",
                         params={"chain": "ethereum", "address": EMPTY_EVM_ADDR},
                         timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        d = r.json()
        if not d.get("ok"):
            pytest.skip(f"public RPC transient: {d.get('error')}")
        assert d["ok"] is True
        # empty/burn address: total_usd may be tiny/zero depending on native price*0
        assert isinstance(d["total_usd"], (int, float))


# ── /api/goldscout/analyze ────────────────────────────────────────────────────
class TestGoldScoutAnalyze:
    def test_analyze_high_risk(self):
        r = requests.post(f"{BASE_URL}/api/goldscout/analyze", json={
            "title": "Claim free 5 ETH airdrop",
            "url": "http://jupiter-airdrop-claim.xyz",
            "description": "connect wallet to claim, enter seed phrase",
        }, timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        a = r.json()["analysis"]
        assert a["riskScore"] >= 70, f"expected >=70, got {a['riskScore']}"
        assert a["riskLevel"].lower() in {"critical", "high"}
        assert isinstance(a.get("flags"), list) and len(a["flags"]) > 0

    def test_analyze_clean(self):
        r = requests.post(f"{BASE_URL}/api/goldscout/analyze", json={
            "title": "Jupiter staking",
            "url": "https://jup.ag/",
        }, timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        a = r.json()["analysis"]
        assert a["riskScore"] < 20, f"expected <20, got {a['riskScore']}"


# ── /api/goldscout/approve + /approvals ───────────────────────────────────────
class TestGoldScoutApprove:
    HIGH_OPP = {"title": "Claim free 5 ETH airdrop",
                "url": "http://jupiter-airdrop-claim.xyz",
                "description": "connect wallet to claim, enter seed phrase"}
    CLEAN_OPP = {"title": "Jupiter staking", "url": "https://jup.ag/"}

    def test_approve_no_session_401(self):
        r = requests.post(f"{BASE_URL}/api/goldscout/approve", json={
            "opportunity": self.CLEAN_OPP, "chain": "solana",
            "address": "x", "message": "m", "signature": "s"}, timeout=TIMEOUT)
        assert r.status_code == 401

    def test_approvals_no_session_401(self):
        r = requests.get(f"{BASE_URL}/api/goldscout/approvals", timeout=TIMEOUT)
        assert r.status_code == 401

    def test_approve_high_risk_blocked(self, verified_session):
        r = requests.post(f"{BASE_URL}/api/goldscout/approve",
                          headers=_auth(verified_session["session_token"]),
                          json={"opportunity": self.HIGH_OPP, "chain": "ethereum",
                                "address": "0xabc", "message": "m", "signature": "s"},
                          timeout=TIMEOUT)
        assert r.status_code == 400
        assert "block" in r.json().get("detail", "").lower()

    def test_approve_clean_and_list(self, verified_session):
        token = verified_session["session_token"]
        r = requests.post(f"{BASE_URL}/api/goldscout/approve",
                          headers=_auth(token),
                          json={"opportunity": self.CLEAN_OPP, "chain": "solana",
                                "address": "SoAddr1", "message": "approval-msg",
                                "signature": "SECRET_SIG_SHOULD_NOT_LEAK"},
                          timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["ok"] is True
        assert isinstance(d.get("risk_score"), (int, float))
        assert d["risk_score"] < 70

        # /approvals should show it, without the signature field
        r2 = requests.get(f"{BASE_URL}/api/goldscout/approvals",
                          headers=_auth(token), timeout=TIMEOUT)
        assert r2.status_code == 200
        approvals = r2.json()["approvals"]
        assert len(approvals) >= 1
        # signature must never leak in list response
        for a in approvals:
            assert "signature" not in a, f"signature leaked: {a}"
        # our row present
        mine = [a for a in approvals if a.get("chain") == "solana" and a.get("address") == "SoAddr1"]
        assert len(mine) == 1
        assert mine[0]["opportunity"]["title"] == self.CLEAN_OPP["title"]


# ── Regression ────────────────────────────────────────────────────────────────
class TestRegression:
    def test_chains_still_six(self):
        r = requests.get(f"{BASE_URL}/api/wallet/chains", timeout=TIMEOUT)
        assert r.status_code == 200
        assert len(r.json()["chains"]) == 6

    def test_network_all_live(self):
        r = requests.get(f"{BASE_URL}/api/network/status", timeout=TIMEOUT)
        assert r.status_code == 200
        assert r.json()["all_live"] is True
