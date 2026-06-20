#!/usr/bin/env python3
"""
YABBAI DeFi Engine — Paper Executor (Simulator ONLY)

Simulates trade fills against live market prices. Tracks a virtual portfolio.
NO real funds. NO real transactions. NO wallet connection. There is deliberately
no code path here that touches a private key or broadcasts a transaction — that
isolation is the whole point of the simulator being a separate deployment.

This is what makes the "$14 and see profit overnight" question answerable
HONESTLY: run it on paper for weeks, and the portfolio curve tells you the truth
about whether the strategy makes money — before a cent is risked.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional


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
    starting_usd: float = 14.0          # mirrors the real question: start small
    cash_usd: float = 14.0
    positions: Dict[str, Position] = field(default_factory=dict)
    realized_pnl_usd: float = 0.0
    trade_count: int = 0

    def total_value(self, price_lookup) -> float:
        """Cash + mark-to-market value of all open positions."""
        positions_value = 0.0
        for pos in self.positions.values():
            price = price_lookup(pos.token_mint) or pos.entry_price
            positions_value += pos.units * price
        return self.cash_usd + positions_value


class PaperExecutor:
    """
    Fills are simulated with realistic frictions so the paper curve is HONEST:
      - slippage (price impact on a tiny order book)
      - swap fee (Jupiter/DEX-style)
    Without these, paper trading lies and tells you you're profitable when fees
    would have eaten you alive — exactly the trap on a $14 account.
    """

    SLIPPAGE_PCT = 0.30      # 0.3% assumed slippage per fill
    FEE_PCT = 0.25           # 0.25% swap/route fee per fill

    def __init__(self, portfolio: VirtualPortfolio, price_lookup):
        self.portfolio = portfolio
        self.price_lookup = price_lookup   # fn(mint) -> current price USD
        self.fills: List[Dict] = []

    def __call__(self, action) -> Dict:
        """ModeController calls this as the executor."""
        if action.action_type in ("buy", "swap"):
            return self._open(action)
        elif action.action_type == "sell":
            return self._close(action)
        return {"ok": False, "error": f"unknown action {action.action_type}"}

    def _apply_friction(self, price: float, side: str) -> float:
        """Buys fill slightly worse (higher), sells slightly worse (lower)."""
        slip = price * (self.SLIPPAGE_PCT / 100.0)
        return price + slip if side == "buy" else price - slip

    def _open(self, action) -> Dict:
        price = self.price_lookup(action.token_mint)
        if not price or price <= 0:
            return {"ok": False, "error": "no price available"}

        usd = min(action.usd_amount, self.portfolio.cash_usd)
        if usd <= 0:
            return {"ok": False, "error": "insufficient virtual cash"}

        fill_price = self._apply_friction(price, "buy")
        fee = usd * (self.FEE_PCT / 100.0)
        net_usd = usd - fee
        units = net_usd / fill_price

        self.portfolio.cash_usd -= usd
        self.portfolio.positions[action.token_mint] = Position(
            token_mint=action.token_mint, entry_price=fill_price,
            usd_invested=usd, units=units,
            opened_at=datetime.now(timezone.utc).isoformat(),
            high_water_price=fill_price,
        )
        self.portfolio.trade_count += 1

        fill = {"ok": True, "side": "buy", "token": action.token_mint[:12],
                "fill_price": round(fill_price, 8), "units": round(units, 6),
                "usd": round(usd, 4), "fee_usd": round(fee, 4),
                "opened": True, "realized_pnl_usd": 0.0,
                "ts": datetime.now(timezone.utc).isoformat()}
        self.fills.append(fill)
        return fill

    def _close(self, action) -> Dict:
        pos = self.portfolio.positions.get(action.token_mint)
        if not pos:
            return {"ok": False, "error": "no open position"}

        price = self.price_lookup(action.token_mint)
        if not price or price <= 0:
            return {"ok": False, "error": "no price available"}

        fill_price = self._apply_friction(price, "sell")
        gross = pos.units * fill_price
        fee = gross * (self.FEE_PCT / 100.0)
        net = gross - fee
        realized = net - pos.usd_invested

        self.portfolio.cash_usd += net
        self.portfolio.realized_pnl_usd += realized
        self.portfolio.trade_count += 1
        del self.portfolio.positions[action.token_mint]

        fill = {"ok": True, "side": "sell", "token": action.token_mint[:12],
                "fill_price": round(fill_price, 8), "gross_usd": round(gross, 4),
                "fee_usd": round(fee, 4), "net_usd": round(net, 4),
                "realized_pnl_usd": round(realized, 4),
                "closed": True, "ts": datetime.now(timezone.utc).isoformat()}
        self.fills.append(fill)
        return fill

    def snapshot(self) -> Dict:
        total = self.portfolio.total_value(self.price_lookup)
        pnl = total - self.portfolio.starting_usd
        pnl_pct = (pnl / self.portfolio.starting_usd * 100.0) if self.portfolio.starting_usd else 0.0
        return {
            "starting_usd": self.portfolio.starting_usd,
            "cash_usd": round(self.portfolio.cash_usd, 4),
            "total_value_usd": round(total, 4),
            "unrealized_plus_realized_pnl_usd": round(pnl, 4),
            "pnl_pct": round(pnl_pct, 2),
            "realized_pnl_usd": round(self.portfolio.realized_pnl_usd, 4),
            "open_positions": len(self.portfolio.positions),
            "trade_count": self.portfolio.trade_count,
        }
