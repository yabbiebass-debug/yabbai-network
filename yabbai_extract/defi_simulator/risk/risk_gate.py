#!/usr/bin/env python3
"""
YABBAI DeFi Engine — Risk Gate (Simulator)

THE SAFETY BOX. Every proposed action — manual, signed, or auto — must pass
through here before it can execute. Limits are enforced BEFORE execution, never
after. A bug in a strategy cannot blow past these constraints because the gate
sits between the strategy and the executor.

This is the single most important file in the system. In the live engine, these
same checks run server-side so nothing client-side can bypass them.

Nothing here promises profit. Every limit is a LOSS-CONTROL or EXPOSURE-CONTROL.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone, date
from enum import Enum
from typing import Optional, List, Dict


class GateDecision(Enum):
    ALLOW = "allow"
    BLOCK = "block"
    HALT_ALL = "halt_all"   # kill-switch / daily cap tripped — stop everything


@dataclass
class RiskLimits:
    """
    The box the user draws. The AI physically cannot act outside this.
    All values are user-set at session start and can only be tightened
    (not loosened) while a session is running.
    """
    max_trade_usd: float = 5.0            # largest single trade
    daily_loss_cap_usd: float = 3.0       # stop everything if cumulative daily loss exceeds this
    daily_volume_cap_usd: float = 50.0    # max total traded per day (exposure control)
    stop_loss_pct: float = 20.0           # exit a position if down this %
    take_profit_pct: float = 40.0         # exit a position if up this % (a CEILING, not a promise)
    max_open_positions: int = 3
    token_allowlist: List[str] = field(default_factory=list)  # only these mints may be touched; empty = none allowed
    kill_switch: bool = False             # user can flip this to halt all activity instantly

    def validate(self):
        """Sanity-check the limits themselves so a misconfiguration can't disable safety."""
        problems = []
        if self.max_trade_usd <= 0:
            problems.append("max_trade_usd must be positive")
        if self.daily_loss_cap_usd <= 0:
            problems.append("daily_loss_cap_usd must be positive")
        if self.stop_loss_pct <= 0 or self.stop_loss_pct > 100:
            problems.append("stop_loss_pct must be between 0 and 100")
        if not self.token_allowlist:
            problems.append("token_allowlist is empty — no tokens permitted (this is a safe default, set one to trade)")
        return problems


@dataclass
class ProposedAction:
    action_type: str            # 'buy' | 'sell' | 'swap'
    token_mint: str
    usd_amount: float
    reason: str                 # why the strategy proposed this
    mode: str                   # 'manual' | 'signed' | 'auto'


@dataclass
class DailyLedger:
    """Tracks cumulative daily activity for cap enforcement. Resets at UTC midnight."""
    day: date = field(default_factory=lambda: datetime.now(timezone.utc).date())
    realized_pnl_usd: float = 0.0
    traded_volume_usd: float = 0.0
    open_positions: int = 0

    def roll_if_new_day(self):
        today = datetime.now(timezone.utc).date()
        if today != self.day:
            self.day = today
            self.realized_pnl_usd = 0.0
            self.traded_volume_usd = 0.0
            # open_positions persists across days


class RiskGate:
    def __init__(self, limits: RiskLimits):
        self.limits = limits
        self.ledger = DailyLedger()
        self.audit_log: List[Dict] = []

    def evaluate(self, action: ProposedAction) -> Dict:
        """
        The one function everything calls. Returns a decision + reason.
        ALLOW only if the action passes every single check.
        """
        self.ledger.roll_if_new_day()
        reasons = []
        decision = GateDecision.ALLOW

        # 1. Kill switch — absolute, checked first
        if self.limits.kill_switch:
            return self._record(action, GateDecision.HALT_ALL,
                                 ["Kill switch is engaged. All activity halted."])

        # 2. Daily loss cap — if tripped, halt EVERYTHING for the day
        if self.ledger.realized_pnl_usd <= -abs(self.limits.daily_loss_cap_usd):
            return self._record(action, GateDecision.HALT_ALL,
                                 [f"Daily loss cap reached ({self.ledger.realized_pnl_usd:.2f} USD). "
                                  "Trading halted until UTC midnight to prevent further loss."])

        # 3. Token allowlist — only permitted mints
        if action.token_mint not in self.limits.token_allowlist:
            reasons.append(f"Token {action.token_mint[:8]}… is not on the allowlist.")
            decision = GateDecision.BLOCK

        # 4. Per-trade size
        if action.usd_amount > self.limits.max_trade_usd:
            reasons.append(f"Trade size ${action.usd_amount:.2f} exceeds max ${self.limits.max_trade_usd:.2f}.")
            decision = GateDecision.BLOCK

        # 5. Daily volume cap (exposure control)
        if self.ledger.traded_volume_usd + action.usd_amount > self.limits.daily_volume_cap_usd:
            reasons.append(f"Would exceed daily volume cap "
                           f"(${self.ledger.traded_volume_usd:.2f} + ${action.usd_amount:.2f} "
                           f"> ${self.limits.daily_volume_cap_usd:.2f}).")
            decision = GateDecision.BLOCK

        # 6. Max open positions (only relevant for opening trades)
        if action.action_type in ("buy", "swap") and self.ledger.open_positions >= self.limits.max_open_positions:
            reasons.append(f"Already at max open positions ({self.limits.max_open_positions}).")
            decision = GateDecision.BLOCK

        if decision == GateDecision.ALLOW:
            reasons.append("Passed all risk checks.")

        return self._record(action, decision, reasons)

    def register_fill(self, action: ProposedAction, realized_pnl_usd: float = 0.0,
                      opened: bool = False, closed: bool = False):
        """Called AFTER a (simulated) execution to update the ledger."""
        self.ledger.roll_if_new_day()
        self.ledger.traded_volume_usd += action.usd_amount
        self.ledger.realized_pnl_usd += realized_pnl_usd
        if opened:
            self.ledger.open_positions += 1
        if closed:
            self.ledger.open_positions = max(0, self.ledger.open_positions - 1)

    def check_position_exit(self, entry_price: float, current_price: float) -> Optional[str]:
        """
        Stop-loss / take-profit check for an OPEN position.
        Returns 'stop_loss', 'take_profit', or None.
        """
        if entry_price <= 0:
            return None
        change_pct = (current_price - entry_price) / entry_price * 100.0
        if change_pct <= -abs(self.limits.stop_loss_pct):
            return "stop_loss"
        if change_pct >= abs(self.limits.take_profit_pct):
            return "take_profit"
        return None

    def status(self) -> Dict:
        self.ledger.roll_if_new_day()
        halted = (self.limits.kill_switch or
                  self.ledger.realized_pnl_usd <= -abs(self.limits.daily_loss_cap_usd))
        return {
            "day": self.ledger.day.isoformat(),
            "realized_pnl_usd": round(self.ledger.realized_pnl_usd, 4),
            "traded_volume_usd": round(self.ledger.traded_volume_usd, 4),
            "open_positions": self.ledger.open_positions,
            "daily_loss_cap_usd": self.limits.daily_loss_cap_usd,
            "daily_volume_cap_usd": self.limits.daily_volume_cap_usd,
            "kill_switch": self.limits.kill_switch,
            "halted": halted,
            "remaining_loss_budget_usd": round(
                max(0.0, self.limits.daily_loss_cap_usd + self.ledger.realized_pnl_usd), 4),
        }

    def _record(self, action: ProposedAction, decision: GateDecision, reasons: List[str]) -> Dict:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "action": action.action_type,
            "token": action.token_mint[:12],
            "usd": action.usd_amount,
            "mode": action.mode,
            "decision": decision.value,
            "reasons": reasons,
        }
        self.audit_log.append(entry)
        if len(self.audit_log) > 1000:
            self.audit_log = self.audit_log[-1000:]
        return {
            "decision": decision.value,
            "allowed": decision == GateDecision.ALLOW,
            "reasons": reasons,
            "ledger": self.status(),
        }
