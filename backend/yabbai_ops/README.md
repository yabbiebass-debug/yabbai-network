# YABBAI Ops — Founder Cockpit

The honest version of the "autonomous revenue OS" spec. It gives you the real
operational discipline a business needs — without pretending it generates profit on
its own (which isn't a real thing).

## What it actually does (all tested, all working)

**1. Truth Protocol** (`core/truth_protocol.py`) — the genuinely valuable core.
Every metric is typed with a mandatory source. Key properties, enforced as code:
- Values without a source are NULL, not zero.
- An ESTIMATE can NEVER be consumed in a decision as if it were real (raises DataGap).
- Revenue only rolls into TOTALS if reconciled to a payment-processor export.
- Missing data → "DATA GAP" and the decision halts. It never fabricates a number.
Tested: refuses estimates/missing data in decisions; counts only reconciled revenue.

**2. Decision Engine + Gates** (`core/decision_engine.py`)
- EV tickets: every action gets EV = P(success)×upside − (cost+risk), so you decide
  with numbers.
- Approval gates by reversibility+cost: LOW (reversible, under micro-cap) acts
  autonomously; MEDIUM needs a peer-agent check; HIGH (irreversible OR over the spend
  cap OR any send/purchase/publish/payout) is QUEUED for human approval, never executed.
- Daily spend cap as a backstop. Tested: sends/purchases/publishes/over-cap all queue;
  content drafting stays low-risk.

**3. Compliance Gate** (`core/compliance_gate.py`)
- Outbound: opt-in or compliant cold outreach only, working unsubscribe, no purchased
  lists. Data: only ToS-permitted sources, no auth-wall scraping. Claims: no fabricated
  testimonials/metrics, no income/earnings claims, no fake scarcity, no "risk-free."
Tested: catches purchased lists, income claims, fake scarcity, auth-wall scraping.

**4. Cockpit + UI** (`core/ops_cockpit.py`, `ui/cockpit.html`, `server.py`)
A founder's dashboard tying it together: reconciled-revenue total, KPIs (with DATA GAP
flags), EV scoring, copy/outreach compliance checks, and the human-approval queue.

## Run it
```
cd yabbai_ops
python -m venv venv && source venv/bin/activate   # (Windows: venv\Scripts\activate)
pip install fastapi uvicorn
python -m uvicorn server:app --host 127.0.0.1 --port 7870
```
Open http://localhost:7870.

Configurable caps: OPS_MICRO_CAP (default $5), OPS_SPEND_CAP ($50), OPS_DAILY_CAP ($100).

## The honest truth this is built on
This organizes your work, scores opportunities, enforces compliance, and gates every
money/irreversible action behind your approval. It is real operational discipline —
the kind that keeps a business from lying to itself or blowing its budget.

What it does NOT do is autonomously generate profit. Getting real paying customers,
delivering real value, closing real sales — that's the actual work of a business, and
no agent loop conjures it. The framework makes you a more disciplined operator. It
doesn't replace the operating. Success = reconciled cash from real customers. That's
yours to earn — this just keeps you honest and organized while you do.
