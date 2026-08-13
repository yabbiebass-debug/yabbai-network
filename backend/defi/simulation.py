"""
DeFi simulation engine — the MANDATORY dry-run stage of the live engine.

Moved from defi_simulator (quote-evaluation, slippage modelling, PnL accounting).
Any live trade path that does not pass through evaluate_quote() is a bug.
No custody, no signing, no broadcasting — pure arithmetic against live prices.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional


# ── slippage / fee friction model (from defi_simulator PaperExecutor) ──────────
SLIPPAGE_PCT = 0.30      # assumed extra slippage per fill on a thin book
FEE_PCT = 0.25           # swap/route fee per fill


@dataclass
class Position:
    token_mint: str
    entry_price: float
    usd_invested: float
    units: float
    opened_at: str
    high_water_price: float = 0.0


@dataclass
class VirtualPortfolio:
    starting_usd: float = 14.0
    cash_usd: float = 14.0
    positions: Dict[str, Position] = field(default_factory=dict)
    realized_pnl_usd: float = 0.0
    trade_count: int = 0

    def total_value(self, price_lookup) -> float:
        v = 0.0
        for pos in self.positions.values():
            price = price_lookup(pos.token_mint) or pos.entry_price
            v += pos.units * price
        return self.cash_usd + v


def apply_friction(price: float, side: str) -> float:
    """Buys fill slightly worse (higher), sells slightly worse (lower)."""
    slip = price * (SLIPPAGE_PCT / 100.0)
    return price + slip if side == "buy" else price - slip


def paper_fill_open(portfolio: VirtualPortfolio, mint: str, usd_amount: float,
                    price: float) -> Dict:
    """PnL accounting: open a simulated position with honest frictions."""
    if not price or price <= 0:
        return {"ok": False, "error": "no price available"}
    usd = min(usd_amount, portfolio.cash_usd)
    if usd <= 0:
        return {"ok": False, "error": "insufficient virtual cash"}
    fill_price = apply_friction(price, "buy")
    fee = usd * (FEE_PCT / 100.0)
    units = (usd - fee) / fill_price
    portfolio.cash_usd -= usd
    portfolio.positions[mint] = Position(mint, fill_price, usd, units,
                                         datetime.now(timezone.utc).isoformat(), fill_price)
    portfolio.trade_count += 1
    return {"ok": True, "side": "buy", "fill_price": round(fill_price, 8),
            "units": round(units, 6), "usd": round(usd, 4), "fee_usd": round(fee, 4)}


def paper_fill_close(portfolio: VirtualPortfolio, mint: str, price: float) -> Dict:
    pos = portfolio.positions.get(mint)
    if not pos:
        return {"ok": False, "error": "no open position"}
    if not price or price <= 0:
        return {"ok": False, "error": "no price available"}
    fill_price = apply_friction(price, "sell")
    gross = pos.units * fill_price
    fee = gross * (FEE_PCT / 100.0)
    net = gross - fee
    realized = net - pos.usd_invested
    portfolio.cash_usd += net
    portfolio.realized_pnl_usd += realized
    portfolio.trade_count += 1
    del portfolio.positions[mint]
    return {"ok": True, "side": "sell", "fill_price": round(fill_price, 8),
            "gross_usd": round(gross, 4), "fee_usd": round(fee, 4),
            "net_usd": round(net, 4), "realized_pnl_usd": round(realized, 4)}


def evaluate_quote(in_amount_base: int, out_amount_base: int,
                   price_impact_pct: float,
                   in_price_usd: Optional[float], out_price_usd: Optional[float],
                   in_decimals: Optional[int], out_decimals: Optional[int]) -> Dict:
    """Quote evaluation: rebuild the expected out-amount from INDEPENDENT live
    prices (Jupiter Price V3) plus the quote's own impact, and measure how far
    the routed quote diverges. divergence_bps=None (fail-open is NOT allowed —
    caller must treat None as blocked) when independent prices are missing."""
    quoted_out = int(out_amount_base or 0)
    if (not in_price_usd or not out_price_usd or in_decimals is None
            or out_decimals is None or quoted_out <= 0 or in_amount_base <= 0):
        return {"ok": False, "divergence_bps": None,
                "reason": "independent price data unavailable — cannot simulate, fail closed"}

    in_ui = in_amount_base / (10 ** in_decimals)
    usd_in = in_ui * in_price_usd
    ideal_out_ui = usd_in / out_price_usd
    try:
        impact = abs(float(price_impact_pct or 0.0))
    except (TypeError, ValueError):
        impact = 0.0
    # impact from the quote + the engine's own friction model
    simulated_out_ui = ideal_out_ui * (1.0 - impact) * (1.0 - FEE_PCT / 100.0)
    simulated_out_base = int(simulated_out_ui * (10 ** out_decimals))
    quoted_out_ui = quoted_out / (10 ** out_decimals)

    mid = max(simulated_out_ui, 1e-18)
    divergence_bps = abs(simulated_out_ui - quoted_out_ui) / mid * 10_000.0

    return {
        "ok": True,
        "usd_in": round(usd_in, 4),
        "ideal_out_ui": round(ideal_out_ui, 10),
        "simulated_out_ui": round(simulated_out_ui, 10),
        "simulated_out_base": simulated_out_base,
        "quoted_out_ui": round(quoted_out_ui, 10),
        "quoted_price_impact_pct": impact,
        "divergence_bps": round(divergence_bps, 1),
        "frictions": {"fee_pct": FEE_PCT, "slippage_model_pct": SLIPPAGE_PCT},
        "price_source": "jupiter price/v3 (independent of the routed quote)",
    }
