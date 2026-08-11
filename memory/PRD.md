# YABBAI.NETWORK V2 — PRD

## Original problem statement
Unify the YABBAI network — 15 surfaces across two stacks (Supabase static-HTML web
surfaces + 5 Python FastAPI backends) — under one hub, one sign-in, one design system.
Additive/integration build: keep all existing functionality, preserve safety invariants.

## Environment reality & chosen architecture
Emergent exposes ONE backend port (8001, via `/api`) + ONE frontend (3000). The original
design (6 ports + Supabase + Netlify + Deno edge functions + Railway) does not map 1:1.
Decision (confirmed with user): **adapt the whole system to run unified inside Emergent.**
- All 5 Python backends mounted into one FastAPI app on :8001, each under `/api/<service>`:
  revenue → `/api/revenue`, ai → `/api/ai`, goldscout → `/api/goldscout`,
  defi → `/api/defi`, ops → `/api/ops`.
- Hub + surfaces served by the React frontend from `/public`; they call backends via
  `window.location.origin + /api/<service>` (same origin in Emergent ingress).
- AI brain uses the Emergent Universal LLM key (Claude Sonnet 4-6) — replaces the heavy
  local Ollama stack and the Supabase edge functions for AI.
- Supabase keys (user-provided) wired into the hub; magic-link/Google auth bypassed in
  preview so the live network is visible.

## Safety invariants (intact, verified by tests)
Income = payment_processor + reconciled only · HIGH actions queued for approval ·
kill-switch halts all activity · NoKeySigner refuses live trades · no fabricated numbers ·
daily caps count pending+executed · compliance gate blocks guarantee/risk-free claims.

## What's implemented (Phase 1 — 2026-06-20)
- Unified backend `/app/backend/server.py` mounting the original revenue_system, defi_simulator,
  yabbai_ops apps + AI router + GoldScout router; `/api/health`, `/api/network/status`, `/api/settings`.
- Fixed original import bugs (defi `simulator`→`defi_simulator`, ops `core`→`yabbai_ops.core`).
- AI router (`ai_router.py`): chat, streaming chat, diagnose, scope-brief, call-guide, catalog-agent.
- GoldScout router (auth-free; Tavily-optional, degrades honestly).
- Hub reconfigured (`/public/hub/index.html`): unified single-origin paths, Supabase keys,
  preview shell boot, prefilled admin key. Civilisation map + live strip + services panel + kill-switch.
- 5 Python dashboards served from frontend (`/revsys /defisim /opscockpit /goldscout /ai`),
  repointed to the unified prefixes. On-brand AI console at `/ai`.
- Fixed pre-existing frontend breakage (webpack-dev-server v5 vs CRA v4 middleware API) in craco.config.js.
- Tests: 17/17 backend pass; all frontend flows pass (see /app/backend/tests/test_unified_backend.py).

## Phase 2 — Multi-tier routing + Google auth (2026-06-20)
- **Multi-tier LLM routing** configurable in `/settings/index.html`: NVIDIA NIM (free, OpenAI-compatible `integrate.api.nvidia.com/v1`) → Emergent Claude → YABBAI Local/Ollama. Requests walk enabled tiers in order until one answers (`ai_router.route_complete`). Reorder/toggle in UI; keys stored in Mongo `network_settings` (secrets never echoed). Endpoints: `/api/ai/providers`, `/api/ai/nvidia/models`, `/api/ai/test`. Graceful fallback verified (no NVIDIA key → falls to Claude).
- **Emergent-managed Google sign-in** gating `/hub` and `/settings` (public `/diagnose` stays open). `/login/index.html` → auth.emergentagent.com → hub exchanges `#session_id` at `POST /api/auth/session` → httpOnly `session_token` cookie (7d). `GET /api/auth/me`, `POST /api/auth/logout`. Mongo `users` + `user_sessions`. Playbook: `/app/auth_testing.md`.
- Tests: iteration_2 — 12/12 backend pass, frontend auth gate + settings UI pass. Fixed MEDIUM bug: Save button disabled until hydration + backend ignores empty `route_order`.

## Phase 3 — Allowlist + Google Authenticator 2FA + multi-chain wallets (2026-06-20)
- **Access lockdown**: Google sign-in restricted to `AUTH_ALLOWLIST` (yabbiebass@gmail.com, thomas.basham1@gmail.com); any other email → 403, no user created.
- **Google Authenticator (TOTP) 2FA** (pyotp): `/2fa` enrollment (QR + secret) → verify; secret encrypted at rest with `APP_ENC_KEY` (Fernet). Gate order for /hub, /settings, /wallets: authed → 2FA-verified. Endpoints `/api/auth/2fa/setup|verify`; `/api/auth/me` returns mfa_required/enrolled/verified.
- **Multi-chain wallets (SAFE — no server private keys)** at `/wallets`: connect MetaMask (EVM) / Phantom (Solana) / Jupiter link; keys stay in the extension, real txs signed client-side. Watch-only live balances via public RPCs across Ethereum, Base, Arbitrum, Polygon, BNB Chain, Solana. Backend `/api/wallet/chains|connect|list|balance` (connect/list require auth+2FA). Verified live: 6.63 ETH + 1679 SOL reads. **Refused (by design + safety contract): cloud-stored private key / autonomous real-money trading — NoKeySigner unchanged.**
- **Supabase**: new project wired in hub; consolidated schema (migrations 001–009) served at `/sql/yabbai_schema.sql` for SQL-Editor paste (service_role/DB access not provided, so data runs via the unified backend meanwhile).
- Tests: iteration_3 — 17/17 backend + 7/7 frontend pass, no blocking issues.

## Phase 4 — Token balances, GoldScout signing, Supabase connector (2026-06-20)
- **Token balances + USD** on `/wallets`: native + curated ERC-20 (via eth_call balanceOf) + SPL (getTokenAccountsByOwner) with live USD via **Coinbase spot** (CoinGecko/Binance rate-limited/geo-blocked here); per-wallet + grand total. `GET /api/wallet/tokens`. Verified ~$15.8k live on vitalik.eth.
- **GoldScout opportunity signing**: `/api/goldscout/analyze` (scam score), `/api/goldscout/approve` (auth+2FA; re-scores server-side, blocks riskScore≥70, logs wallet **message** signature to `goldscout_approvals` — no funds move), `/api/goldscout/approvals`. UI panel on `/wallets`: Scan → HIGH-risk blocked / clean → "Sign to approve" (MetaMask personal_sign / Phantom signMessage).
- **Supabase connector (secure secret input)**: Settings → Supabase section takes a masked `service_role` key (stored server-side, never echoed) + project URL. `GET /api/supabase/status` (auth+2FA) probes known tables; `GET /api/supabase/table/{name}` service-role read proxy (whitelisted). Consolidated schema at `/sql/yabbai_schema.sql`. Data-surface wiring pending user pasting the key + running the SQL.
- Tests: iteration_4 — 13/13 backend + all frontend pass.

## Phase 5 — Security sweep + deployment readiness (2026-06-20)
- **Closed unauth holes**: `/api/settings` (GET+PUT) and all sensitive `/api/ai/*` (chat, stream, providers, nvidia/models, test, scope-brief, call-guide, catalog-agent) now require an authed + 2FA-verified Director (`require_director`). `/api/ai/health` + `/api/ai/diagnose` stay public (lead magnet). Frontend callers send credentials; `/ai` console is now gated.
- **Security headers** middleware (X-Frame-Options SAMEORIGIN, X-Content-Type-Options nosniff, Referrer-Policy, HSTS). **CORS** reads explicit origins from `CORS_ORIGINS` env with `allow_credentials` (no wildcard+credentials).
- **Supabase secrets** wired from env (`SUPABASE_PUBLIC_URL/PUBLISHABLE_KEY/SECRET_KEY/JWT_SIGNING_KEY`); connector authenticates; awaiting schema SQL run.
- Tests: iteration_7 security sweep 34/34 backend + 5/5 frontend pass. **deployment_agent: PASS, 0 blockers.**
- Residual (non-blocking): revenue admin key still prefilled client-side on Director-gated dashboards; defi/ops sub-app endpoints unauthenticated (paper-only, no secrets).

## Deferred backlog (next phases)
- P1: Supabase migrations (001–009) + the 6 data web surfaces (Mission Control /app, Realm OS,
  Agency Floor, Catalog Studio, Client Portal, Vault) wired to Supabase or re-implemented as
  `/api` endpoints. Diagnose surface wired to `/api/ai/diagnose`.
- P1: Settings page — Google Auth (Emergent-managed), Stripe, PayPal, Phantom wallet connections
  (backend stub exists at `/api/settings`).
- P2: Persist reconciled income/ledger to Mongo (currently in-memory). Stripe webhook → `/api/revenue/webhooks/invoice-paid`.
- P2: GoldScout Tavily key + live scout; YABBAI AI agent-team / coding-IDE surfaces.
- P2: data-testids on hub kill-switch chips for regression hardening.

## Key facts
- Admin key: `yabbai-director-key` (X-Admin-Key header), in backend .env ADMIN_API_KEY.
- EMERGENT_LLM_KEY in backend .env.

## Phase 6 — Prod fixes (2026-06-24)
- **2FA lockout hardening** (`auth_router.py` `/2fa/setup` + `2fa/index.html`): QR + manual key now ALWAYS returned/rendered (even when `already_enrolled`), reusing the stored secret — removes the "enrolled but no authenticator → permanent lockout" dead-end. Verified end-to-end: fresh setup returns QR, live TOTP verifies, re-setup still returns QR.
- **Schema paste blocker fixed** (`/sql/yabbai_schema.sql`): removed the 006 catalog seed INSERT that used literal `PASTE_DIRECTOR_USER_ID` (invalid UUID) which would abort the whole SQL run. Script is now paste-clean; tables 001–009 all create. Catalog seed must be done from the app (needs a real auth.users UUID).
- **Hub display bugs** (`hub/index.html`): (1) `checkPythonService` now retries 3x, 8s timeout, backoff 1.5/3/4.5s; probes staggered 200ms; re-poll every 30s. (2) `loadNet` count queries switched from HEAD (`head:true`, deterministic 503) to GET (`.select('id',{count:'exact'}).limit(1)`). (3) failed count → null → renders "—", never a confident 0. money_view/catalog_view untouched. yabbai AI tier already uses 60s httpx timeout (no change needed).
- NOTE: production (revenue-nexus-2.emergent.host) runs a stale build; user must REDEPLOY to pick up all Phase 6 fixes.

## Phase 7 — 2FA reset/recovery + backup codes (2026-06-24)
- **2FA start-over/recovery** (`auth_router.py`): `POST /2fa/reset` mints a brand-new secret + fresh QR + 10 one-time recovery codes (revokes old, forces re-verify, no access grant). `POST /2fa/recover {code}` = single-use HMAC-peppered (`RECOVERY_CODE_PEPPER` in .env) backup code that satisfies the 2FA gate for a lost device. First enrollment via `/2fa/verify` now also returns 10 codes. All verified via curl: reset rotates secret (old TOTP→401), recover single-use (reuse→401), unauth→401, `/me` leaks nothing.
- **`/2fa` page** got "↻ start over" + "lost device?" (backup-code) actions.
- **Backup Codes Panel** in `/settings`: `GET /2fa/recovery-status` (remaining/total) + `POST /2fa/recovery-regenerate` (fresh set, shown once); both require a verified Director (403 if not 2FA-verified, 401 unauth). Card shows "N of 10 left" + Regenerate button.
- Fixed latent bug in `settings/index.html`: `$` helper was used but never defined (broke Supabase card wiring) — now defined.
- Preview DB reset for both Directors + removed a duplicate `thomas.basham1` record.
- PENDING USER ACTIONS: (1) redeploy to push all Phase 6/7 fixes live; (2) run `/sql/yabbai_schema.sql` in Supabase; then main agent wires the 6 live data surfaces.

## Phase 8 — Deploy blocker fix (2026-06-24)
- **Root cause of failed production deploy:** K8s readiness/liveness probe calls `GET 127.0.0.1:8001/health` directly (no `/api` prefix); app only had `/api/health` → 404 → pod never ready → deploy failed.
- **Fix:** added bare `@app.get("/health")` in `server.py` returning `{"status":"healthy"}` (200). Verified locally. deployment_agent: deployable, compilation_passed, no blockers; MongoDB Atlas-ready (env-driven). Only warnings = public Supabase anon key hardcoded in static hub HTML (safe, non-blocking).

## Phase 9 — Wallet demo-data cleanup + YABBAI local tier (2026-06-24)
- **Removed vitalik.eth demo wallet**: deleted stale watch-only DB record (was under yabbiebass@gmail.com, made vitalik.eth render as a "connected MetaMask wallet w/ balances"); removed the hardcoded `0xd8dA6B…A96045` literal from both test files (now env-overridable `TEST_EVM_ADDR`, default burn address); relaxed balance assertion. Grep confirms zero occurrences repo-wide. Token-contract + SPL-mint addresses in wallet_router.py are legitimate and kept.
- Added a **Remove** button to each wallet card (`wallets/index.html`) so users can clear wrongly-listed watch-only wallets (needed for prod where agent can't touch the DB).
- **YABBAI local/Ollama tier via env**: added `YABBAI_TIER_URL`/`YABBAI_TIER_KEY`/`YABBAI_TIER_MODEL` to `backend/.env`; `network_db.get_raw_settings()` overlays them onto yabbai tier fields (env precedence, carries to prod). Verified: tunnel POST /api/chat → 200 `{"content":"OK","model":"llama3.1:8b"}`; router `_yabbai_complete` returns live completions. Tier is last in route_order (emergent→nvidia→yabbai).
- All Phase 9 changes require redeploy for prod.

## Phase 10 — Realm data plane (agency surfaces on Mongo) (2026-06-24)
- New `backend/realm_router.py` (~900 lines) at `/api/realm`: owner-scoped select/insert(rows)/update, 12 RPCs, 4 live views, cycle-runner + AI fns, public intake/vault-config. Registered in server.py. Safety invariants enforced (money=reconciled only, catalog-price lock, no-air gate, approval-gated actions, owner scoping).
- New `frontend/public/shared/realm-client.js` — Supabase-shaped shim → /api/realm. Six surfaces (app/os/floor/studio/portal/vault) migrated off unbuilt Supabase.
- Fixed integration bugs during merge: (a) `route_complete` returns a dict but realm_router treated it as string → added `_ai()` coercion helper; (b) shim `getSession()` read `me.user` but /api/auth/me is top-level → guard on `me.user_id`, alias `id`; (c) 6 surfaces showed a dead magic-link gate on unauth → now redirect to /login. Removed default admin key from hub/revsys. os nav → same-origin.
- Verified: 53/53 test_realm.py + testing_agent iteration_9 ALL GREEN (surfaces render live, unauth redirects, cycle→approval flow, hub strip reads /api/realm/summary).

## Phase 11 — GoldScout real on-chain scanner (2026-06-24)
- Applied external Phase 11 (backend-only): `goldscout_router.py` rewritten + new `goldscout/core/{market,safety,store}.py` + `scout.py` async. Real data: DexScreener (live pairs, no key), Solana RPC mint/freeze authority + GoPlus honeypot flags, durable Mongo store (goldscout_findings/scans/watchlist, owner-scoped). Endpoints: /market/scan, /token/safety, /indexes, /scans, /watch, /opportunities.
- Fixed a merge bug: `/opportunities` called removed `get_findings()` → 500; repointed to `store.load_findings()` (owner-scoped, auth-gated).
- Verified: 24/24 test_goldscout_real.py pass; LIVE smoke on preview pod — DexScreener returned 25 real pairs w/ liquidity/volume, Solana RPC + GoPlus both `ok` (correctly flags USDC's live mint/freeze authority), persistence + indexes work, /opportunities 200 authed / 401 unauth. Testing agent NOT run (per user runbook, to save credits).
- Honest framing preserved: unreachable source = `unknown` not safe; LOW = "no hard flags" not "safe"; research leads not buy signals; NO trading logic. Requires redeploy for prod; run /api/goldscout/indexes once post-deploy.
