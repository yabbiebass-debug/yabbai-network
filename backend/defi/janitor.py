"""
Janitor — rent recovery from a user's OWN Solana accounts.

Classification is PROTECTED-BY-DEFAULT: only an affirmative rule match makes an
account recoverable. Builds UNSIGNED transactions; the user signs AND broadcasts
in Phantom (signAndSendTransaction) — this backend never signs and never
broadcasts (N1). Recoveries book income ONLY via record_settled_income(rail="solana").
"""

import base64
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

# unsigned tx assembly only — no keypairs, no signing (N1). solders is OPTIONAL:
# without it the app boots fine and only the janitor BUILD plane reports 503
# (scan/estimate/report/submit are pure HTTP and keep working).
def _solders():
    try:
        from solders.pubkey import Pubkey
        from solders.instruction import Instruction, AccountMeta
        from solders.message import Message
        from solders.transaction import Transaction
        from solders.hash import Hash
        return Pubkey, Instruction, AccountMeta, Message, Transaction, Hash
    except ImportError:
        raise HTTPException(503, "transaction building unavailable — the 'solders' package "
                                 "is not installed on this deployment (operator opt-in)")

from network_db import db
from revenue_system.defi_backend_patched.settlement import (
    record_settled_income, SettlementVerificationError)
from . import config, jupiter
from .authz import require_user, rpc_call

logger = logging.getLogger("defi.janitor")
router = APIRouter(prefix="/api/defi/janitor", tags=["defi-janitor"])

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
STAKE_PROGRAM = "Stake11111111111111111111111111111111111111"
TX_BYTE_LIMIT = 1232          # Solana packet MTU-derived tx limit
BASE_FEE_LAMPORTS = 5000      # per signature


def _guard_enabled():
    if not config.janitor_enabled():
        raise HTTPException(403, "JANITOR_ENABLED=false")


async def classify_wallet(wallet: str) -> Dict:
    """Every owned account lands in exactly ONE bucket. PROTECTED is the default;
    only affirmative rules mark anything recoverable. Real lamports are read from
    each account — never a hardcoded rent figure."""
    never = set(config.janitor_never_touch())
    dust_usd = config.janitor_dust_usd()
    buckets: Dict[str, List[Dict]] = {"empty_ata": [], "empty_ata_2022": [], "dust": [],
                                      "openorders": [], "stake_deactivated": [], "PROTECTED": []}
    accounts: List[Dict] = []
    for program in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
        res = await rpc_call("getTokenAccountsByOwner",
                             [wallet, {"programId": program}, {"encoding": "jsonParsed"}])
        for acc in (res or {}).get("value", []):
            info = acc["account"]["data"]["parsed"]["info"]
            accounts.append({"pubkey": acc["pubkey"], "program": program,
                             "lamports": acc["account"]["lamports"], "info": info})

    mints = list({a["info"].get("mint") for a in accounts if a["info"].get("mint")})
    price_map = await jupiter.prices(mints) if mints else {}

    for a in accounts:
        info = a["info"]
        mint = info.get("mint", "")
        amt = info.get("tokenAmount") or {}
        raw = int(amt.get("amount") or 0)
        entry = {"account": a["pubkey"], "mint": mint, "lamports": a["lamports"],
                 "amount_raw": raw, "ui_amount": amt.get("uiAmount"),
                 "program": "token-2022" if a["program"] == TOKEN_2022_PROGRAM else "spl-token"}
        # PROTECTED unless an affirmative rule matches
        if mint in never:
            entry["why"] = "JANITOR_NEVER_TOUCH"
            buckets["PROTECTED"].append(entry); continue
        if (info.get("state") or "").lower() != "initialized":
            entry["why"] = f"state={info.get('state')}"
            buckets["PROTECTED"].append(entry); continue
        if info.get("delegate"):
            entry["why"] = "delegate set"
            buckets["PROTECTED"].append(entry); continue
        if raw == 0:
            b = "empty_ata_2022" if a["program"] == TOKEN_2022_PROGRAM else "empty_ata"
            entry["why"] = "zero balance — rent recoverable on close"
            buckets[b].append(entry); continue
        price = (price_map.get(mint) or {}).get("usdPrice")
        if price is None:
            entry["why"] = "no price available — cannot prove dust, fail closed"
            buckets["PROTECTED"].append(entry); continue
        usd = (amt.get("uiAmount") or 0) * price
        entry["usd_value"] = round(usd, 6)
        if usd < dust_usd:
            entry["why"] = f"value ${usd:.4f} < JANITOR_DUST_USD ${dust_usd:.2f}"
            buckets["dust"].append(entry); continue
        entry["why"] = "holds value"
        buckets["PROTECTED"].append(entry)

    # deactivated stake accounts (best effort — many public RPCs refuse this scan)
    stake_note = None
    try:
        res = await rpc_call("getProgramAccounts", [STAKE_PROGRAM, {
            "encoding": "jsonParsed",
            "filters": [{"memcmp": {"offset": 44, "bytes": wallet}}]}])
        for acc in res or []:
            parsed = acc["account"]["data"]["parsed"]
            state = parsed.get("type")
            entry = {"account": acc["pubkey"], "lamports": acc["account"]["lamports"],
                     "stake_state": state}
            if state == "initialized":   # no active delegation → withdrawable
                entry["why"] = "stake initialized, no delegation"
                buckets["stake_deactivated"].append(entry)
            else:
                entry["why"] = f"stake state={state} — withdraw via your wallet after full deactivation"
                buckets["PROTECTED"].append(entry)
    except Exception as e:
        stake_note = f"stake scan unavailable on this RPC ({str(e)[:80]}) — stake accounts NOT scanned"

    return {"wallet": wallet, "buckets": buckets,
            "notes": [n for n in [
                stake_note,
                "openorders scan not supported on the public RPC — open-orders accounts are "
                "not enumerated and therefore default PROTECTED"] if n],
            "ts": datetime.now(timezone.utc).isoformat()}


@router.get("/scan")
async def scan(wallet: str, request: Request, authorization: Optional[str] = Header(None)):
    _guard_enabled()
    await require_user(request, authorization)
    if not (wallet or "").strip():
        raise HTTPException(400, "wallet is required")
    return await classify_wallet(wallet.strip())


@router.get("/estimate")
async def estimate(wallet: str, request: Request, authorization: Optional[str] = Header(None)):
    _guard_enabled()
    await require_user(request, authorization)
    c = await classify_wallet(wallet.strip())
    b = c["buckets"]
    closeable = b["empty_ata"] + b["empty_ata_2022"]
    rent_lamports = sum(a["lamports"] for a in closeable)
    n_txs = max(1, (len(closeable) + 11) // 12) if closeable else 0
    fee_lamports = n_txs * BASE_FEE_LAMPORTS
    fee_bps = config.janitor_fee_bps()
    integ_fee = int(rent_lamports * fee_bps / 10_000)
    net = rent_lamports - fee_lamports - integ_fee
    return {"wallet": c["wallet"],
            "closeable_accounts": len(closeable),
            "dust_accounts": len(b["dust"]),
            "protected_accounts": len(b["PROTECTED"]),
            "rent_recoverable_sol": rent_lamports / 1e9,
            "est_network_fee_sol": fee_lamports / 1e9,
            "integrator_fee_bps": fee_bps,
            "integrator_fee_sol": integ_fee / 1e9,
            "net_recoverable_sol": max(0, net) / 1e9,
            "notes": c["notes"]}


class BuildBody(BaseModel):
    wallet: str
    action: str                  # "close" | "burn" — NEVER mixed in one signature
    accounts: List[str]


async def _fresh_account(pubkey: str) -> Optional[Dict]:
    res = await rpc_call("getAccountInfo", [pubkey, {"encoding": "jsonParsed"}])
    return (res or {}).get("value")


@router.post("/build")
async def build(body: BuildBody, request: Request, authorization: Optional[str] = Header(None)):
    _guard_enabled()
    await require_user(request, authorization)
    if body.action not in ("close", "burn"):
        raise HTTPException(400, "action must be 'close' or 'burn' — closes and dust burns "
                                 "are separately-signed actions, never mixed in one signature")
    if not body.accounts:
        raise HTTPException(400, "no accounts given")
    Pubkey, Instruction, AccountMeta, Message, Transaction, Hash = _solders()
    try:
        owner = Pubkey.from_string(body.wallet)
    except Exception:
        raise HTTPException(400, "wallet is not a valid Solana address")
    never = set(config.janitor_never_touch())

    ixs, meta_rows, total_lamports, mints, burn_lines = [], [], 0, set(), []
    for pk in body.accounts:
        val = await _fresh_account(pk)     # re-verify INSIDE the build, not from the scan
        if not val:
            raise HTTPException(409, f"account {pk[:12]}… no longer exists — rebuild from a fresh scan")
        if val.get("owner") not in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
            raise HTTPException(409, f"account {pk[:12]}… is not a token account — rejected")
        info = val["data"]["parsed"]["info"]
        if info.get("owner") != body.wallet:
            raise HTTPException(403, f"account {pk[:12]}… is not owned by this wallet")
        mint = info.get("mint", "")
        if mint in never:
            raise HTTPException(403, f"mint {mint[:12]}… is on JANITOR_NEVER_TOUCH")
        raw = int((info.get("tokenAmount") or {}).get("amount") or 0)
        program = Pubkey.from_string(val["owner"])

        if body.action == "close":
            if raw != 0:
                raise HTTPException(409, f"account {pk[:12]}… was funded since the scan "
                                         f"(balance {raw}) — build rejected")
            # SPL Token CloseAccount (instruction 9): [account(w), dest(w), owner(s)]
            ixs.append(Instruction(program, bytes([9]), [
                AccountMeta(Pubkey.from_string(pk), False, True),
                AccountMeta(owner, False, True),
                AccountMeta(owner, True, False)]))
            total_lamports += val.get("lamports", 0)
        else:  # burn
            if raw == 0:
                raise HTTPException(409, f"account {pk[:12]}… holds nothing to burn — "
                                         f"this is a close, not a burn (never mixed)")
            price = ((await jupiter.prices([mint])).get(mint) or {}).get("usdPrice")
            ui = (info.get("tokenAmount") or {}).get("uiAmount") or 0
            if price is None or (ui * price) >= config.janitor_dust_usd():
                raise HTTPException(403, f"account {pk[:12]}… is not provably dust "
                                         f"(price unknown or ≥ threshold) — fail closed")
            # SPL Token Burn (instruction 8): [account(w), mint(w), owner(s)] + u64 amount
            ixs.append(Instruction(program, bytes([8]) + raw.to_bytes(8, "little"), [
                AccountMeta(Pubkey.from_string(pk), False, True),
                AccountMeta(Pubkey.from_string(mint), False, True),
                AccountMeta(owner, True, False)]))
            burn_lines.append(f"BURN {ui} of {mint[:8]}… (~${ui * price:.4f}) — irreversible destruction")
        mints.add(mint)
        meta_rows.append({"account": pk, "mint": mint, "lamports": val.get("lamports", 0)})

    bh = await rpc_call("getLatestBlockhash", [{"commitment": "finalized"}])
    blockhash = Hash.from_string(bh["value"]["blockhash"])

    # batch by REAL serialized size against the 1232-byte limit
    txs, batch = [], []
    def _serialize(batch_ixs):
        msg = Message.new_with_blockhash(batch_ixs, owner, blockhash)
        return bytes(Transaction.new_unsigned(msg))
    for ix in ixs:
        trial = batch + [ix]
        if len(_serialize(trial)) > TX_BYTE_LIMIT and batch:
            txs.append(base64.b64encode(_serialize(batch)).decode())
            batch = [ix]
        else:
            batch = trial
    if batch:
        raw_tx = _serialize(batch)
        if len(raw_tx) > TX_BYTE_LIMIT:
            raise HTTPException(400, "single instruction exceeds the 1232-byte tx limit")
        txs.append(base64.b64encode(raw_tx).decode())

    fee_lamports = len(txs) * BASE_FEE_LAMPORTS
    integ_fee = int(total_lamports * config.janitor_fee_bps() / 10_000)
    confirmation = {
        "action": body.action,
        "account_count": len(meta_rows),
        "mints": sorted(mints),
        "sol_recovered": total_lamports / 1e9 if body.action == "close" else 0.0,
        "network_fee_sol": fee_lamports / 1e9,
        "integrator_fee_bps": config.janitor_fee_bps(),
        "integrator_fee_sol": integ_fee / 1e9,
        "net_sol": max(0, total_lamports - fee_lamports - integ_fee) / 1e9 if body.action == "close" else 0.0,
        "burn_line": burn_lines or (["no tokens are burned by this action"] if body.action == "close" else []),
        "note": "You sign and broadcast in Phantom. This backend built unsigned "
                "transactions only; it cannot sign and cannot broadcast.",
    }
    await db.defi_janitor_builds.insert_one({
        "wallet": body.wallet, "action": body.action, "accounts": body.accounts,
        "confirmation": confirmation, "built_at": datetime.now(timezone.utc)})
    return {"ok": True, "transactions": txs, "confirmation": confirmation}


class SubmitBody(BaseModel):
    wallet: str
    signature: str               # the tx id AFTER the user broadcast via Phantom


@router.post("/submit")
async def submit(body: SubmitBody, request: Request, authorization: Optional[str] = Header(None)):
    """Verify a client-broadcast janitor tx on-chain and book the recovery.
    Income routes EXCLUSIVELY through record_settled_income(rail='solana')."""
    _guard_enabled()
    await require_user(request, authorization)
    res = await rpc_call("getTransaction", [body.signature, {
        "encoding": "jsonParsed", "commitment": "confirmed",
        "maxSupportedTransactionVersion": 0}])
    if not res:
        raise HTTPException(404, "signature not found on-chain (yet) — retry once confirmed")
    meta = res.get("meta") or {}
    if meta.get("err") is not None:
        raise HTTPException(400, f"transaction failed on-chain: {meta.get('err')}")
    keys = [str(k.get("pubkey") if isinstance(k, dict) else k)
            for k in (res.get("transaction", {}).get("message", {}).get("accountKeys") or [])]
    try:
        idx = keys.index(body.wallet)
    except ValueError:
        raise HTTPException(400, "wallet is not a party to this transaction")
    delta = (meta.get("postBalances", [])[idx] - meta.get("preBalances", [])[idx])
    recovered_sol = delta / 1e9
    if recovered_sol <= 0:
        return {"ok": True, "recovered_sol": recovered_sol,
                "booked": False, "note": "no positive SOL recovery in this tx — nothing booked"}
    price = ((await jupiter.prices(["So11111111111111111111111111111111111111112"]))
             .get("So11111111111111111111111111111111111111112") or {}).get("usdPrice")
    try:
        if price:
            entry = await record_settled_income(
                amount=round(recovered_sol * price, 2), currency="USD", rail="solana",
                settlement_ref=body.signature, source="janitor rent recovery",
                notes=f"{recovered_sol:.9f} SOL recovered (priced at ${price:.2f}/SOL)",
                expected_party=body.wallet)
        else:
            entry = await record_settled_income(
                amount=round(recovered_sol, 9), currency="SOL", rail="solana",
                settlement_ref=body.signature, source="janitor rent recovery",
                notes="booked in SOL — USD price unavailable at settlement time (never fabricated)",
                expected_party=body.wallet)
    except SettlementVerificationError as e:
        raise HTTPException(400, f"settlement verification failed: {e}")
    return {"ok": True, "recovered_sol": recovered_sol, "booked": True, "entry_id": entry["id"]}


@router.post("/report")
async def report(wallet: str, request: Request, authorization: Optional[str] = Header(None)):
    _guard_enabled()
    await require_user(request, authorization)
    c = await classify_wallet(wallet.strip())
    b = c["buckets"]
    closeable = b["empty_ata"] + b["empty_ata_2022"]
    rent = sum(a["lamports"] for a in closeable) / 1e9
    fee_bps = config.janitor_fee_bps()
    lines = [f"JANITOR REPORT — {wallet}",
             f"generated {datetime.now(timezone.utc).isoformat()}",
             f"closeable empty accounts: {len(closeable)} (rent {rent:.6f} SOL)",
             f"dust accounts (< ${config.janitor_dust_usd():.2f}): {len(b['dust'])}",
             f"protected (default): {len(b['PROTECTED'])}",
             f"integrator fee: {fee_bps} bps — disclosed pre-signature",
             "burns are a separate, separately-signed action from closes."]
    return {"ok": True, "wallet": wallet,
            "report": {"closeable": closeable, "dust": b["dust"],
                       "protected_count": len(b["PROTECTED"]),
                       "rent_recoverable_sol": rent,
                       "integrator_fee_bps": fee_bps, "notes": c["notes"]},
            "printable": "\n".join(lines)}
