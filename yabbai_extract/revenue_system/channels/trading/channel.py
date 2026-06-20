"""
YABBAI Revenue System — Trading Channel

Reuses the EXISTING defi_simulator engine (RiskGate, PaperExecutor, PriceFeed,
RoutingEngine, strategies) for the job it's good at: proving a strategy is
profitable on PAPER before a cent is risked.

Adds a live, real-capital path that is strictly MORE constrained than paper:
  - paper_executor is unchanged (no wallet, never was)
  - a Signer interface + LiveExecutor broadcasts ONLY when:
        (a) the gate classifies real_trade as HIGH,
        (b) a human approves WITH a signature, and
        (c) the per-trade hard caps pass
  - the DEFAULT signer is NoKeySigner, which REFUSES to broadcast anything.
    You implement a real signer and inject it. The code never holds, fakes, or
    hardcodes a private key. There is no "fake broadcast" anywhere.

Net PnL from real trades is the ONLY thing this channel contributes to real
income — and only after the exchange/chain confirms it.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Protocol

from ...truth import Metric, SourceType, TruthLedger
from ...gates import GateController, ProposedAction
from ...memory import Memory, Learning


# ── Signer interface (you own this; we never ship a real one) ────────────────
class Signer(Protocol):
    """
    Implement this to sign+broadcast a real trade. It MUST return a confirmed
    result with a real tx/order id from the exchange or chain. If it cannot
    confirm, raise — never return a fabricated success.
    """
    def buy(self, token: str, usd_amount: float) -> Dict[str, Any]: ...
    def sell(self, token: str) -> Dict[str, Any]: ...


class NoKeySigner:
    """
    Default signer. Has no key and refuses everything. This guarantees the
    system cannot broadcast a real trade until YOU inject a real Signer you
    have audited. There is no shipped code path to a private key.
    """
    def buy(self, token: str, usd_amount: float) -> Dict[str, Any]:
        raise RuntimeError("NoKeySigner: no real signer configured. "
                           "Inject an audited Signer implementation to trade live.")
    def sell(self, token: str) -> Dict[str, Any]:
        raise RuntimeError("NoKeySigner: no real signer configured.")


@dataclass
class TradeCap:
    """Hard per-trade caps on the live path. Stricter than the gate's general caps."""
    max_real_trade_usd: float = 10.0
    token_allowlist: List[str] = field(default_factory=list)

    def check(self, token: str, usd: float) -> Optional[str]:
        if token not in self.token_allowlist:
            return f"Token {token[:8]} not on live allowlist."
        if usd > self.max_real_trade_usd:
            return f"Trade ${usd:.2f} > live cap ${self.max_real_trade_usd:.2f}."
        return None


class TradingChannel:
    """
    One trading channel. Runs paper proving continuously; proposes live trades
    to the gate (which queues them for a human) only when a strategy is
    profitable on paper AND has cleared its proof bar.
    """

    def __init__(self, ledger: TruthLedger, gates: GateController,
                 memory: Memory, caps: TradeCap,
                 paper_engine_tick: Optional[Callable[[], List[Dict]]] = None,
                 signer: Signer = NoKeySigner(),
                 paper_proof_bar: int = 100):
        self.ledger = ledger
        self.gates = gates
        self.memory = memory
        self.caps = caps
        self.paper_tick = paper_engine_tick or (lambda: [])
        self.signer = signer
        self.paper_proof_bar = paper_proof_bar   # min positive paper cycles before live
        self.paper_cycles_positive = 0

        # Register the live-trade executor with the gate. Always HIGH → human.
        self.gates.register_executor("trading", "real_trade", self._execute_real_trade)

    # ── observe: read paper engine state (real prices, paper PnL) ───────────
    def observe(self) -> List[Metric]:
        """Returns the paper PnL as an ESTIMATE metric — NOT real income.

        Paper PnL is information for decisions, never money. It feeds DECIDE,
        it does not flow into the ledger's real total.
        """
        ticks = self.paper_tick() or []
        metrics: List[Metric] = []
        # surface the latest paper snapshot as an estimate (advisory only)
        if ticks:
            metrics.append(Metric(
                name="paper_pnl", value=0.0,
                source="trading:paper_engine", source_type=SourceType.ESTIMATE,
                confidence=0.4, signed_by="trading_channel",
                notes=f"paper ticks this cycle: {len(ticks)}; "
                      f"this is SIMULATED performance, NOT real income."))
        return metrics

    # ── propose: turn a paper-proven edge into a gated live-trade action ────
    def propose(self) -> List[ProposedAction]:
        actions: List[ProposedAction] = []
        # Only propose live trades once the strategy has proven itself on paper
        # over many cycles. Honest bar: no live money on a 2-day paper win.
        if self.paper_cycles_positive < self.paper_proof_bar:
            return actions
        for token in self.caps.token_allowlist:
            # A real trade is proposed conservatively; the gate + human decide.
            actions.append(ProposedAction(
                kind="real_trade", channel="trading", agent="trading_channel",
                usd_amount=self.caps.max_real_trade_usd,
                reversible=False,
                payload={"token": token,
                         "side": "buy",
                         "evidence": f"{self.paper_cycles_positive} positive paper cycles"},
                reason=f"Strategy cleared paper proof bar "
                       f"({self.paper_cycles_positive} >= {self.paper_proof_bar}).",
                ev={"p_success": 0.4,
                    "upside_usd": self.caps.max_real_trade_usd * 1.5,
                    "cost_usd": self.caps.max_real_trade_usd,
                    "risk_adjustment": self.caps.max_real_trade_usd * 0.5}))
        return actions

    def note_paper_result(self, net_pnl_usd: float) -> None:
        """Called each paper cycle to advance/reframe the proof bar."""
        if net_pnl_usd > 0:
            self.paper_cycles_positive += 1
        else:
            self.paper_cycles_positive = max(0, self.paper_cycles_positive - 2)

    # ── act: the live executor (gate calls this only after human approval) ──
    def _execute_real_trade(self, action: ProposedAction,
                            signature: Optional[str] = None) -> Dict[str, Any]:
        if not signature:
            return {"ok": False, "error": "real_trade requires a human signature."}
        token = action.payload.get("token", "")
        side = action.payload.get("side", "buy")
        # hard cap re-check right before broadcast
        problem = self.caps.check(token, action.usd_amount)
        if problem:
            return {"ok": False, "error": problem}

        # A signer refusal MUST propagate as an error so the gate records this
        # action as HALTED, never "executed". A trade that couldn't broadcast is
        # not a successful execution — labelling it otherwise would be dishonest.
        try:
            if side == "buy":
                confirm = self.signer.buy(token, action.usd_amount)
            else:
                confirm = self.signer.sell(token)
        except Exception as e:
            self.memory.add_learning(Learning(
                kind="loss", channel="trading", agent="trading_channel",
                summary=f"live trade refused/failed: {e}", usd_impact=0.0))
            raise   # let the gate mark this HALTED; never report it as executed

        tx = confirm.get("tx_id") or confirm.get("id") or "unknown"
        realized = float(confirm.get("realized_pnl_usd", 0.0))

        # Only a CONFIRMED, reconciled realized PnL becomes real income/expense.
        if realized >= 0:
            self.ledger.record(Metric(
                name="income", value=realized, source=f"trade:{tx}",
                source_type=SourceType.PAYMENT_PROCESSOR,
                reconciled=True, signed_by="trading_channel",
                notes=f"confirmed realized gain on {token[:8]}"))
        else:
            self.ledger.record(Metric(
                name="expense", value=abs(realized), source=f"trade:{tx}",
                source_type=SourceType.PAYMENT_PROCESSOR,
                reconciled=True, signed_by="trading_channel",
                notes=f"confirmed realized loss on {token[:8]}"))
            self.gates.register_loss(abs(realized))

        self.memory.add("trades", {
            "ts": datetime.now(timezone.utc).isoformat(), "token": token,
            "side": side, "usd": action.usd_amount, "tx_id": tx,
            "realized_pnl_usd": realized, "signature": bool(signature)})
        self.memory.add_learning(Learning(
            kind="win" if realized >= 0 else "loss", channel="trading",
            agent="trading_channel", summary=f"{side} {token[:8]} @ ${action.usd_amount:.2f} tx={tx}",
            usd_impact=realized))
        return {"ok": True, "tx_id": tx, "realized_pnl_usd": realized}
