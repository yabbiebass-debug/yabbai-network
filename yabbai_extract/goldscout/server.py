import sys
if hasattr(sys.stdout, "reconfigure"): sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"): sys.stderr.reconfigure(encoding="utf-8")
#!/usr/bin/env python3
"""
GoldScout -- FastAPI Server  (v9.5.0)

Scours the web for DeFi opportunities (airdrops, quests, testnets, staking),
runs every result through a scam-analysis engine, and serves findings via API
+ a full web UI.

Secured behind Google OAuth -- only thomas.basham1@gmail.com may pass.
Connects to: YABBAI Local (port 7860) and DeFi Backend (port 8000).

Run:  python3 goldscout/server.py
Then open http://localhost:8001
"""

import sys, os
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

import asyncio
from datetime import datetime, timezone
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi_sso.sso.google import GoogleSSO
import secrets

from goldscout.core.scout import run_scout, get_findings, clear_findings
from goldscout.core.scam_analysis import GOLDEN_RULES

# ── Google OAuth ──────────────────────────────────────────────────────────────
GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
SESSION_SECRET       = os.environ.get("SESSION_SECRET") or secrets.token_urlsafe(32)
ALLOWED_EMAIL        = "thomas.basham1@gmail.com"
AUTH_PUBLIC          = {"/health", "/auth/login", "/auth/callback", "/auth/logout"}

# Fixed callback URL -- must exactly match what's registered in Google Cloud Console.
# Override GOLDSCOUT_BASE_URL in .env when using a Cloudflare tunnel.
GOLDSCOUT_BASE_URL = os.environ.get("GOLDSCOUT_BASE_URL", "http://localhost:8001")

def _callback_url(request: Request = None) -> str:
    return f"{GOLDSCOUT_BASE_URL.rstrip('/')}/auth/callback"

def _deny_html(reason: str) -> str:
    return f"""<!DOCTYPE html><html><head><title>Access Denied</title>
<style>body{{background:#0a0a0f;color:#e0e0e0;font-family:monospace;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0;}}
.box{{background:#111118;border:1px solid #9945FF;border-radius:8px;padding:2.5rem 3rem;max-width:480px;text-align:center;}}
h2{{color:#9945FF;margin-top:0;}}p{{color:#999;}}a{{color:#14F195;text-decoration:none;}}
</style></head><body><div class="box"><h2>⛔ Access Denied</h2><p>{reason}</p>
<p><a href="/auth/login">← Try a different account</a></p></div></body></html>"""

app = FastAPI(title="GoldScout", version="9.5.0", docs_url=None, redoc_url=None)

class GoldAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if any(request.url.path.startswith(p) for p in AUTH_PUBLIC):
            return await call_next(request)
        email = request.session.get("user_email")
        if email and email.lower() == ALLOWED_EMAIL.lower():
            return await call_next(request)
        return RedirectResponse(f"/auth/login?next={request.url.path}", status_code=302)

# Order: last added = outermost = runs first.
# SessionMiddleware MUST be outermost so request.session exists when auth middleware runs.
app.add_middleware(GoldAuthMiddleware)
app.add_middleware(CORSMiddleware,
                   allow_origins=[os.getenv("BASE44_ORIGIN", "*")],
                   allow_methods=["*"], allow_headers=["*"])
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET,
                   session_cookie="goldscout_session", https_only=False,
                   same_site="lax", max_age=86400 * 7)

# ── Auth routes ───────────────────────────────────────────────────────────────
@app.get("/auth/login", include_in_schema=False)
async def gs_login(request: Request):
    sso = GoogleSSO(GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET,
                    redirect_uri=_callback_url(request), allow_insecure_http=True)
    async with sso:
        return await sso.get_login_redirect()

@app.get("/auth/callback", include_in_schema=False)
async def gs_callback(request: Request):
    sso = GoogleSSO(GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET,
                    redirect_uri=_callback_url(request), allow_insecure_http=True)
    async with sso:
        user = await sso.verify_and_process(request)
    if not user or not user.email:
        return HTMLResponse(_deny_html("No email returned from Google."), status_code=403)
    if user.email.lower() != ALLOWED_EMAIL.lower():
        return HTMLResponse(_deny_html(
            f"Access denied for <b>{user.email}</b>.<br>"
            "This system is locked to a single authorised account."), status_code=403)
    request.session["user_email"] = user.email
    request.session["user_name"]  = user.display_name or user.email
    return RedirectResponse(request.query_params.get("next", "/"), status_code=302)

@app.get("/auth/logout", include_in_schema=False)
async def gs_logout(request: Request):
    request.session.clear()
    return RedirectResponse("/auth/login", status_code=302)


# ── Health ────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"ok": True, "app": "goldscout", "version": "9.5.0",
            "tavily_configured": bool(os.getenv("TAVILY_API_KEY"))}


# ── Scout API ─────────────────────────────────────────────────────────────────
_scout_state = {"running": False, "last_run": None, "last_summary": None}

@app.post("/api/scout/run")
async def scout_run(background_tasks: BackgroundTasks):
    """Kick off a scout run in the background. Returns immediately."""
    if _scout_state["running"]:
        return {"ok": False, "message": "Scout already running -- check /api/scout/status"}

    def _run():
        _scout_state["running"] = True
        try:
            result = run_scout()
            _scout_state["last_summary"] = result["summary"]
            _scout_state["last_run"] = datetime.now(timezone.utc).isoformat()
        except Exception as e:
            _scout_state["last_summary"] = {"error": str(e)}
        finally:
            _scout_state["running"] = False

    background_tasks.add_task(_run)
    return {"ok": True, "message": "Scout started -- results saved to /api/scout/findings"}

@app.get("/api/scout/status")
async def scout_status():
    return {
        "running": _scout_state["running"],
        "last_run": _scout_state["last_run"],
        "last_summary": _scout_state["last_summary"],
        "tavily_configured": bool(os.getenv("TAVILY_API_KEY")),
    }

@app.get("/api/scout/findings")
async def scout_findings(risk: str = "all", category: str = "all"):
    findings = get_findings(risk_filter=risk, category=category)
    return {"total": len(findings), "findings": findings}

@app.post("/api/scout/clear")
async def scout_clear():
    clear_findings()
    return {"ok": True, "message": "All findings cleared"}

@app.get("/api/scout/rules")
async def scout_rules():
    return {"rules": GOLDEN_RULES}


# ── Web UI ────────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def ui():
    html_path = Path(__file__).parent / "ui" / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>GoldScout UI not found.</h1>")


# ── Startup ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    print("=" * 56)
    print("  GOLDSCOUT -- v9.5.0  (Google Auth + Scam Filter)")
    print("=" * 56)
    if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
        print("  [OK] Google OAuth: configured")
    else:
        print("  [!!] GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET not set!")
    if os.getenv("TAVILY_API_KEY"):
        print("  [OK] Tavily API: configured")
    else:
        print("  [!!] TAVILY_API_KEY not set -- scout runs will fail")
        print("    Get a free key at: app.tavily.com")
    print(f"  Authorised user: {ALLOWED_EMAIL}")
    print(f"  Listening on http://0.0.0.0:8001")
    print("=" * 56 + "\n")
    uvicorn.run(app, host="0.0.0.0", port=8001, log_level="warning")
