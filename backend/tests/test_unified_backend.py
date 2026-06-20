"""
YABBAI Network V2 — Unified Gateway Backend Tests
Tests all 5 mounted services + safety invariants on the single-port :8001 gateway.
"""
import os
import time

import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://revenue-nexus-2.preview.emergentagent.com").rstrip("/")
ADMIN_KEY = "yabbai-director-key"
ADMIN_HEADER = {"X-Admin-Key": ADMIN_KEY}

TIMEOUT = 60  # AI calls can be slow


@pytest.fixture(scope="module")
def session():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


# ── Gateway health & network status ──────────────────────────────────────────
class TestGateway:
    def test_health(self, session):
        r = session.get(f"{BASE_URL}/api/health", timeout=TIMEOUT)
        assert r.status_code == 200
        data = r.json()
        assert data["gateway"] == "healthy"
        assert "version" in data

    def test_network_status_all_live(self, session):
        r = session.get(f"{BASE_URL}/api/network/status", timeout=TIMEOUT)
        assert r.status_code == 200
        data = r.json()
        assert data["all_live"] is True
        for svc in ("revenue", "ai", "goldscout", "defi", "ops"):
            assert data["services"][svc]["live"] is True, f"{svc} not live"


# ── Health aliases per service ───────────────────────────────────────────────
class TestServiceHealthAliases:
    @pytest.mark.parametrize("path", [
        "/api/revenue/health",
        "/api/ai/health",
        "/api/goldscout/health",
        "/api/defi/health",
        "/api/ops/health",
    ])
    def test_service_health(self, session, path):
        r = session.get(f"{BASE_URL}{path}", timeout=TIMEOUT)
        assert r.status_code == 200, f"{path} -> {r.status_code} {r.text[:200]}"


# ── Revenue spine: auth gate + status truth ──────────────────────────────────
class TestRevenueAuth:
    def test_status_requires_admin_key(self, session):
        r = session.get(f"{BASE_URL}/api/revenue/api/status", timeout=TIMEOUT)
        assert r.status_code == 401, f"expected 401 unauth got {r.status_code}"

    def test_status_with_admin_key(self, session):
        r = session.get(f"{BASE_URL}/api/revenue/api/status",
                        headers=ADMIN_HEADER, timeout=TIMEOUT)
        assert r.status_code == 200
        data = r.json()
        assert "truth" in data
        assert "gates" in data
        assert isinstance(data["gates"].get("kill_switch"), bool)


# ── Kill-switch flow ─────────────────────────────────────────────────────────
class TestKillSwitch:
    def test_engage_then_disengage(self, session):
        # engage
        r = session.post(f"{BASE_URL}/api/revenue/api/kill-switch",
                         headers=ADMIN_HEADER, json={}, timeout=TIMEOUT)
        assert r.status_code == 200, r.text[:200]

        # verify engaged
        r2 = session.get(f"{BASE_URL}/api/revenue/api/status",
                         headers=ADMIN_HEADER, timeout=TIMEOUT)
        assert r2.status_code == 200
        assert r2.json()["gates"]["kill_switch"] is True

        # disengage
        r3 = session.post(f"{BASE_URL}/api/revenue/api/kill-switch/disengage",
                          headers=ADMIN_HEADER, json={}, timeout=TIMEOUT)
        assert r3.status_code == 200, r3.text[:200]

        # verify disengaged
        r4 = session.get(f"{BASE_URL}/api/revenue/api/status",
                         headers=ADMIN_HEADER, timeout=TIMEOUT)
        assert r4.json()["gates"]["kill_switch"] is False


# ── Reconciled income (manual sale → income increases) ──────────────────────
class TestReconciledIncome:
    def test_manual_sale_increases_real_income(self, session):
        s_before = session.get(f"{BASE_URL}/api/revenue/api/status",
                               headers=ADMIN_HEADER, timeout=TIMEOUT).json()
        income_before = float(s_before["truth"].get("real_income", 0))

        sale = {"sale_id": f"qa1-{int(time.time())}", "product": "Audit",
                "gross_usd": 500, "fee_usd": 20}
        r = session.post(f"{BASE_URL}/api/revenue/api/manual/sale",
                         headers=ADMIN_HEADER, json=sale, timeout=TIMEOUT)
        assert r.status_code == 200, r.text[:200]
        body = r.json()
        assert "net_profit_now" in body or "net_profit" in body or "ok" in body

        s_after = session.get(f"{BASE_URL}/api/revenue/api/status",
                              headers=ADMIN_HEADER, timeout=TIMEOUT).json()
        income_after = float(s_after["truth"].get("real_income", 0))
        assert income_after > income_before, (
            f"income did not increase: before={income_before} after={income_after}")


# ── Compliance gate (blocks guarantee-style claims) ─────────────────────────
class TestComplianceGate:
    def test_block_risky_copy(self, session):
        r = session.post(f"{BASE_URL}/api/revenue/api/compliance/check-copy",
                         headers=ADMIN_HEADER,
                         json={"text": "guaranteed risk-free returns, get rich"},
                         timeout=TIMEOUT)
        assert r.status_code == 200, r.text[:200]
        data = r.json()
        # accept either {issues:[...]} or {ok:false, issues:[...]}
        issues = data.get("issues") or data.get("violations") or []
        assert len(issues) > 0, f"expected compliance issues, got {data}"


# ── AI brain (Universal LLM key) ─────────────────────────────────────────────
class TestAIBrain:
    def test_chat_hello(self, session):
        r = session.post(f"{BASE_URL}/api/ai/chat",
                         json={"message": "Say hello in 3 words"}, timeout=120)
        assert r.status_code == 200, r.text[:300]
        data = r.json()
        assert data.get("ok") is True, data
        content = data.get("content") or data.get("text") or ""
        assert len(content.strip()) > 0

    def test_diagnose(self, session):
        r = session.post(f"{BASE_URL}/api/ai/diagnose",
                         json={"business": "a cafe", "goal": "more bookings"},
                         timeout=180)
        assert r.status_code == 200, r.text[:300]
        data = r.json()
        assert data.get("ok") is True
        plan = data.get("plan") or data.get("diagnosis") or data.get("result")
        assert plan, f"no plan in response: {data}"
        assert isinstance(plan, (dict, list))


# ── DeFi simulator (paper-only) ──────────────────────────────────────────────
class TestDeFi:
    def test_session_create(self, session):
        r = session.post(f"{BASE_URL}/api/defi/api/session/create",
                         json={}, timeout=TIMEOUT)
        assert r.status_code == 200, r.text[:300]
        data = r.json()
        assert data.get("ok") is True
        assert "session_id" in data
        assert "snapshot" in data

    def test_health_flags(self, session):
        r = session.get(f"{BASE_URL}/api/defi/api/health", timeout=TIMEOUT)
        assert r.status_code == 200
        data = r.json()
        assert data.get("custodial") is False
        assert data.get("real_funds") is False


# ── Ops dashboard ────────────────────────────────────────────────────────────
class TestOps:
    def test_dashboard(self, session):
        r = session.get(f"{BASE_URL}/api/ops/api/ops/dashboard", timeout=TIMEOUT)
        assert r.status_code == 200, r.text[:300]
        assert isinstance(r.json(), dict)
