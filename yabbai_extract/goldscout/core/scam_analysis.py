"""
GoldScout -- Scam Analysis Engine (Python port of scamAnalysis.js)

Every opportunity found by the scout is torn apart here for patterns that signal
a scam / wallet-drainer BEFORE it's ever shown to you.

Returns a risk score + named red flags. You still decide and act manually.
LOW score = "no obvious flags found", NOT "guaranteed safe".
"""

import re
from urllib.parse import urlparse
from typing import TypedDict, List

# ── Known-legit domains (boosts confidence, doesn't guarantee safety) ─────────
KNOWN_LEGIT_DOMAINS = {
    "jup.ag", "jupiter.exchange", "ethereum.org", "arbitrum.io", "base.org",
    "sui.io", "polygon.technology", "galxe.com", "layer3.xyz", "coinbase.com",
    "uniswap.org", "aave.com", "lido.fi", "marinade.finance", "jito.network",
    "kamino.finance", "drift.trade", "marginfi.com", "optimism.io", "zksync.io",
    "defillama.com", "dappradar.com",
}

# ── Suspicious domain patterns ────────────────────────────────────────────────
SUSPICIOUS_DOMAIN_PATTERNS = [
    (r"airdrop.?claim",           "Domain contains 'airdrop-claim' -- classic drainer pattern"),
    (r"claim.?(now|reward|token)","Domain pushes urgency to 'claim now' -- common scam framing"),
    (r"(jupiter|uniswap|aave|lido|arbitrum|solana|metamask|phantom).?(airdrop|claim|reward|gift|bonus)",
                                  "Domain impersonates a known protocol with a claim/reward suffix"),
    (r"\.(xyz|top|click|gift|live|online|site|club)$",
                                  "Cheap TLD often used by throwaway scam sites"),
    (r"free.?(crypto|eth|sol|token|money|usdt)",
                                  "Domain literally advertises 'free crypto/money'"),
    (r"\d{1,3}.?(eth|sol|btc|usdt)",
                                  "Domain promises a fixed crypto payout -- bait pattern"),
    (r"(wallet|seed|connect).?(verify|validate|sync|restore)",
                                  "Domain references wallet 'verify/sync/restore' -- seed-phrase phishing"),
]

# ── Text signals (title + snippet) ────────────────────────────────────────────
SCAM_TEXT_SIGNALS = [
    (r"connect (your )?wallet to (claim|receive|verify)",
     "Asks you to connect wallet to claim -- drainer hallmark", 30),
    (r"enter (your )?(seed|recovery|private key|mnemonic)",
     "Requests seed/private key -- NEVER legitimate, guaranteed theft", 100),
    (r"guaranteed (returns?|profit|roi|gains)",
     "Promises guaranteed returns -- impossible, scam tell", 40),
    (r"\b\d{2,4}%\s*(apy|apr|returns?|daily|weekly)",
     "Advertises implausibly high yield", 25),
    (r"limited time|act now|hurry|expires? (soon|in)",
     "Artificial urgency -- pressure tactic", 15),
    (r"send .{0,20}(to receive|to claim|first)",
     "Asks you to SEND crypto to receive more -- advance-fee scam", 100),
    (r"double your|2x your|multiply your",
     "'Double your crypto' -- classic giveaway scam", 60),
    (r"approve (unlimited|max|all)",
     "Pushes unlimited token approval -- drains the approved token", 50),
]

GOLDEN_RULES = [
    "Never enter your seed phrase / private key anywhere. No legitimate airdrop ever asks for it.",
    "Verify the URL character-by-character against the project's official site/socials.",
    "Use a fresh / burner wallet with minimal funds for any new interaction.",
    "Review every transaction in your wallet before signing -- reject 'unlimited approval' requests.",
    "If it promises guaranteed or huge returns, or rushes you, it's a scam. Walk away.",
    "A LOW risk score means 'no obvious flags found', not 'safe'. Your judgment is the final check.",
]


def extract_domain(url: str) -> str:
    try:
        return urlparse(url).hostname.lstrip("www.").lower()
    except Exception:
        return ""


def is_known_legit(domain: str) -> bool:
    return domain in KNOWN_LEGIT_DOMAINS or any(
        domain.endswith("." + d) for d in KNOWN_LEGIT_DOMAINS)


def analyze_opportunity(opp: dict) -> dict:
    """
    Returns: {riskScore, riskLevel, flags, domainTrust, verdict}
    riskScore: 0-100  (higher = more suspicious)
    """
    flags: List[str] = []
    score = 0

    domain = extract_domain(opp.get("url", ""))
    haystack = f"{opp.get('title', '')} {opp.get('snippet', '')}"

    # Domain trust
    domain_trust = "unknown"
    if domain and is_known_legit(domain):
        domain_trust = "known-legit"
        score -= 20
    elif not domain:
        domain_trust = "no-domain"
        score += 15
        flags.append("No valid URL/domain to verify -- treat with suspicion")

    # Suspicious domain patterns
    for pattern, flag in SUSPICIOUS_DOMAIN_PATTERNS:
        if domain and re.search(pattern, domain, re.IGNORECASE):
            score += 35
            flags.append(flag)

    # Text signals
    for pattern, flag, weight in SCAM_TEXT_SIGNALS:
        if re.search(pattern, haystack, re.IGNORECASE):
            score += weight
            flags.append(flag)

    # No HTTPS
    url = opp.get("url", "")
    if url and url.startswith("http://"):
        score += 20
        flags.append("Not HTTPS -- insecure, unusual for a real protocol")

    # Clamp 0-100
    score = max(0, min(100, score))

    if score >= 70:
        risk_level = "critical"
        verdict = ("Strong scam signals. Do NOT connect a wallet here. "
                   "Almost certainly a drainer/phishing site.")
    elif score >= 40:
        risk_level = "high"
        verdict = ("Multiple red flags. Treat as likely unsafe -- "
                   "verify independently before going near it.")
    elif score >= 20:
        risk_level = "medium"
        verdict = ("Some concerns. Research the project independently "
                   "(official socials, docs) before acting.")
    else:
        risk_level = "low"
        if domain_trust == "known-legit":
            verdict = ("No obvious red flags and domain looks legit -- "
                       "still verify the exact URL yourself before connecting anything.")
        else:
            verdict = ("No obvious red flags found, but absence of flags is NOT proof of safety. "
                       "Verify independently.")

    return {
        "riskScore": score, "riskLevel": risk_level,
        "flags": flags, "domainTrust": domain_trust, "verdict": verdict,
    }


def label_type(category: str) -> str:
    return {
        "airdrop": "Airdrop (labor -- eligibility tasks, scam-heavy, verify carefully)",
        "testnet": "Testnet farming (labor -- real tasks, possible future token)",
        "quest":   "Quest / learn-to-earn (labor -- small payouts)",
        "staking": "Staking (requires capital -- real but modest yield, carries risk)",
        "lending": "Lending (requires capital -- yield with principal risk)",
        "lp":      "Liquidity providing (requires capital -- impermanent-loss risk)",
        "points":  "Points program (speculative labor -- no guaranteed payout)",
        "depin":   "DePIN (real-world labor / hardware)",
    }.get(category, category)
