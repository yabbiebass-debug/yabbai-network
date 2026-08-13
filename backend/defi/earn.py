"""
Earn — sourced yield markets + deposit/withdraw builds (client-signed).

Markets carry SOURCED apy + timestamp + the literal label "variable — not
guaranteed", named risk flags, and a shield verdict on the receipt token.
Outlier APY (> EARN_APY_SANITY_PCT) is EXCLUDED and logged — outlier yield is a
scam signal, not an opportunity. Deposits are dark by default; withdrawals work
with EVERYTHING off (N4: exits are never gated). Accrued yield is UNREALIZED —
it never touches the ledger; realized yield books only via
record_settled_income(rail="solana").

Lend build/redeem: Jupiter Lend API (verified live 2026-08-12):
  GET  https://lite-api.jup.ag/lend/v1/earn/tokens
  GET  https://lite-api.jup.ag/lend/v1/earn/positions?users=…
  POST https://lite-api.jup.ag/lend/v1/earn/mint-instructions   {asset, signer, shares}
  POST https://lite-api.jup.ag/lend/v1/earn/redeem-instructions {asset, signer, shares}
"""

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from network_db import db
from revenue_system.defi_backend_patched.settlement import (
    record_settled_income, SettlementVerificationError)
from . import config, jupiter
from .authz import require_user, rpc_call

logger = logging.getLogger("defi.earn")
router = APIRouter(prefix="/api/defi/earn", tags=["defi-earn"])

LST_PROJECTS = {"marinade-liquid-staking": "mSOL", "jito-liquid-staking": "JitoSOL",
                "sanctum-validator-lsts": "LST", "blazestake": "bSOL"}
RISK_FLAGS = {
    "lend": ["smart_contract", "utilization"],
    "lst": ["smart_contract", "depeg", "unstake_delay"],
    "native_stake": ["validator_concentration", "unstake_delay"],
}
APY_LABEL = "variable — not guaranteed"


async def _lend_markets(now: str, excluded: List[Dict]) -> List[Dict]:
    res = await jupiter.lend_get("earn/tokens", {})
    if res.get("http_status") != 200:
        return []
    rows = res.get("data") or []
    receipt_mints = [r.get("address") for r in rows if r.get("address")]
    sh = await jupiter.shield(receipt_mints)
    warnings = sh.get("warnings") or {}
    shield_ok = not sh.get("error")
    out = []
    for r in rows:
        try:
            # supplyRate is bps-scaled per the API payload (rate * 100)
            apy = float(r.get("totalRate") or r.get("supplyRate") or 0) / 100.0
        except (TypeError, ValueError):
            continue
        asset = r.get("asset") or {}
        try:
            price = float(asset.get("price") or 0)
            tvl = int(r.get("totalAssets") or 0) / (10 ** int(asset.get("decimals") or 0)) * price
        except (TypeError, ValueError):
            tvl = 0
        entry = {"kind": "lend", "protocol": "jupiter-lend",
                 "symbol": asset.get("symbol"), "receipt_token": r.get("address"),
                 "receipt_symbol": r.get("symbol"), "asset_mint": asset.get("address"),
                 "apy_pct": round(apy, 3), "apy_source": "jupiter lend api (supplyRate)",
                 "apy_label": APY_LABEL, "apy_ts": now, "tvl_usd": round(tvl),
                 "risk_flags": RISK_FLAGS["lend"],
                 "shield_verdict": (jupiter.shield_verdict(warnings.get(r.get("address"), []))
                                    if shield_ok else "unknown")}
        if tvl < config.earn_min_tvl_usd():
            excluded.append({**entry, "excluded_because": f"TVL ${tvl:,.0f} < EARN_MIN_TVL_USD"})
            continue
        if apy > config.earn_apy_sanity_pct():
            excluded.append({**entry, "excluded_because":
                             f"APY {apy:.1f}% > sanity {config.earn_apy_sanity_pct()}% — outlier yield is a scam signal"})
            logger.warning(f"earn: EXCLUDED outlier APY {apy:.1f}% on {asset.get('symbol')}")
            continue
        if entry["shield_verdict"] in ("caution", "unknown"):
            excluded.append({**entry, "excluded_because": f"shield verdict {entry['shield_verdict']}"})
            continue
        out.append(entry)
    return out


LST_MINTS = {  # issuer-doc verified (Marinade, Jito, BlazeStake, Sanctum)
    "mSOL": "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",
    "JitoSOL": "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn",
    "bSOL": "bSo13r4TkiE4KumL71LsHTPpL2euBYLFx6h9HP3piy1",
    "INF": "5oVNBeEEQvYi1cX3ir8Dx5n1P7pdxydbGF2X4TxVusJm",
}


async def _lst_markets(now: str, excluded: List[Dict]) -> List[Dict]:
    pools = await jupiter.llama_pools()
    _mints = [m for m in (LST_MINTS.get(LST_PROJECTS.get(p.get("project"))) for p in pools
                          if (p.get("chain") or "").lower() == "solana"
                          and p.get("project") in LST_PROJECTS) if m]
    _sh = await jupiter.shield(list(set(_mints))) if _mints else {}
    _warn = _sh.get("warnings") or {}
    _sh_ok = not _sh.get("error")
    out = []
    for p in pools:
        if (p.get("chain") or "").lower() != "solana":
            continue
        if p.get("project") not in LST_PROJECTS:
            continue
        apy = p.get("apy")
        tvl = p.get("tvlUsd") or 0
        _mint = LST_MINTS.get(LST_PROJECTS.get(p.get("project")))
        entry = {"kind": "lst", "protocol": p.get("project"), "symbol": p.get("symbol"),
                 "receipt_mint": _mint,
                 "apy_pct": apy, "apy_source": "defillama pools api",
                 "apy_label": APY_LABEL, "apy_ts": now, "tvl_usd": tvl,
                 "risk_flags": RISK_FLAGS["lst"],
                 "shield_verdict": (jupiter.shield_verdict(_warn.get(_mint, []))
                                    if (_sh_ok and _mint) else "unknown"),
                 "pool_id": p.get("pool")}
        if apy is None:
            continue
        if tvl < config.earn_min_tvl_usd():
            excluded.append({**entry, "excluded_because": f"TVL ${tvl:,.0f} < EARN_MIN_TVL_USD"})
            continue
        if apy > config.earn_apy_sanity_pct():
            excluded.append({**entry, "excluded_because":
                             f"APY {apy:.1f}% > sanity — outlier yield is a scam signal"})
            continue
        out.append(entry)
    return out


@router.get("/markets")
async def markets():
    now = datetime.now(timezone.utc).isoformat()
    excluded: List[Dict] = []
    lend = await _lend_markets(now, excluded)
    lst = await _lst_markets(now, excluded)
    return {"markets": lend + lst,
            "excluded_count": len(excluded),
            "excluded": excluded[:20],
            "kinds_note": "native_stake rates have no verifiable public API source wired yet — "
                          "that kind is omitted rather than estimated (N5)",
            "deposits_enabled": config.earn_enabled(),
            "allowed_protocols": config.earn_allowed_protocols()}


@router.get("/positions")
async def positions(wallet: str, request: Request, authorization: Optional[str] = Header(None)):
    await require_user(request, authorization)
    res = await jupiter.lend_get("earn/positions", {"users": wallet})
    rows = res.get("data") if res.get("http_status") == 200 else None
    out = []
    for p in rows or []:
        tok = p.get("token") or {}
        out.append({"protocol": "jupiter-lend", "receipt_token": tok.get("address"),
                    "symbol": tok.get("symbol"), "shares": p.get("shares"),
                    "underlying_assets": p.get("underlyingAssets"),
                    "underlying_balance": p.get("underlyingBalance"),
                    "accrual_label": "UNREALIZED — accrued yield is not income and never "
                                     "touches the ledger until realized on withdrawal"})
    return {"wallet": wallet, "positions": out,
            "source_ok": res.get("http_status") == 200}


class EarnBuildBody(BaseModel):
    wallet: str
    protocol: str            # must be in EARN_ALLOWED_PROTOCOLS for deposits
    asset_mint: str
    shares: str              # base units
    action: str              # "deposit" | "withdraw"
    usd_value: float = 0.0   # client hint; server re-prices independently


@router.post("/build")
async def build(body: EarnBuildBody, request: Request, authorization: Optional[str] = Header(None)):
    await require_user(request, authorization)
    if body.action not in ("deposit", "withdraw"):
        raise HTTPException(400, "action must be deposit or withdraw")

    if body.action == "deposit":
        # deposits: every gate must open. withdrawals below skip ALL of these (exits never gated).
        if not config.earn_enabled():
            raise HTTPException(403, "EARN_ENABLED=false — deposits are dark")
        if body.protocol not in config.earn_allowed_protocols():
            raise HTTPException(403, f"protocol '{body.protocol}' not in EARN_ALLOWED_PROTOCOLS "
                                     f"(empty = no deposits anywhere)")
        pm = await jupiter.prices([body.asset_mint])
        p = pm.get(body.asset_mint) or {}
        if not p.get("usdPrice") or p.get("decimals") is None:
            raise HTTPException(503, "no independent price for the asset — fail closed")
        usd = int(body.shares) / (10 ** p["decimals"]) * p["usdPrice"]
        if usd > config.earn_max_deposit_usd():
            raise HTTPException(403, f"deposit ${usd:.2f} exceeds EARN_MAX_DEPOSIT_USD "
                                     f"${config.earn_max_deposit_usd():.2f}")
        # total exposure computed server-side from LIVE positions
        pos = await jupiter.lend_get("earn/positions", {"users": body.wallet})
        total = 0.0
        for row in (pos.get("data") or []) if pos.get("http_status") == 200 else []:
            tok_asset = ((row.get("token") or {}).get("asset") or {})
            try:
                dec = int(tok_asset.get("decimals") or 0)
                total += int(row.get("underlyingAssets") or 0) / (10 ** dec) * float(tok_asset.get("price") or 0)
            except (TypeError, ValueError):
                pass
        if total + usd > config.earn_max_total_usd():
            raise HTTPException(403, f"total earn exposure ${total:.2f} + ${usd:.2f} exceeds "
                                     f"EARN_MAX_TOTAL_USD ${config.earn_max_total_usd():.2f}")
        endpoint = "earn/mint-instructions"
    else:
        endpoint = "earn/redeem-instructions"   # works with EVERYTHING off

    res = await jupiter.lend_post(endpoint, {"asset": body.asset_mint,
                                             "signer": body.wallet, "shares": body.shares})
    if res.get("http_status") != 200:
        raise HTTPException(503, f"lend {endpoint} failed (HTTP {res.get('http_status')}): "
                                 f"{res.get('error') or res.get('data')}")
    market = None
    for m in (await _lend_markets(datetime.now(timezone.utc).isoformat(), [])):
        if m.get("asset_mint") == body.asset_mint:
            market = m
            break
    return {"ok": True, "action": body.action, "instructions": res.get("data"),
            "confirmation": {
                "protocol": body.protocol, "asset_mint": body.asset_mint,
                "shares": body.shares,
                "apy_pct": (market or {}).get("apy_pct"),
                "apy_source": (market or {}).get("apy_source"),
                "apy_label": APY_LABEL,
                "risk_flags": (market or {}).get("risk_flags", RISK_FLAGS["lend"]),
                "unstake_delay": "jupiter-lend redemptions settle per protocol conditions",
                "network_fee": "standard Solana tx fee, shown in Phantom pre-signature"},
            "note": "sign and broadcast in Phantom; this backend cannot sign"}


class EarnSubmitBody(BaseModel):
    wallet: str
    signature: str
    action: str                      # deposit | withdraw | claim
    realized_yield_usd: float = 0.0  # only meaningful on withdraw/claim


async def _onchain_received_usd(tx: Dict, wallet: str) -> float:
    """Upper bound of what this wallet verifiably RECEIVED in this tx, priced
    independently. Unpriceable tokens contribute nothing (conservative, N5)."""
    meta = tx.get("meta") or {}
    keys = [str(k.get("pubkey") if isinstance(k, dict) else k)
            for k in (tx.get("transaction", {}).get("message", {}).get("accountKeys") or [])]
    received = 0.0
    if wallet in keys:
        idx = keys.index(wallet)
        sol_delta = (meta.get("postBalances", [])[idx] - meta.get("preBalances", [])[idx]) / 1e9
        if sol_delta > 0:
            pm = await jupiter.prices(["So11111111111111111111111111111111111111112"])
            p = (pm.get("So11111111111111111111111111111111111111112") or {}).get("usdPrice")
            if p:
                received += sol_delta * p
    pre = {(b.get("mint"), b.get("accountIndex")): b for b in meta.get("preTokenBalances") or []
           if b.get("owner") == wallet}
    post = [b for b in meta.get("postTokenBalances") or [] if b.get("owner") == wallet]
    mints = list({b.get("mint") for b in post})
    pm = await jupiter.prices(mints) if mints else {}
    for b in post:
        before = ((pre.get((b.get("mint"), b.get("accountIndex"))) or {})
                  .get("uiTokenAmount") or {}).get("uiAmount") or 0
        after = (b.get("uiTokenAmount") or {}).get("uiAmount") or 0
        delta = (after or 0) - (before or 0)
        price = (pm.get(b.get("mint")) or {}).get("usdPrice")
        if delta > 0 and price:
            received += delta * price
    return round(received, 2)


@router.post("/submit")
async def submit(body: EarnSubmitBody, request: Request, authorization: Optional[str] = Header(None)):
    """Record a client-broadcast earn tx. Realized yield (withdraw/claim only)
    books via record_settled_income — and NEVER more than the on-chain receipt
    this wallet verifiably got in this exact transaction. Accrual never books."""
    await require_user(request, authorization)
    res = await rpc_call("getTransaction", [body.signature, {
        "encoding": "json", "commitment": "confirmed", "maxSupportedTransactionVersion": 0}])
    if not res:
        raise HTTPException(404, "signature not found on-chain (yet)")
    if (res.get("meta") or {}).get("err") is not None:
        raise HTTPException(400, "transaction failed on-chain")
    keys = [str(k.get("pubkey") if isinstance(k, dict) else k)
            for k in (res.get("transaction", {}).get("message", {}).get("accountKeys") or [])]
    if body.wallet not in keys:
        raise HTTPException(403, "wallet is not a party to this transaction")
    await db.defi_earn_events.insert_one({
        "wallet": body.wallet, "action": body.action, "signature": body.signature,
        "ts": datetime.now(timezone.utc)})
    booked = False
    if body.action in ("withdraw", "claim") and body.realized_yield_usd > 0:
        received = await _onchain_received_usd(res, body.wallet)
        if body.realized_yield_usd > received:
            raise HTTPException(400, f"claimed yield ${body.realized_yield_usd:.2f} exceeds the "
                                     f"verifiable on-chain receipt ${received:.2f} in this tx — "
                                     f"refusing to book (no fabricated income)")
        try:
            await record_settled_income(
                amount=round(body.realized_yield_usd, 2), currency="USD", rail="solana",
                settlement_ref=body.signature, source=f"earn {body.action}",
                notes=f"realized yield at withdrawal (on-chain receipt bound ${received:.2f})",
                expected_party=body.wallet)
            booked = True
        except SettlementVerificationError as e:
            raise HTTPException(400, f"settlement verification failed: {e}")
    return {"ok": True, "recorded": True, "income_booked": booked}
