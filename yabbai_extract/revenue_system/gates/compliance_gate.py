#!/usr/bin/env python3
"""
YABBAI Ops — Compliance Gate

Enforces the reputation/legal rules from the spec as actual checks. Outbound messages
must pass opt-in/unsubscribe checks; data sources must be permitted; claims can't be
fabricated. Reputation is a balance-sheet asset — this protects it.

These are guardrails, not legal advice. When something is unclear, the gate flags it
for human/legal review rather than guessing.
"""

import re
from dataclasses import dataclass
from typing import List, Dict


# Phrases that signal fabricated claims / income promises / fake scarcity — banned.
BANNED_CLAIM_PATTERNS = [
    (r"guaranteed (returns?|profit|income|results?)", "Guaranteed-returns claim (banned)"),
    (r"(make|earn) \$?\d+.{0,15}(per|a|each) (day|week|month)", "Income/earnings claim (banned)"),
    (r"risk[- ]?free", "'Risk-free' claim (banned)"),
    (r"limited spots|only \d+ left|act now|expires (today|soon)", "Fake scarcity (banned)"),
    (r"\d+,?\d* (happy |satisfied )?customers", "Unverified customer-count claim — needs real data"),
    (r"as seen (on|in)", "'As seen on' claim — needs to be true and verifiable"),
]


@dataclass
class ComplianceResult:
    ok: bool
    issues: List[str]
    requires_human: bool
    note: str


class ComplianceGate:
    def check_outbound(self, message: str, recipient_opted_in: bool,
                       has_unsubscribe: bool, list_source: str) -> ComplianceResult:
        """Outbound email/DM rules: opt-in or compliant cold outreach, unsubscribe,
        no purchased lists."""
        issues = []
        if list_source.lower() in ("purchased", "bought", "scraped"):
            issues.append("List was purchased/scraped — not permitted. Use opt-in or "
                          "compliant cold-outreach sources only.")
        if not has_unsubscribe:
            issues.append("No working unsubscribe — required for outbound (CAN-SPAM/GDPR/CASL).")
        if not recipient_opted_in and list_source.lower() != "compliant_cold":
            issues.append("Recipient hasn't opted in and source isn't compliant cold outreach.")
        # claim check on the message body
        claim = self._check_claims(message)
        issues.extend(claim)
        return ComplianceResult(
            ok=len(issues) == 0, issues=issues,
            requires_human=len(issues) > 0,
            note=("Outbound passes basic compliance — still your responsibility to follow "
                  "applicable law." if not issues else
                  "Outbound BLOCKED until issues resolved. Reputation/legal risk."))

    def check_data_source(self, source_description: str, terms_permit: bool) -> ComplianceResult:
        """Data acquisition: only from sources whose terms permit it."""
        issues = []
        low = source_description.lower()
        if any(k in low for k in ["scrape", "credential", "auth wall", "bypass", "behind login"]):
            issues.append("Source involves scraping/auth-circumvention — disallowed unless "
                          "terms explicitly permit.")
        if not terms_permit:
            issues.append("Source terms don't permit this use (or unverified). Treat as disallowed.")
        return ComplianceResult(
            ok=len(issues) == 0, issues=issues, requires_human=len(issues) > 0,
            note="Data source OK." if not issues else "Data source BLOCKED — ToS/legal risk.")

    def check_claims(self, text: str) -> ComplianceResult:
        issues = self._check_claims(text)
        return ComplianceResult(
            ok=len(issues) == 0, issues=issues, requires_human=len(issues) > 0,
            note="No fabricated/banned claims detected." if not issues else
                 "Contains banned claims — rewrite before publishing.")

    def _check_claims(self, text: str) -> List[str]:
        found = []
        low = text.lower()
        for pat, label in BANNED_CLAIM_PATTERNS:
            if re.search(pat, low):
                found.append(label)
        return found
