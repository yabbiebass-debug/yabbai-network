"""
YABBAI learning core — the "constantly learning" brain.

Every answer produced by a peer tier (NVIDIA, Groq, Grok, Emergent/Claude) is
captured as a learning exemplar in Mongo. When the YABBAI tier answers, the most
relevant learned Q&As are injected as context (retrieval-augmented distillation),
so YABBAI keeps upgrading off what the other AIs do. The full dataset can be
exported as fine-tune JSONL for training the self-hosted model.
"""

import re
import json
import uuid
from datetime import datetime, timezone

from network_db import db

_STOP = set(("the a an and or of to in on for with is are was be as at by it this "
             "that from you your what how why when where can will would should").split())


def _keywords(text, limit=24):
    words = re.findall(r"[a-z0-9']{3,}", (text or "").lower())
    seen, out = set(), []
    for w in words:
        if w in _STOP or w in seen:
            continue
        seen.add(w)
        out.append(w)
        if len(out) >= limit:
            break
    return out


async def record_exchange(tier, model, prompt, response, latency_ms=0.0, session_id="yabbai"):
    """Store a successful peer-AI answer as a learning exemplar."""
    if not response or not response.strip():
        return
    ts = datetime.now(timezone.utc).isoformat()
    await db.yabbai_exemplars.insert_one({
        "_id": uuid.uuid4().hex,
        "ts": ts,
        "tier": tier,
        "model": model,
        "session_id": session_id,
        "prompt": (prompt or "")[:2000],
        "response": response[:4000],
        "keywords": _keywords(prompt),
        "latency_ms": round(latency_ms, 1),
    })
    await db.yabbai_learning_state.update_one(
        {"_id": "state"},
        {"$inc": {"total_learned": 1, f"by_tier.{tier}": 1},
         "$set": {"last_learned_at": ts, "last_tier": tier, "last_model": model}},
        upsert=True)


async def get_exemplars(prompt, k=3):
    """Top-k most relevant learned exemplars by keyword overlap, recent first."""
    kws = _keywords(prompt)
    if not kws:
        return []
    cands = await (db.yabbai_exemplars
                   .find({"keywords": {"$in": kws}},
                         {"prompt": 1, "response": 1, "keywords": 1, "tier": 1, "model": 1})
                   .sort("ts", -1).limit(200).to_list(200))
    kset = set(kws)
    cands.sort(key=lambda d: -len(kset & set(d.get("keywords") or [])))
    return cands[:k]


def build_learned_context(exemplars):
    if not exemplars:
        return ""
    parts = ["LEARNED KNOWLEDGE — distilled from your peer AI tiers. Use it to answer better:"]
    for i, e in enumerate(exemplars, 1):
        parts.append(f"[{i}] Q: {e.get('prompt', '')[:300]}\n"
                     f"    A (learned from {e.get('tier')}/{e.get('model', '')}): {e.get('response', '')[:600]}")
    return "\n".join(parts)


async def learning_report():
    state = await db.yabbai_learning_state.find_one({"_id": "state"}) or {}
    total = await db.yabbai_exemplars.count_documents({})
    recent = await (db.yabbai_exemplars
                    .find({}, {"_id": 0, "ts": 1, "tier": 1, "model": 1, "prompt": 1})
                    .sort("ts", -1).limit(10).to_list(10))
    for r in recent:
        r["prompt"] = (r.get("prompt") or "")[:120]
    pipe = [{"$unwind": "$keywords"},
            {"$group": {"_id": "$keywords", "n": {"$sum": 1}}},
            {"$sort": {"n": -1}}, {"$limit": 15}]
    topics = [{"topic": d["_id"], "count": d["n"]}
              async for d in db.yabbai_exemplars.aggregate(pipe)]
    return {
        "ok": True,
        "total_exemplars": total,
        "by_tier": state.get("by_tier", {}),
        "last_learned_at": state.get("last_learned_at"),
        "last_teacher": {"tier": state.get("last_tier"), "model": state.get("last_model")},
        "top_topics": topics,
        "recent": recent,
        "how_it_upgrades_yabbai": (
            "Every answer from NVIDIA / Groq / Grok / Claude is stored as an exemplar. "
            "When the YABBAI tier answers, the most relevant learned Q&As are injected as "
            "context (retrieval-augmented distillation). Export the dataset at "
            "/api/ai/learning/export to fine-tune your self-hosted model."),
    }


async def export_jsonl(limit=1000):
    """Chat-format fine-tune dataset (one {'messages': [...]} object per line)."""
    rows = await (db.yabbai_exemplars
                  .find({}, {"_id": 0, "prompt": 1, "response": 1})
                  .sort("ts", -1).limit(limit).to_list(limit))
    return "\n".join(json.dumps({"messages": [
        {"role": "user", "content": r.get("prompt", "")},
        {"role": "assistant", "content": r.get("response", "")}]}) for r in rows)
