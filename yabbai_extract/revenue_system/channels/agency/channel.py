"""
YABBAI Revenue System — Agency Channel (services: AI automation, sites, SEO)

The service funnel. Income is booked ONLY when an invoice is PAID — confirmed
by Stripe/invoice webhook. A signed contract or a "booked call" is NOT income;
it's pipeline value, recorded as an ESTIMATE and labelled as such.

Outbound outreach (cold email/DM) is HIGH and compliance-gated: opt-in or
CAN-SPAM/GDPR-compliant cold only, with suppression + unsubscribe. The gate
enforces this by classifying outbound_email/outbound_dm as HIGH.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from ...truth import Metric, SourceType, TruthLedger
from ...gates import GateController, ProposedAction
from ...memory import Memory, Learning


_PAID_INVOICE_CACHE: set = set()


class AgencyChannel:
    def __init__(self, ledger: TruthLedger, gates: GateController, memory: Memory):
        self.ledger = ledger
        self.gates = gates
        self.memory = memory
        # outbound + contract are HIGH by default in the gates layer
        self.gates.register_executor("agency", "outbound_email",
                                     self._execute_outbound)

    # ── observe: pull paid invoices (real income) + pipeline (estimates) ─────
    def observe(self, fetch_paid_invoices: Optional[Callable[[], List[Dict]]] = None,
                pipeline_value_usd: float = 0.0) -> List[Metric]:
        metrics: List[Metric] = []
        if fetch_paid_invoices is None:
            metrics.append(Metric(
                name="income", value=0.0, source="agency:no_invoice_fetcher",
                source_type=SourceType.ESTIMATE, confidence=0.0,
                notes="DATA GAP: no paid-invoice fetcher wired."))
        else:
            try:
                for inv in (fetch_paid_invoices() or []):
                    iid = inv.get("invoice_id") or inv.get("id")
                    if not iid or iid in _PAID_INVOICE_CACHE:
                        continue
                    _PAID_INVOICE_CACHE.add(iid)
                    amt = float(inv.get("paid_usd", 0))
                    metrics.append(Metric(
                        name="income", value=amt, source=f"invoice:{iid}",
                        source_type=SourceType.PAYMENT_PROCESSOR,
                        reconciled=True, signed_by="agency_channel",
                        notes=f"paid invoice {iid}"))
                    self.memory.add_learning(Learning(
                        kind="win", channel="agency", agent="agency_channel",
                        summary=f"Paid invoice {iid}: ${amt:.2f}", usd_impact=amt))
            except Exception as e:
                metrics.append(Metric(
                    name="income", value=0.0,
                    source=f"agency:fetch_error:{e}", source_type=SourceType.ESTIMATE,
                    notes="DATA GAP: invoice fetch failed."))

        # Pipeline value is NEVER real income — always an estimate.
        if pipeline_value_usd:
            metrics.append(Metric(
                name="pipeline_value", value=pipeline_value_usd,
                source="agency:crm", source_type=SourceType.CRM,
                confidence=0.5, notes="Pipeline (signed proposals, booked calls). "
                "Not income until an invoice is paid."))
        return metrics

    # ── propose: outreach to qualified leads (HIGH → human approval) ────────
    def propose_outreach(self, lead_email: str, first_line: str,
                         offer: str, ev_upside_usd: float = 500.0) -> ProposedAction:
        return ProposedAction(
            kind="outbound_email", channel="agency", agent="agency_channel",
            usd_amount=0.0, reversible=False,
            payload={"to": lead_email, "first_line": first_line[:500],
                     "offer": offer[:1000],
                     "compliance": "opt-in or CAN-SPAM-compliant cold; "
                                   "unsubscribe + suppression enforced"},
            reason=f"Cold outreach to qualified lead (offer: {offer[:60]}).",
            ev={"p_success": 0.05, "upside_usd": ev_upside_usd,
                "cost_usd": 0.0, "risk_adjustment": 0.0})

    def propose(self) -> List[ProposedAction]:
        leads = self.memory.find("leads", lambda r: r.get("status") == "qualified")
        return [self.propose_outreach(l.get("email", ""), l.get("first_line", ""),
                                     l.get("offer", "AI automation audit"))
                for l in leads[:5]]

    # ── act: send only after human approval + compliance assertion ──────────
    def _execute_outbound(self, action: ProposedAction) -> Dict[str, Any]:
        """A real ESP client (you inject) sends. We never ship a sender that
        mass-emails. Without one, we record the approved intent only."""
        to = action.payload.get("to", "")
        self.memory.add("campaigns", {
            "ts": datetime.now(timezone.utc).isoformat(),
            "to": to, "offer": action.payload.get("offer"),
            "status": "approved_pending_esp_client",
            "compliance": action.payload.get("compliance")})
        return {"ok": True, "status": "approved_pending_esp_client", "to": to,
                "note": "Wire an ESP client (e.g. a transactional API with suppression "
                        "+ unsubscribe) to actually send. Unsolicited mass send is blocked."}
