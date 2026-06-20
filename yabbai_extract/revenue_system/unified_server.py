"""
YABBAI Revenue System — Unified Server

One FastAPI service exposing the whole spine: all three channels, the gate
approval queue, real webhooks, and the orchestrator cycle. Run it and you have a
single endpoint family for the entire revenue OS.

  GET  /health
  GET  /api/status                  — truth + gates + memory + pending approvals
  POST /api/cycle                   — run one orchestrator cycle
  GET  /api/approvals               — actions awaiting a human
  POST /api/approve                 — approve a queued action (signature for real_trade)
  POST /api/reject
  POST /webhooks/products           — Gumroad/Etsy sale webhook -> real income
  POST /webhooks/invoice-paid       — Stripe/invoice paid -> real income
  GET  /api/channels/{ch}/scorecard — wins/losses/net per channel

No infra required: in-memory truth ledger + JSON memory. Swap for Postgres later.
"""

import os
import hmac
import hashlib
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from dotenv import load_dotenv
from pathlib import Path

from .truth import TruthLedger, Metric, SourceType
from .gates import GateController, ProposedAction, CapitalCaps
from .gates.compliance_gate import ComplianceGate
from .memory import Memory
from .orchestrator import Orchestrator
from .channels import ProductsChannel, AgencyChannel, TradingChannel, NoKeySigner, TradeCap

load_dotenv()

# ── assemble the spine ────────────────────────────────────────────────────────
ledger = TruthLedger()
gates = GateController(CapitalCaps(
    micro_spend_cap=float(os.environ.get("MICRO_SPEND_CAP", 2.0)),
    spend_cap=float(os.environ.get("SPEND_CAP", 25.0)),
    daily_spend_cap=float(os.environ.get("DAILY_SPEND_CAP", 100.0)),
    daily_loss_cap=float(os.environ.get("DAILY_LOSS_CAP", 15.0))))
memory = Memory(os.environ.get("MEMORY_DIR", "./yabbai_memory"))
orch = Orchestrator(ledger, gates, memory)
compliance = ComplianceGate()

products = ProductsChannel(ledger, gates, memory, os.environ.get("STOREFRONT_URL", ""))
agency = AgencyChannel(ledger, gates, memory)
trading = TradingChannel(ledger, gates, memory,
                         TradeCap(max_real_trade_usd=float(os.environ.get("MAX_REAL_TRADE_USD", 10.0)),
                                  token_allowlist=[]),
                         signer=NoKeySigner(),
                         paper_proof_bar=int(os.environ.get("PAPER_PROOF_BAR", 100)))

# Register channels with the orchestrator so one cycle drives all of them.
orch.register_channel("products", products.observe, products.propose)
orch.register_channel("agency", agency.observe, agency.propose)
orch.register_channel("trading", trading.observe, trading.propose)

# ── security ──────────────────────────────────────────────────────────────────
def require_admin(x_admin_key: Optional[str] = Header(None)) -> None:
    expected = os.environ.get("ADMIN_API_KEY")
    if not expected:
        raise HTTPException(503, "ADMIN_API_KEY not configured")
    if not x_admin_key or not secrets.compare_digest(x_admin_key.encode(), expected.encode()):
        raise HTTPException(401, "Unauthorized")

def verify_webhook(signature_header: Optional[str], body: bytes, secret: str) -> bool:
    if not secret:
        return False
    if not signature_header:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    sig = signature_header.removeprefix("sha256=")   # normalise Gumroad-style prefix
    return secrets.compare_digest(sig, expected)

# ── app ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="YABBAI Revenue System", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

@app.get("/", response_class=HTMLResponse)
async def ui():
    p = Path(__file__).resolve().parent / "ui" / "dashboard.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<h1>YABBAI Revenue System running. Dashboard UI not found.</h1>")

@app.post("/api/compliance/check-copy")
async def compliance_check_copy(request: Request, _: None = Depends(require_admin)):
    b = await request.json()
    r = compliance.check_claims(b.get("text", ""))
    return {"ok": r.ok, "issues": r.issues, "note": r.note}

@app.post("/api/compliance/check-outreach")
async def compliance_check_outreach(request: Request, _: None = Depends(require_admin)):
    b = await request.json()
    r = compliance.check_outbound(b.get("message", ""), b.get("opted_in", False),
                                  b.get("has_unsubscribe", False), b.get("list_source", ""))
    return {"ok": r.ok, "issues": r.issues, "requires_human": r.requires_human, "note": r.note}

@app.get("/health")
async def health():
    return {"status": "healthy", "version": "1.0.0",
            "real_funds_at_risk_without_human": False,
            "trading_signer": "NoKeySigner (refuses all) — inject a real one to trade live",
            "ts": datetime.now(timezone.utc).isoformat()}

@app.get("/api/status")
async def status(_: None = Depends(require_admin)):
    return orch.status()

@app.post("/api/cycle")
async def cycle(_: None = Depends(require_admin)):
    return orch.cycle()

@app.get("/api/approvals")
async def approvals(_: None = Depends(require_admin)):
    return gates.pending_approvals()

class ApproveBody(BaseModel):
    queue_id: str
    approver: str
    signature: Optional[str] = None

@app.post("/api/approve")
async def approve(body: ApproveBody, _: None = Depends(require_admin)):
    res = gates.human_approve(body.queue_id, body.approver, body.signature)
    if not (res.get("outcome") == "executed"):
        raise HTTPException(400, f"Approval failed: {res.get('reasons')}")
    return res

@app.post("/api/reject")
async def reject(body: ApproveBody, _: None = Depends(require_admin)):
    return gates.human_reject(body.queue_id, body.approver, "rejected")

@app.post("/api/kill-switch")
async def kill_switch(_: None = Depends(require_admin)):
    gates.engage_kill_switch()
    return {"kill_switch": True, "status": "ENGAGED — all activity halted"}

@app.post("/api/kill-switch/disengage")
async def kill_switch_disengage(_: None = Depends(require_admin)):
    gates.disengage_kill_switch()
    return {"kill_switch": False, "status": "disengaged — activity resumed"}

@app.get("/api/audit")
async def audit(_: None = Depends(require_admin)):
    """Recent gate audit trail (last 200 entries)."""
    return {"audit": gates._audit[-200:]}

@app.get("/api/ledger")
async def ledger_entries(_: None = Depends(require_admin)):
    """All truth-ledger entries with provenance."""
    return {
        "summary": ledger.summary(),
        "entries": ledger.all_entries(),
        "data_gaps": ledger.data_gaps(),
        "unreconciled": ledger.unreconciled(),
    }

@app.get("/api/channels/{ch}/scorecard")
async def scorecard(ch: str, _: None = Depends(require_admin)):
    return memory.channel_scorecard(ch)

# ── WEBHOOKS — the only places real income enters the system ──────────────────
@app.post("/webhooks/products")
async def products_webhook(request: Request):
    """Storefront sale webhook (Gumroad/Etsy). Verifies HMAC, books REAL income.
    Body shape (normalize per provider in your adapter):
      {sale_id, product, gross_usd, fee_usd, currency, ts}
    """
    secret = os.environ.get("PRODUCTS_WEBHOOK_SECRET", "")
    raw = await request.body()
    sig = request.headers.get("X-Signature") or request.headers.get("X-Gumray-Signature")
    if secret and not verify_webhook(sig, raw, secret):
        raise HTTPException(401, "Invalid webhook signature")
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")
    sales = payload if isinstance(payload, list) else [payload]
    metrics = products.observe(fetch_sales=lambda: sales)
    for m in metrics:
        ledger.record(m)
    return {"booked": len([m for m in metrics if m.is_bookable]),
            "net_profit_now": ledger.net_profit()}

@app.post("/webhooks/invoice-paid")
async def invoice_webhook(request: Request):
    """Stripe/processor 'invoice paid' webhook. Verifies HMAC, books REAL income."""
    secret = os.environ.get("INVOICE_WEBHOOK_SECRET", "")
    raw = await request.body()
    sig = request.headers.get("X-Signature")
    if secret and not verify_webhook(sig, raw, secret):
        raise HTTPException(401, "Invalid webhook signature")
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")
    invs = payload if isinstance(payload, list) else [payload]
    metrics = agency.observe(fetch_paid_invoices=lambda: invs)
    for m in metrics:
        ledger.record(m)
    return {"booked": len([m for m in metrics if m.is_bookable]),
            "net_profit_now": ledger.net_profit()}

# ── manual injection of a confirmed sale/invoice (for testing or non-webhook flows)
class ManualSale(BaseModel):
    sale_id: str; product: str = ""; gross_usd: float; fee_usd: float = 0.0
    currency: str = "usd"

@app.post("/api/manual/sale")
async def manual_sale(s: ManualSale, _: None = Depends(require_admin)):
    metrics = products.observe(fetch_sales=lambda: [s.model_dump()])
    for m in metrics:
        ledger.record(m)
    return {"net_profit_now": ledger.net_profit()}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0",
                port=int(os.environ.get("PORT", 8000)), log_level="info")
