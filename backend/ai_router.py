"""
YABBAI AI surface — multi-tier routed brain.

Routing tiers (configurable order, set in Settings):
  • nvidia   — NVIDIA NIM free OpenAI-compatible endpoints (integrate.api.nvidia.com)
  • emergent — Claude Sonnet via the Emergent Universal LLM key
  • yabbai    — your YABBAI Local / Ollama box (URL + key), the self-hosted fallback

Each request tries the enabled tiers in order until one answers. Settings are read
from Mongo at request time, so rotating a key never needs a restart.
"""

import os
import json

from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from dotenv import load_dotenv
import httpx

from openai import AsyncOpenAI
from emergentintegrations.llm.chat import LlmChat, UserMessage, TextDelta, StreamDone

from network_db import get_raw_settings, DEFAULTS
from auth_router import require_director

load_dotenv()

EMERGENT_LLM_KEY = os.environ.get("EMERGENT_LLM_KEY", "")
router = APIRouter(prefix="/api/ai", tags=["ai"])

SYSTEM_DEFAULT = (
    "You are YABBAI, the routed brain of the YABBAI network — a calm, precise "
    "operations co-pilot for an automation agency. You help with leads, copy, "
    "product audits, diagnostics and code. Be concise, concrete and honest. "
    "Never fabricate numbers; if data is missing, say so."
)


# ── tier implementations ──────────────────────────────────────────────────────
async def _nvidia_complete(s, system, prompt, stream=False):
    key = s.get("nvidia_api_key")
    if not key:
        raise RuntimeError("NVIDIA API key not set")
    client = AsyncOpenAI(base_url=s.get("nvidia_base_url") or DEFAULTS["nvidia_base_url"], api_key=key)
    model = s.get("nvidia_model") or DEFAULTS["nvidia_model"]
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    if stream:
        return await client.chat.completions.create(model=model, messages=msgs,
                                                    temperature=0.4, max_tokens=1024, stream=True), model
    r = await client.chat.completions.create(model=model, messages=msgs,
                                             temperature=0.4, max_tokens=1024)
    return r.choices[0].message.content, model


def _emergent_chat(s, system, session_id):
    return LlmChat(api_key=EMERGENT_LLM_KEY, session_id=session_id,
                   system_message=system).with_model("anthropic",
                   s.get("emergent_model") or DEFAULTS["emergent_model"])


async def _emergent_complete(s, system, prompt, session_id):
    if not EMERGENT_LLM_KEY:
        raise RuntimeError("Emergent LLM key not set")
    chat = _emergent_chat(s, system, session_id)
    content = await chat.send_message(UserMessage(text=prompt))
    return content, s.get("emergent_model") or DEFAULTS["emergent_model"]


async def _yabbai_complete(s, system, prompt):
    url = (s.get("yabbai_url") or "").rstrip("/")
    if not url:
        raise RuntimeError("YABBAI local URL not set")
    headers = {"Content-Type": "application/json"}
    if s.get("yabbai_api_key"):
        headers["X-YABBAI-Key"] = s["yabbai_api_key"]
    async with httpx.AsyncClient(timeout=60.0) as c:
        r = await c.post(f"{url}/api/chat", json={"message": f"{system}\n\n{prompt}"}, headers=headers)
        r.raise_for_status()
        d = r.json()
    content = (d.get("content") or d.get("reply") or d.get("response")
               or d.get("text") or (d.get("message") if isinstance(d.get("message"), str) else ""))
    return content, s.get("yabbai_model") or DEFAULTS["yabbai_model"]


def _order(s):
    order = [t for t in (s.get("route_order") or DEFAULTS["route_order"])
             if s.get(f"{t}_enabled", True)]
    return order or ["emergent"]


async def route_complete(system, prompt, session_id="yabbai"):
    s = await get_raw_settings()
    errors = {}
    for t in _order(s):
        try:
            if t == "emergent":
                content, model = await _emergent_complete(s, system, prompt, session_id)
            elif t == "nvidia":
                content, model = await _nvidia_complete(s, system, prompt)
            elif t == "yabbai":
                content, model = await _yabbai_complete(s, system, prompt)
            else:
                continue
            if content and content.strip():
                return {"content": content, "tier": t, "model": model}
            errors[t] = "empty response"
        except Exception as e:
            errors[t] = str(e)[:160]
    raise HTTPException(502, {"error": "all routing tiers failed", "details": errors})


# ── health / providers ────────────────────────────────────────────────────────
@router.get("/health")
async def health():
    s = await get_raw_settings()
    return {"ok": True, "app": "yabbai-ai", "version": "2.1.0",
            "route_order": _order(s),
            "key_configured": bool(EMERGENT_LLM_KEY) or bool(s.get("nvidia_api_key")) or bool(s.get("yabbai_url"))}


@router.get("/providers")
async def providers(user=Depends(require_director)):
    s = await get_raw_settings()
    return {
        "route_order": s.get("route_order") or DEFAULTS["route_order"],
        "active_order": _order(s),
        "tiers": {
            "nvidia":   {"enabled": s.get("nvidia_enabled", True), "model": s.get("nvidia_model"),
                         "base_url": s.get("nvidia_base_url"), "key_set": bool(s.get("nvidia_api_key")),
                         "label": "NVIDIA NIM (free)"},
            "emergent": {"enabled": s.get("emergent_enabled", True), "model": s.get("emergent_model"),
                         "key_set": bool(EMERGENT_LLM_KEY), "label": "Emergent · Claude"},
            "yabbai":   {"enabled": s.get("yabbai_enabled", True), "url": s.get("yabbai_url"),
                         "model": s.get("yabbai_model"), "key_set": bool(s.get("yabbai_api_key")),
                         "label": "YABBAI Local / Ollama"},
        },
    }


@router.get("/nvidia/models")
async def nvidia_models(user=Depends(require_director)):
    s = await get_raw_settings()
    key = s.get("nvidia_api_key")
    if not key:
        return {"ok": False, "models": [], "message": "Save your NVIDIA API key first."}
    try:
        client = AsyncOpenAI(base_url=s.get("nvidia_base_url") or DEFAULTS["nvidia_base_url"], api_key=key)
        ms = await client.models.list()
        return {"ok": True, "models": sorted([m.id for m in ms.data])}
    except Exception as e:
        return {"ok": False, "models": [], "message": str(e)[:200]}


class TestBody(BaseModel):
    provider: str


@router.post("/test")
async def test_provider(body: TestBody, user=Depends(require_director)):
    s = await get_raw_settings()
    t = body.provider
    try:
        if t == "nvidia":
            c, m = await _nvidia_complete(s, "You are a connectivity test.", "Reply with the single word: OK")
        elif t == "emergent":
            c, m = await _emergent_complete(s, "You are a connectivity test.", "Reply with the single word: OK", "test")
        elif t == "yabbai":
            c, m = await _yabbai_complete(s, "You are a connectivity test.", "Reply with the single word: OK")
        else:
            return {"ok": False, "message": "unknown provider"}
        return {"ok": True, "tier": t, "model": m, "sample": (c or "")[:120]}
    except Exception as e:
        return {"ok": False, "tier": t, "message": str(e)[:200]}


# ── chat ──────────────────────────────────────────────────────────────────────
class ChatBody(BaseModel):
    message: str
    session_id: str = "yabbai-chat"
    system: str | None = None


@router.post("/chat")
async def chat(body: ChatBody, user=Depends(require_director)):
    res = await route_complete(body.system or SYSTEM_DEFAULT, body.message, body.session_id)
    return {"ok": True, "type": "message", "content": res["content"],
            "tier": res["tier"], "model": res["model"]}


@router.post("/chat/stream")
async def chat_stream(body: ChatBody, user=Depends(require_director)):
    system = body.system or SYSTEM_DEFAULT

    async def gen():
        s = await get_raw_settings()
        last_err = None
        for t in _order(s):
            try:
                if t == "nvidia":
                    stream, model = await _nvidia_complete(s, system, body.message, stream=True)
                    yield f"\u200b"  # prime
                    async for chunk in stream:
                        delta = chunk.choices[0].delta.content if chunk.choices else None
                        if delta:
                            yield delta
                    return
                if t == "emergent":
                    if not EMERGENT_LLM_KEY:
                        raise RuntimeError("Emergent key not set")
                    chat_obj = _emergent_chat(s, system, body.session_id)
                    async for ev in chat_obj.stream_message(UserMessage(text=body.message)):
                        if isinstance(ev, TextDelta):
                            yield ev.content
                        elif isinstance(ev, StreamDone):
                            break
                    return
                if t == "yabbai":
                    content, _ = await _yabbai_complete(s, system, body.message)
                    yield content or ""
                    return
            except Exception as e:
                last_err = str(e)
                continue
        yield f"[all routing tiers failed: {last_err}]"

    return StreamingResponse(gen(), media_type="text/plain",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── edge-function equivalents (used by the web surfaces) ──────────────────────
class DiagnoseBody(BaseModel):
    business: str = ""
    website: str = ""
    goal: str = ""
    notes: str = ""


@router.post("/diagnose")
async def diagnose(body: DiagnoseBody):
    system = ("You are YABBAI's Diagnost agent. Given a business, produce a brutally "
              "practical improvement plan. Output STRICT JSON only with keys: headline, summary, "
              "quick_wins (array of {title, impact, effort}), fixes (array of {area, problem, fix, why}), "
              "next_step. No markdown.")
    prompt = f"Business: {body.business}\nWebsite: {body.website}\nGoal: {body.goal}\nNotes: {body.notes}"
    res = await route_complete(system, prompt, "diagnose")
    return {"ok": True, "plan": _safe_json(res["content"]), "tier": res["tier"], "model": res["model"]}


class ScopeBody(BaseModel):
    brief: str
    budget: str = ""
    timeline: str = ""


@router.post("/scope-brief")
async def scope_brief(body: ScopeBody, user=Depends(require_director)):
    system = ("You are YABBAI's scoping agent. Output STRICT JSON only with keys: title, summary, "
              "deliverables (array), milestones (array of {name, outcome}), assumptions (array), price_band. No markdown.")
    res = await route_complete(system, f"Brief: {body.brief}\nBudget: {body.budget}\nTimeline: {body.timeline}", "scope")
    return {"ok": True, "scope": _safe_json(res["content"]), "tier": res["tier"], "model": res["model"]}


class CallGuideBody(BaseModel):
    lead_name: str = ""
    company: str = ""
    stage: str = ""
    context: str = ""


@router.post("/call-guide")
async def call_guide(body: CallGuideBody, user=Depends(require_director)):
    system = ("You are YABBAI's Caller agent. Output STRICT JSON only with keys: opener, "
              "discovery_questions (array), objections (array of {objection, response}), close. No markdown.")
    res = await route_complete(system, f"Lead: {body.lead_name}\nCompany: {body.company}\nStage: {body.stage}\nContext: {body.context}", "callguide")
    return {"ok": True, "guide": _safe_json(res["content"]), "tier": res["tier"], "model": res["model"]}


class CatalogBody(BaseModel):
    idea: str
    audience: str = ""


@router.post("/catalog-agent")
async def catalog_agent(body: CatalogBody, user=Depends(require_director)):
    system = ("You are YABBAI's Auditor agent. Output STRICT JSON only with keys: name, one_liner, "
              "audit_score (0-100 integer), verdict ('list' or 'hold'), strengths (array), risks (array), required_proof (array). No markdown.")
    res = await route_complete(system, f"Product idea: {body.idea}\nAudience: {body.audience}", "catalog")
    return {"ok": True, "audit": _safe_json(res["content"]), "tier": res["tier"], "model": res["model"]}


def _safe_json(raw: str):
    s = (raw or "").strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:]
    try:
        return json.loads(s[s.index("{"): s.rindex("}") + 1])
    except Exception:
        return None
