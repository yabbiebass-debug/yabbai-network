"""
Emergent-managed Google sign-in + Google Authenticator (TOTP) 2FA for YABBAI.

Access is restricted to an email allowlist (AUTH_ALLOWLIST). After Google sign-in
every Director must pass TOTP 2FA (Google Authenticator). TOTP secrets are
encrypted at rest with APP_ENC_KEY (Fernet).
"""

import os
import uuid
import io
import base64
import hmac
import hashlib
import secrets
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple

import httpx
import pyotp
import qrcode
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
RECOVERY_CODE_COUNT = 10

_fernet = Fernet(os.environ["APP_ENC_KEY"].encode())
_pepper = os.environ["RECOVERY_CODE_PEPPER"].encode()
def _enc(s: str) -> str: return _fernet.encrypt(s.encode()).decode()
def _dec(s: str) -> str: return _fernet.decrypt(s.encode()).decode()


def _code_hash(code: str) -> str:
    normalized = "".join(code.split()).upper().encode()
    return hmac.new(_pepper, normalized, hashlib.sha256).hexdigest()


def _new_recovery_codes():
    """Returns (plaintext_list_shown_once, stored_hash_docs)."""
    plain = ["-".join([base64.b32encode(secrets.token_bytes(3)).decode().rstrip("=")[:4]
                       for _ in range(2)]) for _ in range(RECOVERY_CODE_COUNT)]
    stored = [{"hash": _code_hash(c), "used_at": None,
               "created_at": datetime.now(timezone.utc).isoformat()} for c in plain]
    return plain, stored

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
    return {k: v for k, v in user.items() if k not in ("totp_secret_enc", "recovery_codes")}


async def require_director(request: Request, authorization: Optional[str] = Header(None)):
    """FastAPI dependency: caller must be an authed, 2FA-verified Director."""
    session, user = await _session_and_user(request, authorization)
    if not user:
        raise HTTPException(401, "Not authenticated")
    if not session.get("mfa_verified"):
        raise HTTPException(403, "2FA required")
    return user


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


def _qr_data_url(otpauth: str) -> str:
    img = qrcode.make(otpauth)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


@router.post("/2fa/setup")
async def twofa_setup(request: Request, authorization: Optional[str] = Header(None)):
    session, user = await _session_and_user(request, authorization)
    if not user:
        raise HTTPException(401, "Not authenticated")
    already = bool(user.get("totp_enabled") and user.get("totp_secret_enc"))
    # Reuse the existing secret if one exists (stable across reloads AND lets an
    # enrolled Director re-scan the SAME key on a new device — no lockout dead-end),
    # otherwise mint a fresh one.
    if user.get("totp_secret_enc"):
        secret = _dec(user["totp_secret_enc"])
    else:
        secret = pyotp.random_base32()
        await db.users.update_one({"user_id": user["user_id"]},
                                  {"$set": {"totp_secret_enc": _enc(secret), "totp_enabled": False}})
    otpauth = pyotp.totp.TOTP(secret).provisioning_uri(name=user["email"], issuer_name=ISSUER)
    # Always return the QR so the setup screen can render it regardless of state.
    return {"ok": True, "already_enrolled": already, "otpauth_url": otpauth,
            "secret": secret, "qr_data_url": _qr_data_url(otpauth)}


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
    first_enrollment = not user.get("totp_enabled")
    new_codes = None
    if first_enrollment:
        set_fields = {"totp_enabled": True}
        if not user.get("recovery_codes"):
            new_codes, stored = _new_recovery_codes()
            set_fields["recovery_codes"] = stored
        await db.users.update_one({"user_id": user["user_id"]}, {"$set": set_fields})
    await db.user_sessions.update_one({"session_token": session["session_token"]},
                                      {"$set": {"mfa_verified": True}})
    return {"ok": True, "mfa_verified": True, "recovery_codes": new_codes}


@router.post("/2fa/reset")
async def twofa_reset(request: Request, authorization: Optional[str] = Header(None)):
    """Start over: mint a BRAND NEW secret + fresh one-time recovery codes.
    Requires a valid (Google-authed, allowlisted) session; does NOT grant access —
    the Director must still scan the new QR and verify a live code."""
    session, user = await _session_and_user(request, authorization)
    if not user:
        raise HTTPException(401, "Not authenticated")
    secret = pyotp.random_base32()
    plain_codes, stored_codes = _new_recovery_codes()
    await db.users.update_one(
        {"user_id": user["user_id"]},
        {"$set": {"totp_secret_enc": _enc(secret), "totp_enabled": False,
                  "recovery_codes": stored_codes,
                  "mfa_changed_at": datetime.now(timezone.utc).isoformat()}})
    # Any old verified sessions for this user must re-verify against the new secret.
    await db.user_sessions.update_many({"user_id": user["user_id"]},
                                       {"$set": {"mfa_verified": False}})
    otpauth = pyotp.totp.TOTP(secret).provisioning_uri(name=user["email"], issuer_name=ISSUER)
    return {"ok": True, "otpauth_url": otpauth, "secret": secret,
            "qr_data_url": _qr_data_url(otpauth), "recovery_codes": plain_codes}


@router.post("/2fa/recover")
async def twofa_recover(body: CodeBody, request: Request,
                        authorization: Optional[str] = Header(None)):
    """Single-use backup recovery code for a lost authenticator device.
    Atomically consumes one unused code, then verifies the session's 2FA gate."""
    session, user = await _session_and_user(request, authorization)
    if not user:
        raise HTTPException(401, "Not authenticated")
    h = _code_hash(body.code)
    res = await db.users.update_one(
        {"user_id": user["user_id"],
         "recovery_codes": {"$elemMatch": {"hash": h, "used_at": None}}},
        {"$set": {"recovery_codes.$.used_at": datetime.now(timezone.utc).isoformat()}})
    if res.modified_count != 1:
        raise HTTPException(401, "Invalid or already-used recovery code")
    await db.user_sessions.update_one({"session_token": session["session_token"]},
                                      {"$set": {"mfa_verified": True}})
    return {"ok": True, "mfa_verified": True}


async def _require_verified(request, authorization):
    session, user = await _session_and_user(request, authorization)
    if not user:
        raise HTTPException(401, "Not authenticated")
    if not session.get("mfa_verified"):
        raise HTTPException(403, "2FA required")
    return session, user


@router.get("/2fa/recovery-status")
async def recovery_status(request: Request, authorization: Optional[str] = Header(None)):
    """How many one-time backup codes remain (verified Director only)."""
    _, user = await _require_verified(request, authorization)
    codes = user.get("recovery_codes") or []
    remaining = sum(1 for c in codes if not c.get("used_at"))
    return {"ok": True, "total": len(codes), "remaining": remaining,
            "used": len(codes) - remaining}


@router.post("/2fa/recovery-regenerate")
async def recovery_regenerate(request: Request, authorization: Optional[str] = Header(None)):
    """Replace the entire backup-code set with a fresh batch, shown once."""
    _, user = await _require_verified(request, authorization)
    plain_codes, stored_codes = _new_recovery_codes()
    await db.users.update_one({"user_id": user["user_id"]},
                              {"$set": {"recovery_codes": stored_codes}})
    return {"ok": True, "recovery_codes": plain_codes}



@router.post("/logout")
async def logout(request: Request, response: Response, authorization: Optional[str] = Header(None)):
    token = await _resolve_token(request, authorization)
    if token:
        await db.user_sessions.delete_one({"session_token": token})
    response.delete_cookie(COOKIE_NAME, path="/", samesite="none", secure=True)
    return {"ok": True}
