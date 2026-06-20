"""
YABBAI Revenue System — Orchestrator

The OBSERVE → ORIENT → DECIDE → ACT → MEASURE → LEARN loop, run continuously.
This is the CEO agent: it owns cadence, pulls the real picture, ranks candidate
actions by expected value, routes them through the gates, measures outcomes,
and writes learnings to shared memory so every channel improves.

Honest about scope: this orchestrator decides cadence and ordering and enforces
the rails. The actual revenue-producing work lives in the channels. It does NOT
predict the future or guarantee profit; it ranks by EXPECTED VALUE and lets the
gates + human approvals handle the irreversible parts.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from .truth import TruthLedger, Metric, SourceType, DataGap
from .gates import GateController, ProposedAction
from .memory import Memory, Learning


@dataclass
class EVTicket:
    """One-line expected-value record. Every DECIDE produces these."""
    action: ProposedAction
    p_success: float
    upside_usd: float
    cost_usd: float
    risk_adjustment: float = 0.0

    @property
    def ev(self) -> float:
        return round(self.p_success * self.upside_usd - self.cost_usd - self.risk_adjustment, 2)

    def to_dict(self) -> Dict[str, Any]:
        d = {"ev": self.ev, "p_success": self.p_success, "upside_usd": self.upside_usd,
             "cost_usd": self.cost_usd, "risk_adjustment": self.risk_adjustment}
        self.action.ev = d
        return d


class Orchestrator:
    def __init__(self, ledger: TruthLedger, gates: GateController, memory: Memory):
        self.ledger = ledger
        self.gates = gates
        self.memory = memory
        # channels register factories that propose actions each cycle:
        #   () -> List[ProposedAction]   (observation + signals turned into actions)
        self._proposers: Dict[str, Callable[[], List[ProposedAction]]] = {}
        # channels register an observe() that returns real Metrics
        self._observers: Dict[str, Callable[[], List[Metric]]] = {}
        self.cycles_run = 0
        self.last_cycle: Optional[Dict[str, Any]] = None

    # ── registration ───────────────────────────────────────────────────────
    def register_channel(self, channel: str,
                         observer: Callable[[], List[Metric]],
                         proposer: Callable[[], List[ProposedAction]]) -> None:
        self._observers[channel] = observer
        self._proposers[channel] = proposer

    # ── the loop ───────────────────────────────────────────────────────────
    def cycle(self) -> Dict[str, Any]:
        """One full OBSERVE→ORIENT→DECIDE→ACT→MEASURE→LEARN pass."""
        self.cycles_run += 1
        started = datetime.now(timezone.utc).isoformat()

        # OBSERVE — pull real signals from every channel. Missing = DATA GAP.
        observed: List[Metric] = []
        observe_errors: Dict[str, str] = {}
        for ch, fn in self._observers.items():
            try:
                observed.extend(fn() or [])
            except DataGap as e:
                observe_errors[ch] = f"DATA GAP: {e}"
            except Exception as e:
                observe_errors[ch] = f"observe error: {e}"
        for m in observed:
            self.ledger.record(m)

        # ORIENT — score vs KPIs; surface gaps honestly.
        orientation = self.ledger.summary()
        orientation["observe_errors"] = observe_errors

        # DECIDE — collect candidate actions and rank by EV. No inventing data:
        # if a channel can't observe, it contributes no action this cycle.
        tickets: List[EVTicket] = []
        for ch, fn in self._proposers.items():
            try:
                for a in (fn() or []):
                    tickets.append(self._score(a))
            except Exception as e:
                self.memory.add_learning(Learning(
                    kind="signal", channel=ch, agent="orchestrator",
                    summary=f"proposer error: {e}", usd_impact=0.0))
        tickets.sort(key=lambda t: t.ev, reverse=True)

        # ACT — route each through the gates. LOW may execute; HIGH queues for human.
        routed = [self.gates.propose(t.action) for t in tickets]

        # MEASURE — what's the real picture after this cycle's autonomous actions?
        measured = {"real_net": self.ledger.net_profit(),
                    "projected": self.ledger.projected_net()}

        # LEARN — record decisions + outcomes to shared memory.
        for t in tickets:
            self.memory.add_learning(Learning(
                kind="signal", channel=t.action.channel, agent=t.action.agent,
                summary=t.action.reason or t.action.kind,
                detail={"ev": t.to_dict()}, usd_impact=0.0))

        self.last_cycle = {
            "cycle": self.cycles_run, "started": started,
            "observed_metrics": len(observed),
            "candidates": len(tickets), "routed": len(routed),
            "orientation": orientation, "measured": measured,
            "gates_status": self.gates.status(),
            "top_candidates": [t.to_dict() for t in tickets[:5]],
        }
        return self.last_cycle

    def _score(self, a: ProposedAction) -> EVTicket:
        """
        Default EV scoring. Channels may attach a richer .ev to the action; if
        absent we use a conservative default (no inflated upside). The point is
        ranking, not precision — p_success is never treated as knowledge.
        """
        if a.ev:  # channel supplied its own estimate
            return EVTicket(action=a,
                            p_success=float(a.ev.get("p_success", 0.3)),
                            upside_usd=float(a.ev.get("upside_usd", 0.0)),
                            cost_usd=float(a.ev.get("cost_usd", a.usd_amount)),
                            risk_adjustment=float(a.ev.get("risk_adjustment", 0.0)))
        # Conservative default prior. Honest about not knowing the future.
        return EVTicket(action=a, p_success=0.3,
                        upside_usd=max(0.0, a.usd_amount * 2),
                        cost_usd=a.usd_amount)

    # ── human-facing ops ───────────────────────────────────────────────────
    def pending_approvals(self) -> List[Dict[str, Any]]:
        return self.gates.pending_approvals()

    def approve(self, qid: str, approver: str,
                signature: Optional[str] = None) -> Dict[str, Any]:
        return self.gates.human_approve(qid, approver, signature)

    def reject(self, qid: str, by: str, reason: str = "") -> Dict[str, Any]:
        return self.gates.human_reject(qid, by, reason)

    def status(self) -> Dict[str, Any]:
        return {
            "cycles_run": self.cycles_run,
            "truth": self.ledger.summary(),
            "gates": self.gates.status(),
            "memory": self.memory.stats(),
            "pending_approvals": len(self.pending_approvals()),
        }
