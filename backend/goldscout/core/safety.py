"""
GoldScout real token safety.

Turns "safety score" from a text-heuristic guess into a real, multi-source check:

  1. On-chain authority (Solana RPC, free public endpoint) — is the mint authority
     renounced? is the freeze authority renounced? A live freeze authority means the
     token can freeze your wallet's balance; a live mint authority means supply can be
     inflated. These are the two biggest hard signals and they come straight from chain.
  2. GoPlus token security (free, no key) — honeypot / mutable-metadata / transfer-tax
     style flags, aggregated across their sources.
  3. The existing scam-text heuristics (domain trust, red-flag phrases).
  4. Optional AI second opinion via the network's own router — only as a tie-breaker,
     never as the sole basis for a score.

Every external call has a short timeout and degrades honestly: a source that can't be
reached is reported as "unknown", never silently treated as "safe". A LOW score means
"no hard flags found", NOT "safe to ape". Nothing here moves funds.
"""

import asyncio
import logging
from typing import Optional

import httpx

logger = logging.getLogger("goldscout.safety")

SOLANA_RPC = "https://api.mainnet-beta.solana.com"
GOPLUS_SOL = "https://api.gopluslabs.io/api/v1/solana/token_security"
GOPLUS_EVM = "https://api.gopluslabs.io/api/v1/token_security"
# Jupiter Shield — free on-chain risk signals (lite-api, no key, no rate budget).
# PREFERRED over Tavily wherever it answers the same question: Tavily is capped
# at ~12 scans/month on the Dev tier; /shield is not.
JUP_SHIELD = "https://lite-api.jup.ag/ultra/v1/shield"
TIMEOUT = httpx.Timeout(12.0, connect=6.0)

# shield warning types folded into the risk score. Freeze/mint authority are
# EXCLUDED here — the Solana RPC source already scores those (no double count).
_SHIELD_WEIGHTS = {
    "NOT_VERIFIED": ("warning", 15, "Jupiter Shield: token is not verified"),
    "LOW_ORGANIC_ACTIVITY": ("warning", 20, "Jupiter Shield: low organic trading activity"),
    "NEW_LISTING": ("info", 10, "Jupiter Shield: newly listed token"),
    "HAS_PERMANENT_DELEGATE": ("warning", 35, "Jupiter Shield: permanent delegate can move balances"),
    "TRANSFER_TAX": ("warning", 20, "Jupiter Shield: transfer tax detected"),
}


async def _jupiter_shield(mint: str) -> dict:
    """On-chain risk warnings from Jupiter Shield (zero Tavily credits)."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.get(JUP_SHIELD, params={"mints": mint})
            r.raise_for_status()
            warnings = ((r.json() or {}).get("warnings") or {}).get(mint, [])
        return {"source": "jupiter_shield", "status": "ok", "warnings": warnings}
    except Exception as e:
        return {"source": "jupiter_shield", "status": "unknown", "reason": str(e)}

# GoPlus numeric chain ids for the EVM path
EVM_CHAINS = {"ethereum": "1", "bsc": "56", "polygon": "137", "arbitrum": "42161",
              "base": "8453", "optimism": "10", "avalanche": "43114"}


async def _sol_authorities(mint: str) -> dict:
    """Ask Solana RPC directly. mintAuthority/freezeAuthority == null → renounced."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
               "params": [mint, {"encoding": "jsonParsed"}]}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.post(SOLANA_RPC, json=payload)
            r.raise_for_status()
            info = (((r.json().get("result") or {}).get("value") or {})
                    .get("data") or {}).get("parsed", {}).get("info", {})
        if not info:
            return {"source": "solana_rpc", "status": "unknown", "reason": "mint not found or not an SPL token"}
        return {
            "source": "solana_rpc", "status": "ok",
            "mint_authority_renounced": info.get("mintAuthority") in (None, ""),
            "freeze_authority_renounced": info.get("freezeAuthority") in (None, ""),
            "decimals": info.get("decimals"),
        }
    except Exception as e:
        return {"source": "solana_rpc", "status": "unknown", "reason": str(e)}


async def _goplus(mint: str, chain: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            if chain == "solana":
                r = await c.get(GOPLUS_SOL, params={"contract_addresses": mint})
            else:
                cid = EVM_CHAINS.get(chain)
                if not cid:
                    return {"source": "goplus", "status": "unknown", "reason": f"unsupported chain {chain}"}
                r = await c.get(f"{GOPLUS_EVM}/{cid}", params={"contract_addresses": mint})
            r.raise_for_status()
            result = (r.json() or {}).get("result") or {}
        # result is keyed by address (case varies) — take the first entry
        row = None
        for k, v in result.items():
            if k.lower() == mint.lower():
                row = v
                break
        row = row or (next(iter(result.values()), None))
        if not row:
            return {"source": "goplus", "status": "unknown", "reason": "no record"}
        flags = []
        def truthy(x):
            return str(x) in ("1", "true", "True")
        if truthy(row.get("is_honeypot")):
            flags.append(("honeypot", 100, "GoPlus flags this as a honeypot — you can't sell"))
        if truthy(row.get("transfer_pausable")) or truthy(row.get("freezable", {}).get("status") if isinstance(row.get("freezable"), dict) else row.get("transfer_pausable")):
            flags.append(("pausable", 35, "Transfers can be paused by the owner"))
        if truthy(row.get("is_mintable")) or truthy(row.get("mintable", {}).get("status") if isinstance(row.get("mintable"), dict) else row.get("is_mintable")):
            flags.append(("mintable", 30, "Supply can still be minted"))
        tax = row.get("buy_tax") or row.get("sell_tax")
        try:
            if tax and float(tax) > 0.10:
                flags.append(("high_tax", 25, f"High transfer tax (~{float(tax)*100:.0f}%)"))
        except (TypeError, ValueError):
            pass
        return {"source": "goplus", "status": "ok", "flags": flags, "raw_keys": list(row.keys())[:12]}
    except Exception as e:
        return {"source": "goplus", "status": "unknown", "reason": str(e)}


def _base_score(onchain: dict, goplus: dict, jshield: dict, heuristic_score: int) -> tuple[int, list[str], list[str]]:
    """Merge sources into a 0-100 risk score. Higher = riskier."""
    score = 0
    reasons: list[str] = []
    unknowns: list[str] = []

    if onchain.get("status") == "ok":
        if onchain.get("freeze_authority_renounced") is False:
            score += 45
            reasons.append("Freeze authority is LIVE — the token can freeze your balance (hard flag)")
        if onchain.get("mint_authority_renounced") is False:
            score += 30
            reasons.append("Mint authority is LIVE — supply can be inflated")
        if onchain.get("freeze_authority_renounced") and onchain.get("mint_authority_renounced"):
            reasons.append("Mint and freeze authorities both renounced (good sign, not a guarantee)")
    else:
        unknowns.append(f"on-chain authority: {onchain.get('reason', 'unreachable')}")

    if goplus.get("status") == "ok":
        for _name, weight, msg in goplus.get("flags", []):
            score += weight
            reasons.append(msg)
        if not goplus.get("flags"):
            reasons.append("GoPlus found no listed security flags (not a guarantee)")
    else:
        unknowns.append(f"GoPlus: {goplus.get('reason', 'unreachable')}")

    # Jupiter Shield — on-chain warnings (freeze/mint excluded: RPC already scored them)
    if jshield.get("status") == "ok":
        shield_flags = 0
        for w in jshield.get("warnings", []):
            wtype = (w.get("type") or "").upper()
            if wtype in ("HAS_FREEZE_AUTHORITY", "HAS_MINT_AUTHORITY"):
                continue
            sev, weight, msg = _SHIELD_WEIGHTS.get(
                wtype, ("info", 10, f"Jupiter Shield: {w.get('message') or wtype}"))
            score += weight
            reasons.append(msg)
            shield_flags += 1
        if not jshield.get("warnings"):
            reasons.append("Jupiter Shield reports no warnings (not a guarantee)")
        logger.info(f"goldscout safety: shield-sourced signals folded in "
                    f"({shield_flags} scored flags, tavily_calls=0)")
    else:
        unknowns.append(f"Jupiter Shield: {jshield.get('reason', 'unreachable')}")

    # Fold in the text heuristic (from scam_analysis) at reduced weight
    score += int(heuristic_score * 0.3)

    return min(score, 100), reasons, unknowns


def _level(score: int) -> str:
    if score >= 70:
        return "critical"
    if score >= 45:
        return "high"
    if score >= 20:
        return "medium"
    return "low"


async def _ai_second_opinion(name: str, reasons: list[str], unknowns: list[str]) -> Optional[str]:
    """Only a tie-breaker. Never the sole basis for a score. Silent on failure."""
    try:
        import ai_router
        system = ("You are a crypto-safety reviewer. Given the automated findings on a "
                  "token, give ONE plain sentence of caution or context for a retail user. "
                  "Do not tell them to buy or sell. Do not promise safety.")
        prompt = f"Token: {name}\nFindings: {reasons}\nCouldn't check: {unknowns}"
        out = await ai_router.route_complete(system, prompt, session_id="goldscout-safety")
        return (out or "").strip()[:300] or None
    except Exception:
        return None


async def check_token(mint: str, chain: str = "solana", name: str = "",
                      heuristic_score: int = 0, with_ai: bool = False) -> dict:
    """Full real safety check for one token. Read-only."""
    if chain == "solana":
        onchain, goplus, jshield = await asyncio.gather(
            _sol_authorities(mint), _goplus(mint, chain), _jupiter_shield(mint))
    else:
        onchain = {"source": "solana_rpc", "status": "skipped", "reason": "non-Solana chain"}
        jshield = {"source": "jupiter_shield", "status": "skipped", "reason": "non-Solana chain"}
        goplus = await _goplus(mint, chain)

    score, reasons, unknowns = _base_score(onchain, goplus, jshield, heuristic_score)
    level = _level(score)
    ai_note = await _ai_second_opinion(name or mint, reasons, unknowns) if with_ai else None

    verdict = ("AVOID — hard security flag" if level == "critical"
               else "HIGH RISK — do not touch without deep verification" if level == "high"
               else "CAUTION — some flags, verify independently" if level == "medium"
               else "No hard flags found — this is NOT a safety guarantee; verify yourself")

    return {
        "token_address": mint, "chain": chain, "name": name,
        "risk_score": score, "risk_level": level, "verdict": verdict,
        "reasons": reasons,
        "unchecked": unknowns,          # sources we couldn't reach — never assume safe
        "sources": {"onchain": onchain.get("status"), "goplus": goplus.get("status"),
                    "jupiter_shield": jshield.get("status")},
        "ai_note": ai_note,
        "disclaimer": "LOW risk = no hard flags found, not 'safe'. Nothing here moves funds; "
                      "you verify and act in your own wallet.",
    }
