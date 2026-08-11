"""
YABBAI REALM — the agency data plane.

The six Director/Client surfaces (/app Mission Control, /os Realm OS, /floor Agency
Floor, /studio Catalog Studio, /portal Client Portal, /vault The Vault) were written
against Supabase Postgres: 20-odd tables, 12 RPCs, 4 views and 4 Deno edge functions.
None of that was ever provisioned — the schema was never run and no edge function
exists in this repo — so every one of those surfaces boots into a "config needed"
gate and shows nothing.

This router is that missing layer, implemented over the Mongo the backend already
owns. Same table names, same RPC names, same view shapes, same invariants — so the
surfaces work with only a client swap, and nothing is fabricated to fill a gap.

Invariants carried over from `frontend/public/sql/yabbai_schema.sql` (unchanged):
  · every row is owner-scoped (the Postgres RLS equivalent)
  · state transitions happen here, never client-side
  · the no-air gate: a vault product may only go active when it is deliverable_now
    AND audit_status=passed AND has a real asset_ref
  · the catalog-price gate: order fee ∈ {1500,4000,8000}, mrr ∈ {500,1500,3500}
  · nothing side-effectful happens without an approved approval row
  · money = closed clients only; an empty database reports zero, never a guess
"""

import os
import re
import uuid
import secrets
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Header
from pydantic import BaseModel, Field

from network_db import db
from auth_router import require_director, _session_and_user
import ai_router


async def _ai(system, prompt, **kw):
    """ai_router.route_complete returns {content,tier,model} in production, but the
    unit tests stub it with a plain string. Coerce to the text string either way."""
    res = await ai_router.route_complete(system, prompt, **kw)
    return res.get("content", "") if isinstance(res, dict) else (res or "")

router = APIRouter(prefix="/api/realm", tags=["realm"])

# ── catalog (the only prices that may ever be written) ────────────────────────
PACKAGES = {"Starter": 1500, "Growth": 4000, "Full Ops": 8000}
TIERS = {"Maintain": 500, "Optimize": 1500, "Scale": 3500}

LEAD_STAGES = ["New", "Outreach drafted", "Contacted", "Replied", "Awaiting close", "Won", "Dead"]
APPROVAL_TYPES = ["outreach", "contract", "ship", "fork", "save", "spend"]
CLIENT_STATUSES = ["Onboarding", "QA", "Ship pending", "Active", "Paused", "Churned"]
CALL_OUTCOMES = ["no_answer", "not_interested", "callback", "interested", "booked_meeting", "closed_won"]

# Mongo collections are prefixed so they never collide with the auth/goldscout ones.
TABLES = {
    "leads", "clients", "approvals", "actions", "cycles", "events",
    "orders", "value_events", "fleet_updates", "referrals",
    "widgets", "enquiries", "vault_products", "vault_sessions",
    "product_specs", "product_audits", "staff", "assignments", "calls",
    "import_batches", "diagnostics",
}
VIEWS = {"money_view", "catalog_view", "team_view", "my_value_view"}

# Which column carries ownership for each table (the RLS column in the SQL).
OWNER_COL = {
    "leads": "owner", "clients": "owner", "approvals": "owner", "actions": "owner",
    "cycles": "owner", "events": "owner", "vault_products": "owner",
    "vault_sessions": "owner", "product_specs": "owner", "product_audits": "owner",
    "staff": "director", "assignments": "director", "calls": "director",
    "import_batches": "director", "widgets": "director", "fleet_updates": "director",
    "referrals": "referrer", "orders": "client_user", "value_events": "client_user",
    "enquiries": "director", "diagnostics": "owner",
}

# Tables the world may read without a session (the SQL's `using (active = true)` policy).
PUBLIC_READ = {"vault_products"}

# Tables a client-portal user may write directly (everything else is RPC-only).
CLIENT_WRITABLE = {"orders", "referrals"}

MUTABLE = {
    "leads": {"biz", "niche", "suburb", "pain", "email", "phone", "notes", "stage",
              "fit", "score", "source", "batch_id", "assigned_staff"},
    "clients": {"biz", "package", "tier", "mrr", "setup_fee", "setup_paid", "status",
                "build_pct", "health", "stripe_customer_id"},
    "approvals": {"type", "title", "detail", "payload", "agent", "license", "license_class"},
    "vault_products": {"slug", "title", "tagline", "description", "includes", "price_aud",
                       "stripe_link", "gumroad_url", "badge", "config_schema", "sort",
                       "fulfillment", "asset_ref", "audit_status", "audit_notes", "active"},
    "product_specs": {"title", "pitch", "audience", "build_effort", "price_band", "spec", "status"},
    "import_batches": {"source", "filename", "rows_in", "rows_added", "rows_dupe"},
    "fleet_updates": {"title", "detail"},
    "referrals": {"code", "referred_email", "status", "credit"},
    "orders": {"brief", "scope", "package", "fee", "tier", "mrr", "status", "kind"},
    "enquiries": {"status", "ai_reply"},
    "widgets": {"greeting", "booking_url", "accent", "active", "biz"},
    "value_events": {"kind", "amount", "note"},
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(v: Any) -> Any:
    if isinstance(v, datetime):
        return (v if v.tzinfo else v.replace(tzinfo=timezone.utc)).isoformat()
    if isinstance(v, dict):
        return {k: _iso(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_iso(x) for x in v]
    return v


def _clean(doc: Optional[dict]) -> Optional[dict]:
    if doc is None:
        return None
    return {k: _iso(v) for k, v in doc.items() if k != "_id"}


def _col(table: str):
    return db[f"realm_{table}"]


async def _optional_user(request: Request, authorization: Optional[str]):
    """Session if there is one, else None. Used by the public vault gallery."""
    session, user = await _session_and_user(request, authorization)
    if not user or not session.get("mfa_verified"):
        return None
    return user


# ═══════════════════════════════════════════════════════════════════════════════
# QUERY  — the PostgREST-shaped read path the surfaces already speak
# ═══════════════════════════════════════════════════════════════════════════════

class Filter(BaseModel):
    op: str
    col: str
    val: Any = None


class SelectBody(BaseModel):
    table: str
    columns: str = "*"
    filters: list[Filter] = Field(default_factory=list)
    order: Optional[dict] = None          # {"col": "created_at", "ascending": false}
    limit: Optional[int] = None
    count: bool = False                   # return a row count alongside/instead of rows
    head: bool = False                    # count only, no rows
    single: bool = False                  # exactly one row (error if none)
    maybe_single: bool = False            # one row or null


_OPS = {"eq": "$eq", "neq": "$ne", "gt": "$gt", "gte": "$gte",
        "lt": "$lt", "lte": "$lte", "in": "$in"}


def _mongo_filter(filters: list[Filter]) -> dict:
    q: dict = {}
    for f in filters:
        if f.op not in _OPS:
            raise HTTPException(400, f"Unsupported filter operator: {f.op}")
        q.setdefault(f.col, {})[_OPS[f.op]] = f.val
    return q


def _project(columns: str) -> Optional[dict]:
    if columns.strip() in ("*", ""):
        return {"_id": 0}
    cols = [c.strip() for c in columns.split(",") if c.strip()]
    proj = {c: 1 for c in cols}
    proj["_id"] = 0
    return proj


@router.post("/select")
async def select(body: SelectBody, request: Request, authorization: Optional[str] = Header(None)):
    table = body.table
    if table in VIEWS:
        user = await _optional_user(request, authorization)
        if not user:
            raise HTTPException(401, "Not authenticated")
        return {"data": await _view(table, user), "count": None}

    if table not in TABLES:
        raise HTTPException(400, f"Unknown table: {table}")

    user = await _optional_user(request, authorization)
    q = _mongo_filter(body.filters)

    if user:
        q[OWNER_COL[table]] = user["user_id"]
    elif table in PUBLIC_READ:
        q["active"] = True          # the anonymous gallery sees live products only
    else:
        raise HTTPException(401, "Not authenticated")

    col = _col(table)

    total = None
    if body.count or body.head:
        total = await col.count_documents(q)
    if body.head:
        return {"data": None, "count": total}

    cursor = col.find(q, _project(body.columns))
    if body.order:
        direction = 1 if body.order.get("ascending", True) else -1
        cursor = cursor.sort(body.order.get("col", "created_at"), direction)
    cursor = cursor.limit(min(body.limit or 500, 1000))
    rows = [_clean(r) for r in await cursor.to_list(length=1000)]

    if body.single or body.maybe_single:
        if not rows:
            if body.single:
                raise HTTPException(404, f"No row found in {table}")
            return {"data": None, "count": total}
        return {"data": rows[0], "count": total}

    return {"data": rows, "count": total}


async def _view(name: str, user: dict) -> dict | list:
    uid = user["user_id"]

    if name == "money_view":
        # MRR counts ACTIVE retainers only; cash counts fees actually marked paid.
        # An empty realm reports zeros — there is no seeded or estimated number here.
        rows = await _col("clients").find({"owner": uid}, {"_id": 0}).to_list(2000)
        return {
            "mrr": sum(float(c.get("mrr") or 0) for c in rows if c.get("status") == "Active"),
            "cash_collected": sum(float(c.get("setup_fee") or 0) for c in rows if c.get("setup_paid")),
            "active_clients": sum(1 for c in rows if c.get("status") == "Active"),
        }

    if name == "catalog_view":
        rows = await _col("vault_products").find({"owner": uid}, {"_id": 0}).to_list(2000)
        return {
            "total": len(rows),
            "live": sum(1 for p in rows if p.get("active")),
            "deliverable": sum(1 for p in rows if p.get("fulfillment") == "deliverable_now"),
            "building": sum(1 for p in rows if p.get("fulfillment") == "building"),
            "concept": sum(1 for p in rows if p.get("fulfillment") == "concept"),
            "audited": sum(1 for p in rows if p.get("audit_status") == "passed"),
        }

    if name == "my_value_view":
        rows = await _col("value_events").find({"client_user": uid}, {"_id": 0}).to_list(5000)
        def s(kind):
            return sum(float(v.get("amount") or 0) for v in rows if v.get("kind") == kind)
        return {"hours_saved": s("hours_saved"), "leads_generated": s("leads_generated"),
                "bookings": s("bookings"), "revenue_assisted": s("revenue_assisted")}

    if name == "team_view":
        staff = await _col("staff").find({"director": uid}, {"_id": 0}).to_list(500)
        cutoff = _now() - timedelta(days=7)
        out = []
        for s in staff:
            assigned = await _col("assignments").count_documents({"staff_id": s["id"]})
            calls = await _col("calls").find({"staff_id": s["id"]}, {"_id": 0}).to_list(2000)
            out.append({
                "staff_id": s["id"], "name": s.get("name"), "role": s.get("role"),
                "status": s.get("status"), "assigned": assigned,
                "calls_7d": sum(1 for c in calls if _as_dt(c.get("created_at")) > cutoff),
                "wins": sum(1 for c in calls if c.get("outcome") == "closed_won"),
            })
        return out

    raise HTTPException(400, f"Unknown view: {name}")


def _as_dt(v) -> datetime:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(str(v))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


# ═══════════════════════════════════════════════════════════════════════════════
# WRITE  — insert / update, owner-scoped, field-whitelisted
# ═══════════════════════════════════════════════════════════════════════════════

class InsertBody(BaseModel):
    table: str
    rows: list[dict]


class UpdateBody(BaseModel):
    table: str
    patch: dict
    filters: list[Filter] = Field(default_factory=list)


def _defaults(table: str, uid: str) -> dict:
    base = {"id": str(uuid.uuid4()), OWNER_COL[table]: uid, "created_at": _now()}
    if table == "leads":
        base |= {"stage": "New", "source": "manual", "fit": None, "score": None,
                 "batch_id": None, "assigned_staff": None, "last_contact_at": None}
    if table == "clients":
        base |= {"setup_fee": 0, "setup_paid": False, "mrr": 0, "status": "Onboarding",
                 "build_pct": 0, "health": "ok", "created_via": "director"}
    if table == "approvals":
        base |= {"status": "pending", "payload": {}, "agent": "SYSTEM", "decided_at": None}
    if table == "vault_products":
        base |= {"includes": [], "price_aud": 0, "config_schema": {}, "active": False,
                 "sort": 100, "fulfillment": "concept", "audit_status": "pending",
                 "asset_ref": None, "origin": "manual"}
    if table == "product_specs":
        base |= {"status": "idea", "spec": {}}
    if table == "import_batches":
        base |= {"rows_in": 0, "rows_added": 0, "rows_dupe": 0}
    if table == "orders":
        base |= {"status": "Draft", "kind": "build", "scope": {}, "client_id": None}
    if table == "referrals":
        base |= {"status": "sent", "credit": 0}
    if table == "fleet_updates":
        base |= {"shipped_at": _now()}
    return base


def _gate_catalog_prices(doc: dict) -> None:
    """Mirrors trg_catalog_prices: the AI may map a scope, it may never price one."""
    fee, mrr = doc.get("fee"), doc.get("mrr")
    if fee is not None and float(fee) not in {float(v) for v in PACKAGES.values()}:
        raise HTTPException(400, "Fee must come from the fixed catalog (1500/4000/8000).")
    if mrr is not None and float(mrr) not in {float(v) for v in TIERS.values()}:
        raise HTTPException(400, "Retainer must come from the fixed catalog (500/1500/3500).")


def _gate_no_air(doc: dict) -> None:
    """Mirrors trg_no_air: nothing goes on sale that doesn't exist yet."""
    if not doc.get("active"):
        return
    if doc.get("fulfillment") != "deliverable_now":
        raise HTTPException(400, f'Cannot list: product is "{doc.get("fulfillment")}", '
                                 f"not deliverable_now. No selling air.")
    if doc.get("audit_status") != "passed":
        raise HTTPException(400, f"Cannot list: product has not passed white-hat audit "
                                 f"(status: {doc.get('audit_status')}).")
    if not (doc.get("asset_ref") or "").strip():
        raise HTTPException(400, "Cannot list: no deliverable asset attached (asset_ref empty).")


@router.post("/insert")
async def insert(body: InsertBody, user=Depends(require_director)):
    table = body.table
    if table not in MUTABLE:
        raise HTTPException(400, f"Table not writable via insert: {table}")
    uid = user["user_id"]
    out = []
    for raw in body.rows:
        allowed = {k: v for k, v in raw.items() if k in MUTABLE[table]}
        doc = _defaults(table, uid) | allowed
        if table == "orders":
            doc["director"] = uid
            _gate_catalog_prices(doc)
        if table == "vault_products":
            doc.setdefault("slug", _slug(doc.get("title", "product")))
            _gate_no_air(doc)
        if table == "leads" and not (doc.get("biz") or "").strip():
            raise HTTPException(400, "A lead needs a business name.")
        await _col(table).insert_one(dict(doc))
        out.append(_clean(doc))
    return {"data": out}


@router.post("/update")
async def update(body: UpdateBody, user=Depends(require_director)):
    table = body.table
    if table not in MUTABLE:
        raise HTTPException(400, f"Table not writable via update: {table}")
    uid = user["user_id"]
    patch = {k: v for k, v in body.patch.items() if k in MUTABLE[table]}
    if not patch:
        raise HTTPException(400, "Nothing updatable in that patch.")

    q = _mongo_filter(body.filters)
    q[OWNER_COL[table]] = uid

    if table in ("orders", "vault_products"):
        # Gates run against the row as it WILL be, not just the patch.
        for existing in await _col(table).find(q, {"_id": 0}).to_list(500):
            merged = existing | patch
            if table == "orders":
                _gate_catalog_prices(merged)
            else:
                _gate_no_air(merged)

    res = await _col(table).update_many(q, {"$set": patch})
    return {"matched": res.matched_count, "modified": res.modified_count}


def _slug(title: str) -> str:
    base = re.sub(r"[^a-zA-Z0-9]+", "-", title).strip("-").lower() or "product"
    return f"{base}-{secrets.token_hex(2)}"


async def _event(uid: str, agent: str, message: str, cycle_n: int = 0) -> None:
    await _col("events").insert_one({
        "id": str(uuid.uuid4()), "owner": uid, "cycle_n": cycle_n,
        "agent": agent, "message": message, "created_at": _now()})


# ═══════════════════════════════════════════════════════════════════════════════
# RPC  — the state transitions. Same names and semantics as the SQL functions.
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/rpc/{name}")
async def rpc(name: str, args: dict, user=Depends(require_director)):
    fn = _RPCS.get(name)
    if not fn:
        raise HTTPException(400, f"Unknown function: {name}")
    return {"data": await fn(user["user_id"], args or {})}


async def _approve_approval(uid: str, a: dict):
    row = await _col("approvals").find_one({"id": a.get("p_id"), "owner": uid, "status": "pending"})
    if not row:
        raise HTTPException(400, "Approval not found or already decided.")
    if row.get("license_class") == "blocked":
        raise HTTPException(400, "License policy: blocked items cannot be approved.")

    await _col("approvals").update_one(
        {"id": row["id"]}, {"$set": {"status": "approved", "decided_at": _now()}})

    payload = row.get("payload") or {}
    t = row["type"]
    if t == "outreach":
        await _col("leads").update_one({"id": payload.get("lead_id"), "owner": uid},
                                       {"$set": {"stage": "Contacted"}})
    elif t == "contract":
        await _col("leads").update_one({"id": payload.get("lead_id"), "owner": uid},
                                       {"$set": {"stage": "Awaiting close"}})
    elif t == "ship":
        await _col("clients").update_one({"id": payload.get("client_id"), "owner": uid},
                                         {"$set": {"status": "Active"}})
    elif t == "save":
        await _col("clients").update_one({"id": payload.get("client_id"), "owner": uid},
                                         {"$set": {"health": "ok"}})

    await _col("actions").insert_one({"id": str(uuid.uuid4()), "owner": uid,
                                      "approval_id": row["id"], "kind": t,
                                      "result": {}, "executed_at": _now()})
    await _event(uid, "DIRECTOR", f"approved: {row['title']}")
    return {"ok": True}


async def _reject_approval(uid: str, a: dict):
    res = await _col("approvals").update_one(
        {"id": a.get("p_id"), "owner": uid, "status": "pending"},
        {"$set": {"status": "rejected", "decided_at": _now()}})
    if not res.matched_count:
        raise HTTPException(400, "Approval not found or already decided.")
    await _event(uid, "DIRECTOR", f"rejected approval {a.get('p_id')}")
    return {"ok": True}


async def _mark_replied(uid: str, a: dict):
    lead = await _col("leads").find_one({"id": a.get("p_lead_id"), "owner": uid})
    if not lead:
        raise HTTPException(400, "Lead not found.")
    await _col("leads").update_one({"id": lead["id"]}, {"$set": {"stage": "Replied"}})
    await _col("approvals").insert_one({
        "id": str(uuid.uuid4()), "owner": uid, "type": "contract",
        "title": f"Proposal → {lead['biz']}",
        "detail": "Lead replied positive. Approve to send proposal; you take the close call.",
        "agent": "PITCHER", "payload": {"lead_id": lead["id"]},
        "status": "pending", "decided_at": None, "created_at": _now()})
    return {"ok": True}


async def _close_deal(uid: str, a: dict):
    pkg, tier = a.get("p_package"), a.get("p_tier")
    if pkg not in PACKAGES or tier not in TIERS:
        raise HTTPException(400, "Package and tier must come from the catalog.")
    # Prices come from the catalog, never from the client payload.
    fee, mrr = PACKAGES[pkg], TIERS[tier]

    lead = await _col("leads").find_one(
        {"id": a.get("p_lead_id"), "owner": uid, "stage": {"$in": ["Awaiting close", "Replied"]}})
    if not lead:
        raise HTTPException(400, "Lead not found or not ready to close.")
    await _col("leads").update_one({"id": lead["id"]}, {"$set": {"stage": "Won"}})

    cid = str(uuid.uuid4())
    await _col("clients").insert_one({
        "id": cid, "owner": uid, "lead_id": lead["id"], "biz": lead["biz"],
        "package": pkg, "setup_fee": fee, "setup_paid": False, "tier": tier, "mrr": mrr,
        "status": "Onboarding", "build_pct": 0, "health": "ok",
        "stripe_customer_id": None, "created_via": "director", "created_at": _now()})
    await _event(uid, "DIRECTOR",
                 f"closed {lead['biz']} — {pkg} ${fee} + {tier} ${mrr}/mo "
                 f"(setup fee unpaid until reconciled)")
    return {"client_id": cid, "fee": fee, "mrr": mrr}


async def _create_widget(uid: str, a: dict):
    client = await _col("clients").find_one({"id": a.get("p_client"), "owner": uid})
    if not client:
        raise HTTPException(400, "Client not found.")
    w = {"id": str(uuid.uuid4()), "director": uid, "client_id": client["id"],
         "client_user": None, "key": "yw_" + secrets.token_hex(9), "biz": client["biz"],
         "greeting": "G'day — how can we help?", "booking_url": a.get("p_booking"),
         "accent": "#9945FF", "active": True, "created_at": _now()}
    await _col("widgets").insert_one(dict(w))
    await _event(uid, "OS", f"widget provisioned for {client['biz']}")
    return _clean(w)


async def _invite_staff(uid: str, a: dict):
    role = a.get("p_role")
    if role not in ("manager", "caller"):
        raise HTTPException(400, "Role must be manager or caller.")
    email = (a.get("p_email") or "").strip().lower()
    if not email:
        raise HTTPException(400, "An invite needs an email address.")
    if await _col("staff").find_one({"director": uid, "email": email}):
        raise HTTPException(400, "That teammate is already invited.")
    s = {"id": str(uuid.uuid4()), "director": uid, "user_id": None, "email": email,
         "name": a.get("p_name"), "role": role, "status": "invited",
         "invite_code": "inv_" + secrets.token_hex(7),
         "commission_pct": float(a.get("p_commission") or 0), "created_at": _now()}
    await _col("staff").insert_one(dict(s))
    return _clean(s)


async def _accept_invite(uid: str, a: dict):
    res = await _col("staff").update_one(
        {"invite_code": a.get("p_code"), "user_id": None},
        {"$set": {"user_id": uid, "status": "active"}})
    if not res.matched_count:
        raise HTTPException(400, "Invite not found or already used.")
    return {"ok": True}


async def _assign_lead(uid: str, a: dict):
    lead = await _col("leads").find_one({"id": a.get("p_lead"), "owner": uid})
    if not lead:
        raise HTTPException(400, "Lead not found.")
    staff = await _col("staff").find_one({"id": a.get("p_staff"), "director": uid})
    if not staff:
        raise HTTPException(400, "Staff member not found.")
    await _col("assignments").update_one(
        {"lead_id": lead["id"]},
        {"$set": {"director": uid, "staff_id": staff["id"], "assigned_at": _now()},
         "$setOnInsert": {"id": str(uuid.uuid4())}}, upsert=True)
    await _col("leads").update_one({"id": lead["id"]}, {"$set": {"assigned_staff": staff["id"]}})
    return {"ok": True}


_OUTCOME_STAGE = {
    "no_answer": "Contacted", "not_interested": "Dead", "callback": "Contacted",
    "interested": "Replied", "booked_meeting": "Replied",
    "closed_won": "Awaiting close",   # still gated: Director confirms + payment
}


async def _log_call(uid: str, a: dict):
    outcome = a.get("p_outcome")
    if outcome not in CALL_OUTCOMES:
        raise HTTPException(400, f"Outcome must be one of {', '.join(CALL_OUTCOMES)}.")
    lead = await _col("leads").find_one({"id": a.get("p_lead"), "owner": uid})
    if not lead:
        raise HTTPException(400, "Lead not found.")
    staff = await _col("staff").find_one({"user_id": uid, "director": uid})
    if not staff:
        # The SQL dropped attribution when the caller had no staff row, which meant a
        # solo Director's own calls never showed on the floor. Bind them a director row.
        staff = {"id": str(uuid.uuid4()), "director": uid, "user_id": uid,
                 "email": None, "name": "Director", "role": "director", "status": "active",
                 "invite_code": None, "commission_pct": 0, "created_at": _now()}
        await _col("staff").insert_one(dict(staff))
    await _col("calls").insert_one({
        "id": str(uuid.uuid4()), "director": uid, "lead_id": lead["id"],
        "staff_id": staff["id"] if staff else None, "guide": a.get("p_guide"),
        "channel": a.get("p_channel") or "call", "outcome": outcome,
        "notes": a.get("p_notes"), "created_at": _now()})
    await _col("leads").update_one(
        {"id": lead["id"]},
        {"$set": {"stage": _OUTCOME_STAGE[outcome], "last_contact_at": _now()}})
    await _event(uid, (staff or {}).get("name") or "CALLER",
                 f"logged {outcome} on {lead['biz']}")
    return {"ok": True}


async def _spec_to_product(uid: str, a: dict):
    sp = await _col("product_specs").find_one({"id": a.get("p_spec"), "owner": uid})
    if not sp:
        raise HTTPException(400, "Spec not found.")
    spec = sp.get("spec") or {}
    pid = str(uuid.uuid4())
    await _col("vault_products").insert_one({
        "id": pid, "owner": uid, "slug": _slug(sp["title"]), "title": sp["title"],
        "tagline": sp.get("pitch"), "description": spec.get("description") or sp.get("pitch"),
        "includes": spec.get("includes") or [], "price_aud": float(sp.get("price_band") or 0),
        "stripe_link": None, "gumroad_url": None, "badge": None,
        "config_schema": spec.get("config_schema") or {},
        # A fresh idea is a concept, not stock. It cannot go live until it exists.
        "active": False, "sort": 100, "fulfillment": "concept", "audit_status": "pending",
        "asset_ref": None, "origin": "ideation", "created_at": _now()})
    await _col("product_specs").update_one({"id": sp["id"]}, {"$set": {"status": "built"}})
    return {"product_id": pid}


async def _list_product(uid: str, a: dict):
    p = await _col("vault_products").find_one({"id": a.get("p_id"), "owner": uid})
    if not p:
        raise HTTPException(400, "Product not found.")
    _gate_no_air(p | {"active": True})
    await _col("vault_products").update_one({"id": p["id"]}, {"$set": {"active": True}})
    await _event(uid, "STUDIO", f"listed {p['title']} — audited, deliverable, asset attached")
    return {"ok": True}


async def _accept_quote(uid: str, a: dict):
    res = await _col("orders").update_one(
        {"id": a.get("p_order"), "client_user": uid, "status": "Scoped"},
        {"$set": {"status": "Quoted"}})
    if not res.matched_count:
        raise HTTPException(400, "Order not found or not ready.")
    return {"ok": True}


_RPCS = {
    "approve_approval": _approve_approval, "reject_approval": _reject_approval,
    "mark_replied": _mark_replied, "close_deal": _close_deal,
    "create_widget": _create_widget, "invite_staff": _invite_staff,
    "accept_invite": _accept_invite, "assign_lead": _assign_lead, "log_call": _log_call,
    "spec_to_product": _spec_to_product, "list_product": _list_product,
    "accept_quote": _accept_quote,
}


# ═══════════════════════════════════════════════════════════════════════════════
# FUNCTIONS — what the four never-deployed Deno edge functions were meant to do
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/fn/{name}")
async def fn(name: str, body: dict, user=Depends(require_director)):
    if name == "cycle-runner":
        return await _cycle_runner(user["user_id"])
    if name in ("scope-brief", "call-guide", "catalog-agent"):
        return await _ai_passthrough(name, body or {})
    raise HTTPException(400, f"Unknown function: {name}")


async def _ai_passthrough(name: str, body: dict):
    """Same brains as /api/ai/*, reached through the surfaces' function-call shape."""
    if name == "scope-brief":
        return await _call_ai_scope(body)
    if name == "call-guide":
        return await _call_ai_guide(body)
    return await _call_ai_catalog(body)


async def _call_ai_scope(body: dict):
    system = ("You scope automation work for an Australian agency. Map the brief to ONE "
              "catalog package (Starter/Growth/Full Ops) and ONE retainer tier "
              "(Maintain/Optimize/Scale). You do NOT set prices — the catalog does. "
              "Reply as JSON: {package, tier, scope:{summary, deliverables:[]}, rationale}.")
    raw = await _ai(system, str(body.get("brief", ""))[:4000])
    data = ai_router._safe_json(raw) or {"scope": {"summary": raw}}
    data.pop("fee", None)
    data.pop("mrr", None)      # price never comes back from a model
    return data


async def _call_ai_guide(body: dict):
    lead = body.get("lead") or {}
    system = ("You write short, honest cold-call guides for an Australian agency. "
              "No guarantees, no income claims, no pressure tactics. Give an opener, "
              "three discovery questions, two likely objections with replies, and a "
              "single next step. Plain text, under 200 words.")
    prompt = (f"Business: {lead.get('biz')}\nNiche: {lead.get('niche')}\n"
              f"Suburb: {lead.get('suburb')}\nKnown pain: {lead.get('pain')}")
    return {"guide": await _ai(system, prompt)}


async def _call_ai_catalog(body: dict):
    system = ("You are a product ideation agent for a solo automation studio. Propose "
              "buildable products only — nothing requiring headcount or a licence the "
              "studio doesn't hold. Reply as JSON: "
              "{ideas:[{title,pitch,audience,build_effort,price_band,spec:{description,includes:[]}}]}.")
    raw = await _ai(system, str(body.get("prompt", ""))[:4000])
    return ai_router._safe_json(raw) or {"ideas": [], "raw": raw}


async def _cycle_runner(uid: str) -> dict:
    """
    One agent cycle. Scores every unscored lead with the real LLM router and drafts
    outreach for the Hot ones — as PENDING approvals, never as sends.

    If no LLM tier answers, the cycle FAILS. It does not fall back to random scores;
    a fabricated fit rating is worse than no rating.
    """
    n = await _col("cycles").count_documents({"owner": uid}) + 1
    leads = await _col("leads").find(
        {"owner": uid, "stage": "New"}, {"_id": 0}).to_list(200)

    if not leads:
        await _col("cycles").insert_one({
            "id": str(uuid.uuid4()), "owner": uid, "n": n,
            "summary": {"qualified": 0, "drafted": 0}, "started_at": _now()})
        await _event(uid, "QUALIFIER", "nothing to score — add real prospects first", n)
        return {"cycle": n, "qualified": 0, "drafted": 0,
                "message": "No new leads to score."}

    qualified = drafted = 0
    for lead in leads:
        system = ("You qualify B2B leads for a Melbourne automation agency. Score fit 0-100 "
                  "on how well an automation retainer suits them. Reply as JSON only: "
                  '{"score":0-100,"fit":"Hot|Warm|Cold","why":"one sentence"}.')
        prompt = (f"Business: {lead.get('biz')}\nNiche: {lead.get('niche') or 'unknown'}\n"
                  f"Suburb: {lead.get('suburb') or 'unknown'}\n"
                  f"Stated pain: {lead.get('pain') or 'unknown'}")
        try:
            raw = await _ai(system, prompt, session_id=f"cycle-{n}")
        except Exception as e:
            await _event(uid, "QUALIFIER", f"cycle {n} halted — no AI tier answered ({e})", n)
            raise HTTPException(503, "No AI tier answered. Cycle halted rather than "
                                     "guessing scores — check Settings → providers.")

        parsed = ai_router._safe_json(raw) or {}
        fit = parsed.get("fit")
        if fit not in ("Hot", "Warm", "Cold"):
            await _event(uid, "QUALIFIER", f"unreadable score for {lead['biz']} — left unscored", n)
            continue
        score = max(0, min(100, int(parsed.get("score") or 0)))

        await _col("leads").update_one({"id": lead["id"]},
                                       {"$set": {"fit": fit, "score": score}})
        qualified += 1
        await _event(uid, "QUALIFIER", f"{lead['biz']} → {fit} ({score}) · {parsed.get('why','')}", n)

        if fit != "Hot":
            continue

        draft_system = ("Write a short, plain cold email from a solo Melbourne automation "
                        "operator. No guarantees, no income claims, no fake urgency, no "
                        "flattery. Name one specific thing you'd fix and ask for a 10-minute "
                        "call. Under 120 words. Plain text, no subject line.")
        draft = await _ai(draft_system, prompt, session_id=f"cycle-{n}")
        await _col("approvals").insert_one({
            "id": str(uuid.uuid4()), "owner": uid, "type": "outreach",
            "title": f"Outreach → {lead['biz']}",
            "detail": draft.strip(),
            "agent": "PITCHER", "payload": {"lead_id": lead["id"]},
            "status": "pending", "decided_at": None, "created_at": _now()})
        await _col("leads").update_one({"id": lead["id"]},
                                       {"$set": {"stage": "Outreach drafted"}})
        drafted += 1

    await _col("cycles").insert_one({
        "id": str(uuid.uuid4()), "owner": uid, "n": n,
        "summary": {"qualified": qualified, "drafted": drafted}, "started_at": _now()})
    await _event(uid, "CYCLE", f"cycle {n} complete — {qualified} scored, {drafted} drafts queued", n)
    return {"cycle": n, "qualified": qualified, "drafted": drafted}


# ═══════════════════════════════════════════════════════════════════════════════
# PUBLIC — the two anonymous doors. End users have no login and never should.
# ═══════════════════════════════════════════════════════════════════════════════

class IntakeBody(BaseModel):
    key: str
    name: str = ""
    contact: str = ""
    message: str
    want_booking: bool = False


@router.post("/public/intake")
async def public_intake(body: IntakeBody):
    """An embedded widget on a client's own site posts here. Anonymous by design."""
    widget = await _col("widgets").find_one({"key": body.key, "active": True})
    if not widget:
        raise HTTPException(404, "Unknown or inactive widget.")
    msg = (body.message or "").strip()
    if not msg:
        raise HTTPException(400, "Message is empty.")
    if len(msg) > 4000:
        raise HTTPException(400, "Message too long.")

    reply = None
    try:
        system = (f"You answer first-contact enquiries for {widget['biz']}. Be brief, warm "
                  "and concrete. Never quote a price, never promise a timeframe, never "
                  "claim a result. If they want to book, point at the booking link. "
                  "Under 70 words.")
        reply = await _ai(system, msg[:2000], session_id="intake")
    except Exception:
        reply = None            # a dead AI tier must not drop the enquiry

    await _col("enquiries").insert_one({
        "id": str(uuid.uuid4()), "widget_id": widget["id"], "director": widget["director"],
        "client_user": widget.get("client_user"), "name": body.name[:120] or None,
        "contact": body.contact[:200] or None, "message": msg, "ai_reply": reply,
        "want_booking": bool(body.want_booking), "status": "new", "created_at": _now()})
    await _event(widget["director"], "WIDGET", f"enquiry via {widget['biz']}")
    return {"ok": True, "reply": reply, "booking_url": widget.get("booking_url")}


class VaultConfigBody(BaseModel):
    action: str = "next"
    slug: str
    qa: list = Field(default_factory=list)
    email: str = ""


@router.post("/public/vault-config")
async def public_vault_config(body: VaultConfigBody):
    """
    The Vault configurator. Anyone can run it on a LIVE product — which, because of
    the no-air gate, means a product that actually exists and has passed audit.
    """
    p = await _col("vault_products").find_one({"slug": body.slug, "active": True}, {"_id": 0})
    if not p:
        raise HTTPException(404, "Product not found or not listed.")

    qa = body.qa[:12]
    if body.action == "next":
        if len(qa) >= 8:
            return {"done": True}
        system = (f'You configure "{p["title"]}" for a buyer. Ask ONE short question that '
                  "changes how the deliverable is set up. Never ask for payment details, "
                  "passwords, or anything you don't need. Reply as JSON only: "
                  '{"question":"...","options":["...","..."],"done":false}. '
                  'Set done true once you have enough to configure it.')
        raw = await _ai(system, str(qa)[:3000], session_id="vault")
        parsed = ai_router._safe_json(raw)
        if not parsed:
            return {"done": True}
        return parsed

    if body.action == "finish":
        system = (f'Write the setup guide for "{p["title"]}" based on the buyer\'s answers. '
                  "Only describe what this product actually includes: "
                  f"{'; '.join(p.get('includes') or []) or p.get('description') or ''}. "
                  "Invent no features. Plain text, step by step.")
        guide = await _ai(system, str(qa)[:3000], session_id="vault")
        await _col("vault_sessions").insert_one({
            "id": str(uuid.uuid4()), "owner": p["owner"], "product_id": p["id"],
            "qa": qa, "guide": guide, "email": (body.email or "").strip()[:200] or None,
            "created_at": _now()})
        if body.email:
            await _event(p["owner"], "VAULT", f"configurator lead on {p['title']}")
        return {"done": True, "guide": guide}

    raise HTTPException(400, "Unknown action.")


# ═══════════════════════════════════════════════════════════════════════════════
# Health + bootstrap
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/health")
async def health():
    return {"ok": True, "app": "realm", "version": "1.0.0",
            "tables": len(TABLES), "views": len(VIEWS),
            "rpcs": sorted(_RPCS), "ts": _now().isoformat()}


@router.get("/summary")
async def summary(user=Depends(require_director)):
    """One call for the hub strip — real counts, or nulls when a count fails."""
    uid = user["user_id"]
    out: dict[str, Any] = {}
    for t in ("leads", "clients", "approvals", "events", "cycles", "orders",
              "vault_products", "widgets", "enquiries", "staff", "diagnostics"):
        try:
            out[t] = await _col(t).count_documents({OWNER_COL[t]: uid})
        except Exception:
            out[t] = None          # never render a confident zero on a failed read
    out["money"] = await _view("money_view", user)
    # The hub strip renders live figures, not raw table sizes — compute those exactly,
    # with the same failed-count → null contract (render "—", never a confident 0).
    async def _count(t, q):
        try:
            return await _col(t).count_documents({OWNER_COL[t]: uid, **q})
        except Exception:
            return None
    out["strip"] = {
        "leads":     await _count("leads", {"stage": {"$ne": "Dead"}}),
        "approvals": await _count("approvals", {"status": "pending"}),
        "enquiries": await _count("enquiries", {}),
        "diagnostics": await _count("diagnostics", {}),
        "products_live": await _count("vault_products", {"active": True}),
    }
    return out


@router.post("/indexes")
async def ensure_indexes(user=Depends(require_director)):
    """Idempotent. Run once after deploy; cheap enough to re-run any time."""
    created = []
    for t in TABLES:
        owner = OWNER_COL[t]
        await _col(t).create_index([(owner, 1), ("created_at", -1)])
        await _col(t).create_index("id", unique=True)
        created.append(t)
    await _col("staff").create_index("invite_code", unique=True, sparse=True)
    await _col("vault_products").create_index("slug", unique=True, sparse=True)
    await _col("assignments").create_index("lead_id", unique=True)
    return {"ok": True, "indexed": sorted(created)}
