# GoldScout — Reshaped (scam-filter, not scam-indexer)

Drop-in replacements for the Base44 files. Keeps the pirate theme, multi-chain
scout, and findings grid — but flips the purpose from "click EXECUTE to go connect
your wallet" to "researched leads with risk ratings, you act manually."

## Files
- functions/scamAnalysis.js  — the real edge. Scores every opportunity for
  drainer/phishing/scam patterns, returns risk level + named red flags. TESTED:
  correctly flags fake-airdrop, seed-phrase phishing, and advance-fee scams as
  CRITICAL while passing real protocols (Jupiter, Marinade, Layer3) as LOW.
- functions/goldScout.js      — runs the Tavily searches, but filters EVERY result
  through scamAnalysis before storing. Drops guaranteed-theft hits, flags risky
  ones, sorts safest-first. Honest category labels (labor vs capital-required).
- pages/GoldFindings.jsx      — RESEARCH view. No EXECUTE-to-protocol button.
  Each card leads with risk level + red flags; "Research" opens a safety-check
  confirm first. Golden safety rules pinned at top.

## What was removed and why
- The EXECUTE button that opened protocol URLs directly → trained the dangerous
  "see opportunity → connect wallet → approve" reflex on web-sourced links, which
  is exactly how wallets get drained. Replaced with research + risk disclosure.
- Auto-tier trading of real funds → not included. (Tier ROADMAP as guidance only,
  if you want it next — informational, never auto-trading.)
- "Free money" search queries → replaced with named, legitimate activity searches,
  because "free crypto" queries return almost pure scam-bait.

## Integrate in Base44
1. Replace your goldScout function body with functions/goldScout.js (wire its
   `tavilySearch` param to Base44's Tavily integration, `Opportunity` to your entity).
2. Add functions/scamAnalysis.js as a shared module it imports.
3. Replace the GoldFindings page with pages/GoldFindings.jsx.
4. Add fields to the Opportunity entity: risk_score (number), risk_level (string),
   red_flags (array), domain_trust (string), verdict (string), opportunity_type
   (string), status (string).

## The honest truth this is built on
No automated check catches 100% of scams. LOW risk = "no obvious flags found",
NOT "safe". This tool makes you decide with eyes open — it is not a green light to
skip your own judgment, and it never touches your wallet or executes anything.
