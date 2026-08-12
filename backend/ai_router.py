"""
YABBAI AI surface — multi-tier routed brain with a constantly-learning core.

Routing tiers (free-first ladder; paid tier last, always):
  • nvidia     — NVIDIA NIM free OpenAI-compatible endpoints (integrate.api.nvidia.com)
  • groq       — Groq cloud, Llama 3.3 70B versatile (api.groq.com, free tier)
  • cerebras   — Cerebras free tier, ~1M tokens/day (api.cerebras.ai)
  • google     — Google AI Studio free tier via the OpenAI-compatible endpoint
  • openrouter — OpenRouter :free variants by default; paid models only on force_paid
  • grok       — xAI Grok (api.x.ai) — kept in code, DORMANT by default (XAI_ENABLED)
  • yabbai     — your YABBAI Local / Ollama box — optional, disabled by default
  • emergent   — Claude via the Emergent Universal LLM key (PAID, guarded, last)

PAID GUARD: a request only reaches emergent if 3+ free tiers were tried and failed,
or it is explicitly flagged force_paid. Every paid hit is logged with its reason.

CLIENT DATA — two levels, unclassified fails closed to confidential:
  • confidential — client private code/financials/credentials/NDA → yabbai ONLY.
  • business — scope briefs, call guides, lead research → yabbai + groq (no-training
    terms verified; 30-day abuse retention keeps groq out of confidential).

Each request tries the enabled tiers in order until one answers. Settings are read
from Mongo at request time, so rotating a key never needs a restart.

Learning: every successful answer from a non-YABBAI tier is recorded as an
exemplar (yabbai_learning.py). When the YABBAI tier answers, the most relevant
learned Q&As are injected into its system prompt so it constantly upgrades off
what the other AIs do.
"""

import os
import json
import time
import uuid
import asyncio
import logging

from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from dotenv import load_dotenv
import httpx

from openai import AsyncOpenAI
from emergentintegrations.llm.chat import LlmChat, UserMessage, TextDelta, StreamDone

from network_db import get_raw_settings, DEFAULTS
from auth_router import require_director
from yabbai_learning import (record_exchange, get_exemplars, build_learned_context,
                             learning_report, export_jsonl)
import ai_log

load_dotenv()

EMERGENT_LLM_KEY = os.environ.get("EMERGENT_LLM_KEY", "")
router = APIRouter(prefix="/api/ai", tags=["ai"])

SYSTEM_DEFAULT = (
    "You are YABBAI, the routed brain of the YABBAI network — a calm, precise "
    "operations co-pilot for an automation agency. You help with leads, copy, "
    "product audits, diagnostics and code. Be concise, concrete and honest. "
    "Never fabricate numbers; if data is missing, say so."
)

# Two-level data policy (Director-approved). Unclassified/unknown → confidential.
#   confidential — client private code, financials, credentials, NDA material
#                  → yabbai (local) ONLY. Fail closed, no fallback.
#   business     — scope briefs, call guides, lead research from public sources
#                  → verified external tiers permitted.
# groq is business-only: no-training per its Services Agreement, but 30-day abuse
# retention keeps it out of confidential. cerebras/google/openrouter excluded from
# both until the Director verifies their retention terms (google trains on free-tier
# prompts outside UK/CH/EEA/EU).
DATA_POLICY = {
    "confidential": {"yabbai"},
    "business": {"yabbai", "groq"},
}


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
    u = getattr(r, "usage", None)
    usage = {"in": u.prompt_tokens, "out": u.completion_tokens} if u else None
    return r.choices[0].message.content, model, usage


def _emergent_chat(s, system, session_id):
    return LlmChat(api_key=EMERGENT_LLM_KEY, session_id=session_id,
                   system_message=system).with_model("anthropic",
                   s.get("emergent_model") or DEFAULTS["emergent_model"])


async def _emergent_complete(s, system, prompt, session_id):
    if not EMERGENT_LLM_KEY:
        raise RuntimeError("Emergent LLM key not set")
    chat = _emergent_chat(s, system, session_id)
    content = await chat.send_message(UserMessage(text=prompt))
    return content, s.get("emergent_model") or DEFAULTS["emergent_model"], None


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
    return content, s.get("yabbai_model") or DEFAULTS["yabbai_model"], None


# ── observability + rate-limit state (in-memory; resets on restart) ────────────
log = logging.getLogger("ai_router")
TIER_STATS = {}   # tier -> {requests, answered, http_429, errors, latency_ms_total}
_COLD = {}        # tier -> unix ts until which the tier is skipped (rate-limited)


class _RateLimited(Exception):
    def __init__(self, retry_after=None):
        self.retry_after = retry_after


class _ModelNotFound(Exception):
    def __init__(self, model):
        self.model = model


class _PaymentRequired(Exception):
    """402 / insufficient-credit / key-cap. Falls through like a 429 — never a dead tier."""
    pass


PAYMENT_COLD_S = 900  # credit exhaustion is durable; don't hammer for 15 min


def _stat(t):
    return TIER_STATS.setdefault(
        t, {"requests": 0, "answered": 0, "http_429": 0, "errors": 0, "latency_ms_total": 0.0})


def _parse_retry_after(v, default=30):
    if not v:
        return default
    try:
        return max(1, int(float(v)))
    except Exception:
        try:
            from email.utils import parsedate_to_datetime
            import datetime as _dt
            dt = parsedate_to_datetime(v)
            return max(1, int((dt - _dt.datetime.now(dt.tzinfo)).total_seconds()))
        except Exception:
            return default


async def _openai_http_complete(s, system, prompt, prefix, label, model_override=None, extra_payload=None):
    """Shared OpenAI-compatible HTTP tier (groq, grok, cerebras, google, openrouter).
    30s timeout. 429 -> _RateLimited (cold + fallthrough, no retry/queue).
    402 / credit-exhausted -> _PaymentRequired (cold + fallthrough, like a 429).
    Model-not-found -> _ModelNotFound (log the name, fall through)."""
    key = s.get(f"{prefix}_api_key")
    if not key:
        raise RuntimeError(f"{label} API key not set")
    base = (s.get(f"{prefix}_base_url") or DEFAULTS[f"{prefix}_base_url"]).rstrip("/")
    model = model_override or s.get(f"{prefix}_model") or DEFAULTS[f"{prefix}_model"]
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    payload = {"model": model,
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": prompt}],
               "temperature": 0.4, "max_tokens": 1024}
    if extra_payload:
        payload.update(extra_payload)
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(f"{base}/chat/completions", json=payload, headers=headers)
    if r.status_code == 429:
        raise _RateLimited(r.headers.get("Retry-After"))
    if r.status_code == 402:
        raise _PaymentRequired((r.text or "")[:160])
    if r.status_code == 403:
        body_l = (r.text or "").lower()
        if any(w in body_l for w in ("credit", "quota", "billing", "insufficient")):
            raise _PaymentRequired((r.text or "")[:160])
    if r.status_code in (400, 404):
        body = (r.text or "").lower()
        if "model" in body and any(w in body for w in
                                   ("not found", "does not exist", "decommission", "deprecat", "invalid")):
            raise _ModelNotFound(model)
    r.raise_for_status()
    d = r.json()
    u = d.get("usage") or {}
    usage = {"in": u.get("prompt_tokens"), "out": u.get("completion_tokens"),
             "cost": u.get("cost")} if u else None
    return d["choices"][0]["message"]["content"], model, usage


HTTP_TIERS = {"groq": "Groq", "grok": "xAI (Grok)", "cerebras": "Cerebras",
              "google": "Google AI Studio"}


async def _openrouter_complete(s, system, prompt, force_paid=False):
    """OpenRouter — :free variants by default. A paid model is used ONLY when the
    request is explicitly force_paid (never a silent default). usage.include=true
    returns the real per-request cost for spend tracking against the key cap."""
    model = s.get("openrouter_model") or DEFAULTS["openrouter_model"]
    if force_paid and s.get("openrouter_paid_model"):
        model = s["openrouter_paid_model"]
    elif ":free" not in model:
        log.warning("openrouter model '%s' is not :free and request is not force_paid — "
                    "using the default free model", model)
        model = DEFAULTS["openrouter_model"]
    return await _openai_http_complete(s, system, prompt, "openrouter", "OpenRouter",
                                       model_override=model,
                                       extra_payload={"usage": {"include": True}})


PAID_TIERS = {"emergent"}


def _free_failures(attempts):
    """Free tiers that were tried and failed (cold + key-not-set count; data-policy
    skips do not — those tiers were never candidates)."""
    return [a["tier"] for a in attempts
            if a["tier"] not in PAID_TIERS and not a.get("ok")
            and not str(a.get("error") or "").startswith("data policy")
            and str(a.get("error") or "") != "paid guard"]


def _http_status_of(e):
    r = getattr(e, "response", None)
    return getattr(r, "status_code", None) or getattr(e, "status_code", None)


def _log_req(**kw):
    """Fire-and-forget durable request log (ai_log.py)."""
    async def _run():
        try:
            await ai_log.log_request(**kw)
        except Exception:
            log.debug("request log failed", exc_info=True)
    asyncio.create_task(_run())


def _learn(tier, model, prompt, content, latency_ms, session_id):
    """Fire-and-forget: record a peer-tier answer as YABBAI learning material."""
    async def _run():
        try:
            await record_exchange(tier, model, prompt, content, latency_ms, session_id)
        except Exception:
            log.debug("learning record failed", exc_info=True)
    asyncio.create_task(_run())


async def _yabbai_system(system, prompt):
    """Augment YABBAI's system prompt with the most relevant learned exemplars."""
    try:
        learned = build_learned_context(await get_exemplars(prompt))
    except Exception:
        learned = ""
    return f"{system}\n\n{learned}" if learned else system


def _order(s):
    order = [t for t in (s.get("route_order") or DEFAULTS["route_order"])
             if s.get(f"{t}_enabled", True)]
    return order or ["emergent"]


async def route_complete(system, prompt, session_id="yabbai", data_class=None,
                         task_type=None, sensitive=False, force_paid=False):
    """Walk the free-first ladder until a tier answers.
    data_class: None/'open' → full ladder · 'business' → business allowlist ·
    'confidential' (or ANY unrecognised value — fail closed on ambiguity) → yabbai only.
    Paid guard: emergent only after 3+ free tiers failed, or force_paid.
    Every request is durably logged (ai_log.py) with its fallthrough trail."""
    s = await get_raw_settings()
    order = _order(s)
    allowed = None
    if data_class and data_class != "open":
        allowed = DATA_POLICY.get(data_class) or DATA_POLICY["confidential"]
    errors = {}
    attempts = []
    fell_through = []
    rid = uuid.uuid4().hex
    task = task_type or session_id
    for t in order:
        if allowed is not None and t not in allowed:
            errors[t] = f"skipped (data policy: {data_class} → {'/'.join(sorted(allowed))} only)"
            attempts.append({"tier": t, "ok": False, "skipped": True,
                             "error": "data policy", "http_status": None, "latency_ms": None})
            continue
        if t in PAID_TIERS and not force_paid:
            ff = _free_failures(attempts)
            if len(ff) < 3:
                msg = f"paid guard: only {len(ff)} free tiers failed (need 3+ or force_paid)"
                errors[t] = msg
                attempts.append({"tier": t, "ok": False, "skipped": True, "error": "paid guard",
                                 "http_status": None, "latency_ms": None})
                log.warning("emergent blocked by paid guard (%s free failures)", len(ff))
                continue
        if _COLD.get(t, 0) > time.time():
            errors[t] = f"cold: rate-limited, {int(_COLD[t] - time.time())}s left"
            attempts.append({"tier": t, "ok": False, "skipped": True,
                             "error": "cold (rate-limited)", "http_status": None, "latency_ms": None})
            continue
        st = _stat(t)
        st["requests"] += 1
        t0 = time.time()
        try:
            if t == "emergent":
                content, model, usage = await _emergent_complete(s, system, prompt, session_id)
            elif t == "nvidia":
                content, model, usage = await _nvidia_complete(s, system, prompt)
            elif t == "openrouter":
                content, model, usage = await _openrouter_complete(s, system, prompt, force_paid)
            elif t in HTTP_TIERS:
                content, model, usage = await _openai_http_complete(s, system, prompt, t, HTTP_TIERS[t])
            elif t == "yabbai":
                content, model, usage = await _yabbai_complete(s, await _yabbai_system(system, prompt), prompt)
            else:
                continue
            latency = (time.time() - t0) * 1000
            st["latency_ms_total"] += latency
            if content and content.strip():
                st["answered"] += 1
                attempts.append({"tier": t, "ok": True, "skipped": False, "error": None,
                                 "http_status": 200, "latency_ms": round(latency, 1)})
                if t != "yabbai":
                    _learn(t, model, prompt, content, latency, session_id)
                cost = (usage or {}).get("cost")
                paid = t in PAID_TIERS or bool(cost)
                paid_reason = None
                if t in PAID_TIERS:
                    paid_reason = ("force_paid" if force_paid else
                                   "fallback after free failures: " + ",".join(_free_failures(attempts)))
                elif cost:
                    paid_reason = f"openrouter paid model: {model}"
                if paid:
                    log.warning("PAID tier hit: %s (%s) — %s", t, model, paid_reason)
                _log_req(request_id=rid, task_type=task, tier_requested=order[0],
                         tier_served=t, model=model, latency_ms=round(latency, 1),
                         tokens_in=(usage or {}).get("in"), tokens_out=(usage or {}).get("out"),
                         error=None, http_status=200, fell_through_from=list(fell_through),
                         attempts=attempts, sensitive=sensitive, paid=paid,
                         paid_reason=paid_reason, cost_usd=cost, data_class=data_class,
                         prompt=prompt, response=content, session_id=session_id)
                return {"content": content, "tier": t, "model": model, "request_id": rid}
            errors[t] = "empty response"
            fell_through.append(t)
            attempts.append({"tier": t, "ok": False, "skipped": False, "error": "empty response",
                             "http_status": 200, "latency_ms": round(latency, 1)})
        except _RateLimited as e:
            st["http_429"] += 1
            cold = _parse_retry_after(e.retry_after)
            _COLD[t] = time.time() + cold
            log.warning("%s rate-limited (429) — cold %ss, falling through", t, cold)
            errors[t] = f"429 rate-limited (cold {cold}s)"
            fell_through.append(t)
            attempts.append({"tier": t, "ok": False, "skipped": False,
                             "error": f"429 rate-limited (cold {cold}s)", "http_status": 429,
                             "latency_ms": round((time.time() - t0) * 1000, 1)})
        except _PaymentRequired as e:
            st["errors"] += 1
            _COLD[t] = time.time() + PAYMENT_COLD_S
            log.warning("%s insufficient credit (402-class) — cold %ss, falling through: %s",
                        t, PAYMENT_COLD_S, str(e)[:120])
            errors[t] = f"insufficient credit (cold {PAYMENT_COLD_S}s)"
            fell_through.append(t)
            attempts.append({"tier": t, "ok": False, "skipped": False,
                             "error": f"402 insufficient credit: {str(e)[:100]}", "http_status": 402,
                             "latency_ms": round((time.time() - t0) * 1000, 1)})
        except _ModelNotFound as e:
            st["errors"] += 1
            log.warning("%s model not found: '%s' — falling through", t, e.model)
            errors[t] = f"model not found: {e.model}"
            fell_through.append(t)
            attempts.append({"tier": t, "ok": False, "skipped": False,
                             "error": f"model not found: {e.model}", "http_status": 404,
                             "latency_ms": round((time.time() - t0) * 1000, 1)})
        except Exception as e:
            st["errors"] += 1
            errors[t] = str(e)[:160]
            fell_through.append(t)
            attempts.append({"tier": t, "ok": False, "skipped": False, "error": str(e)[:160],
                             "http_status": _http_status_of(e),
                             "latency_ms": round((time.time() - t0) * 1000, 1)})
    if allowed is not None:
        msg = (f"{data_class}-class request failed: no allowlisted tier available "
               f"(allowed: {', '.join(sorted(allowed))}). "
               f"Other tiers are never used for this data class.")
        _log_req(request_id=rid, task_type=task, tier_requested=order[0] if order else None,
                 tier_served=None, model=None, latency_ms=None, tokens_in=None, tokens_out=None,
                 error=msg[:160], http_status=503, fell_through_from=list(fell_through),
                 attempts=attempts, sensitive=sensitive, paid=False, data_class=data_class,
                 prompt=prompt, response=None, session_id=session_id)
        raise HTTPException(503, {"error": msg, "details": errors})
    _log_req(request_id=rid, task_type=task, tier_requested=order[0] if order else None,
             tier_served=None, model=None, latency_ms=None, tokens_in=None, tokens_out=None,
             error="all routing tiers failed", http_status=502,
             fell_through_from=list(fell_through), attempts=attempts, sensitive=sensitive,
             paid=False, data_class=data_class,
             prompt=prompt, response=None, session_id=session_id)
    raise HTTPException(502, {"error": "all routing tiers failed", "details": errors})


# ── health / providers ────────────────────────────────────────────────────────
@router.get("/health")
async def health():
    s = await get_raw_settings()
    return {"ok": True, "app": "yabbai-ai", "version": "2.3.0",
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
            "groq":     {"enabled": s.get("groq_enabled", True), "model": s.get("groq_model"),
                         "base_url": s.get("groq_base_url"), "key_set": bool(s.get("groq_api_key")),
                         "label": "Groq · Llama 3.3 70B (free)"},
            "cerebras": {"enabled": s.get("cerebras_enabled", True), "model": s.get("cerebras_model"),
                         "base_url": s.get("cerebras_base_url"), "key_set": bool(s.get("cerebras_api_key")),
                         "label": "Cerebras (free, ~1M tok/day)"},
            "google":   {"enabled": s.get("google_enabled", True), "model": s.get("google_model"),
                         "base_url": s.get("google_base_url"), "key_set": bool(s.get("google_api_key")),
                         "label": "Google AI Studio (free tier)"},
            "openrouter": {"enabled": s.get("openrouter_enabled", True), "model": s.get("openrouter_model"),
                         "base_url": s.get("openrouter_base_url"), "key_set": bool(s.get("openrouter_api_key")),
                         "label": "OpenRouter · :free variants"},
            "grok":     {"enabled": s.get("grok_enabled", False), "model": s.get("grok_model"),
                         "base_url": s.get("grok_base_url"), "key_set": bool(s.get("grok_api_key")),
                         "label": "xAI Grok (dormant — enable via XAI_ENABLED)"},
            "yabbai":   {"enabled": s.get("yabbai_enabled", True), "url": s.get("yabbai_url"),
                         "model": s.get("yabbai_model"), "key_set": bool(s.get("yabbai_api_key")),
                         "label": "YABBAI Local / Ollama (free, learning)"},
            "emergent": {"enabled": s.get("emergent_enabled", True), "model": s.get("emergent_model"),
                         "key_set": bool(EMERGENT_LLM_KEY), "label": "Emergent · Claude (paid)"},
        },
    }


@router.get("/stats")
async def stats(days: int = 7, user=Depends(require_director)):
    """Durable per-tier observability from Mongo: served/429s/errors/latency/rating,
    paid-tier hits, free-vs-paid ratio, and OpenRouter spend vs its key cap."""
    data = await ai_log.stats(min(max(days, 1), 90))
    now = time.time()
    data["cold_seconds_remaining"] = {t: int(ts - now) for t, ts in _COLD.items() if ts > now}
    data["storage"] = await ai_log.storage_status()
    s = await get_raw_settings()
    data["openrouter"]["key_live"] = await ai_log.openrouter_key_status(
        s.get("openrouter_api_key"),
        s.get("openrouter_base_url") or DEFAULTS["openrouter_base_url"])
    # Tavily (GoldScout news scout) — real usage/limit, cached inside the helper.
    try:
        from goldscout.core.scout import tavily_usage as _tavily_usage
        data["tavily"] = await _tavily_usage(s.get("tavily_api_key"))
    except Exception:
        data["tavily"] = {"configured": bool(s.get("tavily_api_key")), "ok": False,
                          "error": "usage lookup failed"}
    data["ok"] = True
    return data


class RateBody(BaseModel):
    request_id: str
    rating: int


@router.post("/rate")
async def rate_response(body: RateBody, user=Depends(require_director)):
    """Thumbs up (+1) / down (-1) on a logged AI response."""
    if body.rating not in (1, -1):
        raise HTTPException(422, "rating must be 1 or -1")
    if not await ai_log.rate(body.request_id, body.rating):
        raise HTTPException(404, "unknown request_id")
    return {"ok": True}


# ── learning: report back + upgrade dataset ───────────────────────────────────
@router.get("/learning")
async def learning(user=Depends(require_director)):
    """What YABBAI has learned from the other tiers so far."""
    return await learning_report()


@router.get("/learning/export")
async def learning_export(limit: int = 1000, user=Depends(require_director)):
    """Fine-tune-ready JSONL of everything learned — train your own model with it."""
    data = await export_jsonl(min(max(limit, 1), 5000))
    return StreamingResponse(iter([data]), media_type="application/jsonl",
                             headers={"Content-Disposition": "attachment; filename=yabbai_finetune.jsonl"})


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
            c, m, _u = await _nvidia_complete(s, "You are a connectivity test.", "Reply with the single word: OK")
        elif t == "openrouter":
            c, m, _u = await _openrouter_complete(s, "You are a connectivity test.", "Reply with the single word: OK")
        elif t in HTTP_TIERS:
            c, m, _u = await _openai_http_complete(s, "You are a connectivity test.", "Reply with the single word: OK", t, HTTP_TIERS[t])
        elif t == "emergent":
            c, m, _u = await _emergent_complete(s, "You are a connectivity test.", "Reply with the single word: OK", "test")
        elif t == "yabbai":
            c, m, _u = await _yabbai_complete(s, "You are a connectivity test.", "Reply with the single word: OK")
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
    sensitive: bool = False
    force_paid: bool = False


@router.post("/chat")
async def chat(body: ChatBody, user=Depends(require_director)):
    res = await route_complete(body.system or SYSTEM_DEFAULT, body.message, body.session_id,
                               task_type="chat", sensitive=body.sensitive,
                               force_paid=body.force_paid)
    return {"ok": True, "type": "message", "content": res["content"],
            "tier": res["tier"], "model": res["model"], "request_id": res["request_id"]}


@router.post("/chat/stream")
async def chat_stream(body: ChatBody, user=Depends(require_director)):
    system = body.system or SYSTEM_DEFAULT
    rid = uuid.uuid4().hex

    async def gen():
        s = await get_raw_settings()
        order = _order(s)
        last_err = None
        attempts = []
        fell = []

        def _ok(t, model, content, t0, usage=None):
            latency = round((time.time() - t0) * 1000, 1)
            attempts.append({"tier": t, "ok": True, "skipped": False, "error": None,
                             "http_status": 200, "latency_ms": latency})
            cost = (usage or {}).get("cost")
            paid = t in PAID_TIERS or bool(cost)
            paid_reason = None
            if t in PAID_TIERS:
                paid_reason = ("force_paid" if body.force_paid else
                               "fallback after free failures: " + ",".join(_free_failures(attempts)))
            elif cost:
                paid_reason = f"openrouter paid model: {model}"
            if paid:
                log.warning("PAID tier hit (stream): %s (%s) — %s", t, model, paid_reason)
            _log_req(request_id=rid, task_type="chat", tier_requested=order[0] if order else None,
                     tier_served=t, model=model, latency_ms=latency,
                     tokens_in=(usage or {}).get("in"), tokens_out=(usage or {}).get("out"),
                     error=None, http_status=200, fell_through_from=list(fell),
                     attempts=attempts, sensitive=body.sensitive, paid=paid,
                     paid_reason=paid_reason, cost_usd=cost,
                     prompt=body.message, response=content, session_id=body.session_id)

        def _fail(t, err, status, t0):
            fell.append(t)
            attempts.append({"tier": t, "ok": False, "skipped": False, "error": str(err)[:160],
                             "http_status": status,
                             "latency_ms": round((time.time() - t0) * 1000, 1)})

        for t in order:
            if t in PAID_TIERS and not body.force_paid:
                ff = _free_failures(attempts)
                if len(ff) < 3:
                    last_err = f"{t} blocked by paid guard ({len(ff)} free failures, need 3+)"
                    attempts.append({"tier": t, "ok": False, "skipped": True, "error": "paid guard",
                                     "http_status": None, "latency_ms": None})
                    log.warning("emergent blocked by paid guard in stream (%s free failures)", len(ff))
                    continue
            if _COLD.get(t, 0) > time.time():
                last_err = f"{t} cold (rate-limited)"
                attempts.append({"tier": t, "ok": False, "skipped": True,
                                 "error": "cold (rate-limited)", "http_status": None, "latency_ms": None})
                continue
            t0 = time.time()
            try:
                if t == "nvidia":
                    stream, model = await _nvidia_complete(s, system, body.message, stream=True)
                    yield f"\u200b"  # prime
                    acc = []
                    async for chunk in stream:
                        delta = chunk.choices[0].delta.content if chunk.choices else None
                        if delta:
                            acc.append(delta)
                            yield delta
                    content = "".join(acc)
                    _learn(t, model, body.message, content, 0.0, body.session_id)
                    _ok(t, model, content, t0)
                    return
                if t in HTTP_TIERS or t == "openrouter":
                    if t == "openrouter":
                        content, model, usage = await _openrouter_complete(s, system, body.message, body.force_paid)
                    else:
                        content, model, usage = await _openai_http_complete(s, system, body.message, t, HTTP_TIERS[t])
                    yield content or ""
                    _learn(t, model, body.message, content or "", 0.0, body.session_id)
                    _ok(t, model, content or "", t0, usage)
                    return
                if t == "yabbai":
                    content, model, _u = await _yabbai_complete(s, await _yabbai_system(system, body.message), body.message)
                    yield content or ""
                    _ok(t, model, content or "", t0)
                    return
                if t == "emergent":
                    if not EMERGENT_LLM_KEY:
                        raise RuntimeError("Emergent key not set")
                    chat_obj = _emergent_chat(s, system, body.session_id)
                    acc = []
                    async for ev in chat_obj.stream_message(UserMessage(text=body.message)):
                        if isinstance(ev, TextDelta):
                            acc.append(ev.content)
                            yield ev.content
                        elif isinstance(ev, StreamDone):
                            break
                    model = s.get("emergent_model") or DEFAULTS["emergent_model"]
                    content = "".join(acc)
                    _learn(t, model, body.message, content, 0.0, body.session_id)
                    _ok(t, model, content, t0)
                    return
            except _RateLimited as e:
                cold = _parse_retry_after(e.retry_after)
                _COLD[t] = time.time() + cold
                last_err = f"{t} 429"
                _fail(t, f"429 rate-limited (cold {cold}s)", 429, t0)
                continue
            except _PaymentRequired as e:
                _COLD[t] = time.time() + PAYMENT_COLD_S
                last_err = f"{t} 402 insufficient credit"
                _fail(t, f"402 insufficient credit: {str(e)[:100]}", 402, t0)
                continue
            except Exception as e:
                last_err = str(e)
                _fail(t, e, _http_status_of(e), t0)
                continue
        _log_req(request_id=rid, task_type="chat", tier_requested=order[0] if order else None,
                 tier_served=None, model=None, latency_ms=None, tokens_in=None, tokens_out=None,
                 error=str(last_err or "all routing tiers failed")[:160], http_status=502,
                 fell_through_from=list(fell), attempts=attempts, sensitive=body.sensitive,
                 paid=False, prompt=body.message, response=None, session_id=body.session_id)
        yield f"[all routing tiers failed: {last_err}]"

    return StreamingResponse(gen(), media_type="text/plain",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                                      "X-Request-Id": rid})


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
    res = await route_complete(system, f"Brief: {body.brief}\nBudget: {body.budget}\nTimeline: {body.timeline}", "scope", data_class="business")
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
    res = await route_complete(system, f"Lead: {body.lead_name}\nCompany: {body.company}\nStage: {body.stage}\nContext: {body.context}", "callguide", data_class="business")
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
