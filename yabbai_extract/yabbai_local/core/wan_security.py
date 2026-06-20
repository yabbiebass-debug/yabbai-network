#!/usr/bin/env python3
"""
YABBAI v9.5.0 -- WAN Hosting Security Layer

When you host YABBAI from your laptop to the open internet (LAN-->WAN), the threat
model changes: anyone on the internet can attempt to reach your machine, and behind
the API is an agent that runs commands. This module hardens that exposure and, just
as important, WARNS you at startup if your configuration is unsafe.

It does NOT open any ports itself -- port forwarding / tunnel setup happens on your
router or via cloudflared. This is the software-side seatbelt for when you do expose it.

WHAT IT ENFORCES:
  - A strong API key is MANDATORY before WAN exposure (refuses weak/empty keys).
  - The most destructive agent actions are blocked from any non-local caller.
  - Per-IP request logging so you can see who's hitting your machine.
  - A startup preflight that grades your exposure and tells you what's risky.

USE: import and call wan_preflight() at server startup; mount HardenedAuth as the
auth layer (it extends the existing RemoteAuth behavior with WAN-specific checks).
"""

import os
import time
import secrets
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Dict, List


def wan_preflight(api_key: str, bind_host: str, allow_remote_destructive: bool) -> Dict:
    """
    Grade the hosting configuration BEFORE accepting traffic.
    Returns a report with a safety grade and explicit warnings. Print this loudly.
    """
    warnings = []
    critical = []

    # Key strength
    if not api_key or len(api_key) < 20:
        critical.append("API key is missing or too short (<20 chars). On the open internet "
                        "this is the ONLY thing protecting an agent that runs commands. "
                        "Set a long random YABBAI_API_KEY before exposing to WAN.")
    elif api_key in ("changeme", "password", "yabbai", "test", "secret"):
        critical.append("API key is a common/guessable value. Use a long random secret.")

    # Bind host
    if bind_host == "0.0.0.0":
        warnings.append("Binding 0.0.0.0 exposes YABBAI on ALL network interfaces. Correct for "
                        "tunnel/LAN hosting, but make sure your router isn't ALSO port-forwarding "
                        "this unless you intend full public exposure.")

    # Destructive remote
    if allow_remote_destructive:
        critical.append("YABBAI_ALLOW_REMOTE_DESTRUCTIVE=1 is set. This lets REMOTE callers start "
                        "the 24/7 self-editing agent and roll back code. On WAN this is dangerous -- "
                        "unset it so those stay local-only.")

    # Recommend the safe path
    recommendation = (
        "SAFEST WAN HOSTING: use Cloudflare Tunnel (outbound, no router ports opened) + "
        "Cloudflare Access (password). This reaches your laptop from the WAN without exposing "
        "your home IP or opening your firewall. Avoid raw router port-forwarding for a system "
        "with an autonomous agent unless you fully understand the exposure."
    )

    if critical:
        grade = "UNSAFE -- do not expose until fixed"
    elif warnings:
        grade = "ACCEPTABLE with cautions"
    else:
        grade = "OK"

    return {"grade": grade, "critical": critical, "warnings": warnings,
            "recommendation": recommendation}


def print_preflight(report: Dict):
    line = "=" * 64
    print("\n" + line)
    print(f"  WAN HOSTING PREFLIGHT -- grade: {report['grade']}")
    print(line)
    for c in report["critical"]:
        print(f"  [CRITICAL] {c}\n")
    for w in report["warnings"]:
        print(f"  [warn] {w}\n")
    print(f"  {report['recommendation']}")
    print(line + "\n")


class WanAccessLog:
    """Tracks who is hitting the machine -- visibility is half of security."""
    def __init__(self, cap: int = 500):
        self.events: deque = deque(maxlen=cap)
        self.by_ip: Dict[str, int] = defaultdict(int)
        self.denied: Dict[str, int] = defaultdict(int)

    def record(self, ip: str, path: str, allowed: bool):
        self.by_ip[ip] += 1
        if not allowed:
            self.denied[ip] += 1
        self.events.append({"ts": datetime.now(timezone.utc).isoformat(),
                            "ip": ip, "path": path, "allowed": allowed})

    def report(self) -> Dict:
        # Surface IPs with many denials -- likely someone probing
        suspicious = sorted([(ip, n) for ip, n in self.denied.items() if n >= 5],
                           key=lambda x: -x[1])
        return {"total_requests": sum(self.by_ip.values()),
                "unique_ips": len(self.by_ip),
                "denied_attempts": sum(self.denied.values()),
                "suspicious_ips": suspicious[:10],
                "recent": list(self.events)[-30:]}


# Paths that must NEVER be reachable from a non-local caller, even with a valid key,
# unless explicitly overridden. The agent's most dangerous capabilities.
WAN_BLOCKED_PATHS = {
    "/api/agent/start",       # starting the 24/7 self-editing loop
    "/api/agent/rollback",    # rewriting git history
    "/api/agent/cycle",       # running an agent cycle (writes code)
}
