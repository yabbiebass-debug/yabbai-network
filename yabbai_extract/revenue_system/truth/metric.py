"""
YABBAI Revenue System — Truth Layer

The Absolute Truth Protocol, as runnable code. A Metric is never a bare float;
it always carries provenance. This is the single mechanism that enforces
"no mock data, no fabricated gains": a number without a verifiable source is
NULL (never zero), and only payment-processor numbers roll into real totals.

Every agent and every channel must build Metrics through this module. There is
no other path to "income."
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class SourceType(str, Enum):
    """Provenance of a metric value. Only some of these count as real."""
    API = "api"                      # a real product/analytics API response
    PAYMENT_PROCESSOR = "payment_processor"   # Stripe/PayPal/Gumroad webhook or export — REAL MONEY
    ANALYTICS = "analytics"          # GA4/Plausible/etc. real traffic
    CRM = "crm"                      # real pipeline state
    MANUAL = "manual"                # a human entered it (auditable)
    ESTIMATE = "estimate"            # model guess / projection — NEVER real money
    PROJECTED = "projected"          # forward-looking model output — NEVER real money


# Source types that may be summed into REAL income/profit totals. If it's not in
# this set, it can inform planning but it CANNOT be reported as money earned.
REAL_INCOME_SOURCES = {SourceType.PAYMENT_PROCESSOR}

# Source types that may be shown on dashboards as supporting context but must
# always be labelled as non-real.
NON_REAL_SOURCES = {SourceType.ESTIMATE, SourceType.PROJECTED}


class DataGap(Exception):
    """Raised when a decision needs a value that has no verifiable source.

    Per the Truth Protocol, the agent must HALT that decision — it may NOT
    fabricate a plausible number to continue. Catching this is the intended
    behaviour: it surfaces the gap instead of papering over it.
    """


@dataclass
class Metric:
    """A single measured value with full provenance. First-class object."""
    name: str
    value: float
    source: str                         # human-readable origin, e.g. "stripe:ch_3abc"
    source_type: SourceType
    fetched_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    confidence: float = 1.0             # 0..1, only meaningful for non-REAL sources
    reconciled: bool = False            # True only when payment processor has confirmed
    signed_by: Optional[str] = None     # agent or human id that vouches for it
    assumptions: Optional[str] = None   # required for PROJECTED values
    unit: str = "usd"
    notes: Optional[str] = None

    def __post_init__(self):
        # PROJECTED values MUST declare their assumptions — no silent projections.
        if self.source_type == SourceType.PROJECTED and not self.assumptions:
            raise ValueError(
                f"Metric '{self.name}' is PROJECTED but has no assumptions. "
                "All projections must state what was assumed.")
        if not self.source or not self.source.strip():
            raise ValueError(f"Metric '{self.name}' has empty source — unverifiable.")

    @property
    def is_real_income(self) -> bool:
        """The one property the vault and dashboards gate on."""
        return self.source_type in REAL_INCOME_SOURCES and self.reconciled

    @property
    def is_bookable(self) -> bool:
        """May this be summed into a reconciled total? payment_processor AND reconciled=True."""
        return self.source_type in REAL_INCOME_SOURCES and self.reconciled

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name, "value": round(self.value, 4),
            "source": self.source, "source_type": self.source_type.value,
            "fetched_at": self.fetched_at, "confidence": round(self.confidence, 3),
            "reconciled": self.reconciled,
            "signed_by": self.signed_by, "assumptions": self.assumptions,
            "unit": self.unit, "notes": self.notes,
            "is_real_income": self.is_real_income,
        }

    @staticmethod
    def gap(name: str, why: str) -> "Metric":
        """Construct a sentinel for a known missing value (NOT a fake number).

        value is +inf so any EV arithmetic using it fails loudly rather than
        silently treating a missing cost as $0.
        """
        return Metric(name=name, value=float("inf"),
                      source=f"data-gap:{why}", source_type=SourceType.ESTIMATE,
                      reconciled=False, confidence=0.0,
                      notes="DATA GAP — no verifiable source; "
                      "decision must halt until this is resolved.")


class TruthLedger:
    """
    Collects Metrics and answers the only questions that matter:
      - How much REAL money did we make (reconciled only)?
      - What's the projected/estimated picture (labelled as such)?
      - What's unverified (excluded)?

    Nothing here invents numbers. If you ask for a real total and there are no
    payment-processor entries, the answer is $0.00 — honestly — not a guess.
    """

    def __init__(self):
        self._entries: List[Metric] = []

    def record(self, m: Metric) -> Metric:
        if not isinstance(m, Metric):
            raise TypeError("Only Metric objects may be recorded.")
        self._entries.append(m)
        return m

    def record_many(self, metrics: List[Metric]) -> None:
        for m in metrics:
            self.record(m)

    # ── the only totals that mean "money" ──────────────────────────────────
    def real_income(self) -> float:
        """Sum of reconciled income only. This is the number that pays out."""
        return round(sum(m.value for m in self._entries
                         if m.name == "income" and m.is_bookable), 2)

    def real_expense(self) -> float:
        return round(sum(m.value for m in self._entries
                         if m.name == "expense" and m.is_bookable), 2)

    def net_profit(self) -> float:
        """REAL net profit — the only number a payout decision may read."""
        return round(self.real_income() - self.real_expense(), 2)

    # ── labelled, never-as-real views ──────────────────────────────────────
    def projected_net(self) -> Dict[str, Any]:
        """A clearly-labelled projection. Never consumed as real by a gate."""
        proj = [m for m in self._entries if m.source_type == SourceType.PROJECTED]
        est = [m for m in self._entries if m.source_type == SourceType.ESTIMATE]
        return {
            "projected_income": round(sum(m.value for m in proj if m.name == "income"), 2),
            "estimated_income": round(sum(m.value for m in est if m.name == "income"), 2),
            "warning": "PROJECTION/ESTIMATE — not real money. May inform planning, "
                       "never a payout.",
            "assumptions": [m.assumptions for m in proj if m.assumptions],
        }

    def unreconciled(self) -> List[Dict[str, Any]]:
        """Manual entries not yet tied to a payment processor — shown, not summed."""
        return [m.to_dict() for m in self._entries
                if m.source_type == SourceType.MANUAL and m.name == "income"]

    def data_gaps(self) -> List[str]:
        return [m.source for m in self._entries if m.source.startswith("data-gap:")]

    def summary(self) -> Dict[str, Any]:
        return {
            "real_income": self.real_income(),
            "real_expense": self.real_expense(),
            "net_profit": self.net_profit(),
            **self.projected_net(),
            "unreconciled_count": len(self.unreconciled()),
            "data_gaps": self.data_gaps(),
            "entry_count": len(self._entries),
        }

    def all_entries(self) -> List[Dict[str, Any]]:
        return [m.to_dict() for m in self._entries]


def require_real(value: Optional[float], metric: Optional[Metric], decision: str) -> float:
    """
    Decision-time guard. If the metric backing a decision isn't real, raise
    DataGap and halt — do NOT fall through with a guessed value.

    Use at the top of any payout / irreversible action:
        real_net = require_real(ledger.net_profit(), backing_metric, "treasury payout")
    """
    if metric is not None and not metric.is_bookable:
        raise DataGap(
            f"Decision '{decision}' requires a real (payment_processor) value, "
            f"but the backing metric source_type is {metric.source_type.value}. Halting.")
    if value is None or (isinstance(value, float) and value == float("inf")):
        raise DataGap(f"Decision '{decision}' has no verifiable value. Halting.")
    return value
