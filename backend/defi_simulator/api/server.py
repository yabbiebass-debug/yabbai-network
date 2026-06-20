#!/usr/bin/env python3
"""
YABBAI DeFi Engine — Simulator API (simulator.yabbai.network)

Wraps the simulator engine in real HTTP endpoints. This is the same architecture
a live engine would use — sessions, three modes, an agentic loop, risk gating —
but every fill is paper. There is deliberately NO custody, NO private keys, NO
real transactions anywhere in this service. That isolation is the safety model.

Each browser session gets its own engine instance (gate + portfolio + controller
+ routing engine). Sessions live in memory and reset on restart — appropriate for
a learning/simulation tool, and it means there is no user-funds database to breach.

Run:  uvicorn simulator.api.server:app --reload --port 8800
"""

import sys
import asyncio
import secrets
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Optional, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from defi_simulator.risk.risk_gate import RiskGate, RiskLimits
from defi_simulator.core.mode_controller import ModeController, Mode
from defi_simulator.core.paper_executor import PaperExecutor, VirtualPortfolio
from defi_simulator.routing.routing_engine import RoutingEngine
from defi_simulator.routing.price_feed import PriceFeed
from defi_simulator.routing.live_price_adapter import LivePriceAdapter
from defi_simulator.strategies.baseline_strategy import MomentumMeanReversionStrategy


# ══════════════════════════════════════════════════════════════════════════════
# SESSION — one isolated engine per user session
# ══════════════════════════════════════════════════════════════════════════════

class SimSession:
    def __init__(self, session_id: str, starting_usd: float, limits: RiskLimits,
                 mode: Mode, token: str, feed_mode: str = "synthetic"):
        self.id = session_id
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.token = token
        self.feed_mode = feed_mode

        self.gate = RiskGate(limits)
        self.portfolio = VirtualPortfolio(starting_usd=starting_usd, cash_usd=starting_usd)
        self.feed = PriceFeed(window=50, mode=feed_mode)
        self.executor = PaperExecutor(self.portfolio, self.feed.price_lookup)
        self.controller = ModeController(self.gate, self.executor, mode=mode)
        self.engine = RoutingEngine(self.gate, self.controller, self.feed, llm_advisor=None)
        self.engine.set_strategy(MomentumMeanReversionStrategy())

        # Live adapter only created in live mode
        self.live: Optional[LivePriceAdapter] = None
        if feed_mode == "live":
            self.live = LivePriceAdapter(self.feed, [token])
        else:
            # Warm up synthetic indicators immediately
            for _ in range(20):
                self.feed.update_synthetic(token)

        self.auto_running = False
        self._auto_task: Optional[asyncio.Task] = None
        self.warmed = (feed_mode != "live")

    async def warmup_if_needed(self):
        """Live sessions must prime the indicator window with real prices first."""
        if self.live and not self.warmed:
            result = await self.live.warmup(samples=16, interval=1.0)
            self.warmed = result.get("ready", False)
            return result
        return {"warmed": True, "ready": True}

    async def tick_once_live(self):
        """Pull a real price then run one decision cycle."""
        if self.live:
            await self.live.refresh()
        else:
            self.feed.update_synthetic(self.token)
        return self.engine.tick()

    def tick_once(self):
        """Synthetic-only single step (used by fast-forward in synthetic mode)."""
        self.feed.update_synthetic(self.token)
        return self.engine.tick()

    def snapshot(self) -> Dict:
        snap = {
            "session_id": self.id,
            "created_at": self.created_at,
            "mode": self.controller.mode.value,
            "feed_mode": self.feed_mode,
            "auto_running": self.auto_running,
            "warmed": self.warmed,
            "portfolio": self.executor.snapshot(),
            "risk": self.gate.status(),
            "open_positions": list(self.engine.position_entries.keys()),
        }
        if self.live:
            snap["live_feed"] = self.live.status()
        return snap


SESSIONS: Dict[str, SimSession] = {}


# ══════════════════════════════════════════════════════════════════════════════
# REQUEST MODELS
# ══════════════════════════════════════════════════════════════════════════════

class CreateSessionRequest(BaseModel):
    starting_usd: float = Field(14.0, gt=0, le=100000)
    mode: str = Field("manual")               # manual | signed | auto
    feed_mode: str = Field("synthetic")       # synthetic | live
    token: str = Field("So11111111111111111111111111111111111111112")
    max_trade_usd: float = Field(4.0, gt=0)
    daily_loss_cap_usd: float = Field(4.0, gt=0)
    daily_volume_cap_usd: float = Field(40.0, gt=0)
    stop_loss_pct: float = Field(20.0, gt=0, le=100)
    take_profit_pct: float = Field(50.0, gt=0)
    max_open_positions: int = Field(1, ge=1, le=10)

class ModeRequest(BaseModel):
    mode: str

class ConfirmRequest(BaseModel):
    pending_id: str
    signature: Optional[str] = None

class LimitsUpdateRequest(BaseModel):
    # Limits can only be TIGHTENED while running (enforced below)
    max_trade_usd: Optional[float] = None
    daily_loss_cap_usd: Optional[float] = None
    stop_loss_pct: Optional[float] = None
    take_profit_pct: Optional[float] = None
    kill_switch: Optional[bool] = None

class TickRequest(BaseModel):
    count: int = Field(1, ge=1, le=2000)


# ══════════════════════════════════════════════════════════════════════════════
# APP
# ══════════════════════════════════════════════════════════════════════════════

app = FastAPI(title="YABBAI DeFi Simulator API", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

from fastapi.responses import HTMLResponse

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    html_path = Path(__file__).resolve().parent / "dashboard.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>YABBAI DeFi Simulator API running. Dashboard not found.</h1>")


def _get(session_id: str) -> SimSession:
    s = SESSIONS.get(session_id)
    if not s:
        raise HTTPException(404, "Session not found (it may have expired on restart)")
    return s


@app.get("/api/health")
async def health():
    return {"ok": True, "service": "yabbai-defi-simulator", "version": "0.1.0",
            "custodial": False, "real_funds": False, "active_sessions": len(SESSIONS),
            "ts": datetime.now(timezone.utc).isoformat()}


@app.post("/api/session/create")
async def create_session(req: CreateSessionRequest):
    try:
        mode = Mode(req.mode)
    except ValueError:
        raise HTTPException(400, "mode must be manual | signed | auto")

    if req.feed_mode not in ("synthetic", "live"):
        raise HTTPException(400, "feed_mode must be synthetic | live")

    limits = RiskLimits(
        max_trade_usd=req.max_trade_usd,
        daily_loss_cap_usd=req.daily_loss_cap_usd,
        daily_volume_cap_usd=req.daily_volume_cap_usd,
        stop_loss_pct=req.stop_loss_pct,
        take_profit_pct=req.take_profit_pct,
        max_open_positions=req.max_open_positions,
        token_allowlist=[req.token],
        kill_switch=False,
    )
    warnings = limits.validate()
    sid = "sim_" + secrets.token_urlsafe(10)
    SESSIONS[sid] = SimSession(sid, req.starting_usd, limits, mode, req.token, req.feed_mode)
    return {"ok": True, "session_id": sid, "feed_mode": req.feed_mode,
            "warnings": warnings, "snapshot": SESSIONS[sid].snapshot()}


@app.get("/api/session/{session_id}")
async def get_session(session_id: str):
    return _get(session_id).snapshot()


@app.post("/api/session/{session_id}/mode")
async def set_mode(session_id: str, req: ModeRequest):
    s = _get(session_id)
    try:
        s.controller.set_mode(Mode(req.mode))
    except ValueError:
        raise HTTPException(400, "mode must be manual | signed | auto")
    return {"ok": True, "mode": s.controller.mode.value}


@app.post("/api/session/{session_id}/tick")
async def tick(session_id: str, req: TickRequest):
    """Manually advance N ticks (used by manual/signed and for fast-forward sim)."""
    s = _get(session_id)
    results = []
    for _ in range(req.count):
        if s.gate.status()["halted"]:
            break
        results.append(s.tick_once())
    return {"ok": True, "ticks_run": len(results), "snapshot": s.snapshot(),
            "pending": s.controller.list_pending()}


@app.get("/api/session/{session_id}/pending")
async def list_pending(session_id: str):
    return {"ok": True, "pending": _get(session_id).controller.list_pending()}


@app.post("/api/session/{session_id}/confirm")
async def confirm(session_id: str, req: ConfirmRequest):
    """Confirm a pending action (manual = click, signed = signature placeholder)."""
    s = _get(session_id)
    result = s.controller.confirm(req.pending_id, req.signature)
    return {"ok": True, "result": result, "snapshot": s.snapshot()}


@app.post("/api/session/{session_id}/reject")
async def reject(session_id: str, req: ConfirmRequest):
    s = _get(session_id)
    return {"ok": True, "result": s.controller.reject(req.pending_id)}


@app.post("/api/session/{session_id}/limits")
async def update_limits(session_id: str, req: LimitsUpdateRequest):
    """Limits may only be TIGHTENED while running. Kill switch may always be set."""
    s = _get(session_id)
    L = s.gate.limits
    if req.kill_switch is not None:
        L.kill_switch = req.kill_switch          # can always engage/disengage
    if req.max_trade_usd is not None:
        L.max_trade_usd = min(L.max_trade_usd, req.max_trade_usd)
    if req.daily_loss_cap_usd is not None:
        L.daily_loss_cap_usd = min(L.daily_loss_cap_usd, req.daily_loss_cap_usd)
    if req.stop_loss_pct is not None:
        L.stop_loss_pct = min(L.stop_loss_pct, req.stop_loss_pct)
    if req.take_profit_pct is not None:
        L.take_profit_pct = req.take_profit_pct  # tightening TP isn't a safety risk either way
    return {"ok": True, "risk": s.gate.status()}


@app.post("/api/session/{session_id}/warmup")
async def warmup(session_id: str):
    """Prime a live session's indicator window with real prices before trading."""
    s = _get(session_id)
    result = await s.warmup_if_needed()
    return {"ok": True, "warmup": result, "snapshot": s.snapshot()}


@app.post("/api/session/{session_id}/auto/start")
async def auto_start(session_id: str):
    """Start the autonomous loop. Runs within limits, halts itself on cap/kill-switch."""
    s = _get(session_id)
    if s.controller.mode != Mode.AUTO:
        raise HTTPException(400, "Session must be in 'auto' mode to start the loop")
    if s.auto_running:
        return {"ok": True, "already_running": True}

    # Live sessions must be warmed up first
    if s.live and not s.warmed:
        await s.warmup_if_needed()

    s.auto_running = True

    async def loop():
        # Each iteration = one decision cycle. Live mode pulls a real price each tick.
        cadence = 5.0 if s.live else 2.0   # gentler polling on the public price API
        while s.auto_running:
            if s.gate.status()["halted"]:
                s.auto_running = False
                break
            await s.tick_once_live()
            await asyncio.sleep(cadence)

    s._auto_task = asyncio.create_task(loop())
    return {"ok": True, "auto_running": True, "feed_mode": s.feed_mode,
            "note": "Loop runs within your limits and halts automatically if the daily "
                    "loss cap or kill switch trips."}


@app.post("/api/session/{session_id}/auto/stop")
async def auto_stop(session_id: str):
    s = _get(session_id)
    s.auto_running = False
    if s._auto_task:
        s._auto_task.cancel()
    return {"ok": True, "auto_running": False}


@app.post("/api/session/{session_id}/kill")
async def kill_switch(session_id: str):
    """Instant halt — engages kill switch and stops the auto loop."""
    s = _get(session_id)
    s.gate.limits.kill_switch = True
    s.auto_running = False
    if s._auto_task:
        s._auto_task.cancel()
    return {"ok": True, "halted": True}


@app.get("/api/session/{session_id}/history")
async def history(session_id: str, limit: int = 50):
    s = _get(session_id)
    return {"ok": True,
            "executions": s.controller.history[-limit:],
            "fills": s.executor.fills[-limit:],
            "ticks": s.engine.tick_log[-limit:]}


@app.get("/api/session/{session_id}/audit")
async def audit(session_id: str, limit: int = 100):
    """The risk gate's full audit log — every decision and why."""
    s = _get(session_id)
    return {"ok": True, "audit": s.gate.audit_log[-limit:]}


@app.delete("/api/session/{session_id}")
async def end_session(session_id: str):
    s = SESSIONS.pop(session_id, None)
    if s and s._auto_task:
        s._auto_task.cancel()
    return {"ok": True, "ended": session_id is not None}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8800)
