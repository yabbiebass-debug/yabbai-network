"""
Multi-chain wallet surface (SAFE by design).

No private keys ever live here. Users connect MetaMask / Phantom / Jupiter in the
browser (keys stay in the extension, all real txs signed client-side). This router
stores connected *addresses* (watch-only) and reads native balances from public
RPCs across EVM chains + Solana. DeFi/GoldScout "execution" is proposed here and
must be human-approved and wallet-signed — the autonomous loop stays paper.
"""

import os
import uuid
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Request, HTTPException, Header
from pydantic import BaseModel

from network_db import db
from auth_router import _session_and_user

router = APIRouter(prefix="/api/wallet", tags=["wallet"])

# Chains we can benefit from now + future. EVM chains share the MetaMask flow.
CHAINS = {
    "ethereum": {"label": "Ethereum", "kind": "evm", "chain_id": "0x1",
                 "symbol": "ETH", "rpc": "https://ethereum-rpc.publicnode.com", "decimals": 18,
                 "explorer": "https://etherscan.io"},
    "base":     {"label": "Base", "kind": "evm", "chain_id": "0x2105",
                 "symbol": "ETH", "rpc": "https://base-rpc.publicnode.com", "decimals": 18,
                 "explorer": "https://basescan.org"},
    "arbitrum": {"label": "Arbitrum", "kind": "evm", "chain_id": "0xa4b1",
                 "symbol": "ETH", "rpc": "https://arbitrum-one-rpc.publicnode.com", "decimals": 18,
                 "explorer": "https://arbiscan.io"},
    "polygon":  {"label": "Polygon", "kind": "evm", "chain_id": "0x89",
                 "symbol": "POL", "rpc": "https://polygon-bor-rpc.publicnode.com", "decimals": 18,
                 "explorer": "https://polygonscan.com"},
    "bsc":      {"label": "BNB Chain", "kind": "evm", "chain_id": "0x38",
                 "symbol": "BNB", "rpc": "https://bsc-rpc.publicnode.com", "decimals": 18,
                 "explorer": "https://bscscan.com"},
    "solana":   {"label": "Solana", "kind": "solana", "chain_id": "mainnet-beta",
                 "symbol": "SOL", "rpc": "https://api.mainnet-beta.solana.com", "decimals": 9,
                 "explorer": "https://solscan.io"},
}


async def _require_user(request: Request, authorization: Optional[str]):
    session, user = await _session_and_user(request, authorization)
    if not user:
        raise HTTPException(401, "Not authenticated")
    if not session.get("mfa_verified"):
        raise HTTPException(403, "2FA required")
    return user


@router.get("/chains")
async def chains():
    return {"chains": [{"key": k, **{kk: vv for kk, vv in v.items() if kk != "rpc"}} for k, v in CHAINS.items()]}


class ConnectBody(BaseModel):
    address: str
    chain: str
    provider: str = "metamask"      # metamask | phantom | jupiter
    label: str = ""


@router.post("/connect")
async def connect(body: ConnectBody, request: Request, authorization: Optional[str] = Header(None)):
    user = await _require_user(request, authorization)
    if body.chain not in CHAINS:
        raise HTTPException(400, "Unsupported chain")
    doc = {
        "wallet_id": f"w_{uuid.uuid4().hex[:12]}", "user_id": user["user_id"],
        "address": body.address, "chain": body.chain, "provider": body.provider,
        "label": body.label or CHAINS[body.chain]["label"],
        "created_at": datetime.now(timezone.utc),
    }
    await db.wallets.update_one(
        {"user_id": user["user_id"], "address": body.address, "chain": body.chain},
        {"$set": doc}, upsert=True)
    return {"ok": True, "wallet": {k: v for k, v in doc.items() if k != "created_at"}}


@router.get("/list")
async def list_wallets(request: Request, authorization: Optional[str] = Header(None)):
    user = await _require_user(request, authorization)
    rows = await db.wallets.find({"user_id": user["user_id"]}, {"_id": 0, "created_at": 0}).to_list(200)
    return {"wallets": rows}


@router.delete("/{wallet_id}")
async def remove_wallet(wallet_id: str, request: Request, authorization: Optional[str] = Header(None)):
    user = await _require_user(request, authorization)
    await db.wallets.delete_one({"user_id": user["user_id"], "wallet_id": wallet_id})
    return {"ok": True}


@router.get("/balance")
async def balance(chain: str, address: str):
    c = CHAINS.get(chain)
    if not c:
        raise HTTPException(400, "Unsupported chain")
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            if c["kind"] == "evm":
                r = await client.post(c["rpc"], json={"jsonrpc": "2.0", "id": 1,
                    "method": "eth_getBalance", "params": [address, "latest"]})
                wei = int(r.json()["result"], 16)
                native = wei / (10 ** c["decimals"])
            else:
                r = await client.post(c["rpc"], json={"jsonrpc": "2.0", "id": 1,
                    "method": "getBalance", "params": [address]})
                lamports = r.json()["result"]["value"]
                native = lamports / (10 ** c["decimals"])
        return {"ok": True, "chain": chain, "address": address,
                "balance": native, "symbol": c["symbol"], "explorer": c["explorer"]}
    except Exception as e:
        return {"ok": False, "chain": chain, "address": address, "error": str(e)[:160]}
