#!/usr/bin/env python3
"""
YABBAI Ops — Decision Engine + Approval Gates

Two things:
  1. EV tickets — every candidate action gets a one-line expected-value computation
     so you decide with numbers, not vibes. EV = P_success × upside − (cost + risk).
  2. Approval gates — every action is classified by reversibility + cost. LOW acts
     autonomously; MEDIUM needs peer-agent review; HIGH (irreversible OR over the spend
     cap OR any outbound send/purchase/publish/payout/contract) is QUEUED for human
     approval and never auto-executed.

This is what makes "autonomous" mean "within bounds," not "without rails." One bad
loop can't drain an account because the account-touching action physically cannot fire
without you.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, List, Dict


class RiskClass(str, Enum):
    LOW = "low"        # reversible, under micro-cap → agent acts autonomously
    MEDIUM = "medium"  # reversible, micro..spend_cap → peer-agent review
    HIGH = "high"      # irreversible OR ≥ spend_cap OR money/send/publish → HUMAN APPROVAL


# Action keywords that ALWAYS force HIGH regardless of cost — the irreducibly
# consequential / irreversible actions.
HIGH_RISK_ACTIONS = {
    "send", "email", "dm", "message", "post", "publish", "tweet",
    "purchase", "buy", "pay", "payout", "transfer", "withdraw", "charge",
    "contract", "sign", "commit", "delete", "deploy", "refund",
}


@dataclass
class EVTicket:
    action: str
    p_success: float          # 0..1, your honest estimate
    upside: float             # $ if it works
    cost: float               # $ to attempt
    risk_adjustment: float = 0.0
    speed_to_cash_days: Optional[int] = None

    @property
    def ev(self) -> float:
        return round(self.p_success * self.upside - (self.cost + self.risk_adjustment), 2)

    def verdict(self) -> str:
        if self.ev > 0:
            return "positive EV — worth doing if inside gates"
        if self.ev == 0:
            return "neutral EV — marginal, deprioritize"
        return "negative EV — skip"


@dataclass
class QueuedAction:
    id: str
    action: str
    risk_class: RiskClass
    ev_ticket: Optional[Dict]
    reason: str
    cost: float
    status: str = "pending"   # pending | approved | rejected | executed
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class DecisionEngine:
    def __init__(self, micro_spend_cap: float = 5.0, spend_cap: float = 50.0,
                 daily_cap: float = 100.0):
        self.micro_spend_cap = micro_spend_cap   # below this, LOW reversible acts auto
        self.spend_cap = spend_cap               # at/above this → HIGH (human)
        self.daily_cap = daily_cap
        self.spent_today = 0.0
        self.spend_day = datetime.now(timezone.utc).date().isoformat()
        self.queue: Dict[str, QueuedAction] = {}
        self._counter = 0

    def classify(self, action: str, cost: float, reversible: bool) -> RiskClass:
        """The gate logic. Money/send/publish or irreversible or big spend → HIGH."""
        import re
        words = set(re.findall(r"[a-z]+", action.lower()))
        # whole-word match so "post" matches "post a tweet" but not "blog post about X"
        # (we still catch publish/send/etc as their own words)
        if words & HIGH_RISK_ACTIONS:
            # extra guard: "post" as a noun ("blog post", "job post") shouldn't trigger;
            # only treat "post"/"message" as risky if it's clearly the verb (sending out)
            risky = words & HIGH_RISK_ACTIONS
            noun_safe = {"post", "message"}
            # if the ONLY risky word is a noun-ambiguous one AND the action looks like
            # creation ("draft", "write", "create"), don't escalate on that alone
            if risky <= noun_safe and (words & {"draft", "write", "create", "make", "generate"}):
                pass  # creation of a post/message draft is reversible content work
            else:
                return RiskClass.HIGH
        if not reversible:
            return RiskClass.HIGH
        if cost >= self.spend_cap:
            return RiskClass.HIGH
        if cost > self.micro_spend_cap:
            return RiskClass.MEDIUM
        return RiskClass.LOW

    def propose(self, action: str, cost: float, reversible: bool,
                ev_ticket: Optional[EVTicket] = None, reason: str = "") -> Dict:
        """Run an action through the gates. Returns what happens to it."""
        self._roll_day()
        rc = self.classify(action, cost, reversible)

        # Daily cap check — even LOW actions can't blow the daily budget
        if rc == RiskClass.LOW and (self.spent_today + cost) > self.daily_cap:
            rc = RiskClass.HIGH   # would exceed daily cap → escalate to human
            reason = (reason + " [escalated: would exceed daily spend cap]").strip()

        if rc == RiskClass.LOW:
            self.spent_today += cost
            return {"decision": "auto_execute", "risk_class": rc.value,
                    "ev": ev_ticket.ev if ev_ticket else None,
                    "note": "Reversible, under micro-cap — safe to act autonomously."}

        if rc == RiskClass.MEDIUM:
            return {"decision": "peer_review", "risk_class": rc.value,
                    "ev": ev_ticket.ev if ev_ticket else None,
                    "note": "Reversible but above micro-cap — needs a second-agent check."}

        # HIGH → queue for human, never execute
        self._counter += 1
        qid = f"q{self._counter}"
        self.queue[qid] = QueuedAction(
            id=qid, action=action, risk_class=rc,
            ev_ticket=(ev_ticket.__dict__ if ev_ticket else None),
            reason=reason or "Irreversible / money-touching / over cap.", cost=cost)
        return {"decision": "queued_for_human", "risk_class": rc.value, "id": qid,
                "ev": ev_ticket.ev if ev_ticket else None,
                "note": "This touches money, sends/publishes something, is irreversible, "
                        "or exceeds the cap. Queued — you approve before it happens."}

    def pending(self) -> List[Dict]:
        return [q.__dict__ for q in self.queue.values() if q.status == "pending"]

    def approve(self, qid: str) -> Dict:
        q = self.queue.get(qid)
        if not q or q.status != "pending":
            return {"ok": False, "error": "not found or already handled"}
        q.status = "approved"
        # NOTE: approval marks it ready; the ACTUAL execution (sending the email,
        # making the purchase) is still done by YOU or an explicitly-authorized
        # integration — the framework never silently performs money actions itself.
        return {"ok": True, "status": "approved",
                "note": "Approved. Execute it yourself or via an authorized integration. "
                        "The framework does not auto-perform money/send actions."}

    def reject(self, qid: str) -> Dict:
        q = self.queue.get(qid)
        if q:
            q.status = "rejected"
        return {"ok": q is not None}

    def _roll_day(self):
        today = datetime.now(timezone.utc).date().isoformat()
        if today != self.spend_day:
            self.spend_day = today
            self.spent_today = 0.0
