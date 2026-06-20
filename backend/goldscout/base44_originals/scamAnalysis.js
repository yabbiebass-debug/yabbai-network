// scamAnalysis.js — the real edge of GoldScout
//
// This is what flips the project from "indexes the minefield" to "maps the mines."
// Every opportunity the scout finds gets torn apart here for the patterns that
// signal a scam / wallet-drainer, BEFORE it's ever shown to you with a label.
//
// It does NOT click anything, connect any wallet, or execute anything. It analyzes
// URLs, domains, and text signals and returns a risk score + human-readable red flags.
// You still decide and act manually. This just makes sure you decide with eyes open.
//
// Honest scope: no automated check catches 100% of scams. A LOW score means "no
// obvious red flags found," NOT "guaranteed safe." The flags are decision support,
// never a green light to skip your own judgment.

// ── Known-legit domains (allowlist boosts confidence, doesn't guarantee) ─────
const KNOWN_LEGIT_DOMAINS = [
  "jup.ag", "jupiter.exchange", "ethereum.org", "arbitrum.io", "base.org",
  "sui.io", "polygon.technology", "galxe.com", "layer3.xyz", "coinbase.com",
  "uniswap.org", "aave.com", "lido.fi", "marinade.finance", "jito.network",
  "kamino.finance", "drift.trade", "marginfi.com", "optimism.io", "zksync.io",
  "defillama.com", "dappradar.com",
];

// ── Drainer / phishing domain patterns ──────────────────────────────────────
const SUSPICIOUS_DOMAIN_PATTERNS = [
  { re: /-?airdrop-?claim/i, flag: "Domain contains 'airdrop-claim' — classic drainer pattern" },
  { re: /-?claim-?(now|reward|token)/i, flag: "Domain pushes urgency to 'claim now' — common scam framing" },
  { re: /(jupiter|uniswap|aave|lido|arbitrum|solana|metamask|phantom)[-.]?(airdrop|claim|reward|gift|bonus)/i,
    flag: "Domain impersonates a known protocol's name with a claim/reward suffix" },
  { re: /\.(xyz|top|click|gift|live|online|site|club)$/i, flag: "Cheap TLD often used by throwaway scam sites" },
  { re: /free-?(crypto|eth|sol|token|money|usdt)/i, flag: "Domain literally advertises 'free crypto/money'" },
  { re: /\d{1,3}-?(eth|sol|btc|usdt)/i, flag: "Domain promises a fixed crypto payout — bait pattern" },
  { re: /(wallet|seed|connect)-?(verify|validate|sync|restore)/i,
    flag: "Domain references wallet 'verify/sync/restore' — seed-phrase phishing pattern" },
];

// ── Text signals in the title/snippet ────────────────────────────────────────
const SCAM_TEXT_SIGNALS = [
  { re: /connect (your )?wallet to (claim|receive|verify)/i, flag: "Asks you to connect wallet to claim — drainer hallmark", weight: 30 },
  { re: /enter (your )?(seed|recovery|private key|mnemonic)/i, flag: "Requests seed/private key — NEVER legitimate, guaranteed theft", weight: 100 },
  { re: /guaranteed (returns?|profit|roi|gains)/i, flag: "Promises guaranteed returns — impossible, scam tell", weight: 40 },
  { re: /\b(\d{2,4})%\s*(apy|apr|returns?|daily|weekly)/i, flag: "Advertises implausibly high yield", weight: 25 },
  { re: /limited time|act now|hurry|expires? (soon|in)/i, flag: "Artificial urgency — pressure tactic", weight: 15 },
  { re: /send (\d|some|any) .{0,10}(to receive|to claim|first)/i, flag: "Asks you to SEND crypto to receive more — advance-fee scam", weight: 100 },
  { re: /double your|2x your|multiply your/i, flag: "'Double your crypto' — classic giveaway scam", weight: 60 },
  { re: /approve (unlimited|max|all)/i, flag: "Pushes unlimited token approval — drains the approved token", weight: 50 },
];

function extractDomain(url) {
  try {
    return new URL(url).hostname.replace(/^www\./, "").toLowerCase();
  } catch {
    return "";
  }
}

function isKnownLegit(domain) {
  return KNOWN_LEGIT_DOMAINS.some(d => domain === d || domain.endsWith("." + d));
}

/**
 * analyzeOpportunity — the core function.
 * @param {{title?:string, url?:string, snippet?:string, category?:string}} opp
 * @returns {{riskScore:number, riskLevel:string, flags:string[], domainTrust:string, verdict:string}}
 */
export function analyzeOpportunity(opp) {
  const flags = [];
  let score = 0;

  const domain = extractDomain(opp.url || "");
  const haystack = `${opp.title || ""} ${opp.snippet || ""}`;

  // Domain trust
  let domainTrust = "unknown";
  if (domain && isKnownLegit(domain)) {
    domainTrust = "known-legit";
    score -= 20; // lowers risk, but does NOT make it safe on its own
  } else if (!domain) {
    domainTrust = "no-domain";
    score += 15;
    flags.push("No valid URL/domain to verify — treat with suspicion");
  }

  // Suspicious domain patterns
  for (const p of SUSPICIOUS_DOMAIN_PATTERNS) {
    if (domain && p.re.test(domain)) { score += 35; flags.push(p.flag); }
  }

  // Text signals
  for (const s of SCAM_TEXT_SIGNALS) {
    if (s.re.test(haystack)) { score += s.weight; flags.push(s.flag); }
  }

  // No HTTPS
  if (opp.url && opp.url.startsWith("http://")) {
    score += 20; flags.push("Not HTTPS — insecure, unusual for a real protocol");
  }

  // Clamp
  score = Math.max(0, Math.min(100, score));

  // Risk level
  let riskLevel, verdict;
  if (score >= 70) {
    riskLevel = "critical";
    verdict = "Strong scam signals. Do NOT connect a wallet here. Almost certainly a drainer/phishing site.";
  } else if (score >= 40) {
    riskLevel = "high";
    verdict = "Multiple red flags. Treat as likely unsafe — verify independently before going near it.";
  } else if (score >= 20) {
    riskLevel = "medium";
    verdict = "Some concerns. Research the project independently (official socials, docs) before acting.";
  } else {
    riskLevel = "low";
    verdict = domainTrust === "known-legit"
      ? "No obvious red flags and domain looks legit — still verify the exact URL yourself before connecting anything."
      : "No obvious red flags found, but absence of flags is NOT proof of safety. Verify independently.";
  }

  return { riskScore: score, riskLevel, flags, domainTrust, verdict };
}

/**
 * Safety rules shown to the user on every opportunity — the non-negotiables.
 */
export const GOLDEN_RULES = [
  "Never enter your seed phrase / private key anywhere. No legitimate airdrop ever asks for it.",
  "Verify the URL character-by-character against the project's official site/socials.",
  "Use a fresh / burner wallet with minimal funds for any new interaction.",
  "Review every transaction in your wallet before signing — reject 'unlimited approval' requests.",
  "If it promises guaranteed or huge returns, or rushes you, it's a scam. Walk away.",
  "A LOW risk score means 'no obvious flags found', not 'safe'. Your judgment is the final check.",
];
