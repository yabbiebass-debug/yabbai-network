#!/usr/bin/env python3
"""
YABBAI DeFi Engine — Live Price Adapter (Simulator)

Pulls REAL market prices from Jupiter's public price API and feeds them into the
same PriceFeed the strategy already uses. This is the bridge that turns the
simulator from "synthetic random walk" into "paper trading against the actual
live market."

Critical honesty point: the prices are real, the TRADES are still paper. This
adapter only READS public price data. It has no key, no wallet, no ability to
transact. You get a truthful answer to "would my strategy have made money in the
real market right now?" without risking a cent.

Used by the API loop: every tick calls refresh() to pull the latest real price,
then the engine runs its decision cycle on real data.
"""

import asyncio
from datetime import datetime, timezone
from typing import Dict, List, Optional

import httpx

JUPITER_PRICE_URL = "https://lite-api.jup.ag/price/v3"


class LivePriceAdapter:
    def __init__(self, price_feed, tokens: List[str]):
        """
        price_feed: the PriceFeed instance to push real prices into
        tokens: list of mint addresses to track (the session allowlist)
        """
        self.feed = price_feed
        self.tokens = tokens
        self.last_prices: Dict[str, float] = {}
        self.last_refresh: Optional[str] = None
        self.error_count = 0
        self.consecutive_errors = 0

    async def refresh(self) -> Dict:
        """
        Fetch the latest real prices for all tracked tokens and push into the feed.
        Resilient: a failed fetch keeps the last good price and increments an error
        counter rather than crashing the loop (same stale-data-preservation rule as
        the frontend).
        """
        ids = ",".join(self.tokens)
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.get(JUPITER_PRICE_URL, params={"ids": ids},
                                        headers={"Accept": "application/json"})
                resp.raise_for_status()
                data = resp.json().get("data", {})

            updated = {}
            for mint in self.tokens:
                info = data.get(mint) or {}
                price = info.get("price")
                if price and float(price) > 0:
                    p = float(price)
                    self.feed.update_live(mint, p)
                    self.last_prices[mint] = p
                    updated[mint] = p
                elif mint in self.last_prices:
                    # Keep stale price — do not blank
                    self.feed.update_live(mint, self.last_prices[mint])

            self.last_refresh = datetime.now(timezone.utc).isoformat()
            self.consecutive_errors = 0
            return {"ok": True, "updated": updated, "ts": self.last_refresh}

        except Exception as e:
            self.error_count += 1
            self.consecutive_errors += 1
            # Re-feed last known prices so indicators keep computing on stale-but-valid data
            for mint, p in self.last_prices.items():
                self.feed.update_live(mint, p)
            return {"ok": False, "error": str(e),
                    "consecutive_errors": self.consecutive_errors,
                    "using_stale": bool(self.last_prices)}

    async def warmup(self, samples: int = 16, interval: float = 1.0) -> Dict:
        """
        Prime the indicator window with real prices before trading starts.
        Jupiter price doesn't give history, so we sample the live price a number
        of times to build an initial window. (For production you'd backfill from
        a candle API; for the simulator, live sampling is honest enough.)
        """
        ok = 0
        for _ in range(samples):
            r = await self.refresh()
            if r.get("ok"):
                ok += 1
            await asyncio.sleep(interval)
        return {"warmed": ok, "requested": samples,
                "ready": ok >= 14, "last_prices": self.last_prices}

    def status(self) -> Dict:
        return {
            "tokens": self.tokens,
            "last_prices": self.last_prices,
            "last_refresh": self.last_refresh,
            "total_errors": self.error_count,
            "consecutive_errors": self.consecutive_errors,
            "healthy": self.consecutive_errors < 5,
        }
