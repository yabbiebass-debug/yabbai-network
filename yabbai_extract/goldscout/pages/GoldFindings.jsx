import React, { useState, useEffect } from "react";
import { Opportunity } from "@/entities/Opportunity";
import { GOLDEN_RULES } from "@/functions/scamAnalysis";

// GoldFindings — reshaped from the Base44 EXECUTE version into a RESEARCH view.
//
// KEY CHANGES:
//   - No "EXECUTE" button that opens a protocol URL to go connect a wallet.
//     Instead: "RESEARCH" — opens the page in a new tab with a safety interstitial,
//     and shows the risk analysis right on the card so you decide with eyes open.
//   - Every card leads with its risk level + red flags, not with hype.
//   - Golden safety rules pinned at the top, always visible.
//   - Honest labels: "research lead", risk-sorted safest-first.

const RISK_COLORS = {
  low: "#14F195", medium: "#F5A623", high: "#f97316", critical: "#ef4444",
};

export default function GoldFindings() {
  const [findings, setFindings] = useState([]);
  const [filter, setFilter] = useState("all");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Opportunity.list("-found_at")
      .then(setFindings)
      .catch(() => setFindings([]))
      .finally(() => setLoading(false));
  }, []);

  const filtered = findings.filter(f =>
    filter === "all" ? true :
    filter === "safe-ish" ? f.risk_level === "low" :
    filter === "flagged" ? ["high", "critical"].includes(f.risk_level) :
    f.category === filter
  );

  const handleResearch = (f) => {
    const proceed = window.confirm(
      `SAFETY CHECK before you open this\n\n` +
      `${f.title}\nRisk: ${f.risk_level.toUpperCase()} (${f.risk_score}/100)\n\n` +
      (f.red_flags?.length ? `Red flags:\n• ${f.red_flags.join("\n• ")}\n\n` : "") +
      `Remember:\n` +
      `• NEVER enter your seed phrase\n` +
      `• Verify the URL matches the official project\n` +
      `• Use a burner wallet with minimal funds\n` +
      `• Reject 'unlimited approval' requests\n\n` +
      `This opens the page in a new tab for RESEARCH only. You act manually, in your own wallet.\n\n` +
      `Open it?`
    );
    if (proceed) window.open(f.url, "_blank", "noopener,noreferrer");
  };

  return (
    <div style={{ padding: 24, color: "#e8f0ff", fontFamily: "'JetBrains Mono', monospace" }}>
      <h1 style={{ fontFamily: "'Unbounded'", fontSize: 24 }}>
        🏴‍☠️ Gold Findings <span style={{ color: "#4a6080", fontSize: 13 }}>— research leads, risk-rated</span>
      </h1>

      {/* Golden rules — always visible */}
      <div style={{ background: "rgba(245,166,35,0.08)", border: "1px solid #F5A623",
        borderRadius: 8, padding: 14, margin: "16px 0", fontSize: 12.5, lineHeight: 1.7 }}>
        <b style={{ color: "#F5A623" }}>⚓ NON-NEGOTIABLE SAFETY RULES</b>
        <ul style={{ margin: "8px 0 0", paddingLeft: 18 }}>
          {GOLDEN_RULES.map((r, i) => <li key={i}>{r}</li>)}
        </ul>
      </div>

      {/* Filters */}
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 16 }}>
        {["all", "safe-ish", "flagged", "airdrop", "testnet", "quest", "staking", "points", "depin"].map(f => (
          <button key={f} onClick={() => setFilter(f)}
            style={{ background: filter === f ? "rgba(153,69,255,0.2)" : "transparent",
              border: "1px solid rgba(153,69,255,0.35)", color: filter === f ? "#9945FF" : "#4a6080",
              padding: "5px 12px", borderRadius: 4, cursor: "pointer", fontSize: 11,
              textTransform: "uppercase", fontFamily: "inherit" }}>
            {f}
          </button>
        ))}
      </div>

      {loading && <p style={{ color: "#4a6080" }}>Scanning the seas…</p>}
      {!loading && filtered.length === 0 && (
        <p style={{ color: "#4a6080" }}>No findings yet. Run Gold Scout to scour the web.</p>
      )}

      {/* Findings */}
      <div style={{ display: "grid", gap: 12, gridTemplateColumns: "repeat(auto-fill,minmax(300px,1fr))" }}>
        {filtered.map((f, i) => (
          <div key={i} style={{ background: "#060e20",
            border: `1px solid ${RISK_COLORS[f.risk_level] || "#4a6080"}`,
            borderRadius: 8, padding: 14 }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "start" }}>
              <span style={{ fontSize: 10, textTransform: "uppercase", color: "#4a6080" }}>
                {f.opportunity_type || f.category}
              </span>
              <span style={{ fontSize: 10, fontWeight: 700, padding: "2px 8px", borderRadius: 4,
                color: RISK_COLORS[f.risk_level], border: `1px solid ${RISK_COLORS[f.risk_level]}` }}>
                {f.risk_level?.toUpperCase()} {f.risk_score}/100
              </span>
            </div>

            <h3 style={{ fontSize: 14, margin: "8px 0", lineHeight: 1.3 }}>{f.title}</h3>
            <p style={{ fontSize: 11.5, color: "#4a6080", lineHeight: 1.5, marginBottom: 8 }}>
              {(f.snippet || "").slice(0, 120)}…
            </p>

            {/* Red flags front and center */}
            {f.red_flags?.length > 0 && (
              <div style={{ fontSize: 11, color: "#ef4444", marginBottom: 8, lineHeight: 1.5 }}>
                {f.red_flags.map((flag, j) => <div key={j}>🚩 {flag}</div>)}
              </div>
            )}

            <div style={{ fontSize: 11, color: "#4a6080", fontStyle: "italic", marginBottom: 10 }}>
              {f.verdict}
            </div>

            <button onClick={() => handleResearch(f)}
              style={{ background: "transparent", border: "1px solid #9945FF", color: "#9945FF",
                padding: "7px 14px", borderRadius: 5, cursor: "pointer", fontSize: 11,
                textTransform: "uppercase", fontFamily: "inherit", width: "100%" }}>
              🔍 Research (opens with safety check)
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}
