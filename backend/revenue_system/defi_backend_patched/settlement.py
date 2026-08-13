"""
Settlement boundary — the ONLY code path in the whole tree that writes
entry_type="income" to vault_entries. (Non-negotiable N2.)

record_settled_income() verifies an EXTERNAL settlement reference against its
rail before a single row is written. Verify-or-raise: an unreachable verifier
means NO income row, never a provisional one. LLM estimates never touch this.

Rails:
  solana   — transaction signature, confirmed via RPC getTransaction
  stripe   — payment_intent id, status must be "succeeded"
  coinspot — order id, must appear in the read-only completed-order history
  paypal   — inbound capture id, status must be "COMPLETED"
"""

import os
import hmac
import json
import time
import uuid
import hashlib
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger("settlement")

RAILS = ("solana", "stripe", "coinspot", "paypal")


class SettlementVerificationError(Exception):
    """Raised when a settlement reference cannot be positively verified."""


# ── store (same Mongo as the Gold Hunter service; in-memory fallback) ──────────
_mongo_url = os.environ.get("MONGO_URL", "")
db = None
if _mongo_url:
    try:
        from motor.motor_asyncio import AsyncIOMotorClient
        db = AsyncIOMotorClient(_mongo_url, serverSelectionTimeoutMS=5000)[
            os.environ.get("DB_NAME", "yabai_gold_hunter")]
    except Exception as e:  # pragma: no cover
        logger.warning(f"settlement: Mongo unavailable ({e}); in-memory fallback")

_mem: List[Dict] = []   # fallback only when Mongo is absent


# ── rail verifiers — each returns proof dict or raises ─────────────────────────
async def _verify_solana(ref: str, expected_party: Optional[str] = None) -> Dict:
    rpc = os.environ.get("SOLANA_RPC_URL", "").strip() or "https://api.mainnet-beta.solana.com"
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getTransaction",
               "params": [ref, {"encoding": "json", "commitment": "confirmed",
                                "maxSupportedTransactionVersion": 0}]}
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(rpc, json=payload)
            r.raise_for_status()
            body = r.json()
    except Exception as e:
        raise SettlementVerificationError(f"solana RPC unreachable — cannot verify: {e}")
    result = body.get("result")
    if not result:
        raise SettlementVerificationError(f"solana signature not found on-chain: {ref[:24]}…")
    if (result.get("meta") or {}).get("err") is not None:
        raise SettlementVerificationError(f"solana transaction FAILED on-chain: {ref[:24]}…")
    if expected_party:
        keys = [str(k.get("pubkey") if isinstance(k, dict) else k)
                for k in (result.get("transaction", {}).get("message", {}).get("accountKeys") or [])]
        if expected_party not in keys:
            raise SettlementVerificationError(
                f"wallet {expected_party[:12]}… is not a party to transaction {ref[:24]}…")
    return {"rail": "solana", "slot": result.get("slot"),
            "block_time": result.get("blockTime"), "confirmed": True}


async def _verify_stripe(ref: str) -> Dict:
    key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    if not key:
        raise SettlementVerificationError("STRIPE_SECRET_KEY not set — cannot verify, refusing to book")
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"https://api.stripe.com/v1/payment_intents/{ref}",
                            headers={"Authorization": f"Bearer {key}"})
    except Exception as e:
        raise SettlementVerificationError(f"stripe unreachable — cannot verify: {e}")
    if r.status_code != 200:
        raise SettlementVerificationError(f"stripe payment_intent lookup failed (HTTP {r.status_code})")
    pi = r.json()
    if pi.get("status") != "succeeded":
        raise SettlementVerificationError(f"stripe payment_intent status={pi.get('status')} (need succeeded)")
    return {"rail": "stripe", "payment_intent": ref, "amount_received": pi.get("amount_received"),
            "currency": pi.get("currency"), "confirmed": True}


async def _verify_coinspot(ref: str) -> Dict:
    k = os.environ.get("COINSPOT_API_KEY", "").strip()
    s = os.environ.get("COINSPOT_SECRET", "").strip()
    if not k or not s:
        raise SettlementVerificationError("COINSPOT_API_KEY/SECRET not set — cannot verify, refusing to book")
    nonce = int(time.time() * 1000)
    post = json.dumps({"nonce": nonce}, separators=(",", ":"))
    sign = hmac.new(s.encode(), post.encode(), hashlib.sha512).hexdigest()
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post("https://www.coinspot.com.au/api/v2/ro/my/orders/completed",
                             headers={"key": k, "sign": sign, "Content-Type": "application/json"},
                             content=post)
    except Exception as e:
        raise SettlementVerificationError(f"coinspot unreachable — cannot verify: {e}")
    data = r.json() if r.status_code == 200 else {}
    if data.get("status") != "ok":
        raise SettlementVerificationError(f"coinspot history read failed (HTTP {r.status_code})")
    for side in ("buyorders", "sellorders"):
        for o in data.get(side) or []:
            if str(o.get("id")) == str(ref):
                return {"rail": "coinspot", "order_id": ref, "side": side, "confirmed": True}
    raise SettlementVerificationError(f"coinspot order id not found in trade history: {ref}")


async def _verify_paypal(ref: str) -> Dict:
    cid = os.environ.get("PAYPAL_CLIENT_ID", "").strip()
    sec = os.environ.get("PAYPAL_SECRET", "").strip()
    if not cid or not sec:
        raise SettlementVerificationError("PAYPAL_CLIENT_ID/SECRET not set — cannot verify, refusing to book")
    base = os.environ.get("PAYPAL_BASE_URL", "https://api-m.paypal.com")
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            t = await c.post(f"{base}/v1/oauth2/token",
                             data={"grant_type": "client_credentials"}, auth=(cid, sec))
            t.raise_for_status()
            token = t.json()["access_token"]
            r = await c.get(f"{base}/v2/payments/captures/{ref}",
                            headers={"Authorization": f"Bearer {token}"})
    except Exception as e:
        raise SettlementVerificationError(f"paypal unreachable — cannot verify: {e}")
    if r.status_code != 200:
        raise SettlementVerificationError(f"paypal capture lookup failed (HTTP {r.status_code})")
    cap = r.json()
    if cap.get("status") != "COMPLETED":
        raise SettlementVerificationError(f"paypal capture status={cap.get('status')} (need COMPLETED)")
    return {"rail": "paypal", "capture_id": ref,
            "amount": (cap.get("amount") or {}).get("value"), "confirmed": True}


_VERIFIERS = {"solana": _verify_solana, "stripe": _verify_stripe,
              "coinspot": _verify_coinspot, "paypal": _verify_paypal}


async def _already_booked(rail: str, ref: str) -> bool:
    if db is not None:
        return await db.vault_entries.find_one(
            {"settlement_rail": rail, "settlement_ref": ref}) is not None
    return any(e.get("settlement_rail") == rail and e.get("settlement_ref") == ref
               for e in _mem)


async def record_settled_income(amount: float, currency: str, rail: str,
                                settlement_ref: str, finding_id: Optional[str] = None,
                                source: str = "", notes: str = "",
                                expected_party: Optional[str] = None) -> Dict:
    """THE single income writer. Verifies the external settlement first; raises on
    any doubt. Duplicate (rail, ref) pairs are refused so nothing double-books.
    expected_party (solana rail): the wallet that must be a party to the tx."""
    if not isinstance(amount, (int, float)) or amount <= 0:
        raise SettlementVerificationError("amount must be a positive number")
    if rail not in RAILS:
        raise SettlementVerificationError(f"unknown settlement rail '{rail}' (allowed: {RAILS})")
    ref = (settlement_ref or "").strip()
    if not ref:
        raise SettlementVerificationError("settlement_ref is required")
    if await _already_booked(rail, ref):
        raise SettlementVerificationError(f"settlement {rail}:{ref[:24]}… already booked (no double-booking)")

    if rail == "solana":
        proof = await _verify_solana(ref, expected_party=expected_party)
    else:
        proof = await _VERIFIERS[rail](ref)   # verify OR raise — nothing provisional

    entry = {
        "id": str(uuid.uuid4()),
        "source": source or f"settled:{rail}",
        "amount": round(float(amount), 2),
        "currency": (currency or "AUD").upper(),
        "entry_type": "income",
        "network": rail,
        "tx_hash": ref,
        "agent_role": "settlement",
        "notes": notes[:500],
        "reconciled": True,
        "settlement_rail": rail,
        "settlement_ref": ref,
        "settlement_proof": proof,
        "finding_id": finding_id,
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "created_date": datetime.now(timezone.utc).isoformat(),
    }
    if db is not None:
        await db.vault_entries.insert_one(dict(entry))
    else:
        _mem.append(entry)
    logger.info(f"SETTLED INCOME booked: {entry['amount']} {entry['currency']} via {rail} ref={ref[:24]}…")
    return {k: v for k, v in entry.items() if k != "_id"}


async def reconcile_income_rows() -> Dict:
    """Re-verify EVERY income row against its rail. Rows without a rail/ref, or
    failing re-verification, are flagged unverified (never deleted)."""
    started = datetime.now(timezone.utc).isoformat()
    rows: List[Dict] = []
    if db is not None:
        rows = await db.vault_entries.find({"entry_type": "income"}).to_list(100000)
    else:
        rows = [e for e in _mem if e.get("entry_type") == "income"]

    verified, failed = 0, []
    for row in rows:
        rail, ref = row.get("settlement_rail"), row.get("settlement_ref")
        ok, reason = False, ""
        if rail in RAILS and ref:
            try:
                await _VERIFIERS[rail](ref)
                ok = True
            except SettlementVerificationError as e:
                reason = str(e)
        else:
            reason = "no settlement rail/ref on row (pre-boundary legacy row)"
        upd = {"reconcile_ok": ok, "reconcile_checked_at": started}
        if not ok:
            upd["reconcile_fail_reason"] = reason
            failed.append({"id": row.get("id"), "reason": reason})
        else:
            verified += 1
        if db is not None:
            await db.vault_entries.update_one({"id": row.get("id")}, {"$set": upd})
        else:
            row.update(upd)

    result = {"ran_at": started, "checked": len(rows), "verified": verified,
              "unverified": len(failed), "failures": failed[:50], "ok": len(failed) == 0}
    state = {"_id": "vault_reconcile", **result}
    if db is not None:
        await db.reconcile_state.update_one({"_id": "vault_reconcile"},
                                            {"$set": state}, upsert=True)
    return result


async def reconcile_status() -> Dict:
    if db is not None:
        doc = await db.reconcile_state.find_one({"_id": "vault_reconcile"})
        if doc:
            return {k: v for k, v in doc.items() if k != "_id"}
    return {"ran_at": None, "checked": None, "verified": None,
            "unverified": None, "ok": None, "note": "reconcile has never run"}
