#!/usr/bin/env python3
"""
YABBAI Local -- Hybrid Router (the star-network orchestrator)

Decides, for each turn, which model handles it. The whole point is to keep credits
MINIMAL: the local 8B handles everything it can; cloud specialists are called only
when a real signal says the task needs them.

THREE ESCALATION LAYERS (all active, in order):

  1. MANUAL -- the user explicitly asks. A message starting with `@claude`,
     `@gpt`, `@kimi`, `@opus` (or an "escalate" flag from the UI) routes straight
     to that specialist. Always wins. Zero wasted credits -- nothing escalates
     unless asked.

  2. HEURISTIC -- cheap, predictable signals that suggest the 8B will struggle:
     very long prompt, many files referenced, hard-reasoning keywords, big code
     blocks. If a signal fires, propose escalation (configurable: auto or ask).

  3. 8B-AS-JUDGE -- for anything not already decided, the local model first answers
     a tiny yes/no: "can you handle this well, or should this go to a stronger
     model?" Cheap (one short local call, free) and catches what heuristics miss.
     The 8B isn't always right about itself, so its vote is a SUGGESTION, gated by
     the escalation policy below -- it never silently spends credits on its own.

ESCALATION POLICY (user-controlled):
  - 'off'    : never auto-escalate. Only manual @tags reach cloud. (most frugal)
  - 'ask'    : when a layer suggests escalation, ASK the user first. (default)
  - 'auto'   : escalate automatically when layers 2/3 fire. (most hands-off)

Default model for escalation is configurable; default specialist = Claude Sonnet.
"""

import re
from dataclasses import dataclass
from typing import Dict, List, Optional

from .model_pool import ModelPool


HARD_KEYWORDS = [
    "refactor", "architect", "debug this", "why doesn't", "optimize", "prove",
    "design a system", "trace through", "step by step", "edge case", "race condition",
    "concurrency", "algorithm", "complexity", "security review", "audit",
]

TAG_TO_MODEL = {
    "@claude": "claude", "@opus": "claude-opus", "@gpt": "gpt", "@kimi": "kimi",
}


@dataclass
class RouteDecision:
    model_id: str
    layer: str               # 'manual' | 'heuristic' | 'judge' | 'default'
    private: bool
    reason: str
    needs_confirmation: bool = False
    cost_hint: str = "free"


class HybridRouter:
    def __init__(self, pool: ModelPool,
                 workhorse_id: str = "local-8b",
                 default_specialist: str = "claude",
                 policy: str = "ask",
                 learning=None):
        self.pool = pool
        self.workhorse_id = workhorse_id
        self.default_specialist = default_specialist
        self.policy = policy          # 'off' | 'ask' | 'auto'
        self.learning = learning      # optional LearningLayer
        self.stats = {"local_turns": 0, "escalated_turns": 0, "credits_calls": 0}

    def set_policy(self, policy: str):
        if policy in ("off", "ask", "auto"):
            self.policy = policy

    # ── Layer 1: manual tag ──────────────────────────────────────────────────
    def _check_manual(self, text: str) -> Optional[RouteDecision]:
        lowered = text.strip().lower()
        for tag, model_id in TAG_TO_MODEL.items():
            if lowered.startswith(tag):
                m = self.pool.get(model_id)
                if m:
                    return RouteDecision(model_id, "manual", m["private"],
                                         f"You routed this to {m['label']} with {tag}.",
                                         needs_confirmation=False, cost_hint=m["cost_hint"])
        return None

    # ── Layer 2: heuristic ───────────────────────────────────────────────────
    def _check_heuristic(self, text: str) -> Optional[str]:
        signals = []
        if len(text) > 1200:
            signals.append("long/complex prompt")
        if text.count("```") >= 2 or text.count("\n") > 40:
            signals.append("large code block")
        if len(re.findall(r"\.(py|js|ts|tsx|jsx|go|rs|java|c|cpp)\b", text)) >= 3:
            signals.append("multiple files referenced")
        low = text.lower()
        hits = [k for k in HARD_KEYWORDS if k in low]
        if hits:
            signals.append(f"hard-task keyword ({hits[0]})")
        return "; ".join(signals) if signals else None

    # ── Layer 3: 8B self-judge ───────────────────────────────────────────────
    def _check_judge(self, text: str) -> Optional[str]:
        """Ask the local model (free) whether it should escalate. Returns reason or None."""
        judge_system = (
            "You are a routing judge. The user message will be handled by a local 8B model. "
            "Answer ONLY 'LOCAL' if an 8B model can handle it well, or 'ESCALATE' if it needs "
            "a much stronger model (hard reasoning, big refactor, subtle debugging). "
            "Reply with exactly one word.")
        r = self.pool.call(self.workhorse_id, judge_system,
                           [{"role": "user", "content": text[:1500]}], temperature=0.0)
        if r.ok and "ESCALATE" in r.content.upper():
            return "local model judged this beyond its ability"
        return None

    # ── Main decision ─────────────────────────────────────────────────────────
    def decide(self, text: str, run_judge: bool = True) -> RouteDecision:
        # Layer 1 -- manual always wins
        manual = self._check_manual(text)
        if manual:
            return manual

        # If policy is 'off', never auto-escalate
        if self.policy == "off":
            return RouteDecision(self.workhorse_id, "default", True,
                                 "Local model (auto-escalation off).", cost_hint="free")

        # Layer 2 -- heuristic
        heur = self._check_heuristic(text)

        # Layer 3 -- judge (only if heuristic didn't already flag, to save a call)
        judge = None
        if not heur and run_judge:
            judge = self._check_judge(text)

        signal = heur or judge
        if signal:
            spec = self.pool.get(self.default_specialist)
            if not spec:
                return RouteDecision(self.workhorse_id, "default", True,
                                     "No specialist available; staying local.", cost_hint="free")
            layer = "heuristic" if heur else "judge"
            needs_confirm = (self.policy == "ask")
            return RouteDecision(self.default_specialist, layer, spec["private"],
                                 f"Suggesting {spec['label']} -- {signal}.",
                                 needs_confirmation=needs_confirm, cost_hint=spec["cost_hint"])

        # Layer 3.5 -- learned pattern (nudge, never forces). Only if we have a layer.
        if self.learning:
            hint = self.learning.routing_hint(text)
            if hint["lean"] == "escalate" and hint["confidence"] >= 0.5:
                spec = self.pool.get(self.default_specialist)
                if spec:
                    return RouteDecision(self.default_specialist, "learned", spec["private"],
                                         f"Suggesting {spec['label']} -- {hint['reason']}",
                                         needs_confirmation=(self.policy == "ask"),
                                         cost_hint=spec["cost_hint"])

        # Default -- local workhorse
        return RouteDecision(self.workhorse_id, "default", True,
                             "Local model handles this.", cost_hint="free")

    def record(self, decision: RouteDecision, executed_model_id: str):
        self.record_simple(executed_model_id)

    def record_simple(self, executed_model_id: str):
        m = self.pool.get(executed_model_id)
        if m and not m["private"]:
            self.stats["escalated_turns"] += 1
            self.stats["credits_calls"] += 1
        else:
            self.stats["local_turns"] += 1

    def credit_report(self) -> Dict:
        total = self.stats["local_turns"] + self.stats["escalated_turns"]
        local_pct = (self.stats["local_turns"] / total * 100) if total else 100
        return {**self.stats, "total_turns": total,
                "local_pct": round(local_pct, 1),
                "policy": self.policy,
                "note": f"{local_pct:.0f}% of turns stayed local (free + private)."}
