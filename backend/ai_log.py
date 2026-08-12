"""
Durable AI-router request log (Phase 1 logging substrate).

Two collections:
  ai_request_log    — one metrics doc per router request (never expires)
  ai_request_bodies — prompt/response bodies, TTL-expired (default 90 days),
                      capped per field, skipped entirely for sensitive requests.

Env: AI_BODY_TTL_DAYS (90) · AI_BODY_MAX_BYTES (16384, per field) · AI_LOG_ALARM_MB (400)
"""

import os
import time
import logging
from datetime import datetime, timezone, timedelta

import httpx

from network_db import db

log = logging.getLogger("ai_log")

BODY_TTL_DAYS = int(os.environ.get("AI_BODY_TTL_DAYS", "90"))
BODY_MAX_BYTES = int(os.environ.get("AI_BODY_MAX_BYTES", "16384"))
ALARM_MB = int(os.environ.get("AI_LOG_ALARM_MB", "400"))

_indexed = False
_write_n = 0


async def ensure_indexes():
    global _indexed
    if _indexed:
        return
    _indexed = True
    ttl = BODY_TTL_DAYS * 86400
    try:
        await db.ai_request_bodies.create_index("ts", expireAfterSeconds=ttl)
    except Exception:
        try:  # TTL changed since first creation -> adjust in place
            await db.command("collMod", "ai_request_bodies",
                             index={"keyPattern": {"ts": 1}, "expireAfterSeconds": ttl})
        except Exception:
            log.warning("could not set bodies TTL index", exc_info=True)
    try:
        await db.ai_request_log.create_index("ts")
        await db.ai_request_log.create_index("tier_served")
    except Exception:
        log.warning("ai_request_log index creation failed", exc_info=True)


async def log_request(request_id, task_type, tier_requested, tier_served, model,
                      latency_ms, tokens_in, tokens_out, error, http_status,
                      fell_through_from, attempts, sensitive, paid,
                      paid_reason=None, cost_usd=None,
                      prompt=None, response=None, session_id=None):
    global _write_n
    await ensure_indexes()
    now = datetime.now(timezone.utc)
    await db.ai_request_log.insert_one({
        "_id": request_id, "ts": now, "task_type": task_type,
        "tier_requested": tier_requested, "tier_served": tier_served, "model": model,
        "latency_ms": latency_ms, "tokens_in": tokens_in, "tokens_out": tokens_out,
        "error": error, "http_status": http_status,
        "fell_through_from": fell_through_from or [], "attempts": attempts or [],
        "sensitive": bool(sensitive), "paid": bool(paid), "rating": None,
        "paid_reason": paid_reason, "cost_usd": cost_usd,
        "session_id": session_id,
    })
    if not sensitive and (prompt or response):
        await db.ai_request_bodies.insert_one({
            "_id": request_id, "ts": now,
            "prompt": (prompt or "")[:BODY_MAX_BYTES],
            "response": (response or "")[:BODY_MAX_BYTES],
        })
    _write_n += 1
    if _write_n % 50 == 1:
        try:
            await storage_status()  # logs its own alarm warning
        except Exception:
            pass


async def rate(request_id, rating):
    r = await db.ai_request_log.update_one(
        {"_id": request_id},
        {"$set": {"rating": rating, "rated_at": datetime.now(timezone.utc)}})
    return r.matched_count == 1


_storage_cache = {"ts": 0.0, "data": None}


async def storage_status():
    """Size of both log collections; alarm once total crosses AI_LOG_ALARM_MB.
    A failed size read reports null, never a confident 0."""
    if time.time() - _storage_cache["ts"] < 300 and _storage_cache["data"]:
        return _storage_cache["data"]
    out = {"alarm_mb": ALARM_MB, "body_ttl_days": BODY_TTL_DAYS,
           "collections": {}, "total_mb": None, "alarm": False}
    try:
        existing = set(await db.list_collection_names())
        total = 0.0
        for c in ("ai_request_log", "ai_request_bodies"):
            if c not in existing:
                out["collections"][c] = 0.0
                continue
            mb = None
            async for d in db[c].aggregate([{"$collStats": {"storageStats": {}}}]):
                ss = d.get("storageStats", {})
                mb = round((ss.get("size", 0) + ss.get("totalIndexSize", 0)) / 1048576, 2)
            out["collections"][c] = mb
            total += mb or 0.0
        if all(v is not None for v in out["collections"].values()):
            out["total_mb"] = round(total, 2)
            out["alarm"] = total >= ALARM_MB
            if out["alarm"]:
                log.warning("AI log storage alarm: %.1f MB >= %s MB — Atlas M0 cap is 512 MB",
                            total, ALARM_MB)
    except Exception:
        log.debug("storage_status failed", exc_info=True)
    _storage_cache["ts"] = time.time()
    _storage_cache["data"] = out
    return out


async def stats(days=7):
    """Per-tier: tried/served/429s/errors/mean latency/mean rating/paid hits,
    plus the free-vs-paid ratio the whole build is measured by."""
    await ensure_indexes()
    since = datetime.now(timezone.utc) - timedelta(days=days)
    match = {"ts": {"$gte": since}}

    tiers = {}
    attempt_pipe = [
        {"$match": match},
        {"$unwind": "$attempts"},
        {"$group": {
            "_id": "$attempts.tier",
            "tried": {"$sum": {"$cond": [{"$eq": ["$attempts.skipped", True]}, 0, 1]}},
            "skipped": {"$sum": {"$cond": [{"$eq": ["$attempts.skipped", True]}, 1, 0]}},
            "served": {"$sum": {"$cond": ["$attempts.ok", 1, 0]}},
            "http_429": {"$sum": {"$cond": [{"$eq": ["$attempts.http_status", 429]}, 1, 0]}},
            "errors": {"$sum": {"$cond": [{"$and": [
                {"$ne": ["$attempts.ok", True]},
                {"$ne": ["$attempts.skipped", True]},
                {"$ne": ["$attempts.http_status", 429]}]}, 1, 0]}},
            "latency_ms_sum": {"$sum": {"$cond": ["$attempts.ok", "$attempts.latency_ms", 0]}},
        }},
    ]
    async for d in db.ai_request_log.aggregate(attempt_pipe):
        served = d["served"]
        tiers[d["_id"]] = {
            "requests_tried": d["tried"], "skipped": d["skipped"], "served": served,
            "http_429": d["http_429"], "errors": d["errors"],
            "mean_latency_ms": round(d["latency_ms_sum"] / served, 1) if served else None,
            "mean_rating": None, "paid_hits": 0,
        }

    served_pipe = [
        {"$match": {**match, "tier_served": {"$ne": None}}},
        {"$group": {
            "_id": "$tier_served",
            "rating_sum": {"$sum": {"$ifNull": ["$rating", 0]}},
            "rating_n": {"$sum": {"$cond": [{"$ne": ["$rating", None]}, 1, 0]}},
            "paid_hits": {"$sum": {"$cond": ["$paid", 1, 0]}},
        }},
    ]
    async for d in db.ai_request_log.aggregate(served_pipe):
        e = tiers.setdefault(d["_id"], {
            "requests_tried": 0, "skipped": 0, "served": 0, "http_429": 0,
            "errors": 0, "mean_latency_ms": None, "mean_rating": None, "paid_hits": 0})
        e["mean_rating"] = round(d["rating_sum"] / d["rating_n"], 2) if d["rating_n"] else None
        e["paid_hits"] = d["paid_hits"]

    total = await db.ai_request_log.count_documents(match)
    served_total = await db.ai_request_log.count_documents({**match, "tier_served": {"$ne": None}})
    paid_total = await db.ai_request_log.count_documents({**match, "paid": True})
    free_served = served_total - paid_total

    # OpenRouter spend vs its hard key cap (usage.cost is the provider's own number)
    or_win, or_all = None, None
    async for d in db.ai_request_log.aggregate([
            {"$match": {**match, "tier_served": "openrouter"}},
            {"$group": {"_id": None, "usd": {"$sum": {"$ifNull": ["$cost_usd", 0]}},
                        "n": {"$sum": 1}}}]):
        or_win = d
    async for d in db.ai_request_log.aggregate([
            {"$match": {"tier_served": "openrouter"}},
            {"$group": {"_id": None, "usd": {"$sum": {"$ifNull": ["$cost_usd", 0]}}}}]):
        or_all = d

    return {
        "window_days": days, "requests": total, "served": served_total,
        "failed": total - served_total, "paid_hits": paid_total, "free_served": free_served,
        "free_ratio": round(free_served / served_total, 4) if served_total else None,
        "tiers": tiers,
        "openrouter": {
            "served_window": or_win["n"] if or_win else 0,
            "spend_usd_window": round(or_win["usd"], 6) if or_win else 0.0,
            "spend_usd_total": round(or_all["usd"], 6) if or_all else 0.0,
            "key_cap_usd": float(os.environ.get("OPENROUTER_KEY_CAP_USD", "6.50")),
            "key_live": None,  # filled by the router (needs the key)
        },
    }


_or_key_cache = {"ts": 0.0, "data": None}


async def openrouter_key_status(key, base_url="https://openrouter.ai/api/v1"):
    """Live usage/limit from OpenRouter's /key endpoint — the authoritative view of
    the $ cap draining. Null on failure or when no key is set, never a made-up 0."""
    if not key:
        return None
    if time.time() - _or_key_cache["ts"] < 300:
        return _or_key_cache["data"]
    data = None
    try:
        async with httpx.AsyncClient(timeout=6.0) as c:
            r = await c.get(f"{base_url.rstrip('/')}/key",
                            headers={"Authorization": f"Bearer {key}"})
        if r.status_code == 200:
            d = (r.json() or {}).get("data") or {}
            data = {"usage_usd": d.get("usage"), "limit_usd": d.get("limit"),
                    "limit_remaining_usd": d.get("limit_remaining")}
    except Exception:
        data = None
    _or_key_cache["ts"] = time.time()
    _or_key_cache["data"] = data
    return data
