#!/usr/bin/env python3
"""
YABBAI Local -- Learning Layer (Honest ML)

What this IS: a layer that learns YOUR patterns over time and uses them to make
YABBAI a better assistant -- smarter routing, better defaults, recognising the kinds
of tasks you do. Real machine learning applied to a real signal (your own usage).

What this is NOT: anything that "learns to earn money." No model learns to beat
markets from your usage data. That isn't real, so it isn't here. This learns to
serve YOU better, which is genuine and useful.

How it learns (transparent, on-device, no black box):
  - Records every routing decision + outcome (did the local model handle it, or did
    you escalate? did you accept the escalation suggestion or decline it?).
  - Builds simple frequency statistics: which keywords/task-types you tend to
    escalate, which model you pick for which kind of work, your busiest task areas.
  - Feeds those stats back as ADJUSTED routing hints -- e.g. if you always escalate
    "regex" questions to Claude, it learns to suggest that sooner; if you always
    KEEP "explain this" local, it stops suggesting escalation for those.

All data is a local JSON file. Nothing leaves the machine. You can read it, edit it,
or delete it. It's your assistant learning your habits, fully inspectable.
"""

import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional


class LearningLayer:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data = {
            "events": [],                      # raw log of decisions + outcomes
            "escalate_keywords": {},           # keyword -> count of escalations
            "keep_local_keywords": {},         # keyword -> count of kept-local
            "model_preferences": {},           # task_type -> {model: count}
            "task_areas": {},                  # area -> count
        }
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
            except Exception:
                pass

    def _save(self):
        try:
            self.path.write_text(json.dumps(self.data, indent=2))
        except Exception:
            pass

    # ── Recording outcomes (the training signal) ──────────────────────────────
    def record(self, message: str, chosen_model: str, layer: str,
               escalated: bool, user_accepted: Optional[bool] = None):
        """
        Called after every routed turn.
        - escalated: did this go to a cloud specialist?
        - user_accepted: if an escalation was SUGGESTED, did the user accept it?
                         (True/False), or None if not applicable.
        """
        keywords = self._keywords(message)
        event = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "model": chosen_model, "layer": layer, "escalated": escalated,
            "user_accepted": user_accepted, "keywords": keywords,
        }
        self.data["events"].append(event)
        self.data["events"] = self.data["events"][-1000:]   # cap

        # Update keyword stats based on the REAL outcome
        target = self.data["escalate_keywords"] if escalated else self.data["keep_local_keywords"]
        for kw in keywords:
            target[kw] = target.get(kw, 0) + 1

        # Model preference by rough task area
        area = self._task_area(message)
        self.data["task_areas"][area] = self.data["task_areas"].get(area, 0) + 1
        prefs = self.data["model_preferences"].setdefault(area, {})
        prefs[chosen_model] = prefs.get(chosen_model, 0) + 1

        self._save()

    # ── Using what was learned (the payoff) ────────────────────────────────────
    def routing_hint(self, message: str) -> Dict:
        """
        Returns a learned adjustment for the router:
          - 'lean': 'escalate' | 'local' | 'neutral'
          - 'confidence': 0..1 based on how much evidence we have
          - 'reason': human-readable explanation
        The router treats this as ONE input, not an override -- it nudges, never forces.
        """
        keywords = self._keywords(message)
        esc_score = sum(self.data["escalate_keywords"].get(k, 0) for k in keywords)
        loc_score = sum(self.data["keep_local_keywords"].get(k, 0) for k in keywords)
        total = esc_score + loc_score

        if total < 3:
            return {"lean": "neutral", "confidence": 0.0,
                    "reason": "Not enough history yet to learn from."}

        if esc_score > loc_score * 1.5:
            conf = min(1.0, esc_score / (total + 5))
            top = [k for k in keywords if self.data["escalate_keywords"].get(k, 0) > 0]
            return {"lean": "escalate", "confidence": round(conf, 2),
                    "reason": f"You usually escalate tasks like this ({', '.join(top[:2])})."}
        if loc_score > esc_score * 1.5:
            conf = min(1.0, loc_score / (total + 5))
            return {"lean": "local", "confidence": round(conf, 2),
                    "reason": "You usually keep tasks like this local."}
        return {"lean": "neutral", "confidence": 0.0, "reason": "Mixed history; no clear lean."}

    def preferred_model(self, message: str) -> Optional[str]:
        """If you have a clear favourite model for this task area, suggest it."""
        area = self._task_area(message)
        prefs = self.data["model_preferences"].get(area, {})
        if not prefs:
            return None
        top_model, count = max(prefs.items(), key=lambda kv: kv[1])
        total = sum(prefs.values())
        if total >= 4 and count / total >= 0.6:
            return top_model
        return None

    # ── Insight report (so you can SEE what it learned) ───────────────────────
    def insights(self) -> Dict:
        esc = Counter(self.data["escalate_keywords"]).most_common(8)
        loc = Counter(self.data["keep_local_keywords"]).most_common(8)
        areas = Counter(self.data["task_areas"]).most_common(8)
        accept_events = [e for e in self.data["events"] if e.get("user_accepted") is not None]
        accept_rate = (sum(1 for e in accept_events if e["user_accepted"]) / len(accept_events) * 100
                       if accept_events else None)
        return {
            "total_events": len(self.data["events"]),
            "top_escalate_keywords": esc,
            "top_keep_local_keywords": loc,
            "busiest_task_areas": areas,
            "escalation_accept_rate_pct": round(accept_rate, 1) if accept_rate is not None else None,
            "note": "This is everything YABBAI has learned about your habits. It's a local "
                    "file you can inspect or delete anytime.",
        }

    def reset(self):
        self.data = {"events": [], "escalate_keywords": {}, "keep_local_keywords": {},
                     "model_preferences": {}, "task_areas": {}}
        self._save()

    # ── Helpers ────────────────────────────────────────────────────────────────
    @staticmethod
    def _keywords(text: str) -> List[str]:
        words = re.findall(r"[a-z]{4,}", text.lower())
        stop = {"this", "that", "with", "from", "have", "your", "what", "when", "please",
                "would", "could", "should", "about", "make", "want", "need", "help", "into"}
        return [w for w in words if w not in stop][:10]

    @staticmethod
    def _task_area(text: str) -> str:
        low = text.lower()
        if any(k in low for k in ["code", "function", "bug", "refactor", "script", "file", "python", "js"]):
            return "coding"
        if any(k in low for k in ["price", "token", "wallet", "trade", "defi", "solana", "yield"]):
            return "defi"
        if any(k in low for k in ["write", "draft", "essay", "email", "story", "blog"]):
            return "writing"
        if any(k in low for k in ["explain", "what is", "how does", "why"]):
            return "learning"
        return "general"
