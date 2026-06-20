#!/usr/bin/env python3
"""
YABBAI Ops — Operations Cockpit

Ties the three cores together into a founder's cockpit:
  - TruthProtocol: honest metrics, no fabrication, reconciled-only totals
  - DecisionEngine: EV tickets + approval gates (irreversible/money → human)
  - ComplianceGate: outreach/data/claims rules enforced

This is the honest version of the "autonomous revenue OS": it organizes your work,
scores opportunities by EV, checks compliance, and queues every money-touching or
irreversible action for YOUR approval. It does the analysis, drafting, and tracking.
YOU do the irreducibly human part — talking to customers, approving spend, sending.

It does NOT autonomously generate profit, because that isn't a real thing. What it
does is give you the operational discipline that real businesses use to not lie to
themselves and not blow their budget — which is genuinely valuable.
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .truth_protocol import MetricStore, Metric, SourceType, DataGap
from .decision_engine import DecisionEngine, EVTicket
from .compliance_gate import ComplianceGate


# The org roles — as a way to organize YOUR work and YOUR assistants' drafts,
# not as autonomous money-makers. Each is a lens on a real founder function.
ROLES = {
    "CEO": "Sets priorities, allocates capital within caps, resolves conflicts.",
    "CFO": "Owns the truth protocol, reconciliation, spend caps, cashflow, tax records.",
    "CMO": "Demand: content drafts, SEO, paid (within ROAS gates), funnels.",
    "CRO": "Pipeline: qualified leads, outreach drafts (within compliance gates).",
    "COO": "Fulfillment, delivery, QA, automation, client success/renewals.",
    "CTO": "Infrastructure, integrations, data integrity, security.",
    "RESEARCH": "Opportunity scoring, market intel, competitor signal.",
}


class OpsCockpit:
    def __init__(self, data_dir: str,
                 micro_spend_cap: float = 5.0, spend_cap: float = 50.0, daily_cap: float = 100.0):
        d = Path(data_dir)
        d.mkdir(parents=True, exist_ok=True)
        self.metrics = MetricStore(str(d / "metrics.json"))
        self.decisions = DecisionEngine(micro_spend_cap, spend_cap, daily_cap)
        self.compliance = ComplianceGate()

    # ── KPI dashboard (real only) ─────────────────────────────────────────────
    def dashboard(self) -> Dict:
        rev = self.metrics.reconciled_revenue_total()
        # standard KPIs the founder cares about — only show real values
        kpi_names = ["net_profit", "mrr", "cac", "ltv", "roas", "conversion_rate",
                     "refund_rate", "pipeline_value"]
        kpis = {}
        for name in kpi_names:
            m = self.metrics.get(name)
            kpis[name] = ({"value": m.value, "source": m.source_type.value,
                           "real": m.is_real()} if m else {"value": None, "real": False,
                                                           "status": "DATA GAP — not recorded"})
        return {
            "reconciled_revenue": rev,
            "kpis": kpis,
            "pending_approvals": len(self.decisions.pending()),
            "data_gaps": [k for k, v in kpis.items() if not v.get("real")],
            "honesty_note": "Only reconciled, processor-backed revenue is in the total. "
                            "KPIs marked 'DATA GAP' need real data before they inform decisions.",
        }

    # ── Score an opportunity by EV ────────────────────────────────────────────
    def score_action(self, action: str, p_success: float, upside: float, cost: float,
                     reversible: bool = True, risk_adjustment: float = 0.0) -> Dict:
        ticket = EVTicket(action, p_success, upside, cost, risk_adjustment)
        gate = self.decisions.propose(action, cost, reversible, ticket,
                                      reason=f"EV ${ticket.ev}")
        return {"ev_ticket": {"action": action, "ev": ticket.ev, "verdict": ticket.verdict(),
                              "p_success": p_success, "upside": upside, "cost": cost},
                "gate": gate}

    # ── Check an outbound message before it goes ─────────────────────────────
    def check_outreach(self, message: str, opted_in: bool, has_unsubscribe: bool,
                       list_source: str) -> Dict:
        r = self.compliance.check_outbound(message, opted_in, has_unsubscribe, list_source)
        return {"ok": r.ok, "issues": r.issues, "requires_human": r.requires_human, "note": r.note}

    def check_copy(self, text: str) -> Dict:
        r = self.compliance.check_claims(text)
        return {"ok": r.ok, "issues": r.issues, "note": r.note}

    # ── Approvals ──────────────────────────────────────────────────────────────
    def pending_approvals(self) -> List[Dict]:
        return self.decisions.pending()

    def approve(self, qid: str) -> Dict:
        return self.decisions.approve(qid)

    def reject(self, qid: str) -> Dict:
        return self.decisions.reject(qid)

    # ── Record a real metric ──────────────────────────────────────────────────
    def record_metric(self, name: str, value: Optional[float], source: str,
                      source_type: str, reconciled: bool = False) -> Dict:
        try:
            st = SourceType(source_type)
        except ValueError:
            return {"ok": False, "error": f"invalid source_type. Use one of: "
                    f"{[s.value for s in SourceType]}"}
        self.metrics.record(Metric(name, value, source, st, reconciled=reconciled))
        return {"ok": True, "recorded": name,
                "counts_in_totals": st == SourceType.PAYMENT_PROCESSOR and reconciled}

    def roles(self) -> Dict:
        return ROLES
