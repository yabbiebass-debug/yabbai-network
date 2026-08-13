"""Shared auth + RPC helpers for the DeFi routers."""

from typing import Optional

import httpx
from fastapi import HTTPException, Request

from auth_router import _session_and_user
from .config import rpc_url

RPC_TIMEOUT = httpx.Timeout(20.0, connect=8.0)


async def require_user(request: Request, authorization: Optional[str]):
    session, user = await _session_and_user(request, authorization)
    if not user:
        raise HTTPException(401, "Not authenticated")
    if not session.get("mfa_verified"):
        raise HTTPException(403, "2FA required")
    return user


async def rpc_call(method: str, params: list):
    try:
        async with httpx.AsyncClient(timeout=RPC_TIMEOUT) as c:
            r = await c.post(rpc_url(), json={"jsonrpc": "2.0", "id": 1,
                                              "method": method, "params": params})
            r.raise_for_status()
            body = r.json()
    except HTTPException:
        raise
    except Exception as e:
        # public RPCs rate-limit/block datacenter IPs — degrade to a CLEAN JSON
        # error, never an HTML blob. Operator fix: set SOLANA_RPC_URL to a
        # dedicated endpoint (Helius/Triton/QuickNode).
        raise HTTPException(503, f"solana rpc unavailable ({type(e).__name__}) — "
                                 f"set SOLANA_RPC_URL to a dedicated RPC endpoint")
    if "error" in body:
        raise HTTPException(503, f"solana rpc error: {body['error']}")
    return body.get("result")
