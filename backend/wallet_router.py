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
import asyncio
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


ERC20_ABI_BALANCEOF = "0x70a08231"  # balanceOf(address)

# Curated major tokens per EVM chain: (symbol, contract, decimals, coingecko_id)
TOKENS = {
    "ethereum": [
        ("USDC", "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", 6, "usd-coin"),
        ("USDT", "0xdAC17F958D2ee523a2206206994597C13D831ec7", 6, "tether"),
        ("DAI", "0x6B175474E89094C44Da98b954EedeAC495271d0F", 18, "dai"),
        ("WETH", "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", 18, "weth"),
        ("WBTC", "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599", 8, "wrapped-bitcoin"),
    ],
    "base": [
        ("USDC", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", 6, "usd-coin"),
        ("WETH", "0x4200000000000000000000000000000000000006", 18, "weth"),
    ],
    "arbitrum": [
        ("USDC", "0xaf88d065e77c8cC2239327C5EDb3A432268e5831", 6, "usd-coin"),
        ("USDT", "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9", 6, "tether"),
        ("WETH", "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1", 18, "weth"),
        ("ARB", "0x912CE59144191C1204E64559FE8253a0e49E6548", 18, "arbitrum"),
    ],
    "polygon": [
        ("USDC", "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174", 6, "usd-coin"),
        ("USDT", "0xc2132D05D31c914a87C6611C10748AEb04B58e8F", 6, "tether"),
        ("WETH", "0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619", 18, "weth"),
    ],
    "bsc": [
        ("USDT", "0x55d398326f99059fF775485246999027B3197955", 18, "tether"),
        ("WBNB", "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c", 18, "wbnb"),
        ("BUSD", "0xe9e7CEA3DedcA5984780Bafc599bD69ADd087D56", 18, "binance-usd"),
    ],
}
NATIVE_CG = {"ethereum": "ethereum", "base": "ethereum", "arbitrum": "ethereum",
             "polygon": "matic-network", "bsc": "binancecoin", "solana": "solana"}
# known Solana SPL mints -> (symbol, decimals, coingecko_id)
SOL_MINTS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": ("USDC", 6, "usd-coin"),
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": ("USDT", 6, "tether"),
    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN": ("JUP", 6, "jupiter-exchange-solana"),
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263": ("BONK", 5, "bonk"),
}


async def _cg_prices(ids: list) -> dict:
    """Prices via Coinbase spot (CoinGecko/Binance are rate-limited/geo-blocked here).
    Stablecoins are pinned to $1."""
    STABLE = {"usd-coin", "tether", "dai", "binance-usd"}
    CG_TO_CB = {"ethereum": "ETH", "weth": "ETH", "wrapped-bitcoin": "BTC", "arbitrum": "ARB",
                "matic-network": "MATIC", "binancecoin": "BNB", "wbnb": "BNB", "solana": "SOL",
                "jupiter-exchange-solana": "JUP", "bonk": "BONK"}
    wanted = set(i for i in ids if i)
    prices = {}
    bases = set()
    for cg in wanted:
        if cg in STABLE:
            prices[cg] = 1.0
        elif cg in CG_TO_CB:
            bases.add(CG_TO_CB[cg])

    async def _one(client, base):
        try:
            r = await client.get(f"https://api.coinbase.com/v2/prices/{base}-USD/spot")
            return base, float(r.json()["data"]["amount"])
        except Exception:
            return base, 0.0

    if bases:
        try:
            async with httpx.AsyncClient(timeout=12.0) as c:
                results = dict(await asyncio.gather(*[_one(c, b) for b in bases]))
            for cg in wanted:
                if cg in CG_TO_CB and cg not in prices:
                    prices[cg] = results.get(CG_TO_CB[cg], 0.0)
        except Exception:
            pass
    return prices


@router.get("/tokens")
async def tokens(chain: str, address: str):
    c = CHAINS.get(chain)
    if not c:
        raise HTTPException(400, "Unsupported chain")
    out = []
    cg_ids = [NATIVE_CG.get(chain)]
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            if c["kind"] == "evm":
                # native
                r = await client.post(c["rpc"], json={"jsonrpc": "2.0", "id": 1,
                    "method": "eth_getBalance", "params": [address, "latest"]})
                native = int(r.json()["result"], 16) / (10 ** c["decimals"])
                out.append({"symbol": c["symbol"], "amount": native, "cg": NATIVE_CG.get(chain), "native": True})
                # erc-20
                for sym, contract, dec, cg in TOKENS.get(chain, []):
                    cg_ids.append(cg)
                    data = ERC20_ABI_BALANCEOF + address[2:].lower().rjust(64, "0")
                    rr = await client.post(c["rpc"], json={"jsonrpc": "2.0", "id": 1,
                        "method": "eth_call", "params": [{"to": contract, "data": data}, "latest"]})
                    res = rr.json().get("result", "0x0")
                    amt = int(res, 16) / (10 ** dec) if res and res != "0x" else 0
                    if amt > 0:
                        out.append({"symbol": sym, "amount": amt, "cg": cg, "contract": contract})
            else:
                # solana native
                r = await client.post(c["rpc"], json={"jsonrpc": "2.0", "id": 1,
                    "method": "getBalance", "params": [address]})
                native = r.json()["result"]["value"] / (10 ** c["decimals"])
                out.append({"symbol": c["symbol"], "amount": native, "cg": "solana", "native": True})
                # SPL tokens
                rr = await client.post(c["rpc"], json={"jsonrpc": "2.0", "id": 1,
                    "method": "getTokenAccountsByOwner",
                    "params": [address, {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
                               {"encoding": "jsonParsed"}]})
                for acc in rr.json().get("result", {}).get("value", []):
                    info = acc["account"]["data"]["parsed"]["info"]
                    mint = info["mint"]
                    ui = info["tokenAmount"].get("uiAmount") or 0
                    if ui and ui > 0:
                        meta = SOL_MINTS.get(mint)
                        if meta:
                            out.append({"symbol": meta[0], "amount": ui, "cg": meta[2], "mint": mint})
                            cg_ids.append(meta[2])
                        else:
                            out.append({"symbol": mint[:4] + "…", "amount": ui, "cg": None, "mint": mint})
    except Exception:
        return {"ok": False, "chain": chain, "address": address,
                "error": "Balances unavailable — public RPC busy, try again.", "tokens": [], "total_usd": 0}

    prices = await _cg_prices(cg_ids)
    total = 0.0
    for t in out:
        price = prices.get(t.get("cg"), 0) if t.get("cg") else 0
        t["price_usd"] = price
        t["value_usd"] = round(t["amount"] * price, 2)
        total += t["value_usd"]
    out.sort(key=lambda x: x["value_usd"], reverse=True)
    return {"ok": True, "chain": chain, "address": address,
            "tokens": out, "total_usd": round(total, 2), "explorer": c["explorer"]}


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
    except Exception:
        return {"ok": False, "chain": chain, "address": address,
                "error": "Balance unavailable — public RPC busy, try again."}
