"""
Supabase data-layer connector.

The service_role key (pasted securely via Settings, stored encrypted-at-rest posture:
never echoed) lets the backend read/write Supabase Postgres bypassing RLS, so the hub
and surfaces get live data through our own /api without wrestling anon-key RLS.
"""

from typing import Optional

import os
import httpx
from fastapi import APIRouter, Request, HTTPException, Header

from network_db import get_raw_settings
from auth_router import _session_and_user

router = APIRouter(prefix="/api/supabase", tags=["supabase"])

KNOWN = ["money_view", "leads", "enquiries", "diagnostics", "catalog_view",
         "approvals", "clients", "staff", "vault_products", "widgets",
         "orders", "product_audits", "funnel_view"]
# tables the hub live-strip + Mission Control read
READABLE = set(KNOWN)


async def _require_user(request: Request, authorization: Optional[str]):
    session, user = await _session_and_user(request, authorization)
    if not user:
        raise HTTPException(401, "Not authenticated")
    if not session.get("mfa_verified"):
        raise HTTPException(403, "2FA required")
    return user


async def _creds():
    """Prefer env-provided Supabase secrets; fall back to Settings-stored values."""
    env_url = os.environ.get("SUPABASE_PUBLIC_URL")
    env_key = os.environ.get("SUPABASE_SECRET_KEY")
    if env_url and env_key:
        return env_url, env_key
    s = await get_raw_settings()
    return s.get("supabase_url", env_url or ""), s.get("supabase_service_key", env_key or "")


def _headers(key):
    return {"apikey": key, "Authorization": f"Bearer {key}", "Accept": "application/json"}


@router.get("/public-config")
async def public_config():
    """Publishable (by-design public) Supabase config for the static hub page.
    Values come from env only — nothing hardcoded in frontend source."""
    return {"url": os.environ.get("SUPABASE_PUBLIC_URL", ""),
            "publishable_key": os.environ.get("SUPABASE_PUBLISHABLE_KEY", "")}


@router.get("/status")
async def status(request: Request, authorization: Optional[str] = Header(None)):
    await _require_user(request, authorization)
    url, key = await _creds()
    out = {"url": url, "key_set": bool(key), "tables": {}, "connected": False}
    if not (url and key):
        return out
    try:
        async with httpx.AsyncClient(timeout=12.0) as c:
            for t in KNOWN:
                try:
                    r = await c.get(f"{url}/rest/v1/{t}", params={"select": "*", "limit": 1},
                                    headers=_headers(key))
                    out["tables"][t] = (r.status_code == 200)
                    if r.status_code == 200:
                        out["connected"] = True
                except Exception:
                    out["tables"][t] = False
    except Exception as e:
        out["error"] = str(e)[:160]
    out["tables_present"] = sum(1 for v in out["tables"].values() if v)
    out["tables_total"] = len(KNOWN)
    return out


@router.get("/table/{name}")
async def read_table(name: str, request: Request, authorization: Optional[str] = Header(None),
                     select: str = "*", limit: int = 100, order: str = ""):
    await _require_user(request, authorization)
    if name not in READABLE:
        raise HTTPException(400, "Table not permitted")
    url, key = await _creds()
    if not (url and key):
        raise HTTPException(400, "Supabase not configured — set SUPABASE_SECRET_KEY in the backend .env or add the service_role key in Settings.")
    params = {"select": select, "limit": str(limit)}
    if order:
        params["order"] = order
    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.get(f"{url}/rest/v1/{name}", params=params, headers=_headers(key))
    if r.status_code != 200:
        return {"ok": False, "status": r.status_code, "detail": r.text[:200], "rows": []}
    return {"ok": True, "rows": r.json()}
