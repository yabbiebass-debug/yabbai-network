"""
Iteration 6 — Supabase connector env-wiring verification.

Verifies:
  - backend .env exposes 4 Supabase keys
  - /api/supabase/status pulls from env (key_set=true, url matches env), auth+2FA gated
  - key authenticates (probe returns 404/PGRST205, not 401)
  - /api/supabase/table/leads returns ok=false gracefully (no 500)
  - /api/supabase/table/<not_whitelisted> -> 400
  - Regression: /api/network/status all_live=true, /api/settings sanitized
"""
import os
import time
import uuid
from datetime import datetime, timezone, timedelta

import httpx
import pytest
import requests
from pymongo import MongoClient

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "test_database")
TIMEOUT = 30


@pytest.fixture(scope="module")
def mongo():
    c = MongoClient(MONGO_URL)
    yield c[DB_NAME]
    c.close()


def _seed(mongo, mfa_verified: bool):
    ts = int(time.time() * 1000)
    user_id = f"test-user-6-{ts}-{uuid.uuid4().hex[:6]}"
    email = "yabbiebass@gmail.com"  # allowlisted
    token = f"test_session_6_{ts}_{uuid.uuid4().hex[:8]}"
    mongo.users.insert_one({
        "user_id": user_id, "email": email, "name": "Iter6 User",
        "picture": "https://via.placeholder.com/150",
        "role": "director", "totp_enabled": mfa_verified,
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
def session_2fa(mongo):
    s = _seed(mongo, mfa_verified=True)
    yield s
    mongo.users.delete_one({"user_id": s["user_id"]})
    mongo.user_sessions.delete_many({"user_id": s["user_id"]})


@pytest.fixture
def session_no_2fa(mongo):
    s = _seed(mongo, mfa_verified=False)
    yield s
    mongo.users.delete_one({"user_id": s["user_id"]})
    mongo.user_sessions.delete_many({"user_id": s["user_id"]})


def _auth(t):
    return {"Authorization": f"Bearer {t}"}


ENV_URL = "https://gecwxvwziktvaiwdhzeg.supabase.co"


class TestEnvWiring:
    def test_env_has_supabase_keys(self):
        env = open("/app/backend/.env").read()
        for k in ("SUPABASE_PUBLIC_URL", "SUPABASE_PUBLISHABLE_KEY",
                  "SUPABASE_SECRET_KEY", "SUPABASE_JWT_SIGNING_KEY"):
            assert k + "=" in env, f"Missing key {k} in /app/backend/.env"


class TestSupabaseAuthGating:
    def test_status_no_session_401(self):
        r = requests.get(f"{BASE_URL}/api/supabase/status", timeout=TIMEOUT)
        assert r.status_code == 401, r.text

    def test_status_no_2fa_403(self, session_no_2fa):
        r = requests.get(f"{BASE_URL}/api/supabase/status",
                         headers=_auth(session_no_2fa["session_token"]), timeout=TIMEOUT)
        assert r.status_code == 403, r.text


class TestSupabaseStatus:
    def test_status_uses_env(self, session_2fa):
        r = requests.get(f"{BASE_URL}/api/supabase/status",
                         headers=_auth(session_2fa["session_token"]), timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["key_set"] is True, "SUPABASE_SECRET_KEY not picked up from env"
        assert d["url"] == ENV_URL, f"URL mismatch: {d['url']}"
        assert "tables" in d and isinstance(d["tables"], dict)
        assert len(d["tables"]) == 13
        # Graceful state: schema not yet applied -> connected=false / 0 present.
        assert d.get("tables_total") == 13
        assert d.get("tables_present", 0) == 0
        assert d["connected"] is False
        assert "error" not in d, f"Unexpected error: {d.get('error')}"


class TestSupabaseKeyValidity:
    def test_direct_key_auth_returns_pgrst(self):
        """Raw PostgREST probe with sb_secret from env: should be 404/PGRST205, NOT 401."""
        key = os.environ.get("SUPABASE_SECRET_KEY") or \
              [ln.split("=", 1)[1].strip() for ln in open("/app/backend/.env")
               if ln.startswith("SUPABASE_SECRET_KEY=")][0]
        with httpx.Client(timeout=15) as c:
            r = c.get(f"{ENV_URL}/rest/v1/leads",
                      params={"select": "*", "limit": 1},
                      headers={"apikey": key, "Authorization": f"Bearer {key}"})
        assert r.status_code != 401, f"Key rejected: {r.text[:200]}"
        assert r.status_code in (404, 400), f"Unexpected status {r.status_code}: {r.text[:200]}"
        # PGRST205 = schema cache / relation not found (auth OK, missing table)
        assert "PGRST" in r.text or "not find" in r.text.lower(), r.text[:200]


class TestSupabaseReadProxy:
    def test_read_missing_table_graceful(self, session_2fa):
        r = requests.get(f"{BASE_URL}/api/supabase/table/leads",
                         headers=_auth(session_2fa["session_token"]), timeout=TIMEOUT)
        assert r.status_code == 200, r.text  # proxy wraps non-200 upstream
        d = r.json()
        assert d["ok"] is False
        assert d["status"] in (404, 400)
        assert d["rows"] == []

    def test_read_not_whitelisted_400(self, session_2fa):
        r = requests.get(f"{BASE_URL}/api/supabase/table/not_allowed_name",
                         headers=_auth(session_2fa["session_token"]), timeout=TIMEOUT)
        assert r.status_code == 400, r.text


class TestRegression:
    def test_network_status_all_live(self):
        r = requests.get(f"{BASE_URL}/api/network/status", timeout=TIMEOUT)
        assert r.status_code == 200
        d = r.json()
        assert d["all_live"] is True, d

    def test_settings_sanitized(self):
        r = requests.get(f"{BASE_URL}/api/settings", timeout=TIMEOUT)
        assert r.status_code == 200
        d = r.json()
        # No raw secret values leaked. The supabase_service_key must never be echoed raw.
        raw_secret = None
        for ln in open("/app/backend/.env"):
            if ln.startswith("SUPABASE_SECRET_KEY="):
                raw_secret = ln.split("=", 1)[1].strip()
        assert raw_secret
        flat = str(d)
        assert raw_secret not in flat, "Raw Supabase secret leaked via /api/settings"
