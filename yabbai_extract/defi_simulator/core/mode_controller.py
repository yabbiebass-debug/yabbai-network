#!/usr/bin/env python3
"""
YABBAI DeFi Engine — Three-Mode Controller (Simulator)

The user chooses how much autonomy the AI has. Every mode routes through the
SAME RiskGate — the only thing that changes is who/what triggers execution and
who confirms it.

  MANUAL  — AI proposes. Nothing happens until the user clicks "execute" per action.
  SIGNED  — AI proposes. User approves each one (in live: a wallet signature).
            Functionally same gate as manual; the difference is the live engine
            requires a cryptographic signature, not just a click.
  AUTO    — AI proposes AND executes, but ONLY within the RiskGate box, and only
            while the loop is running. Every action still hits the gate first.
            The user is never asked, but the limits they set are absolute.

In the simulator, "execution" is a paper fill against live market prices.
No real funds, no real transactions, ever. This file has no path to a real wallet.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional, Callable

from ..risk.risk_gate import RiskGate, ProposedAction, GateDecision


class Mode(Enum):
    MANUAL = "manual"
    SIGNED = "signed"
    AUTO = "auto"


@dataclass
class PendingAction:
    """An action awaiting user confirmation (manual/signed modes)."""
    id: str
    action: ProposedAction
    gate_result: Dict
    proposed_at: str
    status: str = "pending"   # pending | executed | rejected | expired


class ModeController:
    def __init__(self, gate: RiskGate, executor: Callable, mode: Mode = Mode.MANUAL):
        """
        executor: a function (ProposedAction) -> fill_result. In the simulator this
                  is the PaperExecutor. Swapping this for a real signer is the ONLY
                  change needed for the live engine — and it is deliberately isolated.
        """
        self.gate = gate
        self.executor = executor
        self.mode = mode
        self.pending: Dict[str, PendingAction] = {}
        self.history: List[Dict] = []
        self._counter = 0

    def set_mode(self, mode: Mode):
        """User can change mode anytime. Switching to manual/signed cancels nothing
        already executing; switching to auto does NOT retroactively run pending items."""
        self.mode = mode
        return {"mode": self.mode.value}

    def propose(self, action: ProposedAction) -> Dict:
        """
        The strategy calls this with a proposed action.
        Routing depends on mode, but the gate is checked in ALL cases first.
        """
        action.mode = self.mode.value
        gate_result = self.gate.evaluate(action)

        # Gate says halt-all → nothing proceeds regardless of mode
        if gate_result["decision"] == GateDecision.HALT_ALL.value:
            return {"outcome": "halted", "gate": gate_result}

        # Gate blocked this specific action
        if not gate_result["allowed"]:
            return {"outcome": "blocked", "gate": gate_result}

        # Gate allowed it — now mode decides what happens next
        if self.mode == Mode.AUTO:
            return self._execute(action, gate_result, auto=True)

        # MANUAL or SIGNED — queue it for confirmation, do NOT execute
        self._counter += 1
        pid = f"act_{self._counter}"
        self.pending[pid] = PendingAction(
            id=pid, action=action, gate_result=gate_result,
            proposed_at=datetime.now(timezone.utc).isoformat(),
        )
        return {
            "outcome": "pending_confirmation",
            "pending_id": pid,
            "requires": "signature" if self.mode == Mode.SIGNED else "click",
            "gate": gate_result,
            "action": self._action_dict(action),
        }

    def confirm(self, pending_id: str, signature: Optional[str] = None) -> Dict:
        """User confirms a pending action. In SIGNED mode (live), signature is required."""
        pa = self.pending.get(pending_id)
        if not pa or pa.status != "pending":
            return {"outcome": "not_found_or_resolved"}

        if self.mode == Mode.SIGNED and not signature:
            return {"outcome": "signature_required",
                    "note": "In signed mode a wallet signature is required. (Simulator accepts a placeholder.)"}

        # Re-check the gate at confirmation time — market moved since proposal
        recheck = self.gate.evaluate(pa.action)
        if not recheck["allowed"]:
            pa.status = "rejected"
            return {"outcome": "blocked_on_recheck", "gate": recheck,
                    "note": "Conditions changed since proposal; action no longer passes risk checks."}

        result = self._execute(pa.action, recheck, auto=False, signature=signature)
        pa.status = "executed"
        return result

    def reject(self, pending_id: str) -> Dict:
        pa = self.pending.get(pending_id)
        if pa and pa.status == "pending":
            pa.status = "rejected"
            return {"outcome": "rejected", "pending_id": pending_id}
        return {"outcome": "not_found_or_resolved"}

    def list_pending(self) -> List[Dict]:
        return [
            {"id": pa.id, "action": self._action_dict(pa.action),
             "proposed_at": pa.proposed_at, "status": pa.status}
            for pa in self.pending.values() if pa.status == "pending"
        ]

    def _execute(self, action: ProposedAction, gate_result: Dict,
                 auto: bool, signature: Optional[str] = None) -> Dict:
        """Run the (paper) executor and register the fill with the gate."""
        fill = self.executor(action)

        self.gate.register_fill(
            action,
            realized_pnl_usd=fill.get("realized_pnl_usd", 0.0),
            opened=fill.get("opened", False),
            closed=fill.get("closed", False),
        )

        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "mode": self.mode.value,
            "auto": auto,
            "action": self._action_dict(action),
            "fill": fill,
            "signature": signature,
            "gate_reasons": gate_result.get("reasons", []),
        }
        self.history.append(record)
        return {"outcome": "executed", "fill": fill, "record": record}

    @staticmethod
    def _action_dict(a: ProposedAction) -> Dict:
        return {"type": a.action_type, "token": a.token_mint, "usd": a.usd_amount,
                "reason": a.reason, "mode": a.mode}
