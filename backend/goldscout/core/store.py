"""
GoldScout durable store.

The original scout wrote findings to goldscout/data/findings.json. On Emergent the
app filesystem is wiped on every publish, so every scan was lost on redeploy. This
moves findings, scan history and the watchlist into Mongo (the only durable store),
keyed per user so the network's multi-Director model holds.

Read/analysis only — nothing here moves funds.
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

from network_db import db

F = db.goldscout_findings      # discovered opportunities (news + market), scam-scored
H = db.goldscout_scans         # one row per scan run (audit trail)
W = db.goldscout_watchlist     # tokens/opps a Director is tracking


def _now():
    return datetime.now(timezone.utc)


def _iso(v):
    if isinstance(v, datetime):
        return (v if v.tzinfo else v.replace(tzinfo=timezone.utc)).isoformat()
    if isinstance(v, dict):
        return {k: _iso(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_iso(x) for x in v]
    return v


def _clean(doc):
    return {k: _iso(v) for k, v in doc.items() if k != "_id"} if doc else None


async def add_findings(owner: str, records: list[dict]) -> int:
    """Upsert by (owner, url|token). Returns how many were newly added."""
    added = 0
    for r in records:
        key = r.get("url") or r.get("token_address")
        if not key:
            continue
        r = dict(r)
        r["owner"] = owner
        r.setdefault("id", str(uuid.uuid4()))
        r.setdefault("found_at", _now())
        r["dedupe_key"] = key
        res = await F.update_one(
            {"owner": owner, "dedupe_key": key},
            {"$set": r, "$setOnInsert": {"first_seen": _now()}},
            upsert=True)
        if res.upserted_id is not None:
            added += 1
    return added


async def load_findings(owner: str, risk_filter: str = "all", category: str = "all") -> list[dict]:
    q: dict = {"owner": owner}
    if risk_filter == "safe-ish":
        q["risk_level"] = "low"
    elif risk_filter == "flagged":
        q["risk_level"] = {"$in": ["high", "critical"]}
    elif risk_filter != "all":
        q["risk_level"] = risk_filter
    if category != "all":
        q["category"] = category
    rows = await F.find(q, {"_id": 0}).sort([("risk_score", 1), ("found_at", -1)]).to_list(1000)
    return [_iso(r) for r in rows]


async def clear_findings(owner: str) -> int:
    res = await F.delete_many({"owner": owner})
    return res.deleted_count


async def record_scan(owner: str, kind: str, summary: dict) -> str:
    sid = str(uuid.uuid4())
    await H.insert_one({"id": sid, "owner": owner, "kind": kind,
                        "summary": summary, "ran_at": _now()})
    return sid


async def recent_scans(owner: str, limit: int = 20) -> list[dict]:
    rows = await H.find({"owner": owner}, {"_id": 0}).sort("ran_at", -1).to_list(limit)
    return [_iso(r) for r in rows]


async def add_watch(owner: str, item: dict) -> dict:
    doc = {"id": str(uuid.uuid4()), "owner": owner, "added_at": _now(), **item}
    await W.update_one({"owner": owner, "token_address": item.get("token_address"),
                        "url": item.get("url")},
                       {"$set": doc}, upsert=True)
    return _clean(doc)


async def list_watch(owner: str) -> list[dict]:
    rows = await W.find({"owner": owner}, {"_id": 0}).sort("added_at", -1).to_list(500)
    return [_iso(r) for r in rows]


async def remove_watch(owner: str, watch_id: str) -> bool:
    res = await W.delete_one({"owner": owner, "id": watch_id})
    return res.deleted_count > 0


async def ensure_indexes() -> list[str]:
    await F.create_index([("owner", 1), ("dedupe_key", 1)], unique=True)
    await F.create_index([("owner", 1), ("risk_score", 1)])
    await F.create_index([("owner", 1), ("category", 1)])
    await H.create_index([("owner", 1), ("ran_at", -1)])
    await W.create_index([("owner", 1), ("added_at", -1)])
    return ["goldscout_findings", "goldscout_scans", "goldscout_watchlist"]
