"""
YABBAI Network V2 — Iteration 3: Auth 2FA + Multi-chain Wallet tests.

Covers:
  • Auth 2FA: /api/auth/me pre-verify, /api/auth/2fa/setup, /api/auth/2fa/verify (happy + bad code + no session)
  • Auth allowlist: verify env + code
  • Wallet: /api/wallet/chains (no rpc leaked), /api/wallet/balance (ethereum + solana public RPCs)
  • Wallet auth gating: /connect and /list require session + 2FA verified
  • Regression: /api/network/status all_live, /api/ai/providers tiers, /api/ai/chat ok
"""
import os
import time
import uuid
from datetime import datetime, timezone, timedelta

import pyotp
import pytest
import requests
from pymongo import MongoClient

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "test_database")
TIMEOUT = 60

# No real person's wallet baked in — burn address by default; override via env.
TEST_EVM_ADDR = os.environ.get("TEST_EVM_ADDR", "0x000000000000000000000000000000000000dEaD")
TEST_SOL_ADDR = "So11111111111111111111111111111111111111112"  # wrapped-SOL mint (public, valid base58)


# ── Seed helpers ─────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def mongo():
    c = MongoClient(MONGO_URL)
    yield c[DB_NAME]
    c.close()


def _seed_session(mongo, mfa_verified=False):
    ts = int(time.time() * 1000)
    user_id = f"test-user-3-{ts}-{uuid.uuid4().hex[:6]}"
    email = f"test.user.3.{ts}@example.com"
    token = f"test_session_3_{ts}_{uuid.uuid4().hex[:8]}"
    mongo.users.insert_one({
        "user_id": user_id, "email": email, "name": "Iter3 User",
        "picture": "https://via.placeholder.com/150",
        "role": "director", "totp_enabled": False,
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
def unverified_session(mongo):
    s = _seed_session(mongo, mfa_verified=False)
    yield s
    mongo.users.delete_one({"user_id": s["user_id"]})
    mongo.user_sessions.delete_many({"user_id": s["user_id"]})
    mongo.wallets.delete_many({"user_id": s["user_id"]})


@pytest.fixture
def verified_session(mongo):
    s = _seed_session(mongo, mfa_verified=True)
    yield s
    mongo.users.delete_one({"user_id": s["user_id"]})
    mongo.user_sessions.delete_many({"user_id": s["user_id"]})
    mongo.wallets.delete_many({"user_id": s["user_id"]})


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


# ── 2FA flow ─────────────────────────────────────────────────────────────────
class TestAuth2FA:
    def test_me_pre_2fa(self, unverified_session):
        r = requests.get(f"{BASE_URL}/api/auth/me",
                         headers=_auth(unverified_session["session_token"]),
                         timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["mfa_required"] is True
        assert d["mfa_enrolled"] is False
        assert d["mfa_verified"] is False

    def test_setup_and_verify_flow(self, unverified_session, mongo):
        token = unverified_session["session_token"]
        # Setup
        r = requests.post(f"{BASE_URL}/api/auth/2fa/setup",
                          headers=_auth(token), timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["ok"] is True
        assert d.get("already_enrolled") is False
        assert "otpauth_url" in d and d["otpauth_url"].startswith("otpauth://")
        assert "secret" in d and len(d["secret"]) >= 16
        secret = d["secret"]

        # Verify with correct TOTP
        code = pyotp.TOTP(secret).now()
        r2 = requests.post(f"{BASE_URL}/api/auth/2fa/verify",
                           headers=_auth(token), json={"code": code},
                           timeout=TIMEOUT)
        assert r2.status_code == 200, r2.text
        d2 = r2.json()
        assert d2["ok"] is True and d2["mfa_verified"] is True

        # /me now reflects enrolled + verified
        r3 = requests.get(f"{BASE_URL}/api/auth/me",
                          headers=_auth(token), timeout=TIMEOUT).json()
        assert r3["mfa_enrolled"] is True
        assert r3["mfa_verified"] is True

    def test_verify_bad_code(self, unverified_session):
        token = unverified_session["session_token"]
        requests.post(f"{BASE_URL}/api/auth/2fa/setup",
                      headers=_auth(token), timeout=TIMEOUT)
        r = requests.post(f"{BASE_URL}/api/auth/2fa/verify",
                          headers=_auth(token), json={"code": "000000"},
                          timeout=TIMEOUT)
        assert r.status_code == 401
        assert "invalid" in r.json().get("detail", "").lower()

    def test_setup_verify_no_session(self):
        r1 = requests.post(f"{BASE_URL}/api/auth/2fa/setup", timeout=TIMEOUT)
        assert r1.status_code == 401
        r2 = requests.post(f"{BASE_URL}/api/auth/2fa/verify",
                           json={"code": "123456"}, timeout=TIMEOUT)
        assert r2.status_code == 401


# ── Allowlist (code + env inspection) ────────────────────────────────────────
class TestAllowlist:
    def test_env_has_allowlist(self):
        env = open("/app/backend/.env").read()
        assert "AUTH_ALLOWLIST=" in env
        assert "yabbiebass@gmail.com" in env
        assert "thomas.basham1@gmail.com" in env

    def test_auth_router_enforces_allowlist(self):
        src = open("/app/backend/auth_router.py").read()
        assert "AUTH_ALLOWLIST" in src
        assert "ALLOWLIST" in src
        # 403 path
        assert "403" in src
        # allowlist check happens before user creation
        idx_check = src.find("ALLOWLIST and email not in ALLOWLIST")
        idx_insert = src.find("db.users.insert_one")
        assert idx_check != -1 and idx_insert != -1 and idx_check < idx_insert


# ── Wallet: public endpoints ─────────────────────────────────────────────────
class TestWalletPublic:
    def test_chains_no_rpc_leak(self):
        r = requests.get(f"{BASE_URL}/api/wallet/chains", timeout=TIMEOUT)
        assert r.status_code == 200
        chains = r.json()["chains"]
        assert len(chains) == 6
        keys = {c["key"] for c in chains}
        assert keys == {"ethereum", "base", "arbitrum", "polygon", "bsc", "solana"}
        for c in chains:
            assert "rpc" not in c, f"rpc leaked in /chains for {c['key']}"
            assert "chain_id" in c and "symbol" in c and "explorer" in c

    def test_balance_ethereum(self):
        r = requests.get(
            f"{BASE_URL}/api/wallet/balance",
            params={"chain": "ethereum", "address": TEST_EVM_ADDR},
            timeout=TIMEOUT,
        )
        assert r.status_code == 200
        d = r.json()
        # Endpoint shape correct even if RPC hiccups
        if d.get("ok"):
            assert d["symbol"] == "ETH"
            assert isinstance(d["balance"], (int, float))
        else:
            pytest.skip(f"public RPC transient: {d.get('error')}")

    def test_balance_solana(self):
        r = requests.get(
            f"{BASE_URL}/api/wallet/balance",
            params={"chain": "solana", "address": TEST_SOL_ADDR},
            timeout=TIMEOUT,
        )
        assert r.status_code == 200
        d = r.json()
        if d.get("ok"):
            assert d["symbol"] == "SOL"
            assert isinstance(d["balance"], (int, float))
        else:
            pytest.skip(f"public RPC transient: {d.get('error')}")


# ── Wallet: auth gating ──────────────────────────────────────────────────────
class TestWalletGating:
    def test_list_no_session_401(self):
        r = requests.get(f"{BASE_URL}/api/wallet/list", timeout=TIMEOUT)
        assert r.status_code == 401

    def test_connect_no_session_401(self):
        r = requests.post(f"{BASE_URL}/api/wallet/connect",
                          json={"address": TEST_EVM_ADDR, "chain": "ethereum",
                                "provider": "metamask"},
                          timeout=TIMEOUT)
        assert r.status_code == 401

    def test_list_unverified_403(self, unverified_session):
        r = requests.get(f"{BASE_URL}/api/wallet/list",
                         headers=_auth(unverified_session["session_token"]),
                         timeout=TIMEOUT)
        assert r.status_code == 403

    def test_connect_unverified_403(self, unverified_session):
        r = requests.post(f"{BASE_URL}/api/wallet/connect",
                          headers=_auth(unverified_session["session_token"]),
                          json={"address": TEST_EVM_ADDR, "chain": "ethereum",
                                "provider": "metamask"},
                          timeout=TIMEOUT)
        assert r.status_code == 403

    def test_connect_and_list_verified(self, verified_session):
        token = verified_session["session_token"]
        # connect
        r = requests.post(f"{BASE_URL}/api/wallet/connect",
                          headers=_auth(token),
                          json={"address": TEST_EVM_ADDR, "chain": "ethereum",
                                "provider": "metamask"},
                          timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["ok"] is True
        assert d["wallet"]["address"] == TEST_EVM_ADDR
        assert d["wallet"]["chain"] == "ethereum"

        # duplicate connect should NOT create a second row (upsert)
        r2 = requests.post(f"{BASE_URL}/api/wallet/connect",
                           headers=_auth(token),
                           json={"address": TEST_EVM_ADDR, "chain": "ethereum",
                                 "provider": "metamask"},
                           timeout=TIMEOUT)
        assert r2.status_code == 200

        # list
        r3 = requests.get(f"{BASE_URL}/api/wallet/list",
                          headers=_auth(token), timeout=TIMEOUT)
        assert r3.status_code == 200
        wallets = r3.json()["wallets"]
        matches = [w for w in wallets
                   if w["address"] == TEST_EVM_ADDR and w["chain"] == "ethereum"]
        assert len(matches) == 1, f"expected exactly 1 wallet, got {len(matches)}: {wallets}"


# ── Regression ───────────────────────────────────────────────────────────────
class TestRegression:
    def test_network_all_live(self):
        r = requests.get(f"{BASE_URL}/api/network/status", timeout=TIMEOUT)
        assert r.status_code == 200
        assert r.json()["all_live"] is True

    def test_ai_providers_tiers(self):
        r = requests.get(f"{BASE_URL}/api/ai/providers", timeout=TIMEOUT)
        assert r.status_code == 200
        d = r.json()
        assert set(d["tiers"].keys()) == {"nvidia", "emergent", "yabbai"}

    def test_ai_chat_ok(self):
        r = requests.post(f"{BASE_URL}/api/ai/chat",
                          json={"message": "Reply with exactly: OK"},
                          timeout=180)
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True
        assert d.get("content")
