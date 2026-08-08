"""
YABBAI NETWORK V2 — Unified Backend (Emergent single-port edition)

Every Python backend that used to run on its own port (revenue 7870, AI 7860,
defi 8002, goldscout 8001, ops 7880) is folded into ONE FastAPI app on :8001,
each under an /api/<name> prefix so the Emergent ingress can route it. The hub
and all web surfaces are served by the frontend and talk to these paths.

Safety spine (income = reconciled-only, HIGH-action approval queue, kill-switch,
NoKeySigner) is the ORIGINAL revenue_system code, mounted unchanged.
"""

import os
from datetime import datetime, timezone

from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from dotenv import load_dotenv

from network_db import get_raw_settings, save_settings, sanitize
from auth_router import require_director

load_dotenv()

# ── original backends, mounted as sub-apps ────────────────────────────────────
from revenue_system.unified_server import app as revenue_app
from defi_simulator.api.server import app as defi_app
from yabbai_ops.server import app as ops_app
from ai_router import router as ai_router
from goldscout_router import router as goldscout_router
from auth_router import router as auth_router
from wallet_router import router as wallet_router
from supabase_router import router as supabase_router

# health aliases so every service answers at <prefix>/health (the hub polls this)
@defi_app.get("/health")
async def _defi_health():
    return {"ok": True, "app": "yabbai-defi-simulator", "custodial": False,
            "real_funds": False, "ts": datetime.now(timezone.utc).isoformat()}

@ops_app.get("/health")
async def _ops_health():
    return {"ok": True, "app": "yabbai-ops", "ts": datetime.now(timezone.utc).isoformat()}


# ── main gateway app ──────────────────────────────────────────────────────────
app = FastAPI(title="YABBAI Network Gateway", version="2.0.0")

_ALLOWED = [o for o in [
    "https://revenue-nexus-2.preview.emergentagent.com",
    "https://revenue-nexus-2.emergent.host",
] if o]


class SecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "SAMEORIGIN"
        resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        resp.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return resp


app.add_middleware(SecurityHeaders)
app.add_middleware(CORSMiddleware, allow_origins=_ALLOWED, allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])


@app.get("/api/health")
async def health():
    return {"gateway": "healthy", "version": "2.0.0",
            "ts": datetime.now(timezone.utc).isoformat()}


@app.get("/api/network/status")
async def network_status():
    """In-process services are always up; report them for the hub strip."""
    services = {
        "revenue":   {"live": True, "prefix": "/api/revenue"},
        "ai":        {"live": bool(os.environ.get("EMERGENT_LLM_KEY")), "prefix": "/api/ai"},
        "goldscout": {"live": True, "prefix": "/api/goldscout"},
        "defi":      {"live": True, "prefix": "/api/defi"},
        "ops":       {"live": True, "prefix": "/api/ops"},
    }
    return {"gateway": "healthy", "version": "2.0.0",
            "services": services, "all_live": all(s["live"] for s in services.values()),
            "ts": datetime.now(timezone.utc).isoformat()}


# ── connection settings (LLM routing + payments/auth — configured in /settings UI) ─
@app.get("/api/settings")
async def get_settings(user=Depends(require_director)):
    return sanitize(await get_raw_settings())


@app.put("/api/settings")
async def put_settings(payload: dict, user=Depends(require_director)):
    await save_settings(payload)
    return {"ok": True}


# ── mount the original backends ───────────────────────────────────────────────
app.mount("/api/revenue", revenue_app)
app.mount("/api/defi", defi_app)
app.mount("/api/ops", ops_app)
app.include_router(ai_router)
app.include_router(goldscout_router)
app.include_router(auth_router)
app.include_router(wallet_router)
app.include_router(supabase_router)
