#!/usr/bin/env python3
"""
YABBAI DeFi Engine — AI Routing Engine (Simulator)

This is the "agentic" brain. On each loop tick it:
  1. Pulls live market data for allowlisted tokens
  2. Asks the active strategy for a signal
  3. Optionally asks an LLM to sanity-check / explain the signal (advisory only —
     the LLM can NEVER expand the action beyond what the gate allows)
  4. Emits a ProposedAction (or nothing) to the ModeController

DESIGN HONESTY: the LLM does NOT predict prices. No model does that reliably.
The LLM's job here is reasoning about RISK and EXPLANATION, not fortune-telling.
The actual buy/sell signal comes from a transparent, auditable strategy you can
read and understand — not a black box claiming to see the future.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Protocol

from ..risk.risk_gate import ProposedAction, RiskGate


@dataclass
class MarketSnapshot:
    token_mint: str
    price: float
    sma_short: float          # short moving average
    sma_long: float           # long moving average
    rsi: float                # 0-100
    volatility_pct: float
    ts: str


class Strategy(Protocol):
    """Any strategy implements this. Strategies are transparent and auditable."""
    name: str
    def signal(self, snap: MarketSnapshot, has_position: bool) -> Optional[Dict]:
        """Return {'action': 'buy'|'sell', 'confidence': 0-1, 'reason': str} or None."""
        ...


class RoutingEngine:
    def __init__(self, gate: RiskGate, controller, price_history, llm_advisor=None):
        """
        price_history: object providing .snapshot(mint) -> MarketSnapshot
        llm_advisor: optional callable(prompt)->str for advisory commentary only.
                     If None, the engine runs fully on the transparent strategy.
        """
        self.gate = gate
        self.controller = controller
        self.price_history = price_history
        self.llm_advisor = llm_advisor
        self.strategy: Optional[Strategy] = None
        self.tick_log: List[Dict] = []
        self.position_entries: Dict[str, float] = {}  # mint -> entry price (for SL/TP)

    def set_strategy(self, strategy: Strategy):
        self.strategy = strategy

    def tick(self) -> List[Dict]:
        """One iteration of the agentic loop. Returns what it did this tick."""
        if not self.strategy:
            return [{"tick": "no_strategy"}]

        results = []
        allowlist = self.gate.limits.token_allowlist

        for mint in allowlist:
            snap = self.price_history.snapshot(mint)
            if not snap:
                continue

            has_position = mint in self.position_entries

            # ── 1. Stop-loss / take-profit check FIRST on open positions ──
            if has_position:
                exit_reason = self.gate.check_position_exit(
                    self.position_entries[mint], snap.price)
                if exit_reason:
                    action = ProposedAction(
                        action_type="sell", token_mint=mint,
                        usd_amount=0.0,  # executor sells the whole position
                        reason=f"{exit_reason} triggered at price {snap.price:.6f}",
                        mode=self.controller.mode.value,
                    )
                    out = self.controller.propose(action)
                    if out.get("outcome") == "executed":
                        self.position_entries.pop(mint, None)
                    results.append({"mint": mint[:8], "signal": exit_reason, "routed": out.get("outcome")})
                    continue

            # ── 2. Ask the strategy for a signal ──
            sig = self.strategy.signal(snap, has_position)
            if not sig:
                results.append({"mint": mint[:8], "signal": "hold"})
                continue

            # ── 3. Optional LLM advisory (explanation / risk caution only) ──
            advisory = None
            if self.llm_advisor:
                advisory = self._get_advisory(snap, sig)

            # ── 4. Size the trade WITHIN limits and propose ──
            if sig["action"] == "buy" and not has_position:
                size = min(self.gate.limits.max_trade_usd,
                           self.gate.limits.daily_volume_cap_usd)
                action = ProposedAction(
                    action_type="buy", token_mint=mint, usd_amount=size,
                    reason=sig["reason"] + (f" | AI note: {advisory}" if advisory else ""),
                    mode=self.controller.mode.value,
                )
                out = self.controller.propose(action)
                if out.get("outcome") == "executed":
                    self.position_entries[mint] = snap.price
                results.append({"mint": mint[:8], "signal": "buy",
                                "confidence": sig.get("confidence"), "routed": out.get("outcome")})

            elif sig["action"] == "sell" and has_position:
                action = ProposedAction(
                    action_type="sell", token_mint=mint, usd_amount=0.0,
                    reason=sig["reason"], mode=self.controller.mode.value,
                )
                out = self.controller.propose(action)
                if out.get("outcome") == "executed":
                    self.position_entries.pop(mint, None)
                results.append({"mint": mint[:8], "signal": "sell", "routed": out.get("outcome")})
            else:
                results.append({"mint": mint[:8], "signal": sig["action"], "routed": "no_op"})

        self.tick_log.append({"ts": datetime.now(timezone.utc).isoformat(), "results": results})
        if len(self.tick_log) > 500:
            self.tick_log = self.tick_log[-500:]
        return results

    def _get_advisory(self, snap: MarketSnapshot, sig: Dict) -> str:
        """LLM gives a one-line risk caution. Advisory only — cannot change the action."""
        try:
            prompt = (
                "You are a risk-focused trading advisor. In ONE sentence, note the main "
                "RISK of this proposed action. Do not predict price. Do not encourage. "
                f"Token volatility: {snap.volatility_pct:.1f}%, RSI: {snap.rsi:.0f}. "
                f"Proposed: {sig['action']} because {sig['reason']}."
            )
            return self.llm_advisor(prompt)
        except Exception:
            return ""
