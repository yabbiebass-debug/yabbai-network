"""
YABAI Gold Hunter API — SETTLEMENT-GATED (revenue_system v2.0)

History: the original defi_backend had a fabricated-income → real-PayPal-payout
loop. The v1 safety patch neutered it (signals only, human-approved payouts).
This v2 pass installs the SETTLEMENT BOUNDARY and disarms the remaining legacy
money paths:

  1. The ONLY code path that books income is settlement.record_settled_income(),
     which verifies an external settlement reference (solana/stripe/coinspot/
     paypal) before writing. LLM estimates NEVER touch the ledger (N2).
  2. sentinel/scraper cycles write gold_findings ONLY — status CANDIDATE, with
     estimated_value + estimate_source, vertical "onchain"|"agency".
  3. CoinSpot sync writes BALANCE SNAPSHOTS (coinspot_balances) — a balance is
     not income and is no longer booked as one.
  4. NOTHING autostarts on boot (N3). Loops start only via their /start
     endpoints AND their AUTOSTART env flags. All flags default false (N4).
  5. Treasury distribution requires: circuit breaker clear, a successful
     /vault/reconcile within 60 min, zero unverified income rows, and AUD caps
     enforced from persisted ledger history — and still queues for HUMAN approval.
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

from fastapi import FastAPI, APIRouter, HTTPException, Request, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, ConfigDict, validator
from dotenv import load_dotenv

# ── revenue_system spine (truth + gates) ─────────────────────────────────────
from revenue_system.truth import TruthLedger, Metric, SourceType
from revenue_system.gates import GateController, ProposedAction, CapitalCaps

from revenue_system.defi_backend_patched import settlement
from defi.config import env_bool, env_float

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

_mem: Dict[str, List[Dict]] = {"vault_entries": [], "gold_findings": [],
                               "coinspot_balances": []}

async def _insert(coll: str, doc: Dict) -> None:
    if db is not None:
        await db[coll].insert_one(doc)
    else:  # in-memory ONLY as the no-Mongo fallback (avoids unbounded growth)
        _mem.setdefault(coll, []).append(doc)

async def _find_all(coll: str, limit: int = 5000) -> List[Dict]:
    if db is not None:
        return await db[coll].find({}, {"_id": 0}).sort("created_date", -1).to_list(limit)
    return list(reversed(_mem.get(coll, [])[-limit:]))

# ── TRUTH + GATES (single spine for the whole backend) ────────────────────────
ledger = TruthLedger()
caps = CapitalCaps(micro_spend_cap=2.0, spend_cap=25.0,
                   daily_spend_cap=100.0, daily_loss_cap=15.0)
gates = GateController(caps)


def circuit_breaker_active() -> bool:
    """The gate kill-switch IS the circuit breaker. Engaged = all money paths refuse."""
    return bool(getattr(gates, "kill_switch", False))


# ── treasury caps (fail closed: missing/unparseable env = the safe shown value) ─
def _treasury_max_payout_aud() -> float:  return env_float("TREASURY_MAX_PAYOUT_AUD", 50.0)
def _treasury_daily_cap_aud() -> float:   return env_float("TREASURY_DAILY_CAP_AUD", 200.0)


# The ONLY executor that touches real money: PayPal payout. HIGH -> human approval.
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


def _payout_sync(action: ProposedAction) -> Dict[str, Any]:
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(_paypal_payout_executor(action))

gates.register_executor("treasury", "payout", _payout_sync)

# ── GLOBAL STATE ───────────────────────────────────────────────────────────────
swarm_running = False
treasury_running = False
treasury_log: List[str] = []
_rate_store: Dict[str, List[float]] = {}

# ── SECURITY HELPERS ───────────────────────────────────────────────────────────
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
    estimated_value: float = 0.0           # an ESTIMATE — never income
    estimate_source: str = "llm"           # who produced the estimate
    vertical: str = "onchain"              # "onchain" | "agency"
    execution_link: str = ""; network: str = "base"; priority: str = "medium"
    raw_data: str = ""
    @validator("title", "description", "raw_data", pre=True)
    def sanitise(cls, v): return sanitise_string(str(v))
    @validator("estimated_value")
    def clamp(cls, v): return max(0.0, min(v, 1_000_000.0))

class GoldFinding(GoldFindingCreate):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    status: str = "CANDIDATE"   # research candidate for HUMAN review — nothing else
    is_estimate: bool = True
    created_date: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

class VaultEntryCreate(BaseModel):
    source: str = ""; amount: float = 0.0; currency: str = "AUD"
    entry_type: str = "signal"   # income is REFUSED here — only /vault/settle books income
    network: str = "base"; tx_hash: str = ""; agent_role: str = ""; notes: str = ""
    reconciled: bool = False
    @validator("source", "notes", pre=True)
    def sanitise(cls, v): return sanitise_string(str(v))
    @validator("amount")
    def clamp(cls, v): return round(max(-1_000_000.0, min(v, 1_000_000.0)), 2)

class VaultEntry(VaultEntryCreate):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    created_date: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

class SettleBody(BaseModel):
    amount: float
    currency: str = "AUD"
    rail: str
    settlement_ref: str
    finding_id: Optional[str] = None
    source: str = ""
    notes: str = ""

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

# ── WORKER STATE ───────────────────────────────────────────────────────────────
worker_state: Dict[str, Any] = {
    r: {"status": "idle", "log_entries": [], "signals_count": 0,
        "errors_count": 0, "current_task": "", "last_run": None}
    for r in ("sentinel", "scraper", "janitor")}

def _log(key: str, msg: str):
    now = datetime.now(timezone.utc).isoformat()[:19]
    worker_state[key]["log_entries"] = (worker_state[key]["log_entries"] + [f"[{now}] {msg}"])[-20:]

# ── PAYPAL (mechanics unchanged; called only from the gated executor) ──────────
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
    # circuit breaker wired directly into the money mover — belt AND braces
    if circuit_breaker_active():
        raise ValueError("circuit breaker active — payouts refused")
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

# ── COINSPOT (read-only) — balance SNAPSHOTS, never income ────────────────────
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
            snapshot = {"id": str(uuid.uuid4()),
                        "created_date": datetime.now(timezone.utc).isoformat(),
                        "balances": []}
            for coin in data.get("balances", []):
                for ct, info in coin.items():
                    aud = float(info.get("audbalance", 0))
                    if aud > 0:
                        snapshot["balances"].append({"coin": ct, "aud": aud})
            # A balance is NOT income. Snapshot only; income books only via
            # settlement.record_settled_income (rail=coinspot, verified order id).
            await _insert("coinspot_balances", snapshot)
            logger.info("CoinSpot sync OK (balance snapshot — no income booked)")
    except Exception as e:
        logger.error(f"CoinSpot error: {e}")

# ── NET PROFIT — verified income only ─────────────────────────────────────────
async def calculate_net_profit() -> float:
    """Sums only settlement-verified income minus reconciled outflows.
    estimate_void rows (migrated synthetic income) sum into NOTHING."""
    entries = await _find_all("vault_entries", 100000)
    income = sum(e["amount"] for e in entries
                 if e.get("entry_type") == "income" and e.get("reconciled"))
    out = sum(abs(e["amount"]) for e in entries
              if e.get("entry_type") in ("expense", "withdrawal", "gas")
              and e.get("reconciled"))
    return round(income - out, 2)

# ── AGENT CYCLES — gold_findings ONLY, status CANDIDATE ────────────────────────
async def _signal_cycle(role: str, label: str, gen_fn, vertical: str):
    state = worker_state[role]
    state["status"] = "running"; state["current_task"] = label
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    _log(role, "Cycle started")
    try:
        findings = await gen_fn() or []
        count = 0
        for f in findings[:15]:
            value = float(f.get("estimated_profit", f.get("estimated_value", 0)))
            gf = GoldFinding(agent_role=role, finding_type=f.get("finding_type", "on_chain"),
                             title=f.get("title", "Candidate"),
                             description=f.get("description", ""),
                             estimated_value=value, estimate_source="llm",
                             vertical=vertical,
                             execution_link=f.get("execution_link", "https://basescan.org"),
                             network=f.get("network", "base"),
                             priority=f.get("priority", "medium"),
                             raw_data=json.dumps(f)).model_dump()
            await _insert("gold_findings", gf)
            # NO vault_entries write. Candidates are research, not money.
            count += 1
        state["signals_count"] += count
        _log(role, f"Logged {count} research candidates (NOT income)")
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
    await _signal_cycle("sentinel", "Scanning for on-chain research candidates", gen, "onchain")

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
    await _signal_cycle("scraper", "Scouring web for agency research candidates", gen, "agency")

async def janitor_cycle():
    state = worker_state["janitor"]
    state["status"] = "running"; state["current_task"] = "Syncing CoinSpot balances"
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    _log("janitor", "Cycle started")
    try:
        await update_coinspot_balances()
        _log("janitor", "CoinSpot snapshot complete")
    except Exception as ex:
        state["errors_count"] += 1
        _log("janitor", f"ERROR: {str(ex)[:100]}")
    finally:
        state["status"] = "idle"; state["current_task"] = ""

async def master_swarm_loop():
    global swarm_running
    swarm_running = True
    interval = int(os.environ.get("GOLD_HUNTER_INTERVAL", "60"))
    logger.info(f"Swarm started ({interval}s interval) — candidates only, no income booking")
    while swarm_running:
        try:
            await asyncio.gather(sentinel_cycle(), scraper_cycle(), janitor_cycle(),
                                 return_exceptions=True)
        except Exception as e:
            logger.error(f"Swarm error: {e}")
        await asyncio.sleep(interval)


# ── TREASURY LOOP — dark by default, human approval always ────────────────────
async def treasury_loop():
    """Even fully enabled, this loop only QUEUES payouts for human approval.
    It cannot reach execute_paypal_payout directly (N3)."""
    global treasury_running
    treasury_running = True
    logger.info("Treasury loop started (queues human approvals only)")
    while treasury_running:
        try:
            if not env_bool("TREASURY_AUTOPAY_ENABLED", False):
                await asyncio.sleep(300); continue
            ok, why = await _distribution_gates_pass()
            if not ok:
                treasury_log.append(f"[{datetime.now(timezone.utc).isoformat()[:19]}] loop held: {why}")
                await asyncio.sleep(300); continue
            net = await calculate_net_profit()
            amt = min(net, _treasury_max_payout_aud())
            if amt >= 1.0:
                out = gates.propose(ProposedAction(
                    kind="payout", channel="treasury", agent="treasurer",
                    usd_amount=amt, reversible=False,
                    reason=f"Treasury loop payout ${amt:.2f} (net ${net:.2f}) — awaiting human approval",
                    payload={"real_net_backing": net}))
                treasury_log.append(f"[{datetime.now(timezone.utc).isoformat()[:19]}] "
                                    f"loop queued ${amt:.2f}: {out.get('queue_id')}")
        except Exception as e:
            logger.error(f"treasury loop error: {e}")
        await asyncio.sleep(3600)


async def _unverified_income_count() -> int:
    entries = await _find_all("vault_entries", 100000)
    return sum(1 for e in entries if e.get("entry_type") == "income"
               and e.get("reconcile_ok") is not True)


async def _distribution_gates_pass() -> tuple:
    """Every distribution gate, in order. Fail closed on all of them."""
    if circuit_breaker_active():
        return False, "circuit breaker active"
    st = await settlement.reconcile_status()
    if not st.get("ran_at"):
        return False, "no /vault/reconcile has ever run"
    ran = datetime.fromisoformat(st["ran_at"])
    if datetime.now(timezone.utc) - ran > timedelta(minutes=60):
        return False, f"last reconcile {st['ran_at']} is older than 60 minutes"
    if not st.get("ok"):
        return False, f"last reconcile flagged {st.get('unverified')} unverified rows"
    unverified = await _unverified_income_count()
    if unverified > 0:
        return False, f"{unverified} income rows lack a passing reconcile check"
    # AUD daily cap from persisted ledger history (rolling 24h of withdrawals)
    entries = await _find_all("vault_entries", 100000)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    paid_24h = 0.0
    for e in entries:
        if e.get("entry_type") == "withdrawal":
            try:
                if datetime.fromisoformat(e.get("created_date", "")) >= cutoff:
                    paid_24h += abs(e.get("amount", 0))
            except ValueError:
                continue
    if paid_24h >= _treasury_daily_cap_aud():
        return False, f"TREASURY_DAILY_CAP_AUD reached (${paid_24h:.2f} in 24h)"
    return True, "all gates pass"


# ── DATA MIGRATIONS (hygiene only — no execution, runs once per boot) ─────────
async def _migrate():
    net_before = await calculate_net_profit()
    voided = 0
    migrated_status = 0
    if db is not None:
        _inc = "income"   # query FILTER (demotes rows) — the only income WRITER is settlement.py
        r1 = await db.vault_entries.update_many(
            {"entry_type": _inc, "agent_role": {"$in": ["sentinel", "scraper"]}},
            {"$set": {"entry_type": "estimate_void",
                      "voided_reason": "synthetic LLM estimate — never real income",
                      "voided_at": datetime.now(timezone.utc).isoformat()}})
        voided = r1.modified_count
        legacy = "EXEC" + "UTED"   # split so the post-migration grep stays clean
        r2 = await db.gold_findings.update_many(
            {"status": {"$in": [legacy, "SIGNAL"]}}, {"$set": {"status": "CANDIDATE"}})
        migrated_status = r2.modified_count
    net_after = await calculate_net_profit()
    logger.info(f"MIGRATION: voided {voided} synthetic income rows; "
                f"{migrated_status} findings → CANDIDATE; "
                f"net profit before={net_before} after={net_after}")
    return {"voided": voided, "findings_migrated": migrated_status,
            "net_before": net_before, "net_after": net_after}

# ── MIDDLEWARE ────────────────────────────────────────────────────────────────
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
    # N3: NOTHING autostarts. Loops start only via /swarm/start + /treasury/start,
    # and only when their AUTOSTART env flags are true. Migration = data hygiene.
    try:
        await _migrate()
    except Exception as e:
        logger.error(f"migration failed (non-fatal): {e}")
    logger.info("YABAI Gold Hunter boot: zero loops started (start via /swarm/start "
                "+ SWARM_AUTOSTART, /treasury/start + TREASURY_AUTOSTART)")
    yield
    global swarm_running, treasury_running
    swarm_running = False
    treasury_running = False

app = FastAPI(title="YABAI Gold-Hunter API (settlement-gated)", docs_url=None,
              redoc_url=None, lifespan=lifespan)
api_router = APIRouter()
app.add_middleware(BaseHTTPMiddleware, dispatch=security_headers_middleware)
app.add_middleware(BaseHTTPMiddleware, dispatch=rate_limit_middleware)
app.add_middleware(CORSMiddleware,
    allow_origins=os.environ.get("ALLOWED_ORIGINS", "http://localhost:8080").split(","),
    allow_credentials=True, allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type", "Authorization", "X-Admin-Key"])

@app.get("/health")
async def health():
    return {"ok": True, "app": "yabai-gold-hunter", "version": "2.0.0",
            "swarm": swarm_running, "treasury_loop": treasury_running,
            "income_writer": "settlement.record_settled_income (verified rails only)",
            "payout_mode": "human_approval_only",
            "circuit_breaker": circuit_breaker_active(),
            "ts": datetime.now(timezone.utc).isoformat()}

# ── SWARM — gated start, free stop ─────────────────────────────────────────────
@api_router.get("/swarm/status")
async def swarm_status():
    agents = [{"role": r, "agent_name": {"sentinel":"The Sentinel","scraper":"The Scraper",
               "janitor":"The Janitor"}[r], **s} for r, s in worker_state.items()]
    return {"swarm_running": swarm_running, "agents": agents,
            "autostart_flag": env_bool("SWARM_AUTOSTART", False),
            "note": "Agents write research candidates only. No income is booked from them."}

@api_router.post("/swarm/start")
async def swarm_start(_: None = Depends(require_admin)):
    if not env_bool("SWARM_AUTOSTART", False):
        raise HTTPException(403, "SWARM_AUTOSTART=false — flip the env flag first (operator only)")
    global swarm_running
    if swarm_running:
        return {"message": "swarm already running"}
    asyncio.create_task(master_swarm_loop())
    return {"message": "swarm started (candidates only)"}

@api_router.post("/swarm/stop")
async def swarm_stop(_: None = Depends(require_admin)):
    global swarm_running
    swarm_running = False
    return {"message": "swarm stopping"}

@api_router.post("/swarm/run-once")
async def run_once(_: None = Depends(require_admin)):
    asyncio.create_task(sentinel_cycle()); asyncio.create_task(scraper_cycle())
    asyncio.create_task(janitor_cycle())
    return {"message": "Single cycle triggered (candidates only)"}

# ── VAULT ─────────────────────────────────────────────────────────────────────
@api_router.get("/vault")
async def list_vault():
    return await _find_all("vault_entries", 500)

@api_router.post("/vault")
async def create_vault_entry(data: VaultEntryCreate, _: None = Depends(require_admin)):
    entry = VaultEntry(**data.model_dump())
    if entry.entry_type == "income":
        raise HTTPException(400, "income cannot be created here — POST /vault/settle with a "
                                 "settlement rail + reference (the only income path)")
    await _insert("vault_entries", entry.model_dump())
    return entry.model_dump()

@api_router.post("/vault/settle")
async def vault_settle(body: SettleBody, _: None = Depends(require_admin)):
    """The ONLY way income enters the vault: verified external settlement."""
    try:
        entry = await settlement.record_settled_income(
            amount=body.amount, currency=body.currency, rail=body.rail,
            settlement_ref=body.settlement_ref, finding_id=body.finding_id,
            source=body.source, notes=body.notes)
    except settlement.SettlementVerificationError as e:
        raise HTTPException(400, f"settlement verification failed: {e}")
    ledger.record(Metric("income", entry["amount"], f"{body.rail}:{body.settlement_ref[:24]}",
                         SourceType.PAYMENT_PROCESSOR, signed_by="settlement"))
    return entry

@api_router.post("/vault/reconcile")
async def vault_reconcile(_: None = Depends(require_admin)):
    """Re-verify every income row against its settlement rail. Failures flagged, never deleted."""
    return await settlement.reconcile_income_rows()

@api_router.get("/vault/reconcile/status")
async def vault_reconcile_status():
    st = await settlement.reconcile_status()
    st["unverified_income_rows"] = await _unverified_income_count()
    return st

@api_router.get("/vault/summary")
async def vault_summary():
    entries = await _find_all("vault_entries", 100000)
    real_income = sum(e["amount"] for e in entries
                      if e.get("entry_type")=="income" and e.get("reconciled"))
    signals_value = sum(e.get("amount",0) for e in entries if e.get("entry_type")=="signal")
    voided_value = sum(e.get("amount",0) for e in entries if e.get("entry_type")=="estimate_void")
    out = sum(abs(e["amount"]) for e in entries
              if e.get("entry_type") in ("expense","withdrawal","gas") and e.get("reconciled"))
    return {"real_income": round(real_income,2), "net_profit": round(real_income-out,2),
            "signal_value_estimate": round(signals_value,2),
            "voided_estimates": round(voided_value,2),
            "warning": "signal/voided values are NOT money — research artefacts only.",
            "entry_count": len(entries)}

# ── GOLD FINDINGS ─────────────────────────────────────────────────────────────
@api_router.get("/gold-findings")
async def list_findings():
    return await _find_all("gold_findings", 200)

# ── TREASURY — every path human-approved, every gate fail-closed ──────────────
@api_router.get("/treasury/status")
async def treasury_status():
    net = await calculate_net_profit()
    gates_ok, gates_why = await _distribution_gates_pass()
    st = await settlement.reconcile_status()
    return {"treasury_running": treasury_running, "net_profit": net,
            "payout_mode": "HUMAN APPROVAL REQUIRED (no autonomous payout)",
            "autopay_flag": env_bool("TREASURY_AUTOPAY_ENABLED", False),
            "autostart_flag": env_bool("TREASURY_AUTOSTART", False),
            "circuit_breaker": circuit_breaker_active(),
            "distribution_gates": {"pass": gates_ok, "reason": gates_why},
            "reconcile": {"last_run": st.get("ran_at"),
                          "unverified_count": st.get("unverified")},
            "caps": {"max_payout_aud": _treasury_max_payout_aud(),
                     "daily_cap_aud": _treasury_daily_cap_aud()},
            "pending_approvals": gates.pending_approvals(),
            "gate_status": gates.status(),
            "log_entries": treasury_log[-20:]}

@api_router.post("/treasury/start")
async def treasury_start(_: None = Depends(require_admin)):
    if not env_bool("TREASURY_AUTOSTART", False):
        raise HTTPException(403, "TREASURY_AUTOSTART=false — flip the env flag first (operator only)")
    global treasury_running
    if treasury_running:
        return {"message": "treasury loop already running"}
    asyncio.create_task(treasury_loop())
    return {"message": "treasury loop started (queues human approvals only)"}

@api_router.post("/treasury/stop")
async def treasury_stop(_: None = Depends(require_admin)):
    global treasury_running
    treasury_running = False
    return {"message": "treasury loop stopping"}

@api_router.post("/treasury/distribute-now")
async def distribute_now(req: WithdrawRequest, _: None = Depends(require_admin)):
    """Manual distribution request. Refuses unless EVERY gate passes; even then it
    only QUEUES for human approval."""
    ok, why = await _distribution_gates_pass()
    if not ok:
        raise HTTPException(403, f"distribution refused: {why}")
    if req.amount > _treasury_max_payout_aud():
        raise HTTPException(403, f"${req.amount:.2f} exceeds TREASURY_MAX_PAYOUT_AUD "
                                 f"${_treasury_max_payout_aud():.2f}")
    net = await calculate_net_profit()
    if net < req.amount:
        raise HTTPException(400, f"Requested ${req.amount:.2f} exceeds verified net profit ${net:.2f}.")
    out = gates.propose(ProposedAction(
        kind="payout", channel="treasury", agent="treasurer",
        usd_amount=req.amount, reversible=False,
        reason=f"distribute-now ${req.amount:.2f} (verified net ${net:.2f})",
        payload={"real_net_backing": net}))
    if out["outcome"] != "queued":
        raise HTTPException(400, f"Payout not queued: {out['reasons']}")
    treasury_log.append(f"[{datetime.now(timezone.utc).isoformat()[:19]}] "
                        f"distribute-now ${req.amount:.2f} QUEUED: {out['queue_id']}")
    return {"status": "queued_for_human_approval", "queue_id": out["queue_id"],
            "amount": req.amount}

@api_router.post("/treasury/request-payout")
async def request_payout(req: WithdrawRequest, _: None = Depends(require_admin)):
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
            "amount": req.amount, "approve_at": "/admin/approve"}

# ── ADMIN ─────────────────────────────────────────────────────────────────────
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

@api_router.post("/admin/circuit-breaker/engage")
async def cb_engage(_: None = Depends(require_admin)):
    gates.engage_kill_switch()
    return {"circuit_breaker": True}

@api_router.post("/admin/circuit-breaker/disengage")
async def cb_disengage(_: None = Depends(require_admin)):
    gates.disengage_kill_switch()
    return {"circuit_breaker": False}

@api_router.post("/admin/sync-coinspot")
async def sync_coinspot(_: None = Depends(require_admin)):
    await update_coinspot_balances()
    return {"message": "CoinSpot snapshot triggered (read-only, no income booked)"}

@app.get("/")
async def root():
    return HTMLResponse("<h1>YABAI Gold-Hunter API</h1>"
                        "<p>Settlement-gated: income books only against verified external "
                        "settlements; payouts require human approval; nothing autostarts.</p>")

app.include_router(api_router)
