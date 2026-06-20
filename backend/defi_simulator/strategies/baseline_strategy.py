#!/usr/bin/env python3
"""
YABBAI DeFi Engine — Baseline Strategy (Simulator)

A transparent, auditable strategy. You can read every line and know exactly why
it buys or sells. This is deliberate: an honest system uses rules you can inspect,
not a black box that claims to predict the market.

This implements a classic momentum + mean-reversion hybrid:
  BUY  when short MA crosses above long MA (uptrend forming) AND RSI not overbought
  SELL when short MA crosses below long MA (trend breaking) OR RSI overbought

This is a STARTING POINT, not a money printer. Run it in the simulator for weeks.
The paper curve will tell you honestly whether it beats just holding — most simple
strategies don't, after fees. That lesson is worth more than any promise.
"""

from typing import Dict, Optional
from ..routing.routing_engine import MarketSnapshot


class MomentumMeanReversionStrategy:
    name = "momentum_mean_reversion_v1"

    def __init__(self, rsi_overbought: float = 70.0, rsi_oversold: float = 30.0,
                 min_confidence: float = 0.55):
        self.rsi_overbought = rsi_overbought
        self.rsi_oversold = rsi_oversold
        self.min_confidence = min_confidence

    def signal(self, snap: MarketSnapshot, has_position: bool) -> Optional[Dict]:
        trend_up = snap.sma_short > snap.sma_long
        trend_down = snap.sma_short < snap.sma_long
        spread = abs(snap.sma_short - snap.sma_long) / snap.sma_long if snap.sma_long else 0

        # Confidence scales with how decisive the MA spread is, capped at 1.0
        confidence = min(1.0, 0.5 + spread * 10)

        if not has_position:
            # Enter on uptrend that isn't already overbought
            if trend_up and snap.rsi < self.rsi_overbought and confidence >= self.min_confidence:
                return {"action": "buy", "confidence": round(confidence, 2),
                        "reason": f"Uptrend (MA{snap.sma_short:.4f}>{snap.sma_long:.4f}), "
                                  f"RSI {snap.rsi:.0f} below overbought."}
            # Mean-reversion buy: heavily oversold
            if snap.rsi < self.rsi_oversold:
                return {"action": "buy", "confidence": 0.6,
                        "reason": f"Oversold bounce setup (RSI {snap.rsi:.0f})."}
        else:
            # Exit on trend break or overbought (stop-loss/take-profit handled by the gate)
            if trend_down:
                return {"action": "sell", "confidence": round(confidence, 2),
                        "reason": f"Trend broke (MA{snap.sma_short:.4f}<{snap.sma_long:.4f})."}
            if snap.rsi > self.rsi_overbought:
                return {"action": "sell", "confidence": 0.6,
                        "reason": f"Overbought (RSI {snap.rsi:.0f}), taking profit on signal."}

        return None  # hold
