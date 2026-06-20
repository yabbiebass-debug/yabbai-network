"""
YABBAI Revenue System — Gates Layer

"Autonomous" means within bounds, not without rails. This module is those rails.

Every proposed action is classified by reversibility + cost into a risk class,
and the class decides what must happen before it can execute:

  LOW    — reversible, under the micro-spend cap. Agent runs autonomously.
  MEDIUM — reversible but bigger. Needs a second agent's review (peer check).
  HIGH   — irreversible, OR over the spend cap, OR any outbound/payout/publish/
           contract/real-trade. QUEUED for human approval. Never auto-executed.

Plus hard capital caps and a kill-switch that nothing can bypass. This is the
direct code implementation of the v15 CAPITAL & EXECUTION GATES section.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


class RiskClass(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class GateOutcome(str, Enum):
    EXECUTED = "executed"             # ran (LOW only)
    PEER_REVIEW = "peer_review"       # needs a second agent (MEDIUM)
    QUEUED_FOR_HUMAN = "queued"       # needs a human (HIGH)
    BLOCKED_CAP = "blocked_cap"       # over a hard cap
    HALTED = "halted"                 # kill-switch or daily-loss trip


# Action kinds that are HIGH regardless of cost — they touch the outside world
# irreversibly. Cost is irrelevant; a $0 real-money trade is still HIGH.
ALWAYS_HIGH_KINDS = {
    "payout",                # send real money out
    "publish",               # publish a product/post/listing publicly
    "outbound_email",        # send cold/outbound message
    "outbound_dm",
    "contract",              # sign / commit to a contract
    "real_trade",            # broadcast a real on-chain trade
    "purchase",              # spend real money on software/ads/outsourcing
    "refund",
    "listing_live",          # go live on a storefront
}


@dataclass
class CapitalCaps:
    """Hard caps set by the human. CFO may only TIGHTEN these without approval."""
    micro_spend_cap: float = 2.0       # below this LOW actions run autonomously
    spend_cap: float = 25.0            # MEDIUM ceiling; above = HIGH
    daily_spend_cap: float = 100.0     # total spend across all actions/day
    daily_loss_cap: float = 15.0       # halt all if cumulative daily loss exceeds
    per_channel_daily: Dict[str, float] = field(default_factory=dict)

    def validate(self) -> List[str]:
        problems = []
        if self.micro_spend_cap <= 0:
            problems.append("micro_spend_cap must be positive")
        if self.spend_cap < self.micro_spend_cap:
            problems.append("spend_cap must be >= micro_spend_cap")
        if self.daily_spend_cap <= 0:
            problems.append("daily_spend_cap must be positive")
        return problems


@dataclass
class ProposedAction:
    """An action an agent wants to take. Classified before it can run."""
    kind: str                       # see ALWAYS_HIGH_KINDS + any custom kind
    channel: str                    # "products" | "agency" | "trading" | ...
    agent: str                      # who proposed it
    usd_amount: float = 0.0         # cost / capital involved
    reversible: bool = True
    payload: Dict[str, Any] = field(default_factory=dict)  # channel-specific detail
    reason: str = ""                # WHY (recorded in memory regardless of outcome)
    ev: Optional[Dict[str, Any]] = None   # expected-value ticket, if computed
    proposed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_public(self) -> Dict[str, Any]:
        """Public-safe view for the approval queue (drops secrets in payload)."""
        safe = {k: v for k, v in self.payload.items()
                if not any(s in k.lower() for s in ("key", "secret", "token", "password", "seed"))}
        return {
            "kind": self.kind, "channel": self.channel, "agent": self.agent,
            "usd_amount": round(self.usd_amount, 2), "reversible": self.reversible,
            "payload": safe, "reason": self.reason, "ev": self.ev,
            "proposed_at": self.proposed_at,
        }


class GateController:
    """
    The single chokepoint. Every channel routes its actions through here.

    Lifecycle of an action:
      propose(action) -> classify -> outcome
        LOW    -> executes immediately via the registered executor (still gated by caps)
        MEDIUM -> needs peer_review(agent, action) called by a second agent, then executes
        HIGH   -> enqueued; execute_only after human_approve(id) is called
      Nothing bypasses the kill-switch or caps. Not even the CEO agent.
    """

    def __init__(self, caps: CapitalCaps):
        problems = caps.validate()
        if problems:
            raise ValueError(f"Invalid capital caps: {problems}")
        self.caps = caps
        self.kill_switch: bool = False
        self._daily_spend: float = 0.0
        self._daily_loss: float = 0.0
        self._day = datetime.now(timezone.utc).date()
        # executors registered per (channel, kind): (action) -> result dict
        self._executors: Dict[str, Callable] = {}
        # approval queue: id -> {action, status, result}
        self._queue: Dict[str, Dict[str, Any]] = {}
        self._peer_reviews: Dict[str, List[str]] = {}
        self._counter = 0
        self._audit: List[Dict[str, Any]] = []

    # ── registration ───────────────────────────────────────────────────────
    def register_executor(self, channel: str, kind: str, fn: Callable) -> None:
        self._executors[f"{channel}:{kind}"] = fn

    # ── caps & kill-switch (CFO can tighten; only human can loosen/raise) ──
    def engage_kill_switch(self) -> None:
        self.kill_switch = True

    def disengage_kill_switch(self) -> None:
        # Loosening safety always requires the human path; callers must assert auth.
        self.kill_switch = False

    def tighten_caps(self, **overrides) -> None:
        """CFO path: only LOWER a cap. Any attempt to raise is rejected."""
        for k, v in overrides.items():
            cur = getattr(self.caps, k)
            if isinstance(cur, dict):
                continue  # dict caps handled separately
            if v > cur:
                raise ValueError(f"CFO may not RAISE {k} ({cur} -> {v}). Needs human.")
            setattr(self.caps, k, v)

    def _roll_day(self):
        today = datetime.now(timezone.utc).date()
        if today != self._day:
            self._day = today
            self._daily_spend = 0.0
            self._daily_loss = 0.0

    # ── classification ─────────────────────────────────────────────────────
    def classify(self, a: ProposedAction) -> RiskClass:
        if a.kind in ALWAYS_HIGH_KINDS or not a.reversible:
            return RiskClass.HIGH
        if a.usd_amount >= self.caps.spend_cap:
            return RiskClass.HIGH
        if a.usd_amount >= self.caps.micro_spend_cap:
            return RiskClass.MEDIUM
        return RiskClass.LOW

    # ── the main entrypoint ────────────────────────────────────────────────
    def propose(self, a: ProposedAction) -> Dict[str, Any]:
        self._roll_day()
        rc = self.classify(a)

        # 1. kill-switch is absolute
        if self.kill_switch:
            return self._record(a, rc, GateOutcome.HALTED, ["Kill-switch engaged."])

        # 2. hard caps (checked before any execution path)
        cap_reasons = self._check_caps(a)
        if cap_reasons:
            return self._record(a, rc, GateOutcome.BLOCKED_CAP, cap_reasons)

        # 3. route by class
        if rc == RiskClass.LOW:
            return self._execute(a, rc)
        if rc == RiskClass.MEDIUM:
            self._counter += 1
            qid = f"med_{self._counter}"
            self._queue[qid] = {"action": a, "status": "peer_review",
                                "rc": rc.value, "reviews": [], "result": None}
            return self._record(a, rc, GateOutcome.PEER_REVIEW,
                                [f"Needs peer review. queue_id={qid}"], qid=qid)
        # HIGH
        self._counter += 1
        qid = f"hum_{self._counter}"
        self._queue[qid] = {"action": a, "status": "awaiting_human",
                            "rc": rc.value, "reviews": [], "result": None}
        return self._record(a, rc, GateOutcome.QUEUED_FOR_HUMAN,
                            [f"Requires human approval. queue_id={qid}"], qid=qid)

    def _check_caps(self, a: ProposedAction) -> List[str]:
        reasons = []
        # Count committed spend = already-executed + still-pending in the queue.
        # This prevents stacking many queued actions that together blow the daily cap.
        pending_spend = sum(
            q["action"].usd_amount for q in self._queue.values()
            if q.get("status") in ("peer_review", "awaiting_human"))
        committed = self._daily_spend + pending_spend
        if a.usd_amount + committed > self.caps.daily_spend_cap:
            reasons.append(
                f"Would exceed daily spend cap "
                f"(committed ${committed:.2f}+${a.usd_amount:.2f} > "
                f"${self.caps.daily_spend_cap:.2f}).")
        chan_cap = self.caps.per_channel_daily.get(a.channel)
        if chan_cap is not None:
            chan_spend = sum(x["usd_amount"] for x in self._spend_log_for(a.channel))
            if a.usd_amount + chan_spend > chan_cap:
                reasons.append(f"Would exceed per-channel daily cap for {a.channel}.")
        return reasons

    def _spend_log_for(self, channel: str) -> List[Dict[str, Any]]:
        return [e for e in self._audit
                if e.get("channel") == channel and e.get("outcome") == GateOutcome.EXECUTED.value]

    # ── MEDIUM: peer review ────────────────────────────────────────────────
    def peer_review(self, qid: str, reviewer_agent: str, approve: bool,
                    note: str = "") -> Dict[str, Any]:
        item = self._queue.get(qid)
        if not item or item["status"] != "peer_review":
            return {"ok": False, "error": "not a pending peer-review item"}
        item["reviews"].append({"agent": reviewer_agent, "approve": approve, "note": note})
        if not approve:
            item["status"] = "rejected"
            return {"ok": True, "status": "rejected", "note": "Peer rejected."}
        # one approving peer is enough for MEDIUM
        return self._execute(item["action"], RiskClass(item["rc"]), qid=qid)

    # ── HIGH: human approval ───────────────────────────────────────────────
    def human_approve(self, qid: str, approver: str,
                      signature: Optional[str] = None) -> Dict[str, Any]:
        item = self._queue.get(qid)
        if not item or item["status"] != "awaiting_human":
            return {"ok": False, "error": "not a pending human-approval item"}
        a = item["action"]
        # real_trade additionally requires a signature (wallet or explicit)
        if a.kind == "real_trade" and not signature:
            return {"ok": False, "error": "real_trade requires a signature."}
        # re-check caps at approval time (market/cash may have moved)
        self._roll_day()
        cap_reasons = self._check_caps(a)
        if cap_reasons:
            item["status"] = "blocked_on_recheck"
            return {"ok": False, "status": "blocked_on_recheck", "reasons": cap_reasons}
        return self._execute(a, RiskClass(item["rc"]), qid=qid,
                             approver=approver, signature=signature)

    def human_reject(self, qid: str, by: str, reason: str = "") -> Dict[str, Any]:
        item = self._queue.get(qid)
        if not item:
            return {"ok": False, "error": "not found"}
        item["status"] = "rejected"
        self._record(item["action"], RiskClass(item["rc"]), GateOutcome.HALTED,
                     [f"Rejected by {by}: {reason}"], qid=qid)
        return {"ok": True, "status": "rejected"}

    # ── execution ──────────────────────────────────────────────────────────
    def _execute(self, a: ProposedAction, rc: RiskClass, qid: Optional[str] = None,
                 approver: Optional[str] = None,
                 signature: Optional[str] = None) -> Dict[str, Any]:
        fn = self._executors.get(f"{a.channel}:{a.kind}")
        if fn is None:
            return self._record(a, rc, GateOutcome.HALTED,
                                [f"No executor registered for {a.channel}:{a.kind}"], qid=qid)
        try:
            result = fn(a, signature=signature) if a.kind == "real_trade" else fn(a)
        except Exception as e:
            return self._record(a, rc, GateOutcome.HALTED, [f"Executor error: {e}"], qid=qid)
        # accounting
        if a.usd_amount and a.kind in ALWAYS_HIGH_KINDS:
            self._daily_spend += a.usd_amount
        if qid and qid in self._queue:
            self._queue[qid]["status"] = "executed"
            self._queue[qid]["result"] = result
        return self._record(a, rc, GateOutcome.EXECUTED, ["Executed."], qid=qid,
                            result=result, approver=approver)

    def register_loss(self, usd: float) -> None:
        """Called by a channel when a realized loss occurs (trades, refunds)."""
        self._roll_day()
        self._daily_loss += abs(usd)
        if self._daily_loss >= self.caps.daily_loss_cap:
            self.kill_switch = True

    # ── views ──────────────────────────────────────────────────────────────
    def pending_approvals(self) -> List[Dict[str, Any]]:
        out = []
        for qid, item in self._queue.items():
            if item["status"] in ("awaiting_human", "peer_review"):
                out.append({"queue_id": qid, "status": item["status"],
                            "rc": item["rc"], "action": item["action"].to_public()})
        return out

    def status(self) -> Dict[str, Any]:
        return {
            "kill_switch": self.kill_switch,
            "daily_spend_usd": round(self._daily_spend, 2),
            "daily_loss_usd": round(self._daily_loss, 2),
            "caps": {"micro_spend_cap": self.caps.micro_spend_cap,
                     "spend_cap": self.caps.spend_cap,
                     "daily_spend_cap": self.caps.daily_spend_cap,
                     "daily_loss_cap": self.caps.daily_loss_cap},
            "pending": len([i for i in self._queue.values()
                            if i["status"] in ("awaiting_human", "peer_review")]),
            "queue_size": len(self._queue),
        }

    def _record(self, a: ProposedAction, rc: RiskClass, outcome: GateOutcome,
                reasons: List[str], qid: Optional[str] = None,
                result: Any = None, approver: Optional[str] = None) -> Dict[str, Any]:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": a.kind, "channel": a.channel, "agent": a.agent,
            "usd_amount": round(a.usd_amount, 2), "rc": rc.value,
            "outcome": outcome.value, "reasons": reasons, "queue_id": qid,
            "approver": approver,
        }
        self._audit.append(entry)
        if len(self._audit) > 2000:
            self._audit = self._audit[-2000:]
        return {"outcome": outcome.value, "rc": rc.value, "reasons": reasons,
                "queue_id": qid, "result": result, "action": a.to_public()}
