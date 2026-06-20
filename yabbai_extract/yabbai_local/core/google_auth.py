#!/usr/bin/env python3
"""
YABBAI Local -- Google OAuth2 Auth Layer
Single-user: only thomas.basham1@gmail.com may pass.

Flow:
  GET /auth/login     --> redirect to Google
  GET /auth/callback  --> validate token, check email, set session cookie --> redirect /
  GET /auth/logout    --> clear session --> redirect /auth/login
  Middleware          --> every request checks session; unauthenticated --> /auth/login
                        (public paths: /auth/*, /api/health, /favicon.ico)

Uses fastapi-sso (already in defi_backend deps; added to yabbai_local deps).
Session cookie is signed with SESSION_SECRET (env).
"""

import os
import secrets
from typing import Optional

from fastapi import Request
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi_sso.sso.google import GoogleSSO
from starlette.middleware.sessions import SessionMiddleware

# ── Config ────────────────────────────────────────────────────────────────────
ALLOWED_EMAIL = "thomas.basham1@gmail.com"
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
SESSION_SECRET = os.environ.get("SESSION_SECRET") or secrets.token_urlsafe(32)

# Paths that bypass the auth check
AUTH_PUBLIC_PATHS = {"/auth/login", "/auth/callback", "/auth/logout",
                     "/api/health", "/favicon.ico"}


def make_sso(callback_url: str) -> GoogleSSO:
    return GoogleSSO(
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        redirect_uri=callback_url,
        allow_insecure_http=True,   # fine for localhost tunnel; Cloudflare handles TLS
        scope=["openid", "email", "profile"],
    )


# Fixed callback URL -- must exactly match what's registered in Google Cloud Console.
# Override YABBAI_BASE_URL in .env when using a Cloudflare tunnel.
YABBAI_BASE_URL = os.environ.get("YABBAI_BASE_URL", "http://localhost:7860")

def get_callback_url(request: Request = None) -> str:
    return f"{YABBAI_BASE_URL.rstrip('/')}/auth/callback"


# ── Middleware ─────────────────────────────────────────────────────────────────
class GoogleAuthMiddleware:
    """ASGI middleware: gate every request behind Google auth."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        from starlette.requests import Request as StarletteRequest
        from starlette.responses import RedirectResponse as StarletteRedirect

        request = StarletteRequest(scope, receive)
        path = request.url.path

        # Public paths always pass through
        if any(path.startswith(p) for p in AUTH_PUBLIC_PATHS):
            await self.app(scope, receive, send)
            return

        # Check session
        email = request.session.get("user_email")
        if email == ALLOWED_EMAIL:
            await self.app(scope, receive, send)
            return

        # Not authenticated --> redirect to login
        login_url = f"/auth/login?next={request.url.path}"
        response = StarletteRedirect(login_url)
        await response(scope, receive, send)


# ── Route handlers (mount these onto the FastAPI app) ─────────────────────────
async def auth_login(request: Request):
    """Kick off the Google OAuth dance."""
    callback = get_callback_url(request)
    sso = make_sso(callback)
    async with sso:
        return await sso.get_login_redirect()


async def auth_callback(request: Request):
    """Google redirects here. Validate email, set session, send to app."""
    callback = get_callback_url(request)
    sso = make_sso(callback)
    async with sso:
        user = await sso.verify_and_process(request)

    if not user or not user.email:
        return HTMLResponse(_deny_page("No email returned from Google."), status_code=403)

    if user.email.lower() != ALLOWED_EMAIL.lower():
        return HTMLResponse(_deny_page(
            f"Access denied for <b>{user.email}</b>.<br>"
            f"This system is locked to a single authorised account."
        ), status_code=403)

    # Valid -- write session
    request.session["user_email"] = user.email
    request.session["user_name"] = user.display_name or user.email
    next_url = request.query_params.get("next", "/")
    return RedirectResponse(next_url, status_code=302)


async def auth_logout(request: Request):
    request.session.clear()
    return RedirectResponse("/auth/login", status_code=302)


async def auth_me(request: Request):
    """Convenience endpoint -- returns the signed-in user info."""
    from fastapi.responses import JSONResponse
    email = request.session.get("user_email")
    if not email:
        return JSONResponse({"authenticated": False}, status_code=401)
    return JSONResponse({"authenticated": True, "email": email,
                         "name": request.session.get("user_name", email)})


# ── HTML helpers ───────────────────────────────────────────────────────────────
def _deny_page(reason: str) -> str:
    return f"""<!DOCTYPE html>
<html><head><title>Access Denied -- YABBAI</title>
<style>
  body {{ background:#0a0a0f; color:#e0e0e0; font-family:monospace;
         display:flex; align-items:center; justify-content:center; height:100vh; margin:0; }}
  .box {{ background:#111118; border:1px solid #9945FF; border-radius:8px;
          padding:2.5rem 3rem; max-width:480px; text-align:center; }}
  h2 {{ color:#9945FF; margin-top:0; }}
  p {{ color:#999; line-height:1.6; }}
  a {{ color:#14F195; text-decoration:none; }}
</style></head>
<body><div class="box">
  <h2>⛔ Access Denied</h2>
  <p>{reason}</p>
  <p><a href="/auth/login">← Try a different account</a></p>
</div></body></html>"""
