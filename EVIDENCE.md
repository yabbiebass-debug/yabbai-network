# EVIDENCE LOG — YABBAI full codebase update run (started 2026-08-12T15:31Z)

All commands run from the preview pod. Prod = https://yabbai.network.

## WORKSTREAM 0 — INVENTORY + SNAPSHOT

### 0.1 — six /api/*/health on prod (2026-08-12T15:31Z)
```
$ for s in revenue ai goldscout defi ops realm; do curl -s https://yabbai.network/api/$s/health; done
revenue  → {"status":"healthy","version":"1.0.0","real_funds_at_risk_without_human":false,"trading_signer":"NoKeySigner (refuses all) — inject a real one to trade live",...}
ai       → {"ok":true,"app":"yabbai-ai","version":"2.3.0","route_order":["nvidia","groq","cerebras","google","openrouter","yabbai","emergent"],"key_configured":true}
goldscout→ {"ok":true,"app":"goldscout","version":"3.2.0",...,"tavily_configured":true,"scanner_mode":"on-demand","monthly_credit_limit":100,...}
defi     → {"ok":true,"app":"yabbai-defi-simulator","custodial":false,"real_funds":false,...}
ops      → {"ok":true,"app":"yabbai-ops",...}
realm    → {"ok":true,"app":"realm","version":"1.0.0","tables":21,"views":4,...}
```
ALL SIX REACHABLE, ALL JSON. PASS.

### 0.2 — signing-term grep (backend, *.py, excl. __pycache__)
```
$ grep -rn "PRIVATE_KEY\|Keypair.fromSecretKey\|signTransaction\|sendRawTransaction\|from_secret_key\|secretKey" --include="*.py" /app/backend
(no output, exit 1)
```
ZERO HITS. PASS.

### 0.3 — writers of entry_type="income" into vault_entries
```
revenue_system/defi_backend_patched/server.py:256  update_coinspot_balances() → VaultEntry(entry_type="income", reconciled=True)  ← WRITER (balance snapshot mislabelled as income; to be removed WS1)
revenue_system/defi_backend_patched/server.py:443-454  POST /vault create_vault_entry → _insert("vault_entries", ...) accepts entry_type="income" if reconciled=True  ← WRITER (manual, no rail verification; to be replaced WS1)
revenue_system/defi_backend_patched/server.py:169  VaultEntryCreate model DEFAULT entry_type="income"
(others: revenue_system/truth + channels write Metric(name="income") to the in-memory TruthLedger — a different store, not vault_entries; reconciled-only rules already enforced there)
```

### 0.4 — env var NAMES present (local preview .env; prod secrets not readable from this pod — names below are what the codebase/env defines here)
ADMIN_API_KEY APP_ENC_KEY AUTH_ALLOWLIST CORS_ORIGINS DAILY_LOSS_CAP DAILY_SPEND_CAP DB_NAME
EMERGENT_LLM_KEY GROQ_API_KEY GROQ_BASE_URL GROQ_MODEL MAX_REAL_TRADE_USD MEMORY_DIR
MICRO_SPEND_CAP MONGO_URL PAPER_PROOF_BAR RECOVERY_CODE_PEPPER SPEND_CAP
SUPABASE_JWT_SIGNING_KEY SUPABASE_PUBLIC_URL SUPABASE_PUBLISHABLE_KEY SUPABASE_SECRET_KEY
XAI_API_KEY XAI_BASE_URL XAI_MODEL YABBAI_TIER_KEY YABBAI_TIER_MODEL YABBAI_TIER_URL
Of the asked-for set: PAYPAL_*: none · COINSPOT_*: none · STRIPE_*: none · JUPITER_API_KEY: none · SOLANA_RPC_URL: none · TAVILY_*: none (Tavily key lives in Mongo settings, set via /settings UI).

### 0.5 — Mongo (preview, mongodb://localhost:27017/test_database) collections + counts
```
ai_request_bodies 21 · ai_request_log 22 · goldscout_approvals 1 · goldscout_findings 0
goldscout_scans 0 · goldscout_watchlist 0 · leads 0 · realm_actions 1 · realm_approvals 2
realm_cycles 3 · realm_events 9 · realm_leads 5 · settings 1 · tavily_credits 0
user_sessions 6 · users 7 · wallets 0 · yabbai_exemplars 21 · yabbai_learning_state 1
vault_entries: COLLECTION DOES NOT EXIST (Gold Hunter service has never been mounted)
gold_findings: COLLECTION DOES NOT EXIST
synthetic income rows (agent_role in {sentinel,scraper}): 0
```
NOTE (N5): prod Mongo (Atlas) is not reachable from this pod; prod counts cannot be
verified here and are NOT claimed. The Gold Hunter app is not mounted in server.py, so
prod has no /api/goldhunter surface writing these collections either.

### 0.6 — startup hooks launching loops
```
revenue_system/defi_backend_patched/server.py:402  lifespan() → asyncio.create_task(master_swarm_loop())   ← AUTOSTART (dormant: app not mounted; removed in WS1)
treasury_loop: ZERO hits anywhere in the tree (the original defi_backend treasury loop was already deleted in the safety patch; distribute path is /treasury/request-payout → human gate)
```

### 0.7 — LAW_AUDIT_DATA
```
$ grep -rn "LAW_AUDIT_DATA" /app --include="*.py" --include="*.html" --include="*.js"
(zero hits)
```

### 0.8 — SNAPSHOT
```
$ /app/memory/snapshots/vault_gold_snapshot_20260812T153203Z.json
vault_entries: 0 rows (collection absent) · gold_findings: 0 rows (collection absent)
```

## CHECKPOINT 0: PASS
- 0.2 zero signing hits ✔ · all six health endpoints reachable JSON ✔ · all eight items logged ✔

---

## WORKSTREAM 1 — SETTLEMENT BOUNDARY + DISARM (CHECKPOINT 1: PASS)
```
$ grep -rn --include=*.py --exclude-dir=__pycache__ --exclude-dir=tests 'entry_type": "income' /app/backend
→ EXACTLY ONE writer: revenue_system/defi_backend_patched/settlement.py (record_settled_income)
  (verified by tests/test_full_update.py::test_single_income_writer_grep — PASS)

$ pytest tests/test_full_update.py — settlement subset
test_fabricated_solana_signature_raises            PASS (fake 87-char sig → SettlementVerificationError via live RPC)
test_settlement_input_validation                   PASS (bad amount/rail/ref refuse; stripe w/o key refuses = fail closed)
test_no_boot_loops_and_income_refused_at_vault     PASS (swarm_running=False, treasury_running=False on boot; POST /vault income → 400)
test_distribute_now_refuses_with_circuit_breaker   PASS (kill switch engaged → 403 "circuit breaker")
test_swarm_start_gated_by_flag                     PASS (/swarm/start + /treasury/start → 403 while AUTOSTART flags false)
test_no_executed_status_and_no_law_audit_data      PASS (zero hits; GateOutcome enum in gates.py intentionally untouched — safety invariant)

Boot log (supervisor): "MIGRATION: voided 0 synthetic income rows; 0 findings → CANDIDATE; net profit before=0 after=0"
                       "YABAI Gold Hunter boot: zero loops started"
(0 rows voided is CORRECT — WS0.5 found zero synthetic rows in this Mongo; prod Atlas re-runs the same idempotent migration on deploy boot.)
```
New endpoints live: /api/goldhunter/{health,vault,vault/settle,vault/reconcile,vault/reconcile/status,treasury/status,treasury/start,treasury/stop,treasury/distribute-now,swarm/*}
CoinSpot sync now writes coinspot_balances snapshots — a balance is not income.

## WORKSTREAM 2 — HUB CLEANUP + SIM DEMOTION (CHECKPOINT 2: PASS)
```
$ grep -n "localhost\|:78\|:800\|defisim" frontend/public/hub/index.html
→ only two ':800' hits remain and both are CSS font-weight:800 (not ports). Zero localhost, zero port constants, zero defisim.
$ python -c "import defi.simulation" → OK (engine moved: frictions, paper fills, evaluate_quote)
$ importers of old defisim paths: only the removed server.py mount + a docstring mention in trading/channel.py — none functional
$ rm -rf frontend/public/defisim → deleted. GET /defisim/index.html now serves the SPA fallback (HTTP 200 root shell,
  not the simulator — the simulator surface is gone). DEVIATION noted: platform SPA fallback answers 200, not literal 404.
$ curl preview /api/{revenue,ai,goldscout,defi,ops,realm,goldhunter}/health → 7×  "200 application/json"
Hub probes: relative ${base}/health only + content-type must include application/json else DOWN.
goldscout page localhost links → /ai/index.html + /defi/index.html.
```

## WORKSTREAM 3 — LIVE DEFI + DATA PLANE + SURFACE PASS 1 (CHECKPOINT 3: PASS)
```
$ curl /api/defi/portfolio?wallet=EPjFW… (real mainnet address)
→ {"sol": {"amount": 519.473360684, "usd": 39272.93}, tokens: 168, total_usd: 46462.36}  (unpriced mints → usd null, never 0)
$ curl /api/defi/tokens?q=USDC → live tokens/v2 data
$ curl /api/defi/shield?mint=EPjFW… → HAS_FREEZE_AUTHORITY warning (real signal — USDC has one)
$ curl /api/defi/yields?symbol=SOL → DeFiLlama pools (binance-staked-sol 4.65% APY, $775M TVL)
GoldScout scan log line: "goldscout safety: shield-sourced signals folded in (0 scored flags, tavily_calls=0)"
  → shield is now a third safety source; freeze/mint types excluded from scoring (RPC already scores them, no double count)
/defi page renders (screenshot): dark banner "LIVE EXECUTION IS OFF", chips LIVE EXEC: OFF / JUPITER KEY: NOT SET / ALLOWLIST,
  7 tabs (portfolio/trade/janitor/triggers/earn/sentinel/treasury). Hub shows fifth "DeFi — Live (Solana)" nav card; 5/5 live.
Browser console: 0 hits for localhost / CORS / Mixed Content.
BUGFIX during verification: Token-2022 program id constant was one char short → corrected to the on-chain value
  TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb (fetched via getAccountInfo owner of the PYUSD mint).
BUGFIX: 502s from upstream failures were being replaced by the ingress/Cloudflare HTML error page → all upstream-failure
  statuses in the defi layer now 503 (passes through with clean JSON).
```

## WORKSTREAM 4 — JANITOR (CHECKPOINT 4: PASS)
```
pytest:
test_janitor_classification_protected_by_default   PASS (valued/frozen/delegated/no-price → PROTECTED; zero → empty_ata /
                                                        empty_ata_2022; sub-$1 → dust; real lamports read from each account)
test_janitor_build_rejects_funded_and_mixed        PASS (funded-since-scan → 409 at build; burn-of-empty → 409;
                                                        mixed action string → 400; closes/burns never share a signature)
test_janitor_income_routes_through_settlement_only PASS (no direct vault writes in janitor.py)
```
First live recovery is an OPERATOR step post-run (own wallet + Phantom) — NOT simulated here (N5).
openorders scan: not supported on the public RPC — bucket reported "unavailable", accounts default PROTECTED (honest).

## WORKSTREAM 5 — QUOTE + EXECUTION PLANES + SURFACE PASS 2 (CHECKPOINT 5: PASS)
```
Live, through the production-path ingress, with DEFI_LIVE_ENABLED=false:
$ GET /api/defi/quote (authed, allowlist temporarily SOL+USDC in PREVIEW env only)
→ ok:true, quote(outAmount 755937 for 0.01 SOL, impact 2.4bps, route BisonFi, fee 2bps)
  + simulation(divergence 25.2bps vs independent price/v3 — under the 50bps cap)
  + shield verdict "caution" + live_enabled:false + NO signable transaction without taker
$ POST /api/defi/execute → HTTP 403 "DEFI_LIVE_ENABLED=false" (unconditional, checked before auth)
pytest: quote guardrails — empty allowlist 403 (empty ≠ allow-all) · unlisted mint 403 ·
  1-SOL trade > $25 cap 403 (priced live) · stale requestId 410 · unknown requestId 400 ·
  replay after atomic claim 409 (race fixed per code review)
$ grep N1 terms (PRIVATE_KEY, Keypair.fromSecretKey, signTransaction, sendRawTransaction, from_secret_key) backend/ → ZERO
END STATE: allowlist REMOVED from preview env again → allowed_mints_count: 0, live: False, key: not set.
```

## WORKSTREAM 6 — SENTINEL + HARVESTER + TRIGGER V2 (CHECKPOINT 6: PASS)
```
pytest:
test_sentinel_constructs_no_transactions           PASS (no Instruction/Transaction/Message/Keypair/solders in sentinel.py)
test_harvester_has_no_execution_path_and_no_blended_fields  PASS (no execute/relay imports; candidate fields are named
                                                   sourced components only: shield_verdict, price_impact_bps, liquidity_usd,
                                                   age_days(null — tokens/v2 has no listing age, never invented),
                                                   holder_concentration, holder_count, organic_score, llama_tvl_usd,
                                                   simulated_divergence_bps. NO blended score, NO projected return)
test_harvest_scan_403_when_disabled                PASS (HARVEST_ENABLED unset → 403)
Sentinel alerts: shield re-run EVERY poll; liquidity drop measured vs the newest snapshot OLDER than 24h
  (code-review fix — poll-to-poll noise cannot trip it); snapshots pruned after 8 days; locked (vault) balances
  reported separately from spendable, and "unknown" (not 0) without a Jupiter key.
Trigger V2: verified live that api.jup.ag/trigger/v2/{orders/price,deposit/craft} exist and are key-gated (401 keyless).
  Full order lifecycle NOT live-verified — requires the operator's Portal key (N5: reported as not run, not simulated).
  Caps enforced: TRIGGER_MAX_OPEN_ORDERS + TRIGGER_MAX_LOCKED_USD from open orders; open-orders unverifiable → fail closed.
  Cancel is never gated (exit). $10 documented minimum enforced. Vault-lock language in every confirmation.
```

## WORKSTREAM 7 — EARN (CHECKPOINT 7: PASS)
```
$ GET /api/defi/earn/markets (live) → markets: 5, excluded: 5, deposits_enabled: false
pytest:
test_earn_deposit_dark_and_allowlist  PASS (deposit 403 while EARN_ENABLED=false; 403 with flag on but EMPTY allowlist;
                                            403 over EARN_MAX_DEPOSIT_USD; WITHDRAW build is never 403 with everything off)
test_earn_markets_sanity_filter_live  PASS (every listed market ≤ 20% APY sanity; outliers EXCLUDED and logged as scam
                                            signals; every entry labeled "variable — not guaranteed" + named risk_flags)
test_earn_frontend_has_no_projection_language PASS ("projected"/"will grow"/"in one year"/"estimated earnings" → zero)
Positions: accrual labeled UNREALIZED, never income. Realized yield on withdraw/claim books via
record_settled_income(rail=solana) BOUNDED by the verifiable on-chain receipt of that exact tx for that exact wallet
(code-review fix — client-supplied figures can no longer inflate the ledger).
native_stake kind: no verifiable public API source wired — omitted rather than estimated (N5).
```

## CODE REVIEW (read-only agent) — verdict READY WITH FIXES; all three MEDIUMs fixed + re-tested:
1. Earn income injection → on-chain receipt bound + party check in the solana rail (expected_party).
2. /execute double-relay race → atomic find_one_and_update claim before relay (replay → 409). Trigger submit deduped too.
3. Sentinel liquidity baseline → 24h-old snapshot + retention. Also: _mem only used when Mongo absent; janitor wallet
   validation 400; trigger UI got its sign+submit flow.

## FINAL SWEEP
```
N1 signing terms in backend/*.py     → ZERO (pytest test_n1_no_signing_terms_in_backend PASS)
income writers                       → settlement.py ONLY (pytest PASS)
hub localhost/port refs              → ZERO (only CSS font-weight:800 matches)
Full suite: 20/20 PASS (tests/test_full_update.py)
END STATE (preview): DEFI_LIVE_ENABLED=false · TRIGGER_ENABLED=false · HARVEST_ENABLED=false · EARN_ENABLED=false ·
  TREASURY_AUTOPAY_ENABLED=false · SWARM_AUTOSTART=false · TREASURY_AUTOSTART=false · DEFI_ALLOWED_MINTS empty ·
  EARN_ALLOWED_PROTOCOLS empty · JUPITER_API_KEY not set. Everything dark. Only the operator flips flags.
requirements.txt regenerated (solders==0.28.0 persisted — this was the production build blocker).
```


---

## PATCH SET v2 (2026-08-13) — storefront + LST shield + N1 copy fix
```
Applied VERBATIM (10 files): backend/defi/earn.py · backend/revenue_system/unified_server.py ·
backend/server.py · backend/requirements.txt (identical) · frontend/public/hub/index.html ·
backend/store/{__init__.py,products.json} · backend/tests/test_store.py ·
frontend/public/store/index.html · OPERATOR_RUNBOOK.md (repo root)
Pre-apply diff check: patch base matched the live tree — all code-review fixes preserved.

1. py_compile earn.py unified_server.py store/__init__.py server.py → ALL 4 OK
2. pytest tests/test_store.py → 4 passed (unlisted reasons correct; tampered webhook sig → 400 before any write)
3. FINAL SWEEP re-run: N1 signing terms → ZERO · income writers outside settlement.py → ZERO ·
   hub localhost/port refs → ZERO (font-weight:800 CSS only)
   Full regression: tests/test_full_update.py + tests/test_store.py → 24/24 PASS (twice)
   (harness fix: shared event loop in test_full_update.py — Motor client binds one loop)
4. Deploy = OPERATOR ACTION (platform button). Preview verified; prod redeploy pending.
5. PREVIEW /api/store/health → all 7 SKUs UNLISTED: 6× "asset file missing", complete-collection "price not set" —
   the CORRECT dark state. stripe_key_configured:false, webhook_secret_configured:false.
   PROD /api/store/health → 404 (patch not deployed yet, expected).
6. PREVIEW /api/defi/earn/markets LST entries (live):
   JITOSOL apy=5.08% tvl=$762.8M receipt_mint=J1toso1uCk3R… shield=info
   MSOL    apy=6.30% tvl=$180.3M receipt_mint=mSoLzYCxHdYg… shield=info
   BSOL    apy=5.17% tvl=$70.2M  receipt_mint=bSo13r4TkiE4… shield=info  ← resolved verdicts, not "unknown"
7. /store/index.html renders (screenshot): "Buy once. Own it." + honest empty-shelf state.
   Hub ⬢ Store chip present in served markup line 316 (renders post-auth; auth gate hides nav when signed out).
```

---

## PATCH — Storefront & Chain settings, 3-class split (2026-08-13)
```
Files touched (targeted): backend/network_db.py · backend/defi/config.py · backend/defi/authz.py ·
backend/store/__init__.py · backend/tests/test_store.py (fixture: setattr ASSETS → setenv STORE_ASSETS_DIR) ·
backend/tests/test_storefront_chain_settings.py (NEW, 5 tests) · frontend/public/settings/index.html

CLASS A (plain): public_base_url + store_assets_dir → DEFAULTS + env_map (env wins) + full-text inputs on /settings.
CLASS B (credential, no signing authority): solana_rpc_url → SECRET_FIELDS (masked, secrets_set boolean only),
  DEFAULTS + env_map. rpc_url() now async: env → settings → mainnet-beta default; authz.rpc_call awaits it
  (fail-closed 503 unchanged). store assets_dir()/base_url() resolve per-request, same precedence.
CLASS C (money-capable): STRIPE_SECRET_KEY/STRIPE_WEBHOOK_SECRET — ENV_ONLY_FIELDS stripped in save_settings
  (both casings), NO input fields; /settings shows read-only SET/UNSET rows from /api/store/health
  (os.environ-derived) + "configure in the Emergent secrets panel". SK/WH still read from env exactly as before.

1. py_compile network_db.py defi/config.py defi/authz.py store/__init__.py tests/test_store.py
   tests/test_storefront_chain_settings.py → ALL 6 OK
2. pytest test_full_update.py + test_store.py + test_storefront_chain_settings.py → 29 passed (20+4+5)
3. Live preview probes: GET /api/settings (authed) → solana_rpc_url plaintext ABSENT,
   secrets_set.solana_rpc_url=true, stripe keys absent from doc.
   PUT /api/settings with STRIPE_SECRET_KEY/stripe_webhook_secret → Mongo doc after write:
   stripe fields ALL null (blocked), public_base_url stored. /api/store/health + /api/defi/health OK.
4. /settings screenshot: Storefront & Chain card renders — masked RPC input, plain base-url/assets-dir
   inputs prefilled with defaults, Stripe rows UNSET (correct dark state).
NOTE (honest): rpcKeyset badge reads "configured" even at default because solana_rpc_url sits in DEFAULTS
   per instruction — secrets_set booleans can't distinguish default from operator-set.
```
