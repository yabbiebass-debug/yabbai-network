#!/usr/bin/env python3
"""
YABBAI DeFi Engine — Prediction Market Strategy (Simulator)

This is the module the YouTube tutorial gestures at — built honestly.

THE ONE RULE THAT MATTERS (straight from the smartest comment on that video):
    EXPECTED VALUE > WIN RATE.
A bot that wins 68% of the time still LOSES money if the losses are bigger than
the wins. So this strategy NEVER bets on confidence alone. It bets only when the
expected value, AFTER fees, is positive by a required margin.

How a prediction market works:
  - A contract for outcome "YES" trades at a price between $0 and $1.
  - That price IS the market's implied probability. $0.64 = market thinks 64% likely.
  - If it resolves YES you get $1.00 per contract; if NO you get $0.
  - So buying YES at $0.64: win +$0.36, lose -$0.64.

Your EDGE only exists if YOUR probability estimate differs from the market price.
  - You think 75%, market says 64% → market underprices YES → positive EV to buy YES.
  - You think 50%, market says 64% → market overprices YES → positive EV to buy NO.
  - You think 64%, market says 64% → NO EDGE → do not bet (this is most of the time).

This strategy is deliberately transparent. The "AI" part (estimating probability)
is pluggable; the EV gate around it is fixed and auditable. That gate is what
separates a real method from a confident-looking demo.
"""

from dataclasses import dataclass
from typing import Dict, Optional, Callable


@dataclass
class PredictionSnapshot:
    """One prediction-market contract's current state."""
    market_id: str
    question: str
    market_price: float        # 0..1, the market-implied probability of YES
    fee_pct: float = 2.0       # round-trip fee assumption
    liquidity_usd: float = 0.0


class PredictionMarketStrategy:
    name = "prediction_ev_v1"

    def __init__(self,
                 prob_estimator: Optional[Callable] = None,
                 min_edge: float = 0.08,          # require ≥8 percentage points of disagreement
                 min_ev_per_dollar: float = 0.05, # require ≥5c expected profit per $1 staked, after fees
                 max_market_price: float = 0.92,  # avoid near-certain contracts (tiny upside, fat tail)
                 min_market_price: float = 0.08):
        """
        prob_estimator: callable(PredictionSnapshot) -> float in [0,1], YOUR estimate.
                        If None, a transparent placeholder is used (see _default_estimate)
                        which is INTENTIONALLY not predictive — it forces you to plug in
                        a real estimate rather than trusting a black box.
        """
        self.prob_estimator = prob_estimator or self._default_estimate
        self.min_edge = min_edge
        self.min_ev_per_dollar = min_ev_per_dollar
        self.max_market_price = max_market_price
        self.min_market_price = min_market_price

    def evaluate(self, snap: PredictionSnapshot) -> Dict:
        """
        Returns a full decision with the EV math shown, so every bet is auditable.
        side: 'yes' | 'no' | None (no bet)
        """
        my_prob = max(0.0, min(1.0, self.prob_estimator(snap)))
        market = snap.market_price
        fee = snap.fee_pct / 100.0

        # Skip illiquid or extreme-priced markets
        if not (self.min_market_price <= market <= self.max_market_price):
            return self._no_bet(snap, my_prob, "Market price outside tradable band.")

        edge = my_prob - market   # positive → YES underpriced; negative → YES overpriced

        if abs(edge) < self.min_edge:
            return self._no_bet(snap, my_prob,
                                f"Edge {edge:+.3f} below minimum {self.min_edge}. "
                                "Market and estimate agree — no bet (this is correct most of the time).")

        # ── EV per $1 staked, AFTER fees ──
        # Buy YES at `market`: contracts per $1 = 1/market; win pays $1 each.
        #   EV = my_prob * (1/market) * 1  - 1   (then minus fees)
        # Buy NO at (1-market): symmetric.
        if edge > 0:
            side = "yes"
            price = market
            win_prob = my_prob
        else:
            side = "no"
            price = 1.0 - market
            win_prob = 1.0 - my_prob

        if price <= 0:
            return self._no_bet(snap, my_prob, "Degenerate price.")

        gross_ev_per_dollar = win_prob * (1.0 / price) - 1.0
        ev_per_dollar = gross_ev_per_dollar - fee   # fees drag every trade

        if ev_per_dollar < self.min_ev_per_dollar:
            return self._no_bet(snap, my_prob,
                                f"EV {ev_per_dollar:+.3f}/$ after fees below minimum "
                                f"{self.min_ev_per_dollar}. Confidence isn't enough — EV gates the bet.")

        return {
            "action": "bet",
            "side": side,
            "my_prob": round(my_prob, 4),
            "market_price": round(market, 4),
            "edge": round(edge, 4),
            "ev_per_dollar": round(ev_per_dollar, 4),
            "reason": (f"{side.upper()} @ {price:.2f}: my est {my_prob:.0%} vs market {market:.0%} "
                       f"(edge {edge:+.0%}), EV {ev_per_dollar:+.2f}/$ after {snap.fee_pct:.0f}% fees."),
        }

    def _no_bet(self, snap, my_prob, why) -> Dict:
        return {"action": "hold", "side": None, "my_prob": round(my_prob, 4),
                "market_price": round(snap.market_price, 4), "ev_per_dollar": None, "reason": why}

    @staticmethod
    def _default_estimate(snap: PredictionSnapshot) -> float:
        """
        PLACEHOLDER — deliberately NOT predictive. It just returns the market price,
        which means it will NEVER find an edge (edge = 0 always → no bets).

        This is on purpose. It forces an honest truth: with no real probability model,
        there is no edge, and the correct number of bets is ZERO. To actually trade,
        you must plug in a real estimator (a Claude prompt that reads news/data, a
        statistical model, your own domain knowledge). The simulator then tells you
        whether that estimator is CALIBRATED — i.e. whether your 64% really happens 64%.
        """
        return snap.market_price
