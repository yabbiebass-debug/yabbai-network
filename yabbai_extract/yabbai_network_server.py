"""
YABBAI Network — Unified Python Server

Runs ALL five Python backends as sub-processes and exposes a
network-level /health aggregator + serves the hub HTML at GET /.

Ports (each backend keeps its own):
  Revenue System  → 7870
  YABBAI AI       → 7860
  DeFi Simulator  → 8002
  GoldScout       → 8001
  Ops Cockpit     → 7880
  This gateway    → 8080

Usage:
  python yabbai_network_server.py

Or start each backend independently — see start_all.bat / start_all.sh.
"""

import os
import sys
import signal
import subprocess
import asyncio
import secrets
import time
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any

import httpx
from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from dotenv import load_dotenv

load_dotenv()

ROOT        = Path(__file__).resolve().parent
ADMIN_KEY   = os.environ.get("ADMIN_API_KEY", "")
PORT        = int(os.environ.get("GATEWAY_PORT", 8080))

# ── sub-service definitions ────────────────────────────────────────────────────
SERVICES = [
    {
        "name":    "revenue",
        "label":   "Revenue System",
        "module":  "revenue_system.unified_server:app",
        "port":    int(os.environ.get("REVENUE_PORT", 7870)),
        "cwd":     ROOT,
    },
    {
        "name":    "ai",
        "label":   "YABBAI AI",
        "module":  "yabbai_local.api.server:app",
        "port":    int(os.environ.get("AI_PORT", 7860)),
        "cwd":     ROOT,
    },
    {
        "name":    "defi",
        "label":   "DeFi Simulator",
        "module":  "defi_simulator.api.server:app",
        "port":    int(os.environ.get("DEFI_PORT", 8002)),
        "cwd":     ROOT,
    },
    {
        "name":    "goldscout",
        "label":   "GoldScout",
        "module":  "goldscout.server:app",
        "port":    int(os.environ.get("GOLDSCOUT_PORT", 8001)),
        "cwd":     ROOT,
    },
    {
        "name":    "ops",
        "label":   "Ops Cockpit",
        "module":  "yabbai_ops.server:app",
        "port":    int(os.environ.get("OPS_PORT", 7880)),
        "cwd":     ROOT,
    },
]

# ── gateway app ───────────────────────────────────────────────────────────────
app = FastAPI(title="YABBAI Network Gateway", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

_procs: Dict[str, subprocess.Popen] = {}
_start_time = time.time()


def require_admin(x_admin_key: str | None = Header(None)) -> None:
    if not ADMIN_KEY:
        raise HTTPException(503, "ADMIN_API_KEY not configured on gateway")
    if not x_admin_key or not secrets.compare_digest(x_admin_key.encode(), ADMIN_KEY.encode()):
        raise HTTPException(401, "Unauthorized")


@app.get("/", response_class=HTMLResponse)
async def hub():
    """Serve the hub HTML."""
    p = ROOT / "hub" / "index.html"
    if p.exists():
        return HTMLResponse(p.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>YABBAI Network — hub/index.html not found.</h1>")


@app.get("/health")
async def health():
    """Aggregate health of all sub-services."""
    statuses: Dict[str, Any] = {}
    async with httpx.AsyncClient(timeout=3.0) as client:
        for svc in SERVICES:
            url = f"http://localhost:{svc['port']}/health"
            try:
                r = await client.get(url)
                statuses[svc["name"]] = {"live": r.is_success, "port": svc["port"]}
            except Exception as e:
                statuses[svc["name"]] = {"live": False, "port": svc["port"], "error": str(e)[:80]}
    all_live = all(s["live"] for s in statuses.values())
    return {
        "gateway":   "healthy",
        "version":   "2.0.0",
        "uptime_s":  round(time.time() - _start_time),
        "ts":        datetime.now(timezone.utc).isoformat(),
        "services":  statuses,
        "all_live":  all_live,
    }


@app.get("/network/status")
async def network_status():
    return await health()


# ── subprocess management ─────────────────────────────────────────────────────
def start_service(svc: dict) -> subprocess.Popen:
    cmd = [
        sys.executable, "-m", "uvicorn", svc["module"],
        "--host", "0.0.0.0",
        "--port", str(svc["port"]),
        "--log-level", "warning",
    ]
    env = {**os.environ}
    proc = subprocess.Popen(cmd, cwd=str(svc["cwd"]), env=env)
    print(f"  started {svc['label']:20s} → http://localhost:{svc['port']}  (pid {proc.pid})")
    return proc


def stop_all():
    print("\nShutting down sub-services…")
    for name, proc in _procs.items():
        try:
            proc.terminate()
            proc.wait(timeout=5)
            print(f"  stopped {name}")
        except Exception:
            proc.kill()


if __name__ == "__main__":
    import uvicorn

    if not ADMIN_KEY:
        print("WARNING: ADMIN_API_KEY not set. Set it in .env before going live.")

    print("=" * 60)
    print("YABBAI Network Gateway — starting all services")
    print("=" * 60)

    for svc in SERVICES:
        try:
            _procs[svc["name"]] = start_service(svc)
        except Exception as e:
            print(f"  WARN: could not start {svc['label']}: {e}")

    # give sub-services a moment to bind
    time.sleep(2)
    print(f"\nGateway → http://localhost:{PORT}")
    print(f"Hub UI  → http://localhost:{PORT}/")
    print(f"Health  → http://localhost:{PORT}/health")
    print("=" * 60)

    def _shutdown(sig, frame):
        stop_all()
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
    finally:
        stop_all()
