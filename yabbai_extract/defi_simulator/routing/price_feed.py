#!/usr/bin/env python3
"""
YABBAI DeFi Engine — Price Feed + Indicators (Simulator)

Maintains a rolling price window per token and computes the indicators the
strategy needs (short/long SMA, RSI, volatility).

Two modes:
  LIVE      — pulls real prices from Jupiter (honest data for paper trading)
  SYNTHETIC — generates a realistic random-walk price series for offline testing,
              so you can run the whole engine with no network. Clearly labeled
              synthetic so it is never mistaken for real performance.
"""

import math
import random
from collections import deque
from datetime import datetime, timezone
from typing import Dict, Optional, Deque

from ..routing.routing_engine import MarketSnapshot


class PriceFeed:
    def __init__(self, window: int = 50, mode: str = "synthetic"):
        self.window = window
        self.mode = mode
        self.histories: Dict[str, Deque[float]] = {}
        self._synth_state: Dict[str, float] = {}

    def _ensure(self, mint: str):
        if mint not in self.histories:
            self.histories[mint] = deque(maxlen=self.window)
            self._synth_state[mint] = random.uniform(0.5, 5.0)

    def update_live(self, mint: str, price: float):
        """Feed a real price in (call this from the API layer with Jupiter data)."""
        self._ensure(mint)
        if price and price > 0:
            self.histories[mint].append(price)

    def update_synthetic(self, mint: str, drift: float = 0.0002, vol: float = 0.02):
        """Advance a synthetic price one step (random walk with slight drift)."""
        self._ensure(mint)
        last = self._synth_state[mint]
        shock = random.gauss(drift, vol)
        new_price = max(0.0001, last * (1 + shock))
        self._synth_state[mint] = new_price
        self.histories[mint].append(new_price)
        return new_price

    def snapshot(self, mint: str) -> Optional[MarketSnapshot]:
        h = self.histories.get(mint)
        if not h or len(h) < 15:
            return None
        prices = list(h)
        sma_short = sum(prices[-7:]) / 7
        sma_long = sum(prices[-14:]) / 14
        rsi = self._rsi(prices, 14)
        vol = self._volatility(prices[-14:])
        return MarketSnapshot(
            token_mint=mint, price=prices[-1],
            sma_short=sma_short, sma_long=sma_long, rsi=rsi,
            volatility_pct=vol,
            ts=datetime.now(timezone.utc).isoformat(),
        )

    @staticmethod
    def _rsi(prices, period=14) -> float:
        if len(prices) < period + 1:
            return 50.0
        gains, losses = [], []
        for i in range(-period, 0):
            change = prices[i] - prices[i - 1]
            (gains if change >= 0 else losses).append(abs(change))
        avg_gain = sum(gains) / period if gains else 0.0001
        avg_loss = sum(losses) / period if losses else 0.0001
        rs = avg_gain / avg_loss if avg_loss else 100
        return 100 - (100 / (1 + rs))

    @staticmethod
    def _volatility(prices) -> float:
        if len(prices) < 2:
            return 0.0
        mean = sum(prices) / len(prices)
        variance = sum((p - mean) ** 2 for p in prices) / len(prices)
        return (math.sqrt(variance) / mean * 100.0) if mean else 0.0

    def price_lookup(self, mint: str) -> Optional[float]:
        h = self.histories.get(mint)
        return h[-1] if h else None
