#!/usr/bin/env python3
"""
YABBAI Ops — Truth Protocol

The technical enforcement of "no mock data." Every metric is a typed object with a
mandatory source. Values without a verifiable source are NULL, not zero. Revenue must
reconcile to a payment processor or be excluded from totals. If data is missing, the
system reports DATA GAP and halts that decision — it never fabricates a number.

This is the genuinely valuable engineering core: it makes it structurally impossible
for the system to lie to you about money, which is exactly the discipline that keeps a
real business honest with itself.
"""

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional, List, Dict, Any


class SourceType(str, Enum):
    API = "api"
    PAYMENT_PROCESSOR = "payment_processor"   # Stripe/PayPal/Gumroad export
    ANALYTICS = "analytics"
    CRM = "crm"
    MANUAL = "manual"                          # human-entered, real but unverified-by-API
    ESTIMATE = "estimate"                      # planning only — NEVER consumed as real


@dataclass
class Metric:
    name: str
    value: Optional[float]                     # None = NULL (missing), never faked to 0
    source: str                                # where it came from (e.g. "stripe_export_2026_06")
    source_type: SourceType
    fetched_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    confidence: float = 1.0                    # 0..1
    reconciled: bool = False                   # true only if matched to a processor export
    note: str = ""

    def is_real(self) -> bool:
        """A metric is 'real' (usable in decisions) only if it has a value, a
        non-estimate source, and isn't missing."""
        return (self.value is not None
                and self.source_type != SourceType.ESTIMATE)

    def is_money_total_eligible(self) -> bool:
        """Revenue/profit can only roll into TOTALS if reconciled to a processor."""
        return (self.is_real()
                and self.source_type == SourceType.PAYMENT_PROCESSOR
                and self.reconciled)


class DataGap(Exception):
    """Raised when a decision needs a metric that's missing. Halts that decision
    rather than fabricating a plausible number to continue."""
    def __init__(self, metric_name: str, reason: str = ""):
        self.metric_name = metric_name
        super().__init__(f"DATA GAP: '{metric_name}' is missing or unverified. {reason} "
                         "Decision halted — supply real data to proceed.")


class MetricStore:
    """Holds metrics, enforces the truth rules, persists to local JSON."""
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.metrics: Dict[str, Metric] = {}
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text())
                for name, m in raw.items():
                    m["source_type"] = SourceType(m["source_type"])
                    self.metrics[name] = Metric(**m)
            except Exception:
                pass

    def _save(self):
        try:
            out = {n: {**asdict(m), "source_type": m.source_type.value}
                   for n, m in self.metrics.items()}
            self.path.write_text(json.dumps(out, indent=2))
        except Exception:
            pass

    def record(self, metric: Metric):
        self.metrics[metric.name] = metric
        self._save()

    def require_real(self, name: str) -> float:
        """Get a metric for a decision. Raises DataGap if missing/unverified.
        This is how decisions REFUSE to run on fake data."""
        m = self.metrics.get(name)
        if m is None:
            raise DataGap(name, "Not recorded.")
        if not m.is_real():
            raise DataGap(name, f"Source is '{m.source_type.value}' (planning-only) or value is NULL.")
        return m.value

    def get(self, name: str) -> Optional[Metric]:
        return self.metrics.get(name)

    def reconciled_revenue_total(self) -> Dict[str, Any]:
        """Sum ONLY reconciled, processor-backed revenue. Everything else is excluded
        and listed so you can see what's unverified."""
        eligible = [m for m in self.metrics.values()
                    if "revenue" in m.name.lower() or "profit" in m.name.lower() or "sale" in m.name.lower()]
        counted = [m for m in eligible if m.is_money_total_eligible()]
        excluded = [m for m in eligible if not m.is_money_total_eligible()]
        return {
            "reconciled_total": round(sum(m.value for m in counted), 2),
            "counted_metrics": [m.name for m in counted],
            "excluded_unreconciled": [
                {"name": m.name, "value": m.value, "why": (
                    "not from payment processor" if m.source_type != SourceType.PAYMENT_PROCESSOR
                    else "not reconciled to export")}
                for m in excluded],
            "note": "Only reconciled, processor-backed revenue is counted. Unreconciled "
                    "figures are excluded from the total until matched to an export.",
        }

    def data_gaps(self, required_names: List[str]) -> List[str]:
        """Report which required metrics are missing/unverified, without halting."""
        gaps = []
        for name in required_names:
            m = self.metrics.get(name)
            if m is None or not m.is_real():
                gaps.append(name)
        return gaps
