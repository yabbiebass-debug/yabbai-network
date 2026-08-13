"""Fail-closed env config for the live DeFi layer (non-negotiable N4).

Missing or unparseable env == the SAFE value. Empty allowlist == nothing allowed.
Every execution flag defaults False. Read at call time so an operator flag flip
needs only a restart, never a code change.
"""

import os
from typing import List

from network_db import get_raw_settings


def env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def env_float(name: str, default: float) -> float:
    v = os.environ.get(name, "")
    try:
        return float(v.strip()) if v and v.strip() else default
    except (TypeError, ValueError):
        return default


def env_int(name: str, default: int) -> int:
    v = os.environ.get(name, "")
    try:
        return int(v.strip()) if v and v.strip() else default
    except (TypeError, ValueError):
        return default


def env_list(name: str) -> List[str]:
    v = os.environ.get(name, "") or ""
    return [x.strip() for x in v.split(",") if x.strip()]


# ── execution master switches — all dark by default ───────────────────────────
def live_enabled() -> bool:        return env_bool("DEFI_LIVE_ENABLED", False)
def trigger_enabled() -> bool:     return env_bool("TRIGGER_ENABLED", False)
def harvest_enabled() -> bool:     return env_bool("HARVEST_ENABLED", False)
def earn_enabled() -> bool:        return env_bool("EARN_ENABLED", False)

# ── defi execution limits ──────────────────────────────────────────────────────
def max_trade_usd() -> float:          return env_float("DEFI_MAX_TRADE_USD", 25.0)
def daily_cap_usd() -> float:          return env_float("DEFI_DAILY_CAP_USD", 100.0)
def max_price_impact_bps() -> int:     return env_int("DEFI_MAX_PRICE_IMPACT_BPS", 100)
def max_quote_divergence_bps() -> int: return env_int("DEFI_MAX_QUOTE_DIVERGENCE_BPS", 50)
def allowed_mints() -> List[str]:      return env_list("DEFI_ALLOWED_MINTS")   # empty = NOTHING tradeable

# ── janitor ────────────────────────────────────────────────────────────────────
def janitor_enabled() -> bool:     return env_bool("JANITOR_ENABLED", True)
def janitor_dust_usd() -> float:   return env_float("JANITOR_DUST_USD", 1.00)
def janitor_never_touch() -> List[str]: return env_list("JANITOR_NEVER_TOUCH")
def janitor_fee_bps() -> int:      return max(0, env_int("JANITOR_FEE_BPS", 0))

# ── sentinel / harvester / trigger ────────────────────────────────────────────
def sentinel_enabled() -> bool:        return env_bool("SENTINEL_ENABLED", True)
def sentinel_liq_drop_pct() -> float:  return env_float("SENTINEL_LIQ_DROP_PCT", 40.0)
def sentinel_depeg_bps() -> int:       return env_int("SENTINEL_DEPEG_BPS", 100)
def harvest_max_candidates() -> int:   return max(1, env_int("HARVEST_MAX_CANDIDATES", 25))
def trigger_max_open_orders() -> int:  return max(0, env_int("TRIGGER_MAX_OPEN_ORDERS", 5))
def trigger_max_locked_usd() -> float: return env_float("TRIGGER_MAX_LOCKED_USD", 100.0)

# ── earn ───────────────────────────────────────────────────────────────────────
def earn_allowed_protocols() -> List[str]: return env_list("EARN_ALLOWED_PROTOCOLS")  # empty = no deposits
def earn_max_deposit_usd() -> float:   return env_float("EARN_MAX_DEPOSIT_USD", 250.0)
def earn_max_total_usd() -> float:     return env_float("EARN_MAX_TOTAL_USD", 1000.0)
def earn_min_tvl_usd() -> float:       return env_float("EARN_MIN_TVL_USD", 50_000_000.0)
def earn_apy_sanity_pct() -> float:    return env_float("EARN_APY_SANITY_PCT", 20.0)


def rpc_url() -> str:
    return (os.environ.get("SOLANA_RPC_URL", "") or "").strip() or "https://api.mainnet-beta.solana.com"


async def jupiter_key() -> str:
    """JUPITER_API_KEY env always wins; /settings (Mongo) is the fallback.
    Acceptable ONLY because this key holds no signing authority (quotes + relaying
    already-signed transactions). Never extend to credentials that can move funds."""
    env = (os.environ.get("JUPITER_API_KEY", "") or "").strip()
    if env:
        return env
    s = await get_raw_settings()
    return (s.get("jupiter_api_key") or "").strip()
