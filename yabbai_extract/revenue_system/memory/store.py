"""
YABBAI Revenue System — Shared Memory

One store every agent and channel reads & writes. This is how the system
compounds: every outcome (win or loss) is recorded with its reason, so no agent
ever knowingly repeats an avoidable documented failure, and every channel can
see what the others learned.

JSON-file backed — zero external dependencies, survives restarts, trivially
inspectable. Swap the backend for Postgres+pgvector later without touching the
public API; the public surface is the same.
"""

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class Learning:
    """A typed outcome record. The compounding asset of the whole system."""
    kind: str               # "win" | "loss" | "signal" | "objection" | "refund_reason" | ...
    channel: str            # products | agency | trading | ...
    agent: str
    summary: str
    detail: Dict[str, Any] = field(default_factory=dict)
    usd_impact: float = 0.0          # signed: + win, - loss; 0 if not monetary
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class Memory:
    """
    Append-only store over JSON. Honest about its limits: this is a local,
    single-process store, perfect for one operator running the suite. For
    multi-process scale, mirror the same interface to Postgres.
    """

    COLLECTIONS = ("learnings", "clients", "leads", "campaigns",
                   "products", "offers", "signals", "trades")

    def __init__(self, root: str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._files: Dict[str, Path] = {}
        for c in self.COLLECTIONS:
            p = self.root / f"{c}.json"
            self._files[c] = p
            if not p.exists():
                self._write(c, [])

    # ── low-level ──────────────────────────────────────────────────────────
    def _read(self, coll: str) -> List[Dict[str, Any]]:
        try:
            return json.loads(self._files[coll].read_text(encoding="utf-8"))
        except Exception:
            return []

    def _write(self, coll: str, data: List[Dict[str, Any]]) -> None:
        tmp = self._files[coll].with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self._files[coll])   # atomic-ish on most filesystems

    # ── public API ─────────────────────────────────────────────────────────
    def add(self, coll: str, record: Dict[str, Any]) -> Dict[str, Any]:
        if coll not in self._files:
            raise ValueError(f"Unknown collection '{coll}'")
        if "ts" not in record:
            record["ts"] = datetime.now(timezone.utc).isoformat()
        data = self._read(coll)
        data.append(record)
        self._write(coll, data)
        return record

    def add_learning(self, learning: Learning) -> Dict[str, Any]:
        return self.add("learnings", learning.to_dict())

    def get(self, coll: str, limit: int = 100) -> List[Dict[str, Any]]:
        data = self._read(coll)
        return list(reversed(data[-limit:]))   # newest first

    def find(self, coll: str, predicate) -> List[Dict[str, Any]]:
        return [r for r in self._read(coll) if predicate(r)]

    def query_learnings(self, channel: Optional[str] = None,
                        kind: Optional[str] = None,
                        limit: int = 50) -> List[Dict[str, Any]]:
        out = []
        for r in reversed(self._read("learnings")):
            if channel and r.get("channel") != channel:
                continue
            if kind and r.get("kind") != kind:
                continue
            out.append(r)
            if len(out) >= limit:
                break
        return out

    def prior_failures(self, channel: str, limit: int = 20) -> List[str]:
        """Short summaries of past losses in a channel — for 'don't repeat me'."""
        return [r.get("summary", "")
                for r in self.query_learnings(channel=channel, kind="loss", limit=limit)]

    def channel_scorecard(self, channel: str) -> Dict[str, Any]:
        """Wins/losses & net impact for a channel — real numbers from memory."""
        recs = self.query_learnings(channel=channel, limit=1000)
        wins = [r for r in recs if r.get("kind") == "win"]
        losses = [r for r in recs if r.get("kind") == "loss"]
        return {
            "channel": channel,
            "wins": len(wins), "losses": len(losses),
            "net_usd": round(sum(r.get("usd_impact", 0) for r in recs), 2),
            "win_rate": round(len(wins) / len(recs), 3) if recs else None,
        }

    def stats(self) -> Dict[str, int]:
        return {c: len(self._read(c)) for c in self.COLLECTIONS}
