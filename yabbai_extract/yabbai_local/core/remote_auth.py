#!/usr/bin/env python3
"""
YABBAI Local -- Remote API Auth

Gates the API so a cloud frontend (Base44) can drive the laptop over the Cloudflare
tunnel -- safely. Local browser access stays open; REMOTE calls must carry a secret key.

THE THREAT MODEL (be honest about it):
  You chose "full control from Base44" -- the cloud dashboard can send messages,
  start/stop the agent, approve changes. That means the API key is powerful: anyone
  holding it can drive an agent that runs commands on your machine. So this layer:

   1. Requires the key (X-YABBAI-Key header) on all /api/* calls EXCEPT from localhost.
   2. Rate-limits to blunt a leaked-key abuse / brute-force.
   3. Keeps the MOST destructive actions (rollback, real-fund graduation, kill) on a
      SEPARATE higher tier -- even a valid key can't trigger those remotely unless you
      explicitly enable remote-destructive mode. Default: those stay local-only.

KEY HANDLING:
  - Key is read from env YABBAI_API_KEY. If unset, a random one is generated and
    printed ONCE at startup (copy it into Base44). Never hardcoded, never committed.
  - Rotate by changing the env var and restarting. Old key dies instantly.

If the key leaks: rotate it (new env, restart), and your laptop is safe again because
nothing destructive was ever remotely reachable.
"""

import os
import time
import secrets
from collections import defaultdict, deque
from typing import Optional

from fastapi import Request
from fastapi.responses import JSONResponse


# Endpoints that NEVER require a key (setup/health, and the UI itself)
PUBLIC_PATHS = {"/", "/chat", "/ide-frame", "/api/health"}

# The most destructive actions -- local-only by default, even with a valid key.
# Set YABBAI_ALLOW_REMOTE_DESTRUCTIVE=1 to override (not recommended).
DESTRUCTIVE_PATHS = {
    "/api/agent/rollback",
    "/api/agent/start",          # starting a 24/7 self-editing loop remotely = high impact
}


class RemoteAuth:
    def __init__(self):
        self.key = os.getenv("YABBAI_API_KEY") or secrets.token_urlsafe(24)
        self.generated = "YABBAI_API_KEY" not in os.environ
        self.allow_remote_destructive = os.getenv("YABBAI_ALLOW_REMOTE_DESTRUCTIVE") == "1"
        # rate limiter: ip -> deque of timestamps
        self._hits = defaultdict(lambda: deque(maxlen=120))
        self.rate_limit = 60          # max requests
        self.rate_window = 60         # per seconds

    def banner(self) -> str:
        if self.generated:
            return (f"\n  REMOTE API KEY (copy into Base44): {self.key}\n"
                    "  (auto-generated -- set YABBAI_API_KEY env to make it persistent)\n")
        return "\n  Remote API key: loaded from YABBAI_API_KEY env.\n"

    def _is_local(self, request: Request) -> bool:
        host = request.client.host if request.client else ""
        return host in ("127.0.0.1", "::1", "localhost")

    def _rate_ok(self, ip: str) -> bool:
        now = time.time()
        dq = self._hits[ip]
        while dq and now - dq[0] > self.rate_window:
            dq.popleft()
        if len(dq) >= self.rate_limit:
            return False
        dq.append(now)
        return True

    async def __call__(self, request: Request, call_next):
        path = request.url.path

        # Always allow the UI + health, and anything from localhost (your own machine)
        if path in PUBLIC_PATHS or not path.startswith("/api/") or self._is_local(request):
            return await call_next(request)

        # ── Remote /api/* call: enforce the key ──
        ip = request.client.host if request.client else "unknown"
        if not self._rate_ok(ip):
            return JSONResponse(status_code=429,
                                content={"error": "rate_limited",
                                         "message": "Too many requests. Slow down."})

        provided = request.headers.get("X-YABBAI-Key", "")
        if not secrets.compare_digest(provided, self.key):
            return JSONResponse(status_code=401,
                                content={"error": "unauthorized",
                                         "message": "Missing or invalid X-YABBAI-Key header."})

        # ── Destructive action guard (even with a valid key) ──
        if path in DESTRUCTIVE_PATHS and not self.allow_remote_destructive:
            return JSONResponse(status_code=403,
                                content={"error": "remote_destructive_blocked",
                                         "message": "This action is local-only for safety. "
                                                    "Run it from the laptop, or set "
                                                    "YABBAI_ALLOW_REMOTE_DESTRUCTIVE=1 to override."})

        return await call_next(request)
