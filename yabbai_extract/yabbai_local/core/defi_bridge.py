#!/usr/bin/env python3
"""
YABBAI Local -- DeFi Bridge

Connects the unified YABBAI app to the Gold Hunter DeFi backend (real, zero-seeded)
and to live on-chain data. This is the DeFi half of the merge.

HONEST DESIGN:
  - This bridge only READS. It surfaces real vault balance, real net profit, real
    token prices. It does NOT generate income and shows no fabricated numbers.
  - If the Gold Hunter backend isn't running, every panel shows "not connected"
    instead of a fake figure. Honesty over theatre -- the whole reason we stripped
    the old Math.random() yield loop.
  - Money-moving actions (payouts/withdrawals) are NOT exposed here -- those stay
    admin-gated in the Gold Hunter backend itself. This bridge is read-only by design.

Config via env:
  GOLDHUNTER_URL   (default http://localhost:8000)  -- your running Gold Hunter API
  GOLDHUNTER_TOKEN (optional)                        -- admin token for vault reads
"""

import os
from datetime import datetime, timezone
from typing import Dict, List

import httpx

GOLDHUNTER_URL = os.getenv("GOLDHUNTER_URL", "http://localhost:8000")
GOLDHUNTER_TOKEN = os.getenv("GOLDHUNTER_TOKEN", "")
JUPITER_PRICE = "https://lite-api.jup.ag/price/v3"
SOLANA_RPC = "https://api.mainnet-beta.solana.com"


class DefiBridge:
    def __init__(self):
        self.gh_url = GOLDHUNTER_URL.rstrip("/")
        self.token = GOLDHUNTER_TOKEN

    def _gh_headers(self) -> Dict:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def gold_hunter_connected(self) -> bool:
        try:
            r = httpx.get(f"{self.gh_url}/health", timeout=3)
            return r.status_code == 200
        except Exception:
            return False

    def vault_summary(self) -> Dict:
        """Real net profit / vault balance from Gold Hunter. No seeding, no fakes."""
        if not self.gold_hunter_connected():
            return {"connected": False,
                    "message": f"Gold Hunter backend not reachable at {self.gh_url}. "
                               "Start it to see real vault data."}
        try:
            r = httpx.get(f"{self.gh_url}/api/vault/summary", headers=self._gh_headers(), timeout=8)
            r.raise_for_status()
            data = r.json()
            return {"connected": True, "source": "gold_hunter", "real": True, **data}
        except Exception as e:
            return {"connected": True, "error": str(e),
                    "message": "Connected but vault read failed (check admin token)."}

    def treasury_status(self) -> Dict:
        if not self.gold_hunter_connected():
            return {"connected": False}
        try:
            r = httpx.get(f"{self.gh_url}/api/treasury/status", headers=self._gh_headers(), timeout=8)
            r.raise_for_status()
            return {"connected": True, **r.json()}
        except Exception as e:
            return {"connected": True, "error": str(e)}

    def token_price(self, mint: str) -> Dict:
        """Live Jupiter price -- real market data."""
        try:
            r = httpx.get(JUPITER_PRICE, params={"ids": mint},
                          headers={"Accept": "application/json"}, timeout=8)
            r.raise_for_status()
            info = r.json().get("data", {}).get(mint, {})
            price = float(info.get("price", 0))
            return {"ok": True, "mint": mint, "price": price,
                    "ts": datetime.now(timezone.utc).isoformat()}
        except Exception as e:
            return {"ok": False, "error": str(e), "note": "Jupiter unreachable from this host."}

    def sol_balance(self, owner: str) -> Dict:
        """Live SOL balance -- real on-chain data."""
        try:
            r = httpx.post(SOLANA_RPC, json={"jsonrpc": "2.0", "id": 1,
                           "method": "getBalance", "params": [owner]},
                           headers={"Content-Type": "application/json"}, timeout=8)
            lamports = (r.json() or {}).get("result", {}).get("value", 0)
            return {"ok": True, "owner": owner, "sol": lamports / 1e9}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def overview(self) -> Dict:
        """One call the dashboard uses -- everything real, clearly labelled."""
        vault = self.vault_summary()
        return {
            "ts": datetime.now(timezone.utc).isoformat(),
            "gold_hunter_connected": vault.get("connected", False),
            "vault": vault,
            "treasury": self.treasury_status(),
            "honesty_note": "All figures are real (Gold Hunter vault + live chain data) or "
                            "marked 'not connected'. No simulated yield, ever.",
        }
