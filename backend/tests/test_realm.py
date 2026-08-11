"""
Smoke test for realm_router: boots the router against an in-memory Mongo with a
stubbed auth dependency and a stubbed LLM, then walks the real agency flow.

Run: python3 smoke_realm.py
"""
import asyncio, os, sys, types, json

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "smoke")
os.environ.setdefault("APP_ENC_KEY", "fXK4hE2Uu9m0i8vJq3yQpZ7nR1sT6wB5cD8eG0aH2kM=")
os.environ.setdefault("RECOVERY_CODE_PEPPER", "smoke-pepper")

from mongomock_motor import AsyncMongoMockClient

# swap the real Mongo for an in-memory one before anything imports `db`
import network_db
network_db._client = AsyncMongoMockClient()
network_db._db = network_db._client["smoke"]
network_db.db = network_db._db

import auth_router
auth_router.db = network_db.db

import ai_router

CALLS = {"n": 0}
async def fake_route_complete(system, prompt, session_id="yabbai"):
    CALLS["n"] += 1
    if "qualify B2B leads" in system:
        hot = "Bendigo" in prompt or "no booking" in prompt
        return json.dumps({"score": 88 if hot else 34,
                           "fit": "Hot" if hot else "Cold",
                           "why": "clear booking gap" if hot else "no obvious fit"})
    if "cold email" in system:
        return "Noticed you take bookings by phone only. I'd wire up online booking. 10 minutes this week?"
    return "ok"
ai_router.route_complete = fake_route_complete

import realm_router
realm_router.ai_router = ai_router

from fastapi import FastAPI
from fastapi.testclient import TestClient

USER = {"user_id": "director-1", "email": "director@example.com"}

app = FastAPI()
app.include_router(realm_router.router)
app.dependency_overrides[realm_router.require_director] = lambda: USER

# /select uses _optional_user (not the dependency) — stub that too
async def fake_optional_user(request, authorization):
    return USER if getattr(request.state, "anon", False) is not True else None
realm_router._optional_user = fake_optional_user

c = TestClient(app)
PASS, FAIL = [], []

def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {extra}" if (extra and not cond) else ""))

def sel(**kw):
    r = c.post("/api/realm/select", json=kw)
    assert r.status_code == 200, r.text
    return r.json()

print("\n── health ──")
h = c.get("/api/realm/health").json()
check("health responds", h["ok"] and len(h["rpcs"]) == 12, h)

print("\n── empty realm reports zero, not a guess ──")
m = sel(table="money_view")["data"]
check("money_view zeroed", m == {"mrr": 0, "cash_collected": 0, "active_clients": 0}, m)
check("leads empty", sel(table="leads")["data"] == [])

print("\n── add leads ──")
r = c.post("/api/realm/insert", json={"table": "leads", "rows": [
    {"biz": "Bendigo Dental", "niche": "dental", "suburb": "Bendigo", "pain": "no booking"},
    {"biz": "Quiet Cafe", "niche": "hospitality", "suburb": "Fitzroy", "pain": "none stated"},
]})
check("insert leads", r.status_code == 200 and len(r.json()["data"]) == 2, r.text)
leads = {l["biz"]: l for l in sel(table="leads")["data"]}
check("owner scoped", all(l["owner"] == "director-1" for l in leads.values()))
check("stage defaults New", all(l["stage"] == "New" for l in leads.values()))

r = c.post("/api/realm/insert", json={"table": "leads", "rows": [{"biz": "  "}]})
check("blank business rejected", r.status_code == 400, r.text)

print("\n── cycle-runner (real scoring path) ──")
r = c.post("/api/realm/fn/cycle-runner", json={})
cyc = r.json()
check("cycle ran", r.status_code == 200 and cyc["cycle"] == 1, r.text)
check("both leads scored", cyc["qualified"] == 2, cyc)
check("only Hot drafted", cyc["drafted"] == 1, cyc)
apps = sel(table="approvals", filters=[{"op": "eq", "col": "status", "val": "pending"}])["data"]
check("outreach approval queued", len(apps) == 1 and apps[0]["type"] == "outreach", apps)
check("draft is not auto-sent",
      [l for l in sel(table="leads")["data"] if l["biz"] == "Bendigo Dental"][0]["stage"] == "Outreach drafted")

print("\n── cycle halts instead of guessing when the AI is down ──")
async def dead(*a, **k): raise RuntimeError("all tiers down")
ai_router.route_complete = dead
c.post("/api/realm/insert", json={"table": "leads", "rows": [{"biz": "Third Co"}]})
r = c.post("/api/realm/fn/cycle-runner", json={})
check("halts on AI failure", r.status_code == 503, r.status_code)
third = [l for l in sel(table="leads")["data"] if l["biz"] == "Third Co"][0]
check("no fabricated score written", third["fit"] is None and third["score"] is None, third)
ai_router.route_complete = fake_route_complete

print("\n── approval gate drives state ──")
aid = apps[0]["id"]
r = c.post("/api/realm/rpc/approve_approval", json={"p_id": aid})
check("approve ok", r.status_code == 200, r.text)
bd = [l for l in sel(table="leads")["data"] if l["biz"] == "Bendigo Dental"][0]
check("lead → Contacted", bd["stage"] == "Contacted", bd["stage"])
check("action ledger written", len(sel(table="actions")["data"]) == 1)
r = c.post("/api/realm/rpc/approve_approval", json={"p_id": aid})
check("double-approve rejected", r.status_code == 400, r.text)

print("\n── close the deal (catalog prices only) ──")
c.post("/api/realm/rpc/mark_replied", json={"p_lead_id": bd["id"]})
bd2 = [l for l in sel(table="leads")["data"] if l["biz"] == "Bendigo Dental"][0]
check("lead → Replied", bd2["stage"] == "Replied", bd2["stage"])
check("contract gate queued", any(a["type"] == "contract" for a in
      sel(table="approvals", filters=[{"op": "eq", "col": "status", "val": "pending"}])["data"]))

r = c.post("/api/realm/rpc/close_deal", json={
    "p_lead_id": bd["id"], "p_package": "Growth", "p_tier": "Optimize",
    "p_fee": 99, "p_mrr": 1})          # client tries to dictate price
d = r.json()["data"]
check("close ok", r.status_code == 200, r.text)
check("price forced from catalog", d["fee"] == 4000 and d["mrr"] == 1500, d)

r = c.post("/api/realm/rpc/close_deal", json={
    "p_lead_id": bd["id"], "p_package": "Mate's Rates", "p_tier": "Optimize"})
check("off-catalog package rejected", r.status_code == 400, r.text)

print("\n── money is reconciled-only ──")
m = sel(table="money_view")["data"]
check("MRR still 0 (client not Active)", m["mrr"] == 0, m)
check("cash still 0 (fee unpaid)", m["cash_collected"] == 0, m)

cid = d["client_id"]
c.post("/api/realm/update", json={"table": "clients", "patch": {"status": "QA", "build_pct": 100},
                                  "filters": [{"op": "eq", "col": "id", "val": cid}]})
c.post("/api/realm/insert", json={"table": "approvals", "rows": [{
    "type": "ship", "title": "Ship build", "agent": "SHIPPER", "payload": {"client_id": cid}}]})
ship = [a for a in sel(table="approvals", filters=[{"op": "eq", "col": "type", "val": "ship"}])["data"]][0]
c.post("/api/realm/rpc/approve_approval", json={"p_id": ship["id"]})
m = sel(table="money_view")["data"]
check("MRR appears only after ship approval", m["mrr"] == 1500 and m["active_clients"] == 1, m)
c.post("/api/realm/update", json={"table": "clients", "patch": {"setup_paid": True},
                                  "filters": [{"op": "eq", "col": "id", "val": cid}]})
check("cash appears only when marked paid", sel(table="money_view")["data"]["cash_collected"] == 4000)

print("\n── the no-air gate ──")
c.post("/api/realm/insert", json={"table": "product_specs", "rows": [
    {"title": "Booking Bot", "pitch": "Answers after hours", "price_band": 149,
     "spec": {"includes": ["prompt pack", "embed snippet"]}}]})
spec = sel(table="product_specs")["data"][0]
r = c.post("/api/realm/rpc/spec_to_product", json={"p_spec": spec["id"]})
pid = r.json()["data"]["product_id"]
prod = sel(table="vault_products")["data"][0]
check("idea lands as inactive concept",
      prod["active"] is False and prod["fulfillment"] == "concept" and prod["audit_status"] == "pending", prod)

r = c.post("/api/realm/rpc/list_product", json={"p_id": pid})
check("cannot list a concept", r.status_code == 400 and "No selling air" in r.text, r.text)

c.post("/api/realm/update", json={"table": "vault_products", "patch": {"fulfillment": "deliverable_now"},
                                  "filters": [{"op": "eq", "col": "id", "val": pid}]})
r = c.post("/api/realm/rpc/list_product", json={"p_id": pid})
check("cannot list unaudited", r.status_code == 400 and "audit" in r.text, r.text)

c.post("/api/realm/update", json={"table": "vault_products", "patch": {"audit_status": "passed"},
                                  "filters": [{"op": "eq", "col": "id", "val": pid}]})
r = c.post("/api/realm/rpc/list_product", json={"p_id": pid})
check("cannot list with no asset", r.status_code == 400 and "asset" in r.text, r.text)

c.post("/api/realm/update", json={"table": "vault_products", "patch": {"asset_ref": "s3://vault/booking-bot.zip"},
                                  "filters": [{"op": "eq", "col": "id", "val": pid}]})
r = c.post("/api/realm/rpc/list_product", json={"p_id": pid})
check("lists once it genuinely exists", r.status_code == 200, r.text)
check("catalog_view counts it live", sel(table="catalog_view")["data"]["live"] == 1)

r = c.post("/api/realm/update", json={"table": "vault_products", "patch": {"active": True, "fulfillment": "concept"},
                                      "filters": [{"op": "eq", "col": "id", "val": pid}]})
check("gate also blocks the back door (direct update)", r.status_code == 400, r.text)

print("\n── staff floor ──")
r = c.post("/api/realm/rpc/invite_staff", json={"p_email": "Caller@Example.com  ", "p_name": "Sam",
                                                "p_role": "caller", "p_commission": 10})
staff = r.json()["data"]
check("invite created", r.status_code == 200 and staff["email"] == "caller@example.com", r.text)
check("invite code minted", staff["invite_code"].startswith("inv_"))
r = c.post("/api/realm/rpc/invite_staff", json={"p_email": "x@y.com", "p_role": "director"})
check("cannot invite a second director", r.status_code == 400, r.text)

quiet = [l for l in sel(table="leads")["data"] if l["biz"] == "Quiet Cafe"][0]
c.post("/api/realm/rpc/assign_lead", json={"p_lead": quiet["id"], "p_staff": staff["id"]})
r = c.post("/api/realm/rpc/log_call", json={"p_lead": quiet["id"], "p_outcome": "interested",
                                            "p_notes": "wants a callback"})
check("call logged", r.status_code == 200, r.text)
check("outcome moved the lead",
      [l for l in sel(table="leads")["data"] if l["biz"] == "Quiet Cafe"][0]["stage"] == "Replied")
r = c.post("/api/realm/rpc/log_call", json={"p_lead": quiet["id"], "p_outcome": "sold_them_a_dream"})
check("bogus outcome rejected", r.status_code == 400, r.text)
tv = {r["role"]: r for r in sel(table="team_view")["data"]}
check("team_view lists the invited caller", tv["caller"]["assigned"] == 1, tv)
check("director's own call is attributed", tv["director"]["calls_7d"] == 1, tv)

print("\n── order pricing gate ──")
r = c.post("/api/realm/insert", json={"table": "orders", "rows": [
    {"brief": "need a booking flow", "fee": 2750, "package": "Growth"}]})
check("off-catalog fee rejected", r.status_code == 400, r.text)
r = c.post("/api/realm/insert", json={"table": "orders", "rows": [
    {"brief": "need a booking flow", "fee": 4000, "mrr": 1500, "package": "Growth", "tier": "Optimize"}]})
check("catalog fee accepted", r.status_code == 200, r.text)

print("\n── public doors ──")
w = c.post("/api/realm/rpc/create_widget", json={"p_client": cid, "p_booking": "https://cal.example/x"}).json()["data"]
check("widget minted", w["key"].startswith("yw_"), w)
r = c.post("/api/realm/public/intake", json={"key": w["key"], "message": "do you do after-hours?"})
check("anonymous enquiry accepted", r.status_code == 200, r.text)
check("enquiry stored", len(sel(table="enquiries")["data"]) == 1)
r = c.post("/api/realm/public/intake", json={"key": "yw_nope", "message": "hi"})
check("unknown widget key rejected", r.status_code == 404, r.status_code)
r = c.post("/api/realm/public/intake", json={"key": w["key"], "message": "   "})
check("empty enquiry rejected", r.status_code == 400, r.status_code)

print("\n── injection / whitelist safety ──")
r = c.post("/api/realm/select", json={"table": "users"})
check("cannot read the auth users table", r.status_code == 400, r.text)
r = c.post("/api/realm/insert", json={"table": "actions", "rows": [{"kind": "forged"}]})
check("cannot forge an action ledger row", r.status_code == 400, r.text)
r = c.post("/api/realm/update", json={"table": "clients", "patch": {"owner": "someone-else"},
                                      "filters": [{"op": "eq", "col": "id", "val": cid}]})
check("cannot reassign ownership", r.status_code == 400, r.text)
r = c.post("/api/realm/select", json={"table": "leads",
                                      "filters": [{"op": "$where", "col": "x", "val": "1"}]})
check("unknown operator rejected", r.status_code == 400, r.text)

print(f"\n{'='*54}\n{len(PASS)} passed · {len(FAIL)} failed")
if FAIL:
    print("failed: " + ", ".join(FAIL))
    sys.exit(1)
