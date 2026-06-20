// goldScout.js — reshaped: a scam-FILTER, not a scam-indexer.
//
// WHAT CHANGED FROM THE BASE44 VERSION:
//   - Every web result is now run through analyzeOpportunity() and gets a risk score
//     + red flags BEFORE being stored. The old version stored raw search hits as
//     "treasure"; this version maps the mines.
//   - Critical-risk findings are flagged hard (or dropped) instead of shown as leads.
//   - No EXECUTE / auto-action. Findings are RESEARCH LEADS with risk ratings.
//   - Honest category framing: these are labor/quest/airdrop activities, not "free money".
//
// This runs the same Tavily searches across chains — but its job is to protect you
// from the 90% that are traps, which is the actual valuable skill in this space.

import { analyzeOpportunity } from "./scamAnalysis.js";

// Honest search queries — note we deliberately AVOID "free money" style queries that
// return pure scam-bait, and instead target legitimate, named activity types.
const SEARCHES = [
  // Airdrops / testnets — named, current, legitimate framing
  { q: "legitimate crypto airdrops this month verified projects", category: "airdrop", chain: "multi" },
  { q: "active testnet incentive programs ethereum arbitrum", category: "testnet", chain: "ethereum" },
  { q: "Solana ecosystem airdrop eligibility checker official", category: "airdrop", chain: "solana" },
  // Quests / learn-to-earn — genuinely $0 labor
  { q: "Layer3 Galxe active quests rewards", category: "quest", chain: "multi" },
  { q: "learn to earn crypto programs official", category: "quest", chain: "multi" },
  // Staking / yield — real but capital-required (flagged as such)
  { q: "liquid staking SOL JitoSOL mSOL official rates", category: "staking", chain: "solana" },
  // Points programs — speculative labor
  { q: "crypto points programs active 2026 official", category: "points", chain: "multi" },
  // DePIN — real-world labor
  { q: "DePIN projects earn rewards hardware official", category: "depin", chain: "multi" },
];

/**
 * Base44 backend function signature. Returns findings WITH risk analysis attached.
 * Replace `tavilySearch` with Base44's actual Tavily integration call.
 */
export default async function goldScout({ tavilySearch, Opportunity }) {
  const findings = [];
  let scanned = 0, flaggedScam = 0, kept = 0;

  for (const search of SEARCHES) {
    let results = [];
    try {
      results = await tavilySearch({ query: search.q, max_results: 5 });
    } catch (e) {
      continue; // a failed search shouldn't kill the run
    }

    for (const r of (results || [])) {
      scanned++;
      const opp = {
        title: r.title || "",
        url: r.url || "",
        snippet: r.content || r.snippet || "",
        category: search.category,
        chain: search.chain,
      };

      // ── THE FILTER: analyze before storing ──
      const analysis = analyzeOpportunity(opp);

      // Drop the worst — don't even surface guaranteed-theft results as leads
      if (analysis.riskScore >= 85) {
        flaggedScam++;
        continue;
      }
      if (analysis.riskLevel === "critical" || analysis.riskLevel === "high") {
        flaggedScam++;
      }

      const record = {
        ...opp,
        risk_score: analysis.riskScore,
        risk_level: analysis.riskLevel,
        red_flags: analysis.flags,
        domain_trust: analysis.domainTrust,
        verdict: analysis.verdict,
        // Honest framing — never "free money"
        opportunity_type: labelType(search.category),
        requires_capital: ["staking", "lending", "lp"].includes(search.category),
        is_labor: ["airdrop", "testnet", "quest", "points", "depin"].includes(search.category),
        found_at: new Date().toISOString(),
        status: "lead", // NOT "executable" — it's a research lead
      };

      // Persist via Base44 entity
      if (Opportunity?.create) {
        try { await Opportunity.create(record); kept++; } catch {}
      }
      findings.push(record);
    }
  }

  return {
    summary: {
      scanned,
      kept,
      flagged_as_risky: flaggedScam,
      note: `Scanned ${scanned} web results. ${flaggedScam} carried scam red flags. ` +
            `These are RESEARCH LEADS with risk ratings — verify each independently and ` +
            `act manually in your own wallet. No auto-execution. Absence of flags ≠ safe.`,
    },
    findings: findings.sort((a, b) => a.risk_score - b.risk_score), // safest-looking first
  };
}

function labelType(category) {
  const map = {
    airdrop: "Airdrop (labor — eligibility tasks, scam-heavy, verify carefully)",
    testnet: "Testnet farming (labor — real tasks, possible future token)",
    quest: "Quest / learn-to-earn (labor — small payouts)",
    staking: "Staking (requires capital — real but modest yield, carries risk)",
    lending: "Lending (requires capital — yield with principal risk)",
    lp: "Liquidity providing (requires capital — impermanent-loss risk)",
    points: "Points program (speculative labor — no guaranteed payout)",
    depin: "DePIN (real-world labor / hardware)",
  };
  return map[category] || category;
}
