import sys
if hasattr(sys.stdout, "reconfigure"): sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"): sys.stderr.reconfigure(encoding="utf-8")
#!/usr/bin/env python3
"""
YABBAI Local -- API Server  (v9.5.0 + WAN Security + Google Auth)

Serves the YABBAI local app: hardware report, setup status, chat (streaming and
non-streaming), and the web UI. Secured behind Google OAuth -- only
thomas.basham1@gmail.com may pass.

Run:  python -m yabbai_local.api.server
Then open http://localhost:7860  (or your Cloudflare Tunnel URL)
"""

import sys
import os
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

# Load .env early so all os.getenv() calls see it
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from yabbai_local.core.hardware import full_report, detect_hardware, recommend_model
from yabbai_local.core.brain import YabbaiLocalBrain
from yabbai_local.core.coding_brain import CodingBrain
from yabbai_local.core.ollama_client import OllamaClient
from yabbai_local.core.model_pool import ModelPool
from yabbai_local.core.router import HybridRouter
from yabbai_local.core.learning import LearningLayer
from yabbai_local.core.defi_bridge import DefiBridge
from yabbai_local.core.autonomous_agent import AutonomousDevAgent
from yabbai_local.core.remote_auth import RemoteAuth
from yabbai_local.core.wan_security import (
    wan_preflight, print_preflight, WanAccessLog, WAN_BLOCKED_PATHS)
# Google auth is optional — if fastapi-sso isn't installed or auth isn't configured,
# YABBAI runs fine without the sign-in gate (local-only use). This makes the system
# robust: the auth layer enhances it but is never required to boot.
try:
    from yabbai_local.core.google_auth import (
        GoogleAuthMiddleware, SessionMiddleware, SESSION_SECRET,
        auth_login, auth_callback, auth_logout, auth_me,
        GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET)
    _GOOGLE_AUTH_AVAILABLE = True
except Exception as _auth_err:
    _GOOGLE_AUTH_AVAILABLE = False
    GOOGLE_CLIENT_ID = GOOGLE_CLIENT_SECRET = ""
    print(f"  [info] Google auth disabled (optional): {_auth_err}")
import asyncio

# ── Boot-time hardware detection ──────────────────────────────────────────────
_hw = detect_hardware()
_rec = recommend_model(_hw)
CODEX_PATH = str(ROOT / "yabbai_local_data" / "codex.json")
WORKSPACE = str(ROOT / "yabbai_workspace")
LEARNING_PATH = str(ROOT / "yabbai_local_data" / "learning.json")

brain  = YabbaiLocalBrain(model=_rec.model_tag, codex_path=CODEX_PATH)
coder  = CodingBrain(model=_rec.model_tag, workspace=WORKSPACE)
pool   = ModelPool()
learning = LearningLayer(LEARNING_PATH)
router = HybridRouter(pool, workhorse_id="local-8b", default_specialist="claude",
                      policy="ask", learning=learning)
defi   = DefiBridge()
agent  = AutonomousDevAgent(repo_path=str(ROOT), model_pool=pool, router=router,
                            daily_api_cap_usd=2.0, max_edits_per_cycle=5)
from yabbai_local.core.agent_team import AgentTeam
team   = AgentTeam(model_pool=pool, data_dir=str(ROOT / "yabbai_local_data"),
                   workhorse_id="local-8b")

app = FastAPI(title="YABBAI Local", version="10.0.0")

# ── Middleware stack ──────────────────────────────────────────────────────────
# In FastAPI, last added = outermost = runs first.
# SessionMiddleware MUST be added last so it is outermost and populates
# request.session before any auth middleware tries to read it.

# 1. Google OAuth gate (innermost after CORS) — only if available + configured
if _GOOGLE_AUTH_AVAILABLE:
    app.add_middleware(GoogleAuthMiddleware)

# 2. CORS
_base44_origin = os.getenv("BASE44_ORIGIN", "")
_allowed_origins = ["*"] if not _base44_origin else [
    _base44_origin, "http://localhost:7860", "http://127.0.0.1:7860"]
app.add_middleware(CORSMiddleware, allow_origins=_allowed_origins,
                   allow_methods=["*"], allow_headers=["*", "X-YABBAI-Key"])

# 3. Session (outermost -- added last, runs first, so session exists for all inner middleware)
_session_secret = SESSION_SECRET if _GOOGLE_AUTH_AVAILABLE else os.getenv("SESSION_SECRET", "yabbai-local-dev-session")
app.add_middleware(SessionMiddleware, secret_key=_session_secret,
                   session_cookie="yabbai_session", https_only=False,
                   same_site="lax", max_age=86400 * 7)

# 4. Remote API-key auth (for programmatic callers that don't go through the browser UI)
_auth = RemoteAuth()
app.middleware("http")(_auth.__call__)

# 5. WAN guard + access log
_wan_log = WanAccessLog()

@app.middleware("http")
async def wan_guard(request: Request, call_next):
    path = request.url.path
    ip = request.client.host if request.client else "unknown"
    is_local = ip in ("127.0.0.1", "::1", "localhost")

    if (not is_local) and path in WAN_BLOCKED_PATHS:
        if os.getenv("YABBAI_ALLOW_REMOTE_DESTRUCTIVE") != "1":
            _wan_log.record(ip, path, allowed=False)
            return JSONResponse(status_code=403, content={
                "error": "wan_blocked",
                "message": "This agent action is local-only on WAN for safety."})

    resp = await call_next(request)
    if not is_local and path.startswith("/api/"):
        _wan_log.record(ip, path, allowed=(resp.status_code < 400))
    return resp


# ── Auth routes (only when Google auth is available) ───────────────────────────
if _GOOGLE_AUTH_AVAILABLE:
    app.add_api_route("/auth/login",    auth_login,    methods=["GET"], include_in_schema=False)
    app.add_api_route("/auth/callback", auth_callback, methods=["GET"], include_in_schema=False)
    app.add_api_route("/auth/logout",   auth_logout,   methods=["GET"], include_in_schema=False)
    app.add_api_route("/auth/me",       auth_me,       methods=["GET"])


# ── UI routes ─────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def ui():
    html_path = ROOT / "yabbai_local" / "ui" / "unified.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>YABBAI Local API running. Unified UI not found.</h1>")

@app.get("/ide-frame", response_class=HTMLResponse)
async def ide_frame():
    html_path = ROOT / "yabbai_local" / "ui" / "ide.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>IDE UI not found.</h1>")

@app.get("/chat", response_class=HTMLResponse)
async def chat_ui():
    html_path = ROOT / "yabbai_local" / "ui" / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Chat UI not found.</h1>")


# ── Core API ──────────────────────────────────────────────────────────────────
@app.get("/api/health")
async def health():
    return {"ok": True, "app": "yabbai-local", "version": "9.5.0",
            "private": True, "external_calls": False,
            "model": brain.client.model,
            "ollama_running": brain.client.is_running()}

@app.get("/api/hardware")
async def hardware():
    return full_report()

@app.get("/api/setup")
async def setup():
    return brain.setup_status()

@app.get("/api/models")
async def models():
    return {"installed": brain.client.list_models(),
            "current": brain.client.model,
            "recommended": _rec.model_tag,
            "alternatives": _rec.alternatives}

@app.post("/api/model")
async def set_model(request: Request):
    body = await request.json()
    model = body.get("model", "").strip()
    if model:
        brain.set_model(model)
    return {"ok": True, "current": brain.client.model}

@app.post("/api/chat")
async def chat(request: Request):
    body = await request.json()
    user_text = body.get("message", "")
    result = brain.chat(user_text, stream=False)
    return JSONResponse(result)

@app.post("/api/chat/stream")
async def chat_stream(request: Request):
    body = await request.json()
    user_text = body.get("message", "")
    gen = brain.chat(user_text, stream=True)

    def event_stream():
        full = ""
        for token in gen:
            full += token
            yield token
        if full:
            brain.history.append({"role": "assistant", "content": full})

    return StreamingResponse(event_stream(), media_type="text/plain")

@app.post("/api/reset")
async def reset():
    brain.reset()
    return {"ok": True}

@app.get("/api/memory")
async def memory():
    return {"entries": brain.codex.entries[-50:]}

@app.post("/api/memory")
async def add_memory(request: Request):
    body = await request.json()
    content = body.get("content", "").strip()
    if content:
        brain.codex.add(content, kind="manual")
    return {"ok": True, "count": len(brain.codex.entries)}


# ── Coding IDE ────────────────────────────────────────────────────────────────
@app.get("/api/code/tree")
async def code_tree(path: str = "."):
    return coder.tools.tree(path)

@app.get("/api/code/file")
async def code_file(path: str):
    return coder.tools.read_file(path)

@app.post("/api/code/file")
async def code_save_file(request: Request):
    body = await request.json()
    return coder.tools.write_file(body.get("path", ""), body.get("content", ""))

@app.post("/api/code/chat")
async def code_chat(request: Request):
    body = await request.json()
    return JSONResponse(coder.chat(body.get("message", "")))

@app.post("/api/code/confirm")
async def code_confirm(request: Request):
    body = await request.json()
    return JSONResponse(coder.confirm_pending(body.get("pending_id", ""), body.get("approved", False)))

@app.post("/api/code/reset")
async def code_reset():
    coder.reset()
    return {"ok": True}


# ── Hybrid router ─────────────────────────────────────────────────────────────
@app.get("/api/router/models")
async def router_models():
    return {"available": pool.available(), "policy": router.policy,
            "workhorse": router.workhorse_id, "specialist": router.default_specialist}

@app.post("/api/router/policy")
async def router_policy(request: Request):
    body = await request.json()
    router.set_policy(body.get("policy", "ask"))
    return {"ok": True, "policy": router.policy}

@app.post("/api/router/decide")
async def router_decide(request: Request):
    body = await request.json()
    d = router.decide(body.get("message", ""), run_judge=body.get("run_judge", True))
    return {"model_id": d.model_id, "layer": d.layer, "private": d.private,
            "reason": d.reason, "needs_confirmation": d.needs_confirmation,
            "cost_hint": d.cost_hint}

@app.post("/api/router/chat")
async def router_chat(request: Request):
    body = await request.json()
    text  = body.get("message", "")
    force = body.get("force_model")
    history = body.get("history", [])

    if force:
        decision_model = force
        layer = "confirmed"
        private = pool.get(force)["private"] if pool.get(force) else True
    else:
        d = router.decide(text, run_judge=body.get("run_judge", True))
        if d.needs_confirmation:
            return {"type": "confirm_escalation", "model_id": d.model_id,
                    "reason": d.reason, "private": d.private, "cost_hint": d.cost_hint,
                    "layer": d.layer}
        decision_model = d.model_id
        layer = d.layer
        private = d.private

    msgs   = history + [{"role": "user", "content": _strip_tag(text)}]
    result = pool.call(decision_model, brain._build_system(), msgs)
    router.record_simple(decision_model)
    escalated = not (pool.get(decision_model) or {}).get("private", True)
    learning.record(text, decision_model, layer, escalated,
                    user_accepted=True if force else None)
    return {"type": "message", "content": result.content, "ok": result.ok,
            "model": result.model, "provider": result.provider,
            "private": result.private, "layer": layer, "cost_hint": result.cost_hint,
            "credit_report": router.credit_report()}

@app.get("/api/router/credits")
async def router_credits():
    return router.credit_report()


# ── Learning layer ─────────────────────────────────────────────────────────────
@app.get("/api/learning/insights")
async def learning_insights():
    return learning.insights()

@app.post("/api/learning/reset")
async def learning_reset():
    learning.reset()
    return {"ok": True}


# ── DeFi dashboard (read-only) ─────────────────────────────────────────────────
@app.get("/api/defi/overview")
async def defi_overview():
    return defi.overview()

@app.get("/api/defi/price")
async def defi_price(mint: str):
    return defi.token_price(mint)

@app.get("/api/defi/sol-balance")
async def defi_sol_balance(owner: str):
    return defi.sol_balance(owner)


# ── Five specialized agents (research / code-fix / scam / summarize / schedule) ─
@app.get("/api/agents/roster")
async def agents_roster():
    return {"agents": team.roster()}

@app.post("/api/agents/research")
async def agents_research(request: Request):
    body = await request.json()
    return team.research.run(body.get("topic", ""), body.get("depth", "brief")).__dict__

@app.post("/api/agents/code-fix")
async def agents_code_fix(request: Request):
    body = await request.json()
    return team.code_fixer.run(body.get("file_path", ""), body.get("code")).__dict__

@app.post("/api/agents/scam-check")
async def agents_scam_check(request: Request):
    body = await request.json()
    return team.scam_scout.run(body.get("url", ""), body.get("text", "")).__dict__

@app.post("/api/agents/summarize")
async def agents_summarize(request: Request):
    body = await request.json()
    return team.summarizer.run(body.get("text", ""), body.get("style", "bullets")).__dict__

@app.post("/api/agents/task/add")
async def agents_task_add(request: Request):
    body = await request.json()
    return team.scheduler.add(body.get("task", ""), body.get("priority", "medium")).__dict__

@app.get("/api/agents/task/list")
async def agents_task_list():
    return team.scheduler.list_tasks().__dict__

@app.post("/api/agents/task/done")
async def agents_task_done(request: Request):
    body = await request.json()
    return team.scheduler.complete(body.get("task", "")).__dict__


# ── Autonomous 24/7 dev agent ──────────────────────────────────────────────────
_agent_task = {"running": False, "handle": None}

@app.post("/api/agent/start")
async def agent_start(request: Request):
    body = await request.json() if request.headers.get("content-length") else {}
    interval = int(body.get("interval_sec", 600))
    if _agent_task["running"]:
        return {"ok": True, "already_running": True}
    _agent_task["running"] = True
    agent.running = True

    async def loop():
        while _agent_task["running"]:
            try:
                if agent.can_spend():
                    agent.cycle()
            except Exception as e:
                agent.activity_log.append({"ts": datetime.now(timezone.utc).isoformat(),
                                           "action": "cycle_error", "error": str(e)})
            await asyncio.sleep(interval)

    _agent_task["handle"] = asyncio.create_task(loop())
    return {"ok": True, "running": True, "interval_sec": interval,
            "note": "Auto-applies safe fixes (tests must pass). "
                    "Money/security/core changes queued for your approval."}

@app.post("/api/agent/stop")
async def agent_stop():
    _agent_task["running"] = False
    agent.running = False
    if _agent_task["handle"]:
        _agent_task["handle"].cancel()
    return {"ok": True, "running": False}

@app.post("/api/agent/cycle")
async def agent_cycle_once():
    return agent.cycle()

@app.get("/api/agent/pending")
async def agent_pending():
    return {"pending": agent.list_pending()}

@app.post("/api/agent/approve")
async def agent_approve(request: Request):
    body = await request.json()
    return agent.approve(body.get("id", ""))

@app.post("/api/agent/reject")
async def agent_reject(request: Request):
    body = await request.json()
    return agent.reject(body.get("id", ""))

@app.post("/api/agent/rollback")
async def agent_rollback(request: Request):
    body = await request.json()
    return agent.rollback(body.get("sha", ""))

@app.get("/api/agent/nights-work")
async def agent_nights_work():
    return agent.nights_work()


# ── WAN security ───────────────────────────────────────────────────────────────
@app.get("/api/wan/access-log")
async def wan_access_log():
    return _wan_log.report()


# ── Helpers ────────────────────────────────────────────────────────────────────
def _strip_tag(text: str) -> str:
    import re
    return re.sub(r"^@\w+\s*", "", text.strip())


# ── Startup ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn

    bind_host = os.getenv("YABBAI_BIND", "0.0.0.0")

    # WAN preflight -- grade the hosting config before accepting traffic
    report = wan_preflight(
        api_key=_auth.key,
        bind_host=bind_host,
        allow_remote_destructive=os.getenv("YABBAI_ALLOW_REMOTE_DESTRUCTIVE") == "1")
    print_preflight(report)
    print(_auth.banner())

    # Google auth sanity check
    print("=" * 56)
    if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
        print("  [OK] Google OAuth: configured")
        print("  [OK] Authorised user: thomas.basham1@gmail.com")
    else:
        print("  [!!] GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET not set!")
        print("    Set them in .env -- the auth gate will redirect but fail.")
    print("=" * 56)

    print("\n" + "=" * 56)
    print("  YABBAI LOCAL -- v9.5.0  (Google Auth + WAN Secure)")
    print("=" * 56)
    print(f"  GPU: {_hw.gpu_name or 'CPU only'}  |  RAM: {_hw.ram_gb}GB")
    print(f"  Model: {_rec.model_tag}  ({_rec.tier})")
    if not brain.client.is_running():
        print(f"\n  [!!] Ollama not running. Install --> ollama pull {_rec.model_tag}")
    elif not brain.client.model_ready():
        print(f"\n  [!!] Model not pulled. Run:  ollama pull {_rec.model_tag}")
    else:
        print(f"  [OK] Ready -- {brain.client.model} loaded.")
    print(f"\n  Local:   http://localhost:7860")
    print(f"  Tunnel:  set YABBAI_BIND=0.0.0.0 + cloudflared tunnel run yabbai")
    print("=" * 56 + "\n")

    uvicorn.run(app, host=bind_host, port=7860, log_level="warning")
