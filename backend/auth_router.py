"""
Emergent-managed Google sign-in + Google Authenticator (TOTP) 2FA for YABBAI.

Access is restricted to an email allowlist (AUTH_ALLOWLIST). After Google sign-in
every Director must pass TOTP 2FA (Google Authenticator). TOTP secrets are
encrypted at rest with APP_ENC_KEY (Fernet).
"""

import os
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple

import httpx
import pyotp
from fastapi import APIRouter, Request, Response, HTTPException, Header
from pydantic import BaseModel
from cryptography.fernet import Fernet
from dotenv import load_dotenv

from network_db import db

load_dotenv()

EMERGENT_SESSION_URL = "https://demobackend.emergentagent.com/auth/v1/env/oauth/session-data"
COOKIE_NAME = "session_token"
SESSION_DAYS = 7
ALLOWLIST = {e.strip().lower() for e in os.environ.get("AUTH_ALLOWLIST", "").split(",") if e.strip()}
ISSUER = "YABBAI.NETWORK"

_fernet = Fernet(os.environ["APP_ENC_KEY"].encode())
def _enc(s: str) -> str: return _fernet.encrypt(s.encode()).decode()
def _dec(s: str) -> str: return _fernet.decrypt(s.encode()).decode()

router = APIRouter(prefix="/api/auth", tags=["auth"])


async def _resolve_token(request: Request, authorization: Optional[str]) -> Optional[str]:
    token = request.cookies.get(COOKIE_NAME)
    if not token and authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    return token


async def _session_and_user(request: Request, authorization: Optional[str]) -> Tuple[Optional[dict], Optional[dict]]:
    token = await _resolve_token(request, authorization)
    if not token:
        return None, None
    session = await db.user_sessions.find_one({"session_token": token}, {"_id": 0})
    if not session:
        return None, None
    expires_at = session["expires_at"]
    if isinstance(expires_at, str):
        expires_at = datetime.fromisoformat(expires_at)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < datetime.now(timezone.utc):
        return None, None
    user = await db.users.find_one({"user_id": session["user_id"]}, {"_id": 0})
    return session, user


def _public_user(user: dict) -> dict:
    return {k: v for k, v in user.items() if k not in ("totp_secret_enc",)}


@router.post("/session")
async def create_session(response: Response, x_session_id: Optional[str] = Header(None)):
    if not x_session_id:
        raise HTTPException(400, "Missing X-Session-ID")
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(EMERGENT_SESSION_URL, headers={"X-Session-ID": x_session_id})
    if r.status_code != 200:
        raise HTTPException(401, "Invalid or expired session_id")
    data = r.json()
    email = (data.get("email") or "").lower()
    if not email:
        raise HTTPException(401, "No email returned")
    if ALLOWLIST and email not in ALLOWLIST:
        raise HTTPException(403, "Access restricted — this network is locked to the authorised Directors.")

    existing = await db.users.find_one({"email": email}, {"_id": 0})
    if existing:
        user_id = existing["user_id"]
        await db.users.update_one({"user_id": user_id},
                                  {"$set": {"name": data.get("name"), "picture": data.get("picture")}})
    else:
        user_id = f"user_{uuid.uuid4().hex[:12]}"
        await db.users.insert_one({
            "user_id": user_id, "email": email, "name": data.get("name"),
            "picture": data.get("picture"), "role": "director",
            "totp_enabled": False, "created_at": datetime.now(timezone.utc),
        })

    session_token = data.get("session_token") or uuid.uuid4().hex
    expires_at = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    await db.user_sessions.update_one(
        {"session_token": session_token},
        {"$set": {"user_id": user_id, "session_token": session_token, "mfa_verified": False,
                  "expires_at": expires_at, "created_at": datetime.now(timezone.utc)}},
        upsert=True,
    )
    response.set_cookie(key=COOKIE_NAME, value=session_token, httponly=True,
                        secure=True, samesite="none", path="/",
                        max_age=SESSION_DAYS * 24 * 3600)
    user = await db.users.find_one({"user_id": user_id}, {"_id": 0})
    return {"ok": True, "user": _public_user(user), "mfa_enrolled": bool(user.get("totp_enabled"))}


@router.get("/me")
async def me(request: Request, authorization: Optional[str] = Header(None)):
    session, user = await _session_and_user(request, authorization)
    if not user:
        raise HTTPException(401, "Not authenticated")
    out = _public_user(user)
    out["mfa_required"] = True
    out["mfa_enrolled"] = bool(user.get("totp_enabled"))
    out["mfa_verified"] = bool(session.get("mfa_verified"))
    return out


@router.post("/2fa/setup")
async def twofa_setup(request: Request, authorization: Optional[str] = Header(None)):
    session, user = await _session_and_user(request, authorization)
    if not user:
        raise HTTPException(401, "Not authenticated")
    if user.get("totp_enabled") and user.get("totp_secret_enc"):
        return {"ok": True, "already_enrolled": True}
    secret = pyotp.random_base32()
    await db.users.update_one({"user_id": user["user_id"]},
                              {"$set": {"totp_secret_enc": _enc(secret), "totp_enabled": False}})
    otpauth = pyotp.totp.TOTP(secret).provisioning_uri(name=user["email"], issuer_name=ISSUER)
    return {"ok": True, "already_enrolled": False, "otpauth_url": otpauth, "secret": secret}


class CodeBody(BaseModel):
    code: str


@router.post("/2fa/verify")
async def twofa_verify(body: CodeBody, request: Request, response: Response,
                       authorization: Optional[str] = Header(None)):
    session, user = await _session_and_user(request, authorization)
    if not user:
        raise HTTPException(401, "Not authenticated")
    enc = user.get("totp_secret_enc")
    if not enc:
        raise HTTPException(400, "2FA not set up yet — call /2fa/setup first.")
    secret = _dec(enc)
    if not pyotp.TOTP(secret).verify(body.code.strip(), valid_window=1):
        raise HTTPException(401, "Invalid or expired code")
    if not user.get("totp_enabled"):
        await db.users.update_one({"user_id": user["user_id"]}, {"$set": {"totp_enabled": True}})
    await db.user_sessions.update_one({"session_token": session["session_token"]},
                                      {"$set": {"mfa_verified": True}})
    return {"ok": True, "mfa_verified": True}


@router.post("/logout")
async def logout(request: Request, response: Response, authorization: Optional[str] = Header(None)):
    token = await _resolve_token(request, authorization)
    if token:
        await db.user_sessions.delete_one({"session_token": token})
    response.delete_cookie(COOKIE_NAME, path="/", samesite="none", secure=True)
    return {"ok": True}
