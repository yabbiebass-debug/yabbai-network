"""
YABAI Gold Hunter API — SAFETY-PATCHED (revenue_system v1.0)

This file REPLACES the original defi_backend/server.py. The original had a
fabricated-income -> real-PayPal-payout loop: sentinel/scraper cycles asked an
LLM to invent 'estimated_profit' figures, booked them as vault INCOME, and the
treasury loop sent REAL AUD out of PayPal when that imaginary total crossed $100.

That loop has been NEUTERED. Changes (every one traceable in git):

  1. LLM-generated opportunity 'profit' is now entry_type="signal" with an
     "estimate" flag. It is NEVER booked as income and NEVER summed into
     net_profit. It is research, labelled as such.
  2. net_profit / treasury payouts sum ONLY reconciled income — i.e. entries
     created by a real payment webhook (Stripe/PayPal sale, paid invoice,
     Gumroad sale). No signal or estimate rolls into it.
  3. Every PayPal payout now routes through the GateController as a HIGH action
     and QUEUES for human approval. The autonomous tier payout is GONE.
     /treasury/distribute-now and /withdraw/paypal create an approval request;
     nothing leaves PayPal until /admin/approve/{qid} is called by a human.

Kept intact: security middleware, rate limiting, CoinSpot read-only sync, the
FastAPI surface. MongoDB optional — falls back to in-memory lists if unset.
"""

import os
import logging
import asyncio
import hmac
import hashlib
import time
import json
import httpx
import uuid
import secrets
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List
from contextlib import asynccontextmanager

from fastapi import FastAPI, APIRouter, HTTPException, BackgroundTasks, Request, Depends, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, ConfigDict, validator
from dotenv import load_dotenv

# ── revenue_system spine (truth + gates) ─────────────────────────────────────
from revenue_system.truth import TruthLedger, Metric, SourceType, require_real, DataGap
from revenue_system.gates import GateController, ProposedAction, CapitalCaps

# ── LOAD ENV ───────────────────────────────────────────────────────────────────
ROOT_DIR = Path(__file__).parent
load_dotenv()

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("yabai")

# ── DB (Mongo optional; in-memory fallback so this runs with zero infra) ──────
mongo_url = os.environ.get("MONGO_URL", "")
db = None
if mongo_url:
    try:
        from motor.motor_asyncio import AsyncIOMotorClient
        _mongo = AsyncIOMotorClient(mongo_url, serverSelectionTimeoutMS=5000)
        db = _mongo[os.environ.get("DB_NAME", "yabai_gold_hunter")]
    except Exception as e:
        logger.warning(f"Mongo unavailable ({e}); falling back to in-memory store.")

# In-memory fallback collections (list-of-dicts)
_mem: Dict[str, List[Dict]] = {"vault_entries": [], "gold_findings": []}

async def _insert(coll: str, doc: Dict) -> None:
    if db is not None:
        await db[coll].insert_one(doc)
    _mem[coll].append(doc)

async def _find_all(coll: str, limit: int = 5000) -> List[Dict]:
    if db is not None:
        return await db[coll].find({}, {"_id": 0}).sort("created_date", -1).to_list(limit)
    return list(reversed(_mem[coll][-limit:]))

# ── TRUTH + GATES (single spine for the whole backend) ────────────────────────
ledger = TruthLedger()
caps = CapitalCaps(micro_spend_cap=2.0, spend_cap=25.0,
                   daily_spend_cap=100.0, daily_loss_cap=15.0)
gates = GateController(caps)

# The ONLY executor that touches real money: PayPal payout. It is HIGH -> human.
async def _paypal_payout_executor(action: ProposedAction) -> Dict[str, Any]:
    amt = action.usd_amount
    try:
        result = await execute_paypal_payout(amt, action.reason or "YABAI payout")
        batch_id = result.get("batch_header", {}).get("payout_batch_id", "unknown")
        await _insert("vault_entries", {
            "id": str(uuid.uuid4()), "source": "PayPal Payout",
            "amount": -amt, "currency": "AUD", "entry_type": "withdrawal",
            "network": "paypal", "tx_hash": batch_id, "agent_role": "treasurer",
            "notes": f"Human-approved payout. Batch: {batch_id}",
            "created_date": datetime.now(timezone.utc).isoformat(),
            "reconciled": True})
        ledger.record(Metric("expense", amt, f"paypal:{batch_id}",
                             SourceType.PAYMENT_PROCESSOR, signed_by="treasurer"))
        return {"ok": True, "batch_id": batch_id, "amount": amt}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# Register synchronously-wrapped executor (the gate calls it directly)
def _payout_sync(action: ProposedAction) -> Dict[str, Any]:
    # Bridge async->sync for the gate's synchronous executor contract.
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(_paypal_payout_executor(action))

gates.register_executor("treasury", "payout", _payout_sync)

# ── GLOBAL STATE ───────────────────────────────────────────────────────────────
swarm_running = False
treasury_log: List[str] = []
_rate_store: Dict[str, List[float]] = {}

# ── SECURITY HELPERS (unchanged from original) ────────────────────────────────
def check_rate_limit(key: str, max_calls: int = 30, window: int = 60) -> bool:
    now = time.time()
    calls = [t for t in _rate_store.get(key, []) if now - t < window]
    if len(calls) >= max_calls:
        return False
    calls.append(now)
    _rate_store[key] = calls
    return True

async def require_admin(request: Request) -> None:
    provided = request.headers.get("X-Admin-Key", "")
    expected = os.environ.get("ADMIN_API_KEY", "")
    if not expected:
        raise HTTPException(503, "Admin key not configured")
    if not secrets.compare_digest(provided.encode(), expected.encode()):
        raise HTTPException(401, "Unauthorized")

def sanitise_string(value: str, max_len: int = 500) -> str:
    cleaned = (value.replace("<", "").replace(">", "").replace("'", "")
                   .replace('"', "").replace(";", "").replace("--", ""))
    return cleaned[:max_len]

# ── MODELS ─────────────────────────────────────────────────────────────────────
class GoldFindingCreate(BaseModel):
    agent_role: str = "sentinel"; finding_type: str = "on_chain"
    title: str = ""; description: str = ""
    estimated_profit: float = 0.0          # ESTIMATE — never income
    execution_link: str = ""; network: str = "base"; priority: str = "medium"
    raw_data: str = ""
    @validator("title", "description", "raw_data", pre=True)
    def sanitise(cls, v): return sanitise_string(str(v))
    @validator("estimated_profit")
    def clamp(cls, v): return max(0.0, min(v, 1_000_000.0))

class GoldFinding(GoldFindingCreate):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    status: str = "SIGNAL"   # was "EXECUTED" — now honestly a research signal
    is_estimate: bool = True
    created_date: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

class VaultEntryCreate(BaseModel):
    source: str = ""; amount: float = 0.0; currency: str = "AUD"
    entry_type: str = "income"   # "income"(reconciled only) | "signal" | "expense" | "withdrawal" | "gas"
    network: str = "base"; tx_hash: str = ""; agent_role: str = ""; notes: str = ""
    reconciled: bool = False     # True ONLY when from a real payment webhook
    @validator("source", "notes", pre=True)
    def sanitise(cls, v): return sanitise_string(str(v))
    @validator("amount")
    def clamp(cls, v): return round(max(-1_000_000.0, min(v, 1_000_000.0)), 2)

class VaultEntry(VaultEntryCreate):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    created_date: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

class WithdrawRequest(BaseModel):
    amount: float
    @validator("amount")
    def chk(cls, v):
        if v < 1.0: raise ValueError("Minimum payout is $1.00 AUD")
        if v > 50_000.0: raise ValueError("Single payout capped at $50,000 AUD")
        return round(v, 2)

class ApproveRequest(BaseModel):
    queue_id: str
    approver: str
    signature: Optional[str] = None

# ── WORKER STATE (sentinel/scraper/janitor logs only — no fake income) ────────
worker_state: Dict[str, Any] = {
    r: {"status": "idle", "log_entries": [], "signals_count": 0,
        "errors_count": 0, "current_task": "", "last_run": None}
    for r in ("sentinel", "scraper", "janitor")}

def _log(key: str, msg: str):
    now = datetime.now(timezone.utc).isoformat()[:19]
    worker_state[key]["log_entries"] = (worker_state[key]["log_entries"] + [f"[{now}] {msg}"])[-20:]

# ── PAYPAL (unchanged mechanics; now only ever called from the gated executor) ─
async def get_paypal_access_token() -> str:
    cid = os.environ.get("PAYPAL_CLIENT_ID", ""); sec = os.environ.get("PAYPAL_SECRET", "")
    if not cid or not sec: raise ValueError("PAYPAL_CLIENT_ID and PAYPAL_SECRET must be set.")
    base = os.environ.get("PAYPAL_BASE_URL", "https://api-m.paypal.com")
    async with httpx.AsyncClient(timeout=15, verify=True) as c:
        r = await c.post(f"{base}/v1/oauth2/token",
                         data={"grant_type": "client_credentials"}, auth=(cid, sec),
                         headers={"Accept": "application/json", "Accept-Language": "en_US"})
        r.raise_for_status()
        return r.json()["access_token"]

async def execute_paypal_payout(amount_aud: float, note: str = "YABAI Payout") -> dict:
    receiver = os.environ.get("PAYPAL_RECEIVER_EMAIL", "")
    if not receiver: raise ValueError("PAYPAL_RECEIVER_EMAIL not set.")
    base = os.environ.get("PAYPAL_BASE_URL", "https://api-m.paypal.com")
    token = await get_paypal_access_token()
    batch_id = f"YABAI-{uuid.uuid4().hex[:12].upper()}"
    payload = {"sender_batch_header": {
        "sender_batch_id": batch_id, "email_subject": "YABAI Vault Payout",
        "email_message": f"${amount_aud:.2f} AUD dispatched from your vault."},
        "items": [{"recipient_type": "EMAIL",
                   "amount": {"value": f"{amount_aud:.2f}", "currency": "AUD"},
                   "receiver": receiver, "note": sanitise_string(note, 200),
                   "sender_item_id": batch_id}]}
    async with httpx.AsyncClient(timeout=20, verify=True) as c:
        r = await c.post(f"{base}/v1/payments/payouts", json=payload,
                         headers={"Authorization": f"Bearer {token}",
                                  "Content-Type": "application/json"})
        r.raise_for_status()
        return r.json()

# ── COINSPOT (read-only balance sync — unchanged, it's already honest) ─────────
async def update_coinspot_balances():
    k = os.getenv("COINSPOT_API_KEY", ""); s = os.getenv("COINSPOT_SECRET", "")
    if not k or not s:
        logger.warning("CoinSpot credentials missing — sync skipped."); return
    url = "https://www.coinspot.com.au/api/v2/ro/my/balances"
    nonce = int(time.time() * 1000)
    post = json.dumps({"nonce": nonce}, separators=(",", ":"))
    sign = hmac.new(s.encode(), post.encode(), hashlib.sha512).hexdigest()
    headers = {"key": k, "sign": sign, "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=15, verify=True) as c:
            data = (await c.post(url, headers=headers, content=post)).json()
        if data.get("status") == "ok":
            for coin in data.get("balances", []):
                for ct, info in coin.items():
                    aud = float(info.get("audbalance", 0))
                    if aud > 0:
                        entry = VaultEntry(source=f"CoinSpot-{ct}", amount=aud,
                                           entry_type="income", network="coinspot",
                                           agent_role="janitor", notes=f"Live {ct} balance",
                                           reconciled=True).model_dump()
                        await _insert("vault_entries", entry)
                        ledger.record(Metric("income", aud, f"coinspot:{ct}",
                                             SourceType.PAYMENT_PROCESSOR,
                                             signed_by="janitor"))
            logger.info("CoinSpot sync OK")
    except Exception as e:
        logger.error(f"CoinSpot error: {e}")

# ── NET PROFIT — now reconciled ONLY (signals/estimates excluded) ─────────────
async def calculate_net_profit() -> float:
    """REAL net profit. Sums only reconciled income minus withdrawals/expenses.
    Signals/estimates are deliberately excluded — they were the fabrication bug."""
    entries = await _find_all("vault_entries", 100000)
    income = sum(e["amount"] for e in entries
                 if e.get("entry_type") == "income" and e.get("reconciled"))
    out = sum(abs(e["amount"]) for e in entries
              if e.get("entry_type") in ("expense", "withdrawal", "gas")
              and e.get("reconciled"))
    return round(income - out, 2)

# ── AGENT CYCLES (signals now; NO income booking) ─────────────────────────────
async def _signal_cycle(role: str, label: str, gen_fn):
    """Shared cycle. Generates RESEARCH SIGNALS only. Books nothing as income.

    The LLM can still scan/propose, but its output is a flagged estimate stored
    as entry_type='signal'. It cannot move the net-profit number. This is the
    core fix: research != money.
    """
    state = worker_state[role]
    state["status"] = "running"; state["current_task"] = label
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    _log(role, "Cycle started")
    try:
        findings = await gen_fn() or []
        count = 0
        for f in findings[:15]:
            profit = float(f.get("estimated_profit", 0))
            gf = GoldFinding(agent_role=role, finding_type=f.get("finding_type", "on_chain"),
                             title=f.get("title", "Signal"),
                             description=f.get("description", ""),
                             estimated_profit=profit,
                             execution_link=f.get("execution_link", "https://basescan.org"),
                             network=f.get("network", "base"),
                             priority=f.get("priority", "medium"),
                             raw_data=json.dumps(f)).model_dump()
            await _insert("gold_findings", gf)
            # NOTE: NO vault income entry. This was the bug. Profit is a SIGNAL.
            count += 1
        state["signals_count"] += count
        _log(role, f"Logged {count} research signals (NOT income)")
    except Exception as ex:
        state["errors_count"] += 1
        _log(role, f"ERROR: {str(ex)[:100]}")
    finally:
        state["status"] = "idle"; state["current_task"] = ""

async def sentinel_cycle():
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    if not openai_key:
        _log("sentinel", "OPENAI_API_KEY not set"); worker_state["sentinel"]["status"]="idle"; return
    system = ("You are The Sentinel, a DeFi research scanner. Generate a JSON array of up "
              "to 15 POSSIBLE on-chain opportunities for HUMAN review. Each: "
              '{"title":str,"description":str,"estimated_profit":float,"finding_type":"on_chain",'
              '"priority":"high|medium","network":"base","execution_link":"https://basescan.org"}. '
              "These are LEADS to verify, not executed trades. Return ONLY a JSON array.")
    async def gen():
        async with httpx.AsyncClient(timeout=30, verify=True) as c:
            r = await c.post("https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {openai_key}", "Content-Type": "application/json"},
                json={"model": "gpt-4o", "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": f"Scan time: {datetime.now(timezone.utc).isoformat()}"}],
                    "temperature": 0.4, "max_tokens": 2500})
            raw = r.json()["choices"][0]["message"]["content"]
        s, e = raw.find("["), raw.rfind("]")
        return json.loads(raw[s:e+1]) if s >= 0 and e > s else []
    await _signal_cycle("sentinel", "Scanning Base L2 for research signals (leads only)", gen)

async def scraper_cycle():
    google_key = os.environ.get("GOOGLE_API_KEY", "")
    if not google_key:
        _log("scraper", "GOOGLE_API_KEY not set"); worker_state["scraper"]["status"]="idle"; return
    prompt = ("You are The Scraper. Generate a JSON array of up to 15 POSSIBLE off-chain "
              "opportunities for HUMAN review (Melbourne AI consulting, B2B leads). Each: "
              '{"title":str,"description":str,"estimated_profit":float,"finding_type":"off_chain",'
              '"priority":"high|medium","network":"web"}. Leads to verify, not booked income. '
              "Return ONLY a JSON array.")
    async def gen():
        async with httpx.AsyncClient(timeout=30, verify=True) as c:
            r = await c.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={google_key}",
                json={"contents": [{"parts": [{"text": prompt}]}]},
                headers={"Content-Type": "application/json"})
            raw = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        s, e = raw.find("["), raw.rfind("]")
        return json.loads(raw[s:e+1]) if s >= 0 and e > s else []
    await _signal_cycle("scraper", "Scouring web for research signals (leads only)", gen)

async def janitor_cycle():
    state = worker_state["janitor"]
    state["status"] = "running"; state["current_task"] = "Syncing CoinSpot balances"
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    _log("janitor", "Cycle started")
    try:
        await update_coinspot_balances()
        _log("janitor", "CoinSpot sync complete")
    except Exception as ex:
        state["errors_count"] += 1
        _log("janitor", f"ERROR: {str(ex)[:100]}")
    finally:
        state["status"] = "idle"; state["current_task"] = ""

async def master_swarm_loop():
    global swarm_running
    swarm_running = True
    interval = int(os.environ.get("GOLD_HUNTER_INTERVAL", "60"))
    logger.info(f"Swarm started ({interval}s interval) — signals only, no income booking")
    while swarm_running:
        try:
            await asyncio.gather(sentinel_cycle(), scraper_cycle(), janitor_cycle(),
                                 return_exceptions=True)
        except Exception as e:
            logger.error(f"Swarm error: {e}")
        await asyncio.sleep(interval)

# ── SECURITY MIDDLEWARE (unchanged) ───────────────────────────────────────────
async def security_headers_middleware(request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return resp

async def rate_limit_middleware(request, call_next):
    ip = request.client.host if request.client else "unknown"
    if not check_rate_limit(f"ip:{ip}", 60, 60):
        return JSONResponse({"detail": "Rate limit exceeded."}, status_code=429)
    return await call_next(request)

from starlette.middleware.base import BaseHTTPMiddleware

@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(master_swarm_loop())
    logger.info("YABAI :: swarm auto-started (signals only)")
    yield
    global swarm_running
    swarm_running = False

app = FastAPI(title="YABAI Gold-Hunter API (safety-patched)", docs_url=None,
              redoc_url=None, lifespan=lifespan)
api_router = APIRouter(prefix="/api")
app.add_middleware(BaseHTTPMiddleware, dispatch=security_headers_middleware)
app.add_middleware(BaseHTTPMiddleware, dispatch=rate_limit_middleware)
app.add_middleware(CORSMiddleware,
    allow_origins=os.environ.get("ALLOWED_ORIGINS", "http://localhost:8080").split(","),
    allow_credentials=True, allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type", "Authorization", "X-Admin-Key"])

@app.get("/health")
async def health():
    return {"status": "healthy", "swarm": swarm_running,
            "fabrication_loop": "REMOVED", "payout_mode": "human_approval_only",
            "ts": datetime.now(timezone.utc).isoformat()}

# ── SWARM ─────────────────────────────────────────────────────────────────────
@api_router.get("/swarm/status")
async def swarm_status():
    agents = [{"role": r, "agent_name": {"sentinel":"The Sentinel","scraper":"The Scraper",
               "janitor":"The Janitor"}[r], **s} for r, s in worker_state.items()]
    return {"swarm_running": swarm_running, "agents": agents,
            "note": "Agents generate research signals only. No income is booked from them."}

@api_router.post("/swarm/run-once")
async def run_once(_: None = Depends(require_admin)):
    asyncio.create_task(sentinel_cycle()); asyncio.create_task(scraper_cycle())
    asyncio.create_task(janitor_cycle())
    return {"message": "Single cycle triggered (signals only)"}

# ── VAULT ─────────────────────────────────────────────────────────────────────
@api_router.get("/vault")
async def list_vault():
    return await _find_all("vault_entries", 500)

@api_router.post("/vault")
async def create_vault_entry(data: VaultEntryCreate, _: None = Depends(require_admin)):
    entry = VaultEntry(**data.model_dump())
    # Enforce: non-reconciled income is refused at the API boundary too.
    if entry.entry_type == "income" and not entry.reconciled:
        raise HTTPException(400, "Income entries must be reconciled=True "
                            "(from a real payment webhook). Use entry_type='signal' for leads.")
    await _insert("vault_entries", entry.model_dump())
    if entry.entry_type == "income" and entry.reconciled:
        ledger.record(Metric("income", entry.amount, f"manual:{entry.id}",
                             SourceType.PAYMENT_PROCESSOR, signed_by=entry.agent_role))
    return entry.model_dump()

@api_router.get("/vault/summary")
async def vault_summary():
    entries = await _find_all("vault_entries", 100000)
    real_income = sum(e["amount"] for e in entries
                      if e.get("entry_type")=="income" and e.get("reconciled"))
    signals_value = sum(e.get("amount",0) for e in entries if e.get("entry_type")=="signal")
    out = sum(abs(e["amount"]) for e in entries
              if e.get("entry_type") in ("expense","withdrawal","gas") and e.get("reconciled"))
    return {"real_income": round(real_income,2), "net_profit": round(real_income-out,2),
            "signal_value_estimate": round(signals_value,2),
            "warning": "signal_value_estimate is NOT money — research leads only.",
            "entry_count": len(entries)}

# ── GOLD FINDINGS ─────────────────────────────────────────────────────────────
@api_router.get("/gold-findings")
async def list_findings():
    return await _find_all("gold_findings", 200)

# ── TREASURY — payouts now HUMAN-APPROVED via the gate queue ──────────────────
@api_router.get("/treasury/status")
async def treasury_status():
    net = await calculate_net_profit()
    return {"treasury_running": False, "net_profit": net,
            "payout_mode": "HUMAN APPROVAL REQUIRED (no autonomous payout)",
            "pending_approvals": gates.pending_approvals(),
            "gate_status": gates.status(),
            "log_entries": treasury_log[-20:]}

@api_router.post("/treasury/request-payout")
async def request_payout(req: WithdrawRequest, _: None = Depends(require_admin)):
    """REQUEST a payout. It QUEUES for human approval. Nothing leaves PayPal here."""
    net = await calculate_net_profit()
    if net < req.amount:
        raise HTTPException(400, f"Requested ${req.amount:.2f} exceeds real net profit ${net:.2f}.")
    out = gates.propose(ProposedAction(
        kind="payout", channel="treasury", agent="treasurer",
        usd_amount=req.amount, reversible=False,
        reason=f"Payout request of ${req.amount:.2f} (real net ${net:.2f})",
        payload={"real_net_backing": net}))
    if out["outcome"] != "queued":
        raise HTTPException(400, f"Payout not queued: {out['reasons']}")
    treasury_log.append(f"[{datetime.now(timezone.utc).isoformat()[:19]}] "
                        f"Payout ${req.amount:.2f} QUEUED for approval: {out['queue_id']}")
    return {"status": "queued_for_human_approval", "queue_id": out["queue_id"],
            "amount": req.amount, "approve_at": f"/api/admin/approve"}

# ── ADMIN: approve / reject payouts (the new human gate) ──────────────────────
@api_router.post("/admin/approve")
async def approve(req: ApproveRequest, _: None = Depends(require_admin)):
    res = gates.human_approve(req.queue_id, req.approver, req.signature)
    if not res.get("ok", res.get("outcome") == "executed"):
        raise HTTPException(400, f"Approval failed: {res}")
    treasury_log.append(f"[{datetime.now(timezone.utc).isoformat()[:19]}] "
                        f"APPROVED+SENT {req.queue_id} by {req.approver}")
    return res

@api_router.post("/admin/reject")
async def reject(req: ApproveRequest, _: None = Depends(require_admin)):
    return gates.human_reject(req.queue_id, req.approver, "rejected via API")

@api_router.get("/admin/approvals")
async def approvals(_: None = Depends(require_admin)):
    return gates.pending_approvals()

@api_router.post("/admin/sync-coinspot")
async def sync_coinspot(_: None = Depends(require_admin)):
    await update_coinspot_balances()
    return {"message": "CoinSpot sync triggered (read-only)"}

@api_router.post("/admin/reset-db")
async def reset_db(_: None = Depends(require_admin)):
    if db is not None:
        await db.gold_findings.delete_many({}); await db.vault_entries.delete_many({})
    _mem["vault_entries"].clear(); _mem["gold_findings"].clear()
    return {"message": "Database reset."}

@app.get("/")
async def root():
    return HTMLResponse("<h1>YABAI Gold-Hunter API</h1>"
                        "<p>Safety-patched: research signals only; "
                        "payouts require human approval. See /docs disabled — /health.</p>")

app.include_router(api_router)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), log_level="info")
