"""
YABBAI AI surface — unified brain powered by the Emergent Universal LLM key.

Replaces the heavy local Ollama stack + the Supabase edge functions
(ai-proxy / diagnose / scope-brief / call-guide / catalog-agent) with a
single Claude-backed router. Same 3-tier story for the UI; here the primary
tier is Claude Sonnet via the Universal key.
"""

import os
import json
from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from dotenv import load_dotenv

from emergentintegrations.llm.chat import LlmChat, UserMessage, TextDelta, StreamDone

load_dotenv()

EMERGENT_LLM_KEY = os.environ.get("EMERGENT_LLM_KEY", "")
MODEL = ("anthropic", "claude-sonnet-4-6")

router = APIRouter(prefix="/api/ai", tags=["ai"])


def _chat(system: str, session_id: str = "yabbai") -> LlmChat:
    return LlmChat(
        api_key=EMERGENT_LLM_KEY,
        session_id=session_id,
        system_message=system,
    ).with_model(*MODEL)


async def _complete(system: str, prompt: str, session_id: str = "yabbai") -> str:
    chat = _chat(system, session_id)
    return await chat.send_message(UserMessage(text=prompt))


# ── health ────────────────────────────────────────────────────────────────────
@router.get("/health")
async def health():
    return {
        "ok": True,
        "app": "yabbai-ai",
        "version": "2.0.0",
        "brain": "anthropic:claude-sonnet-4-6 (Emergent Universal key)",
        "key_configured": bool(EMERGENT_LLM_KEY),
        "routing": ["anthropic", "openrouter", "ollama"],
        "ts": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/router/models")
async def router_models():
    return {
        "available": [
            {"id": "claude", "provider": "anthropic", "model": "claude-sonnet-4-6", "private": False, "tier": "primary"},
        ],
        "policy": "auto",
        "workhorse": "claude",
        "specialist": "claude",
    }


# ── chat ──────────────────────────────────────────────────────────────────────
class ChatBody(BaseModel):
    message: str
    session_id: str = "yabbai-chat"
    system: str | None = None


SYSTEM_DEFAULT = (
    "You are YABBAI, the routed brain of the YABBAI network — a calm, precise "
    "operations co-pilot for an automation agency. You help with leads, copy, "
    "product audits, diagnostics and code. Be concise, concrete and honest. "
    "Never fabricate numbers; if data is missing, say so."
)


@router.post("/chat")
async def chat(body: ChatBody):
    if not EMERGENT_LLM_KEY:
        return {"ok": False, "content": "LLM key not configured."}
    content = await _complete(body.system or SYSTEM_DEFAULT, body.message, body.session_id)
    return {"ok": True, "type": "message", "content": content,
            "model": "claude-sonnet-4-6", "provider": "anthropic", "private": False}


@router.post("/chat/stream")
async def chat_stream(body: ChatBody):
    async def gen():
        chat_obj = _chat(body.system or SYSTEM_DEFAULT, body.session_id)
        async for ev in chat_obj.stream_message(UserMessage(text=body.message)):
            if isinstance(ev, TextDelta):
                yield ev.content
            elif isinstance(ev, StreamDone):
                break
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
    """Free Fix-Plan lead magnet — returns a concrete improvement plan as JSON."""
    system = (
        "You are YABBAI's Diagnost agent. Given a business, produce a brutally "
        "practical, no-fluff improvement plan. Output STRICT JSON only with keys: "
        "headline (string), summary (string), "
        "quick_wins (array of {title, impact, effort}), "
        "fixes (array of {area, problem, fix, why}), "
        "next_step (string). No markdown, no commentary."
    )
    prompt = (
        f"Business: {body.business}\nWebsite: {body.website}\n"
        f"Goal: {body.goal}\nNotes: {body.notes}\n\n"
        "Generate the fix-plan JSON."
    )
    raw = await _complete(system, prompt, "diagnose")
    return {"ok": True, "plan": _safe_json(raw), "raw": raw}


class ScopeBody(BaseModel):
    brief: str
    budget: str = ""
    timeline: str = ""


@router.post("/scope-brief")
async def scope_brief(body: ScopeBody):
    system = (
        "You are YABBAI's scoping agent. Turn a plain-language client brief into a "
        "tight project scope. Output STRICT JSON only with keys: title, summary, "
        "deliverables (array of strings), milestones (array of {name, outcome}), "
        "assumptions (array of strings), price_band (string). No markdown."
    )
    prompt = f"Brief: {body.brief}\nBudget: {body.budget}\nTimeline: {body.timeline}"
    raw = await _complete(system, prompt, "scope")
    return {"ok": True, "scope": _safe_json(raw), "raw": raw}


class CallGuideBody(BaseModel):
    lead_name: str = ""
    company: str = ""
    stage: str = ""
    context: str = ""


@router.post("/call-guide")
async def call_guide(body: CallGuideBody):
    system = (
        "You are YABBAI's Caller agent. Produce a focused call guide for a sales "
        "conversation. Output STRICT JSON only with keys: opener (string), "
        "discovery_questions (array of strings), objections (array of {objection, response}), "
        "close (string). No markdown."
    )
    prompt = (f"Lead: {body.lead_name}\nCompany: {body.company}\n"
              f"Stage: {body.stage}\nContext: {body.context}")
    raw = await _complete(system, prompt, "callguide")
    return {"ok": True, "guide": _safe_json(raw), "raw": raw}


class CatalogBody(BaseModel):
    idea: str
    audience: str = ""


@router.post("/catalog-agent")
async def catalog_agent(body: CatalogBody):
    system = (
        "You are YABBAI's Auditor agent. Evaluate a product idea and score it so the "
        "catalog 'never sells air'. Output STRICT JSON only with keys: name, "
        "one_liner, audit_score (0-100 integer), verdict ('list' or 'hold'), "
        "strengths (array), risks (array), required_proof (array). No markdown."
    )
    prompt = f"Product idea: {body.idea}\nAudience: {body.audience}"
    raw = await _complete(system, prompt, "catalog")
    return {"ok": True, "audit": _safe_json(raw), "raw": raw}


def _safe_json(raw: str):
    s = raw.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1] if "```" in s[3:] else s.strip("`")
        s = s.replace("json", "", 1).strip() if s.lstrip().lower().startswith("json") else s
    try:
        start = s.index("{")
        end = s.rindex("}") + 1
        return json.loads(s[start:end])
    except Exception:
        return None
