"""
YABBAI Network V2 — Auth + Multi-tier Routing tests (iteration 2).

Covers:
  • Auth: /api/auth/me (no token → 401, with token → user, cookie also works)
  • Auth: /api/auth/logout invalidates the session
  • Auth: /api/auth/session with bogus X-Session-ID → 401, no user created
  • Routing: /api/ai/providers, /api/settings GET/PUT (secrets never echoed)
  • Routing: /api/ai/nvidia/models graceful when no key
  • Routing: /api/ai/test with each provider
  • Routing: /api/ai/chat falls through to emergent when nvidia/yabbai have no key
  • Regression: /api/network/status all_live, /api/revenue/api/status w/ admin key
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
ADMIN_HEADER = {"X-Admin-Key": "yabbai-director-key"}
TIMEOUT = 60


# ── Seed helpers ──────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def mongo():
    c = MongoClient(MONGO_URL)
    yield c[DB_NAME]
    c.close()


@pytest.fixture(scope="module")
def seeded_session(mongo):
    """Create a user + session token per /app/auth_testing.md."""
    ts = int(time.time() * 1000)
    user_id = f"test-user-{ts}-{uuid.uuid4().hex[:6]}"
    email = f"test.user.{ts}@example.com"
    session_token = f"test_session_{ts}_{uuid.uuid4().hex[:8]}"
    mongo.users.insert_one({
        "user_id": user_id, "email": email, "name": "Test User",
        "picture": "https://via.placeholder.com/150",
        "role": "director",
        "created_at": datetime.now(timezone.utc),
    })
    mongo.user_sessions.insert_one({
        "user_id": user_id, "session_token": session_token,
        "expires_at": datetime.now(timezone.utc) + timedelta(days=7),
        "created_at": datetime.now(timezone.utc),
    })
    yield {"user_id": user_id, "email": email, "session_token": session_token}
    # cleanup
    mongo.users.delete_one({"user_id": user_id})
    mongo.user_sessions.delete_many({"user_id": user_id})


# ── Auth ──────────────────────────────────────────────────────────────────────
class TestAuth:
    def test_me_no_token_401(self):
        r = requests.get(f"{BASE_URL}/api/auth/me", timeout=TIMEOUT)
        assert r.status_code == 401

    def test_me_bearer_token(self, seeded_session):
        r = requests.get(
            f"{BASE_URL}/api/auth/me",
            headers={"Authorization": f"Bearer {seeded_session['session_token']}"},
            timeout=TIMEOUT,
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["user_id"] == seeded_session["user_id"]
        assert data["email"] == seeded_session["email"]
        assert data.get("role") == "director"

    def test_me_cookie(self, seeded_session):
        r = requests.get(
            f"{BASE_URL}/api/auth/me",
            cookies={"session_token": seeded_session["session_token"]},
            timeout=TIMEOUT,
        )
        assert r.status_code == 200, r.text
        assert r.json()["email"] == seeded_session["email"]

    def test_session_exchange_bogus_id(self, mongo):
        before = mongo.users.count_documents({})
        r = requests.post(
            f"{BASE_URL}/api/auth/session",
            headers={"X-Session-ID": f"bogus-{uuid.uuid4().hex}"},
            timeout=TIMEOUT,
        )
        assert r.status_code == 401
        after = mongo.users.count_documents({})
        assert after == before, "bogus session must NOT create a user"

    def test_logout_kills_session(self, mongo):
        # create a fresh disposable session so we don't nuke the shared one
        ts = int(time.time() * 1000)
        user_id = f"test-user-logout-{ts}"
        token = f"test_session_logout_{ts}"
        mongo.users.insert_one({
            "user_id": user_id, "email": f"lo.{ts}@example.com",
            "name": "Logout User", "role": "director",
            "created_at": datetime.now(timezone.utc),
        })
        mongo.user_sessions.insert_one({
            "user_id": user_id, "session_token": token,
            "expires_at": datetime.now(timezone.utc) + timedelta(days=7),
            "created_at": datetime.now(timezone.utc),
        })
        try:
            headers = {"Authorization": f"Bearer {token}"}
            # sanity: session works
            r = requests.get(f"{BASE_URL}/api/auth/me", headers=headers, timeout=TIMEOUT)
            assert r.status_code == 200
            # logout
            r2 = requests.post(f"{BASE_URL}/api/auth/logout", headers=headers, timeout=TIMEOUT)
            assert r2.status_code == 200
            # session gone
            r3 = requests.get(f"{BASE_URL}/api/auth/me", headers=headers, timeout=TIMEOUT)
            assert r3.status_code == 401
        finally:
            mongo.users.delete_one({"user_id": user_id})
            mongo.user_sessions.delete_many({"user_id": user_id})


# ── Routing / Settings ────────────────────────────────────────────────────────
class TestRoutingProviders:
    def test_providers_shape(self):
        r = requests.get(f"{BASE_URL}/api/ai/providers", timeout=TIMEOUT)
        assert r.status_code == 200
        d = r.json()
        assert "route_order" in d
        assert set(d["tiers"].keys()) == {"nvidia", "emergent", "yabbai"}
        for t in ("nvidia", "emergent", "yabbai"):
            assert "enabled" in d["tiers"][t]
            assert "key_set" in d["tiers"][t]

    def test_settings_persist_route_order(self):
        new_order = ["nvidia", "emergent", "yabbai"]
        r = requests.put(
            f"{BASE_URL}/api/settings",
            json={"route_order": new_order},
            timeout=TIMEOUT,
        )
        assert r.status_code == 200
        g = requests.get(f"{BASE_URL}/api/settings", timeout=TIMEOUT).json()
        assert g["route_order"] == new_order
        # secrets returned only as booleans; never raw
        assert "secrets_set" in g
        assert isinstance(g["secrets_set"], dict)
        for k in ("nvidia_api_key", "yabbai_api_key"):
            assert k not in g, f"raw secret {k} leaked in GET /api/settings"
            assert k in g["secrets_set"]
            assert isinstance(g["secrets_set"][k], bool)

    def test_nvidia_models_no_key(self):
        # ensure no key currently set
        s = requests.get(f"{BASE_URL}/api/settings", timeout=TIMEOUT).json()
        if s.get("secrets_set", {}).get("nvidia_api_key"):
            pytest.skip("nvidia key is configured; graceful-no-key path not applicable")
        r = requests.get(f"{BASE_URL}/api/ai/nvidia/models", timeout=TIMEOUT)
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is False
        assert "nvidia" in (d.get("message") or "").lower()

    def test_test_nvidia_no_key(self):
        s = requests.get(f"{BASE_URL}/api/settings", timeout=TIMEOUT).json()
        if s.get("secrets_set", {}).get("nvidia_api_key"):
            pytest.skip("nvidia key configured")
        r = requests.post(f"{BASE_URL}/api/ai/test", json={"provider": "nvidia"}, timeout=TIMEOUT)
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is False
        assert "nvidia" in (d.get("message") or "").lower()

    def test_test_emergent_ok(self):
        r = requests.post(f"{BASE_URL}/api/ai/test", json={"provider": "emergent"}, timeout=120)
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True, d
        assert d.get("sample")

    def test_test_yabbai_no_url(self):
        s = requests.get(f"{BASE_URL}/api/settings", timeout=TIMEOUT).json()
        if s.get("yabbai_url"):
            pytest.skip("yabbai URL configured")
        r = requests.post(f"{BASE_URL}/api/ai/test", json={"provider": "yabbai"}, timeout=TIMEOUT)
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is False
        assert "yabbai" in (d.get("message") or "").lower()

    def test_graceful_degradation_no_secrets(self, mongo):
        """
        Snapshot secrets, clear them, verify graceful messages, restore.
        Exercises the paths the spec explicitly calls out even when the env
        currently has NVIDIA + YABBAI configured.
        """
        settings_col = mongo.settings
        doc = settings_col.find_one({"_id": "network_settings"}) or {}
        snapshot = {k: doc.get(k) for k in ("nvidia_api_key", "yabbai_url", "yabbai_api_key")}
        try:
            settings_col.update_one(
                {"_id": "network_settings"},
                {"$unset": {"nvidia_api_key": "", "yabbai_url": "", "yabbai_api_key": ""}},
            )
            # nvidia models graceful
            r = requests.get(f"{BASE_URL}/api/ai/nvidia/models", timeout=TIMEOUT).json()
            assert r["ok"] is False
            assert "save your nvidia api key" in (r.get("message") or "").lower()
            # nvidia test graceful
            r = requests.post(f"{BASE_URL}/api/ai/test", json={"provider": "nvidia"}, timeout=TIMEOUT).json()
            assert r["ok"] is False and "nvidia api key not set" in (r.get("message") or "").lower()
            # yabbai test graceful
            r = requests.post(f"{BASE_URL}/api/ai/test", json={"provider": "yabbai"}, timeout=TIMEOUT).json()
            assert r["ok"] is False and "yabbai local url not set" in (r.get("message") or "").lower()
            # emergent test still ok
            r = requests.post(f"{BASE_URL}/api/ai/test", json={"provider": "emergent"}, timeout=120).json()
            assert r["ok"] is True and r.get("sample")
            # chat falls all the way through to emergent
            requests.put(f"{BASE_URL}/api/settings",
                         json={"route_order": ["nvidia", "emergent", "yabbai"]}, timeout=TIMEOUT)
            r = requests.post(f"{BASE_URL}/api/ai/chat",
                              json={"message": "Reply: OK"}, timeout=180).json()
            assert r["ok"] is True and r["tier"] == "emergent", r
        finally:
            restore = {k: v for k, v in snapshot.items() if v is not None}
            if restore:
                settings_col.update_one({"_id": "network_settings"}, {"$set": restore})

    def test_chat_falls_through_when_nvidia_missing(self):
        """
        Spec: With nvidia-first order and no nvidia key, chat should fall through
        to emergent. If nvidia IS configured in this env, verify nvidia answers
        (proves top-of-order tier is used); the fallback path is exercised by
        temporarily disabling nvidia below.
        """
        s = requests.get(f"{BASE_URL}/api/settings", timeout=TIMEOUT).json()
        nvidia_ready = s.get("secrets_set", {}).get("nvidia_api_key")
        # set order nvidia-first
        requests.put(f"{BASE_URL}/api/settings",
                     json={"route_order": ["nvidia", "emergent", "yabbai"]},
                     timeout=TIMEOUT)
        r = requests.post(f"{BASE_URL}/api/ai/chat",
                          json={"message": "Reply with exactly: OK"}, timeout=180)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["ok"] is True and d.get("content")
        if nvidia_ready:
            assert d["tier"] == "nvidia", f"nvidia configured but not used: {d}"
        else:
            assert d["tier"] == "emergent", f"expected emergent fallback: {d}"

        # Now force the fallback path: disable nvidia, keep nvidia at top of order.
        requests.put(f"{BASE_URL}/api/settings",
                     json={"nvidia_enabled": False}, timeout=TIMEOUT)
        try:
            r2 = requests.post(f"{BASE_URL}/api/ai/chat",
                               json={"message": "Reply with exactly: OK"}, timeout=180)
            assert r2.status_code == 200, r2.text
            d2 = r2.json()
            assert d2["ok"] is True
            assert d2["tier"] == "emergent", f"expected emergent when nvidia disabled: {d2}"
        finally:
            requests.put(f"{BASE_URL}/api/settings",
                         json={"nvidia_enabled": True}, timeout=TIMEOUT)


# ── Regression ────────────────────────────────────────────────────────────────
class TestRegression:
    def test_network_status_all_live(self):
        r = requests.get(f"{BASE_URL}/api/network/status", timeout=TIMEOUT)
        assert r.status_code == 200
        assert r.json()["all_live"] is True

    def test_revenue_status_admin_key(self):
        r = requests.get(
            f"{BASE_URL}/api/revenue/api/status",
            headers=ADMIN_HEADER, timeout=TIMEOUT,
        )
        assert r.status_code == 200
        d = r.json()
        assert isinstance(d["gates"]["kill_switch"], bool)
