"""
Iteration 5 — BUG-FIX verification for 2FA QR + stable secret.

The bug: the /2fa page showed a blank QR (client-side qrcode CDN failed) and the
TOTP secret was not stable across repeated /2fa/setup calls -> scanned codes
never verified.

The fix:
  • /api/auth/2fa/setup returns qr_data_url = 'data:image/png;base64,...' (server-side PNG)
  • Same call returns SAME secret on repeat until /2fa/verify enables totp
  • /frontend/public/2fa/index.html no longer references a jsdelivr QR CDN
"""
import os
import time
import uuid
import re
from datetime import datetime, timezone, timedelta

import pyotp
import pytest
import requests
from pymongo import MongoClient

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "test_database")
TIMEOUT = 60


@pytest.fixture(scope="module")
def mongo():
    c = MongoClient(MONGO_URL)
    yield c[DB_NAME]
    c.close()


def _seed(mongo, mfa_verified=False, totp_enabled=False, totp_secret_enc=None):
    ts = int(time.time() * 1000)
    user_id = f"test-user-5-{ts}-{uuid.uuid4().hex[:6]}"
    email = f"test.user.5.{ts}.{uuid.uuid4().hex[:4]}@example.com"
    token = f"test_session_5_{ts}_{uuid.uuid4().hex[:8]}"
    doc = {
        "user_id": user_id, "email": email, "name": "Iter5 User",
        "picture": "https://via.placeholder.com/150",
        "role": "director", "totp_enabled": totp_enabled,
        "created_at": datetime.now(timezone.utc),
    }
    if totp_secret_enc:
        doc["totp_secret_enc"] = totp_secret_enc
    mongo.users.insert_one(doc)
    mongo.user_sessions.insert_one({
        "user_id": user_id, "session_token": token,
        "mfa_verified": mfa_verified,
        "expires_at": datetime.now(timezone.utc) + timedelta(days=7),
        "created_at": datetime.now(timezone.utc),
    })
    return {"user_id": user_id, "email": email, "session_token": token}


@pytest.fixture
def fresh_session(mongo):
    s = _seed(mongo, mfa_verified=False)
    yield s
    mongo.users.delete_one({"user_id": s["user_id"]})
    mongo.user_sessions.delete_many({"user_id": s["user_id"]})


def _auth(t):
    return {"Authorization": f"Bearer {t}"}


class TestBugFix2FAQR:
    def test_setup_returns_qr_data_url_and_secret(self, fresh_session):
        token = fresh_session["session_token"]
        r = requests.post(f"{BASE_URL}/api/auth/2fa/setup",
                          headers=_auth(token), timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["ok"] is True
        assert d["already_enrolled"] is False
        assert isinstance(d.get("secret"), str) and len(d["secret"]) >= 16
        assert d.get("otpauth_url", "").startswith("otpauth://")
        qr = d.get("qr_data_url", "")
        assert qr.startswith("data:image/png;base64,"), f"qr_data_url wrong prefix: {qr[:40]!r}"
        # base64 payload is non-empty and decodes as PNG (starts with \x89PNG)
        import base64
        payload = qr.split(",", 1)[1]
        raw = base64.b64decode(payload)
        assert raw[:8] == b"\x89PNG\r\n\x1a\n", "qr_data_url payload is not a PNG"
        assert len(raw) > 100

    def test_secret_stable_across_repeat_setup(self, fresh_session):
        """Bug fix: repeated /2fa/setup must return the SAME secret."""
        token = fresh_session["session_token"]
        r1 = requests.post(f"{BASE_URL}/api/auth/2fa/setup",
                           headers=_auth(token), timeout=TIMEOUT).json()
        r2 = requests.post(f"{BASE_URL}/api/auth/2fa/setup",
                           headers=_auth(token), timeout=TIMEOUT).json()
        assert r1["secret"] == r2["secret"], "secret must be stable across setup calls"
        assert r1["otpauth_url"] == r2["otpauth_url"]

    def test_correct_totp_verifies(self, fresh_session):
        token = fresh_session["session_token"]
        s = requests.post(f"{BASE_URL}/api/auth/2fa/setup",
                          headers=_auth(token), timeout=TIMEOUT).json()
        code = pyotp.TOTP(s["secret"]).now()
        r = requests.post(f"{BASE_URL}/api/auth/2fa/verify",
                          headers=_auth(token), json={"code": code},
                          timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["ok"] is True and d["mfa_verified"] is True

        # /me now shows enrolled + verified
        me = requests.get(f"{BASE_URL}/api/auth/me",
                         headers=_auth(token), timeout=TIMEOUT).json()
        assert me["mfa_enrolled"] is True
        assert me["mfa_verified"] is True

    def test_wrong_totp_rejected_401(self, fresh_session):
        token = fresh_session["session_token"]
        requests.post(f"{BASE_URL}/api/auth/2fa/setup",
                      headers=_auth(token), timeout=TIMEOUT)
        r = requests.post(f"{BASE_URL}/api/auth/2fa/verify",
                         headers=_auth(token), json={"code": "000000"},
                         timeout=TIMEOUT)
        assert r.status_code == 401

    def test_already_enrolled_returns_no_qr(self, mongo, fresh_session):
        """Regression: after successful enrol, /2fa/setup returns already_enrolled=true, no QR/secret."""
        token = fresh_session["session_token"]
        s = requests.post(f"{BASE_URL}/api/auth/2fa/setup",
                          headers=_auth(token), timeout=TIMEOUT).json()
        code = pyotp.TOTP(s["secret"]).now()
        r = requests.post(f"{BASE_URL}/api/auth/2fa/verify",
                          headers=_auth(token), json={"code": code},
                          timeout=TIMEOUT)
        assert r.status_code == 200
        # Now already enrolled
        r2 = requests.post(f"{BASE_URL}/api/auth/2fa/setup",
                           headers=_auth(token), timeout=TIMEOUT)
        assert r2.status_code == 200
        d2 = r2.json()
        assert d2["ok"] is True
        assert d2.get("already_enrolled") is True
        assert "qr_data_url" not in d2 or not d2.get("qr_data_url")
        assert "secret" not in d2

        # Fresh code still verifies for the already-enrolled user
        code2 = pyotp.TOTP(s["secret"]).now()
        r3 = requests.post(f"{BASE_URL}/api/auth/2fa/verify",
                           headers=_auth(token), json={"code": code2},
                           timeout=TIMEOUT)
        assert r3.status_code == 200
        assert r3.json()["mfa_verified"] is True


class TestFrontend2FAPage:
    def test_page_has_no_cdn_qr_dependency(self):
        """Bug fix: the CDN qrcode script must be gone (it was the failure)."""
        html = open("/app/frontend/public/2fa/index.html").read()
        assert "jsdelivr" not in html, "jsdelivr CDN reference should be removed"
        assert "qrcode.min.js" not in html
        # server-provided QR is used
        assert "qr_data_url" in html
        # has the required data-testids
        assert 'data-testid="totp-code"' in html
        assert 'data-testid="totp-verify"' in html
        assert 'id="qrimg"' in html
