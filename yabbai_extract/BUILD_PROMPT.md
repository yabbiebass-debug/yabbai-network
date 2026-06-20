# YABBAI.NETWORK V2 — EMERGENT BUILD PROMPT

Paste everything below this line into Emergent.

---

YABBAI.NETWORK V2 — UNIFIED NETWORK BUILD

You are completing the final unified version of the YABBAI network.
This is an ADDITIVE, INTEGRATION build — do NOT remove or break any
existing functionality. Analyse what exists, keep everything, wire it
together under one hub with one sign-in and one consistent design.

════════════════════════════════════════════════════════════════════
WHAT THIS ZIP CONTAINS — 15 SURFACES, TWO STACKS
════════════════════════════════════════════════════════════════════

STACK A — SUPABASE WEB SURFACES (Netlify-deployable static HTML):
  hub/index.html          ← main entry portal (ALREADY EXTENDED — keep)
  app/index.html          ← Mission Control / Director cockpit
  os/index.html           ← Realm OS / ecosystem map
  floor/index.html        ← Agency Floor / leads + AI call guides
  studio/index.html       ← Catalog Studio / product builder
  portal/index.html       ← Client Portal (public)
  vault/index.html        ← Product Vault (public buyers)
  diagnose/index.html     ← Free Fix-Plan tool (public, no auth)
  widget/yabbai-connect.js← Embeddable AI widget for client sites
  supabase/functions/     ← Deno edge functions (ai-proxy, call-guide,
                             catalog-agent, cycle-runner, diagnose,
                             realm-intake, scope-brief, stripe-webhook,
                             vault-config, _shared/ratelimit)
  supabase/migrations/    ← 9 Postgres migrations (001–009)

STACK B — PYTHON FASTAPI BACKENDS:
  revenue_system/         ← Revenue System v1.1 (port 7870)
    unified_server.py       FastAPI server
    truth/metric.py         Metric(name,value,source,source_type,
                            fetched_at,confidence,reconciled)
    gates/gates.py          GateController: LOW/MEDIUM/HIGH, caps,
                            kill-switch, daily spend+loss caps
    gates/compliance_gate.py  Claims checker + outreach checker
    channels/products/      Books income from payment webhooks only
    channels/agency/        Pipeline ≠ income; paid-invoice only
    channels/trading/       Paper-only; NoKeySigner refuses live trades
    orchestrator.py         EV-scoring cycle driver
    memory/store.py         JSON-backed shared memory
    tests/test_safety.py    32 safety assertions (all pass)
    ui/dashboard.html       Admin dashboard (dark themed)
    API: /api/status /api/cycle /api/approvals /api/approve /api/reject
         /api/kill-switch /api/kill-switch/disengage /api/audit
         /api/ledger /webhooks/products /webhooks/invoice-paid
         /api/compliance/check-copy /api/compliance/check-outreach

  yabbai_local/           ← YABBAI Local AI v1.6 (port 7860)
    api/server.py           FastAPI, serves chat + tool + IDE
    core/brain.py           LLM brain (Anthropic/OpenRouter/Ollama)
    core/autonomous_agent.py  Autonomous task agent
    core/coding_brain.py    Code generation + execution
    core/agent_team.py      Multi-agent team coordination
    core/model_pool.py      Model management + routing
    core/learning.py        Persistent learning store
    core/hardware.py        System hardware monitor
    core/wan_security.py    WAN/tunnel security
    ui/index.html           Chat interface
    ui/ide.html             Code IDE
    ui/unified.html         Combined view (USE AS BASE FOR AI SURFACE)

  defi_simulator/         ← DeFi Paper Trading Engine (port 8002)
    api/server.py           FastAPI server
    api/dashboard.html      Paper trading dashboard
    core/paper_executor.py  Paper trade execution (NO real money)
    core/mode_controller.py Mode switching (paper only in this deploy)
    risk/risk_gate.py       Risk management
    routing/price_feed.py   Historical price data
    routing/live_price_feed.py  Live market data
    routing/routing_engine.py   Trade routing
    strategies/             Baseline + predictive strategies

  goldscout/              ← GoldScout Opportunity Scanner (port 8001)
    server.py               FastAPI + Google OAuth gate
    core/scout.py           Web scouting (airdrops, quests, testnets)
    core/scam_analysis.py   Scam detection + safety scoring
    ui/index.html           Findings dashboard
    functions/              Original JS functions (goldScout.js,
                            scamAnalysis.js)

  yabbai_ops/             ← Ops Cockpit (port 7880)
    server.py               FastAPI server
    core/truth_protocol.py  Truth protocol v1
    core/compliance_gate.py Compliance checker
    core/decision_engine.py Decision engine
    core/ops_cockpit.py     Ops orchestrator
    ui/cockpit.html         Cockpit dashboard

  yabbai_network_server.py← Unified gateway (port 8080) — starts all
                             sub-services + serves hub + /health

════════════════════════════════════════════════════════════════════
DESIGN SYSTEM — APPLY EXACTLY, NO EXCEPTIONS
════════════════════════════════════════════════════════════════════

CSS variables (every surface must use these exact values):
  --void:   #020814     deepest background
  --panel:  #081226     card / panel
  --panel2: #0c1a38     input / nested panel
  --purple: #9945FF     brand, active borders, highlights
  --green:  #14F195     confirmed, money, live, wins
  --amber:  #F5A623     warning, pending, pipeline
  --red:    #FF4D6D     danger, losses, kill-switch
  --ink:    #EEF1FB     primary text
  --dim:    #9AA3C0     secondary text
  --dim2:   #5e6688     muted / disabled
  --line:   rgba(153,69,255,.18)   subtle border

Fonts (load from Google Fonts):
  Unbounded  400,600,800  — headings, brand, KPI values, display
  JetBrains Mono  400,500,700  — body, code, labels, data

Body background:
  radial-gradient(1100px 600px at 50% -10%,
  rgba(153,69,255,.10),transparent 60%), #020814

Buttons:
  border: 1px solid var(--purple)
  color: var(--purple)
  background: transparent
  text-transform: uppercase
  letter-spacing: 1.5px
  border-radius: 3px
  hover: background var(--purple), color #fff

Nav bar:
  flex, border-bottom: 1px solid var(--line)
  brand Unbounded left, status chips right

Chips:
  9px, letter-spacing 1.5px, border-radius 99px
  border: 1px solid (color matches state)
  text-transform: uppercase

Cards:
  border: 1px solid var(--line)
  background: var(--panel)
  border-radius: 6px
  padding: 16-18px
  hover: border-color var(--purple), translateY(-2px)

════════════════════════════════════════════════════════════════════
WHAT NEEDS DOING — IN PRIORITY ORDER
════════════════════════════════════════════════════════════════════

PRIORITY 1 — HUB CONFIGURATION (already built, just configure):
  Open hub/index.html.
  Paste your Supabase URL + anon key into the CONFIG block.
  Paste each deployed Supabase surface URL into the SURFACES block.
  For Python backends on Railway/Render/Fly, update the PY block
  with deployed URLs (defaults are localhost for local dev).
  Deploy hub/ to Netlify as yabbai.network (or your domain).

PRIORITY 2 — SUPABASE SETUP:
  1. Run all 9 migrations in order (supabase/migrations/001-009).
  2. Deploy all edge functions:
       supabase functions deploy ai-proxy
       supabase functions deploy call-guide
       supabase functions deploy catalog-agent
       supabase functions deploy cycle-runner
       supabase functions deploy diagnose
       supabase functions deploy realm-intake
       supabase functions deploy scope-brief
       supabase functions deploy stripe-webhook
       supabase functions deploy vault-config
  3. Set Supabase secrets:
       supabase secrets set ANTHROPIC_API_KEY=sk-ant-...
       supabase secrets set ANTHROPIC_MODEL=claude-sonnet-4-6
       supabase secrets set STRIPE_SECRET_KEY=sk_live_...
       supabase secrets set STRIPE_WEBHOOK_SECRET=whsec_...
       supabase secrets set ADMIN_API_KEY=<same key as Python>

PRIORITY 3 — PYTHON BACKENDS:
  1. Copy .env.example to .env, fill ALL values.
  2. pip install -r requirements_all.txt
  3. Run: start_all.bat  (Windows)  or  bash start_all.sh  (Mac/Linux)
     OR:  python yabbai_network_server.py  (unified on port 8080)
  4. Open hub/index.html → enter your ADMIN_API_KEY → click
     "Connect backends" → all service chips turn green.
  5. Verify: Revenue System kill-switch shows "armed" (green).
     Trading signer shows "NoKeySigner — live trading disabled".

PRIORITY 4 — WEBHOOK WIRING:
  In Stripe dashboard:
    Add endpoint: https://your-supabase-project.supabase.co/functions/v1/stripe-webhook
    Events: checkout.session.completed, invoice.paid
  In Gumroad / product platform:
    Add endpoint: https://your-revenue-backend/webhooks/products
    Secret: PRODUCTS_WEBHOOK_SECRET from .env

PRIORITY 5 — OPENROUTER (optional, activates tier-2 brain):
  supabase secrets set OPENROUTER_API_KEY=sk-or-...
  supabase secrets set OPENROUTER_MODEL=anthropic/claude-sonnet-4-6
  No redeploy needed. Hub routing panel updates automatically.

════════════════════════════════════════════════════════════════════
SURFACES — FULL MAP
════════════════════════════════════════════════════════════════════

Hub (hub/index.html):
  Single sign-in via Supabase magic-link.
  Shows: 12-agent civilisation map (SVG, animated packets), live
  network strip (MRR, cash, reconciled income, leads, enquiries,
  fix-plans, products live, pending approvals), Python services
  status panel (health checks every 60s), routing panel (3-tier
  brain), 7 web surface cards + 5 Python surface cards.
  Nav: kill-switch button, pending-approvals chip, revenue income.
  Kill-switch: one click in nav → halts ALL revenue activity.

Mission Control (app/index.html):
  Pipeline table (leads by stage), approval queue (the 3 gates:
  outreach / contract / ship), agent cycle runner, money KPIs.
  Reads from: leads, approvals, money_view, cycle_log tables.

Realm OS (os/index.html):
  Ecosystem map of all surfaces, widget forge (generate embed code),
  real-time enquiry stream, surface health status.

Agency Floor (floor/index.html):
  Import leads from CSV, per-lead AI call guide (call-guide edge fn),
  stage management, outcome logging.

Catalog Studio (studio/index.html):
  AI-assisted product ideation (catalog-agent edge fn), audit scoring,
  "never sell air" constraint (only audited products go live).

Client Portal (portal/index.html):
  Client brief form → AI scope brief (scope-brief edge fn) → Stripe
  payment → build tracking → mutual ledger of value delivered.

The Vault (vault/index.html):
  Template store for buyers. Products configure themselves on purchase.

Free Fix-Plan / Diagnose (diagnose/index.html):
  Public, no auth. Anyone runs it → gets a real improvement plan →
  optionally books a call. The primary lead magnet.

Revenue System (http://localhost:7870 or deployed URL):
  Admin dashboard: reconciled income (payment_processor + reconciled=
  true only), DATA GAP flags, per-channel scorecards, approval queue
  (approve/reject pending actions), compliance copy-checker, audit log,
  full truth ledger, kill-switch engage/disengage.
  Safety: 32 passing tests. No fabricated numbers. No autonomous money
  movement. NoKeySigner refuses live trades.

YABBAI AI (http://localhost:7860):
  Three views: chat (index.html), IDE (ide.html), unified (unified.html)
  Brain: Anthropic → OpenRouter → Ollama (same 3-tier as Supabase).
  Capabilities: chat, code generation, autonomous tasks, tool use,
  multi-agent team, hardware monitoring, learning persistence.

GoldScout (http://localhost:8001):
  Google OAuth gated. Scans for DeFi opportunities: airdrops, quests,
  testnets, staking. Every finding run through scam analysis with a
  safety score. Only passes findings with clean scores.

DeFi Simulator (http://localhost:8002):
  Paper trading only. Strategy backtests on real market data.
  Risk gate enforces hard limits. No live trades — ever, on this
  deployment. Live execution requires audited signer on operator's
  machine (NoKeySigner default refuses).

Ops Cockpit (http://localhost:7880):
  Truth protocol v1 + compliance gate + decision engine. Complements
  the Revenue System spine. Use for ops reviews and compliance checks.

════════════════════════════════════════════════════════════════════
SAFETY PROPERTIES — NON-NEGOTIABLE, MUST NOT BE BROKEN
════════════════════════════════════════════════════════════════════

These invariants are enforced in code and verified by 32 tests:

1. INCOME = payment_processor source AND reconciled=True ONLY.
   Estimates, projections, CRM values, and manual entries are NEVER
   counted as real income. A number without a verifiable source is
   NULL, not zero.

2. HIGH-class actions (payout / outreach / publish / purchase /
   real_trade) are ALWAYS queued for human approval. They are NEVER
   auto-executed. The hub shows the queue; the Director approves.

3. The kill-switch halts ALL activity when engaged. It can be engaged
   from the hub nav bar. Only the Director can disengage it.

4. No live trading on this deployment. The TradingChannel's signer is
   NoKeySigner, which raises RuntimeError on any broadcast attempt.
   Live execution requires an audited Signer on the operator's own
   machine — never a cloud-side key store.

5. No fabricated numbers anywhere. If data is missing, the system
   raises DataGap and halts the decision. The dashboard shows "DATA
   GAP" flags, never invented placeholders.

6. Daily spend cap counts BOTH executed AND queued/pending spend.
   CFO agents can only TIGHTEN caps, never raise them.

7. Compliance gate blocks: income/earnings claims, "guaranteed
   returns," "risk-free," fake scarcity, unverified testimonials,
   purchased/scraped lists, missing unsubscribe links.

Do NOT add any code path that bypasses, auto-approves, or weakens
any of the above. The hub MAY read and display these values. It
must not modify the safety logic.

════════════════════════════════════════════════════════════════════
WHAT NOT TO DO
════════════════════════════════════════════════════════════════════

- Do NOT alter the 12-agent civilisation map SVG
- Do NOT change Supabase auth flow (magic-link stays)
- Do NOT alter existing web surfaces — only the hub is extended
- Do NOT hardcode any secrets, keys, or API tokens
- Do NOT build live trade execution or a key store
- Do NOT use colours, fonts, or patterns outside the design system
- Do NOT fabricate revenue numbers or simulate income
- Do NOT bypass the approval queue for any HIGH-class action
- Do NOT remove the kill-switch

════════════════════════════════════════════════════════════════════
THE RESULT
════════════════════════════════════════════════════════════════════

One domain. One magic-link sign-in. Fifteen surfaces under one hub.
The civilisation map breathes — twelve agents, animated work-packets,
one routed brain. The live strip shows real reconciled income only.
Python backends appear as green cards when running. Kill-switch in the
nav. Revenue System approval queue visible from the hub.

This is the whole wing — spread to its fullest, every surface in
formation, the truth ledger connecting them all.
