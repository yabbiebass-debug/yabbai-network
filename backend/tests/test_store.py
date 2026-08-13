"""Offline unit tests for the storefront (backend/store).
Style-matched to the suite: pytest, explicit evidence in assertion messages.
Runs with NO network and NO Mongo: db + settlement are monkeypatched.
"""
import hashlib, hmac, importlib, json, os, sys, time, types
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")
os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_testsecret"
os.environ["STRIPE_SECRET_KEY"] = "sk_test_dummy"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import store  # noqa: E402


class _FakeColl:
    def __init__(self): self.rows = {}
    async def update_one(self, q, u, upsert=False):
        k = q["session_id"]
        if k not in self.rows and upsert:
            self.rows[k] = dict(u["$setOnInsert"])
    async def find_one(self, q, proj=None):
        if "session_id" in q: return self.rows.get(q["session_id"])
        if "token" in q:
            for r in self.rows.values():
                if r["token"] == q["token"]: return r
        return None


class _FakeDB:
    def __init__(self): self.entitlements = _FakeColl()


booked = []
async def _fake_record(amount, currency, rail, ref, **kw):
    if any(b[2:] == (rail, ref) for b in booked):
        raise store.SettlementVerificationError(f"settlement {rail}:{ref[:24]}… already booked (no double-booking)")
    booked.append((amount, currency, rail, ref))
    return {"entry_type": "income", "amount": amount, "rail": rail}


@pytest.fixture()
def client(monkeypatch, tmp_path):
    booked.clear()
    monkeypatch.setattr(store, "db", _FakeDB())
    monkeypatch.setattr(store, "record_settled_income", _fake_record)
    monkeypatch.setenv("STORE_ASSETS_DIR", str(tmp_path))  # env wins in assets_dir()
    monkeypatch.setattr(store, "WH", "whsec_testsecret")
    app = FastAPI(); app.include_router(store.router)
    return TestClient(app), tmp_path


def _signed(payload: dict) -> tuple[bytes, str]:
    raw = json.dumps(payload).encode()
    t = str(int(time.time()))
    sig = hmac.new(b"whsec_testsecret", f"{t}.".encode() + raw, hashlib.sha256).hexdigest()
    return raw, f"t={t},v1={sig}"


def _paid_event(session="cs_test_1", pi="pi_test_1", sku="orchestrator", cents=2900):
    return {"type": "checkout.session.completed",
            "data": {"object": {"id": session, "payment_status": "paid",
                                "payment_intent": pi, "amount_total": cents,
                                "currency": "aud", "metadata": {"sku": sku},
                                "customer_details": {"email": "b@t.test"}}}}


def test_unpriced_and_assetless_skus_are_unlisted(client):
    c, assets = client
    (assets / "orchestrator.zip").write_bytes(b"zip")
    h = c.get("/api/store/health").json()
    assert "orchestrator" in h["listed"], h
    assert h["unlisted"]["complete-collection"] == "price not set", h
    assert h["unlisted"]["aos-v2"] == "asset file missing", h
    prods = [p["sku"] for p in c.get("/api/store/products").json()["products"]]
    assert "complete-collection" not in prods and "orchestrator" in prods


def test_webhook_rejects_bad_signature_before_any_write(client):
    c, _ = client
    raw = json.dumps(_paid_event()).encode()
    r = c.post("/api/store/webhook", content=raw,
               headers={"stripe-signature": "t=1,v1=deadbeef"})
    assert r.status_code == 400, r.text
    assert booked == [], "tampered webhook must never book income"


def test_paid_webhook_books_once_and_grants_entitlement(client):
    c, assets = client
    (assets / "orchestrator.zip").write_bytes(b"real product bytes")
    raw, sig = _signed(_paid_event())
    r = c.post("/api/store/webhook", content=raw, headers={"stripe-signature": sig})
    assert r.status_code == 200, r.text
    assert len(booked) == 1 and booked[0][2:] == ("stripe", "pi_test_1")
    e = c.get("/api/store/entitlement/cs_test_1").json()
    assert e["ready"] and e["sku"] == "orchestrator"
    # replay (Stripe retry): same 200, still exactly ONE income row, same token
    r2 = c.post("/api/store/webhook", content=raw, headers={"stripe-signature": sig})
    assert r2.status_code == 200 and len(booked) == 1, "replay must not double-book"
    assert c.get("/api/store/entitlement/cs_test_1").json()["token"] == e["token"]
    d = c.get(f"/api/store/download/{e['token']}")
    assert d.status_code == 200 and d.content == b"real product bytes"


def test_download_unknown_token_404_and_missing_asset_503(client):
    c, assets = client
    assert c.get("/api/store/download/nope").status_code == 404
    (assets / "orchestrator.zip").write_bytes(b"x")
    raw, sig = _signed(_paid_event(session="cs2", pi="pi2"))
    c.post("/api/store/webhook", content=raw, headers={"stripe-signature": sig})
    tok = c.get("/api/store/entitlement/cs2").json()["token"]
    (assets / "orchestrator.zip").unlink()          # asset vanishes after purchase
    assert c.get(f"/api/store/download/{tok}").status_code == 503, "no placeholder downloads"
