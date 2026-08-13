# YABBAI.NETWORK V2 — PRD / State of the World

## Original problem statement
React(static HTML surfaces)/FastAPI/MongoDB platform at https://yabbai.network
(custom domain on revenue-nexus-2.emergent.host). Safe, measurable multi-tier AI
router + revenue system + GoldScout + live Solana DeFi. Credit-disciplined, phased,
evidence-logged (EVIDENCE.md at repo root). User = operator, extremely safety-conscious.

## NON-NEGOTIABLES (override everything)
- N1 no server-side signing (no PRIVATE_KEY/Keypair/signTransaction/sendRawTransaction in backend)
- N2 settled income only: settlement.record_settled_income() is the ONLY vault income writer
- N3 no autonomous execution; nothing autostarts on boot
- N4 fail closed (missing env = safe; empty allowlist = nothing; exits NEVER gated)
- N5 no fabricated data (failed lookup renders —, never 0; tests not run are reported as not run)
- AI router (tier order nvidia→groq→cerebras→google→openrouter→yabbai→emergent) is FROZEN — do not touch
- GateController / kill switch / Truth Ledger reconciled-only rules: do not alter

## Architecture (2026-08-13)
- Gateway: /app/backend/server.py — mounts/includes:
  /api/revenue (unified_server) · /api/ai · /api/goldscout · /api/ops · /api/realm ·
  /api/goldhunter (settlement-gated Gold Hunter, revenue_system/defi_backend_patched/server.py) ·
  /api/defi (live DeFi layer, /app/backend/defi/*) · /api/store (storefront, /app/backend/store/) ·
  auth/wallet/supabase routers
- Settlement boundary: /app/backend/revenue_system/defi_backend_patched/settlement.py
  rails: solana (RPC getTransaction + optional expected_party), stripe (PI succeeded),
  coinspot (order id in history), paypal (capture COMPLETED). Dedupe on (rail, ref).
  reconcile_income_rows() + status; surfaced on /defi Treasury panel.
- DeFi layer (/app/backend/defi/): config.py (fail-closed env), jupiter.py (Swap V2
  api.jup.ag/swap/v2, shield lite-api ultra/v1, tokens/v2, price/v3, trigger/v2 [key-gated],
  lend/v1, DeFiLlama), simulation.py (mandatory dry-run, divergence bps), service.py
  (health/portfolio/tokens/shield/yields/quote/execute/history; execute 403 unconditional
  while DEFI_LIVE_ENABLED=false, atomic requestId claim, 24h notional cap), janitor.py
  (PROTECTED-default, solders LAZY import — 503 if absent), sentinel.py (read-only, 24h
  liquidity baseline), harvester.py (dark, no execution imports), trigger.py (V2,
  key-required, caps, cancel never gated), earn.py (admission filters, LST receipt_mint +
  shield verdicts, deposits dark, withdraw never gated, realized yield bounded by on-chain receipt).
- Store (/app/backend/store/): Stripe Checkout → signature-verified webhook →
  record_settled_income(rail=stripe) → entitlement → TTL download. SKUs auto-unlist without
  price or asset file (STORE_ASSETS_DIR, default /app/store_assets). 7 SKUs in products.json.
- Frontend surfaces: /hub (same-origin relative /health probes, JSON content-type check,
  Store chip, supabase config from /api/supabase/public-config — nothing hardcoded),
  /defi (portfolio/trade+Phantom/janitor/triggers/earn/sentinel/treasury), /store, /settings
  (Tavily + Jupiter masked key cards), /goldscout, /revsys, /opscockpit, /wallets, /login, /2fa.
  /defisim DELETED (sim engine lives on as defi/simulation.py; defi_simulator pkg unmounted, preserved).

## Env flags (all dark; operator-only; see /app/backend/.env.example canonical block)
DEFI_LIVE_ENABLED / TRIGGER_ENABLED / HARVEST_ENABLED / EARN_ENABLED /
TREASURY_AUTOPAY_ENABLED / SWARM_AUTOSTART / TREASURY_AUTOSTART = false
DEFI_ALLOWED_MINTS + EARN_ALLOWED_PROTOCOLS = empty (nothing allowed)
JUPITER_API_KEY (or /settings masked card; env wins) · SOLANA_RPC_URL (default public RPC —
operator should set dedicated) · STRIPE_SECRET_KEY + STRIPE_WEBHOOK_SECRET + PUBLIC_BASE_URL (store)
Caps: DEFI_MAX_TRADE_USD 25 · DEFI_DAILY_CAP_USD 100 · impact 100bps · divergence 50bps ·
TRIGGER 5 orders/$100 · EARN $250/$1000/TVL$50M/APY20% · TREASURY $50/$200 AUD

## What's been implemented (dates)
- ≤2026-08-11: Phases 1–2 (AI logging, free-first ladder), GoldScout+Tavily budget guard (100cr/mo)
- 2026-08-12/13: FULL CODEBASE UPDATE run — WS0 inventory/snapshot · WS1 settlement boundary +
  Gold Hunter disarm (+mount /api/goldhunter) · WS2 hub cleanup + sim demotion · WS3 live DeFi
  data plane + /defi page + shield→GoldScout (tavily_calls=0) · WS4 janitor · WS5 quote/execute +
  Phantom pass 2 · WS6 sentinel/harvester/trigger · WS7 earn. Code review (READY WITH FIXES):
  earn income injection bounded by on-chain receipt, execute race atomic claim, sentinel 24h baseline.
  Deployment fixes: solders→requirements then made optional+removed (platform blockchain-lib flag),
  supabase hub config env-driven via /api/supabase/public-config, upstream 502→503 (Cloudflare).
  Tests: /app/backend/tests/test_full_update.py — 20 checkpoint tests.
- 2026-08-13: PATCH SET v2 applied verbatim (user-authored): store service + page + tests +
  OPERATOR_RUNBOOK.md, LST receipt_mint + resolved shield verdicts in earn markets, N1 copy fix
  in revenue /health, hub Store chip. 24/24 tests. Preview verified; PROD REDEPLOY PENDING.

## Testing
- /app/backend/tests/test_full_update.py (20) + tests/test_store.py (4) → 24/24
- Evidence log: /app/EVIDENCE.md (checkpoints 0–7 + final sweeps + patch set v2)
- Auth-gated testing: seed session per /app/auth_testing.md (Mongo user_sessions with mfa_verified)

## Prioritized backlog
- P0 (operator): redeploy to prod; then curl prod /api/store/health + /api/defi/earn/markets;
  set STRIPE_WEBHOOK_SECRET + upload store assets to go live; enter Jupiter key via /settings
- P1: prod smoke of /defi surface with Phantom (operator wallet); first janitor recovery (operator step)
- P2 (only when operator chooses): flip flags one at a time per OPERATOR_RUNBOOK.md
- Known limits (honest): Trigger V2 lifecycle not live-verified (needs operator key);
  janitor /build needs solders installed (operator opt-in, clean 503 otherwise);
  openorders/stake scans limited on public RPC (accounts default PROTECTED);
  native_stake earn kind omitted (no verifiable source)
