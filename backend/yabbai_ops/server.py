#!/usr/bin/env python3
"""
YABBAI Ops — Server

Standalone FastAPI server for the founder cockpit. Run it on its own, or fold the
endpoints into your main YABBAI server. Honest metrics, EV decisions, human-gated
actions — the operational discipline layer.

Run:  python -m uvicorn server:app --host 127.0.0.1 --port 7870
Open: http://localhost:7870
"""

import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

from yabbai_ops.core.ops_cockpit import OpsCockpit

ROOT = Path(__file__).resolve().parent
DATA = str(ROOT / "ops_data")

app = FastAPI(title="YABBAI Ops", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

ops = OpsCockpit(DATA,
                 micro_spend_cap=float(os.getenv("OPS_MICRO_CAP", "5")),
                 spend_cap=float(os.getenv("OPS_SPEND_CAP", "50")),
                 daily_cap=float(os.getenv("OPS_DAILY_CAP", "100")))


@app.get("/", response_class=HTMLResponse)
async def ui():
    p = ROOT / "ui" / "cockpit.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists() else "<h1>Cockpit UI missing</h1>")

@app.get("/api/ops/dashboard")
async def dashboard():
    return ops.dashboard()

@app.post("/api/ops/score")
async def score(request: Request):
    b = await request.json()
    return ops.score_action(b.get("action", ""), b.get("p_success", 0), b.get("upside", 0),
                            b.get("cost", 0), b.get("reversible", True), b.get("risk_adjustment", 0))

@app.post("/api/ops/check-outreach")
async def check_outreach(request: Request):
    b = await request.json()
    return ops.check_outreach(b.get("message", ""), b.get("opted_in", False),
                              b.get("has_unsubscribe", False), b.get("list_source", ""))

@app.post("/api/ops/check-copy")
async def check_copy(request: Request):
    b = await request.json()
    return ops.check_copy(b.get("text", ""))

@app.get("/api/ops/pending")
async def pending():
    return {"pending": ops.pending_approvals()}

@app.post("/api/ops/approve")
async def approve(request: Request):
    b = await request.json()
    return ops.approve(b.get("id", ""))

@app.post("/api/ops/reject")
async def reject(request: Request):
    b = await request.json()
    return ops.reject(b.get("id", ""))

@app.post("/api/ops/metric")
async def metric(request: Request):
    b = await request.json()
    return ops.record_metric(b.get("name", ""), b.get("value"), b.get("source", ""),
                             b.get("source_type", "manual"), b.get("reconciled", False))

@app.get("/api/ops/roles")
async def roles():
    return ops.roles()

@app.get("/api/health")
async def health():
    return {"ok": True, "app": "yabbai-ops"}


if __name__ == "__main__":
    import uvicorn
    print("\n  YABBAI Ops — Founder Cockpit")
    print("  Honest metrics · EV decisions · human-gated actions")
    print("  Open http://localhost:7870\n")
    uvicorn.run(app, host="127.0.0.1", port=7870, log_level="warning")
