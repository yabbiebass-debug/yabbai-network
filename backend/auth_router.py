"""
Emergent-managed Google sign-in for the YABBAI network.

Flow: the login page sends the user to auth.emergentagent.com with a redirect
back to /hub/index.html; that page returns with #session_id=..., which the hub
POSTs here. We exchange it (server-side) for user data + a 7-day session token,
store both in Mongo, and set an httpOnly cookie. All other pages verify via
/api/auth/me.
"""

import os
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx
from fastapi import APIRouter, Request, Response, HTTPException, Header
from dotenv import load_dotenv

from network_db import db

load_dotenv()

EMERGENT_SESSION_URL = "https://demobackend.emergentagent.com/auth/v1/env/oauth/session-data"
COOKIE_NAME = "session_token"
SESSION_DAYS = 7

router = APIRouter(prefix="/api/auth", tags=["auth"])


async def _resolve_token(request: Request, authorization: Optional[str]) -> Optional[str]:
    token = request.cookies.get(COOKIE_NAME)
    if not token and authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    return token


async def _current_user(request: Request, authorization: Optional[str]):
    token = await _resolve_token(request, authorization)
    if not token:
        return None
    session = await db.user_sessions.find_one({"session_token": token}, {"_id": 0})
    if not session:
        return None
    expires_at = session["expires_at"]
    if isinstance(expires_at, str):
        expires_at = datetime.fromisoformat(expires_at)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < datetime.now(timezone.utc):
        return None
    return await db.users.find_one({"user_id": session["user_id"]}, {"_id": 0})


@router.post("/session")
async def create_session(response: Response, x_session_id: Optional[str] = Header(None)):
    if not x_session_id:
        raise HTTPException(400, "Missing X-Session-ID")
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(EMERGENT_SESSION_URL, headers={"X-Session-ID": x_session_id})
    if r.status_code != 200:
        raise HTTPException(401, "Invalid or expired session_id")
    data = r.json()
    email = data.get("email")
    if not email:
        raise HTTPException(401, "No email returned")

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
            "created_at": datetime.now(timezone.utc),
        })

    session_token = data.get("session_token") or uuid.uuid4().hex
    expires_at = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    await db.user_sessions.update_one(
        {"session_token": session_token},
        {"$set": {"user_id": user_id, "session_token": session_token,
                  "expires_at": expires_at, "created_at": datetime.now(timezone.utc)}},
        upsert=True,
    )
    response.set_cookie(key=COOKIE_NAME, value=session_token, httponly=True,
                        secure=True, samesite="none", path="/",
                        max_age=SESSION_DAYS * 24 * 3600)
    user = await db.users.find_one({"user_id": user_id}, {"_id": 0})
    return {"ok": True, "user": user}


@router.get("/me")
async def me(request: Request, authorization: Optional[str] = Header(None)):
    user = await _current_user(request, authorization)
    if not user:
        raise HTTPException(401, "Not authenticated")
    return user


@router.post("/logout")
async def logout(request: Request, response: Response, authorization: Optional[str] = Header(None)):
    token = await _resolve_token(request, authorization)
    if token:
        await db.user_sessions.delete_one({"session_token": token})
    response.delete_cookie(COOKIE_NAME, path="/", samesite="none", secure=True)
    return {"ok": True}
