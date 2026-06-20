"""
YABBAI Revenue System — Products Channel (Gumroad / Etsy / digital storefronts)

The honest money path: a real listing, a real buyer, a real payment. Income is
booked ONLY when a payment-processor webhook/export confirms a sale. There is no
code path here that invents a sale or a price.

Agents in this channel can DRAFT product ideas, DRAFT listing copy, and DRAFT
price tests — but publishing (listing_live) is HIGH and queues for a human, and
income only appears when the storefront confirms it.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from ...truth import Metric, SourceType, TruthLedger
from ...gates import GateController, ProposedAction
from ...memory import Memory, Learning


# Idempotency: never book the same confirmed sale twice.
_SALE_CACHE: set = set()


class ProductsChannel:
    def __init__(self, ledger: TruthLedger, gates: GateController, memory: Memory,
                 storefront_url: str = ""):
        self.ledger = ledger
        self.gates = gates
        self.memory = memory
        self.storefront_url = storefront_url
        self.gates.register_executor("products", "listing_live",
                                     self._execute_listing_publish)

    # ── observe: pull real sales from storefront API/export ─────────────────
    def observe(self, fetch_sales: Optional[Callable[[], List[Dict]]] = None
                ) -> List[Metric]:
        """
        fetch_sales: a callable returning confirmed sales from the storefront,
        e.g. Gumroad/Etsy API or a parsed export. Each sale:
          {sale_id, product, gross_usd, fee_usd, currency, ts}
        Returns REAL income Metrics. No fetcher => DATA GAP (no invented sales).
        """
        if fetch_sales is None:
            # No live fetcher wired — surface as a data gap, do NOT invent sales.
            return [Metric(name="income", value=0.0,
                           source="products:no_fetcher", source_type=SourceType.ESTIMATE,
                           confidence=0.0, notes="DATA GAP: no storefront fetcher wired.")]
        try:
            sales = fetch_sales() or []
        except Exception as e:
            return [Metric(name="income", value=0.0,
                           source=f"products:fetch_error:{e}", source_type=SourceType.ESTIMATE,
                           confidence=0.0, notes="DATA GAP: storefront fetch failed.")]
        metrics: List[Metric] = []
        for s in sales:
            sid = s.get("sale_id") or s.get("id")
            if not sid or sid in _SALE_CACHE:
                continue
            _SALE_CACHE.add(sid)
            net = float(s.get("gross_usd", 0)) - float(s.get("fee_usd", 0))
            metrics.append(Metric(
                name="income", value=max(0.0, net),
                source=f"storefront:{sid}", source_type=SourceType.PAYMENT_PROCESSOR,
                reconciled=True, signed_by="products_channel",
                unit=s.get("currency", "usd"),
                notes=f"{s.get('product','product')} sale"))
            self.memory.add("products", {
                "ts": s.get("ts") or datetime.now(timezone.utc).isoformat(),
                "sale_id": sid, "product": s.get("product"),
                "gross_usd": s.get("gross_usd"), "fee_usd": s.get("fee_usd"),
                "net_usd": round(net, 2)})
            self.memory.add_learning(Learning(
                kind="win", channel="products", agent="products_channel",
                summary=f"Sale: {s.get('product')} ${net:.2f} net", usd_impact=net))
        return metrics

    # ── propose: draft new products / price tests (publishing is HIGH) ───────
    def propose_product_draft(self, product: str, price_usd: float,
                              rationale: str, draft_copy: str = "") -> ProposedAction:
        """A draft is LOW cost (reversible). Going LIVE is HIGH via listing_live."""
        return ProposedAction(
            kind="listing_live", channel="products", agent="products_channel",
            usd_amount=0.0, reversible=False,
            payload={"product": product, "price_usd": price_usd,
                     "rationale": rationale, "draft_copy": draft_copy[:2000]},
            reason=f"Propose listing '{product}' @ ${price_usd:.2f}. {rationale}",
            ev={"p_success": 0.35, "upside_usd": price_usd * 30,
                "cost_usd": 0.0, "risk_adjustment": 0.0})

    def propose(self) -> List[ProposedAction]:
        """Default proposer: surface stored product drafts as publish actions."""
        drafts = self.memory.find("products", lambda r: r.get("status") == "draft")
        out: List[ProposedAction] = []
        for d in drafts[:5]:
            out.append(self.propose_product_draft(
                d.get("product", "draft"), d.get("price_usd", 9.0),
                d.get("rationale", "queued draft"), d.get("draft_copy", "")))
        return out

    # ── act: publish only after human approval ──────────────────────────────
    def _execute_listing_publish(self, action: ProposedAction) -> Dict[str, Any]:
        """Publishing is gated HIGH. The actual storefront API call lives in a
        storefront client YOU inject (not shipped) — we record intent + outcome
        in memory either way. There is no fake 'published: true' here."""
        product = action.payload.get("product", "")
        # In production, call the injected Gumroad/Etsy client here. Without one,
        # we mark it as 'queued_for_storefront_client' rather than pretending.
        self.memory.add("products", {
            "ts": datetime.now(timezone.utc).isoformat(),
            "product": product, "price_usd": action.payload.get("price_usd"),
            "status": "approved_pending_storefront_client",
            "rationale": action.payload.get("rationale")})
        return {"ok": True, "status": "approved_pending_storefront_client",
                "product": product,
                "note": "Wire a Gumroad/Etsy API client to complete publish. "
                        "Income books only when a real sale confirms."}
