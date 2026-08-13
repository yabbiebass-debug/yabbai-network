# YABBAI NETWORK — Operator Runbook

Emergency actions come first, on purpose. Read this section once now, so you know it
exists when you need it in a hurry.

## 1. STOP / EXIT — how to get out of anything

- KILL A SUBSYSTEM: unset (or set false) its flag in the Emergent secrets panel and
  restart: DEFI_LIVE_ENABLED, TRIGGER_ENABLED, HARVEST_ENABLED, EARN_ENABLED,
  TREASURY_AUTOPAY_ENABLED. All default false; the app fails closed if a flag is
  missing or unparseable.
- WITHDRAW EVERYTHING: withdrawals and cancels are NEVER gated by any flag.
  - Earn: /defi page → Earn tab → withdraw builds work with everything off.
  - Trigger orders: cancel is always available; funds unlock from the vault on cancel.
  - Wallet funds: your keys live in Phantom, never on the server — nothing on this
    system can hold your money hostage, by design.
- CIRCUIT BREAKER: POST /api/goldhunter/breaker/on (admin) blocks all treasury
  distribution regardless of other settings.
- WORST CASE: remove STRIPE_SECRET_KEY / JUPITER_API_KEY from secrets and restart —
  checkout and quoting shut off cleanly.

## 2. FIRST REVENUE — store go-live (do this before anything on-chain)

1. Upload the seven product zips to STORE_ASSETS_DIR (default /app/store_assets):
   aos-v2.zip, aos-webdesigner.zip, aos-developer.zip, aos-smm.zip, orchestrator.zip,
   starter-bundle.zip, complete-collection.zip. A SKU with no file stays unlisted.
2. Stripe (test mode first): set STRIPE_SECRET_KEY=sk_test_…; create a webhook endpoint
   for https://yabbai.network/api/store/webhook (event: checkout.session.completed) and
   set STRIPE_WEBHOOK_SECRET from it.
3. Verify: GET /api/store/health → your uploaded SKUs under "listed". Make a test-mode
   purchase end to end (card 4242…): pay → return page shows the download → file opens.
4. Confirm the ledger: exactly one income row, rail=stripe, for the test PaymentIntent.
   Run POST /api/goldhunter/vault/reconcile — unverified_count must be 0.
5. Switch to live keys. Set complete-collection's price in backend/store/products.json
   when ready — it lists itself automatically.

## 3. FIRST ON-CHAIN INCOME — janitor (deterministic, no market risk)

1. Set SOLANA_RPC_URL (any reliable mainnet RPC).
2. /defi → Janitor → Scan wallet with your own address. PROTECTED is the default
   classification; only affirmatively-safe buckets are offered.
3. Build closes (closes and dust burns are always separate signatures), review the
   confirmation (accounts, SOL recovered, fees, net), sign in Phantom.
4. The recovery books itself through the settlement rail only after the transaction is
   confirmed on-chain for YOUR wallet. Check /api/goldhunter/vault/reconcile/status.

## 4. QUOTES AND TRADING (only after 2 and 3 feel routine)

1. JUPITER_API_KEY from portal.jup.ag → masked /settings field or env (env wins).
2. Quotes work immediately; execution stays 403 until BOTH: DEFI_LIVE_ENABLED=true AND
   DEFI_ALLOWED_MINTS contains the mints you deliberately allow (empty = nothing).
3. Caps are server-side and start at: max A$-equivalent USD 25/trade, USD 100/day,
   100 bps max price impact. Raise them only in env, only on purpose.
4. Every trade is a real Phantom click. There is no auto-approve anywhere. If you ever
   see one, that is a bug — kill DEFI_LIVE_ENABLED and report it.

## 5. EARN (parking, not growth)

1. EARN_ENABLED=true AND EARN_ALLOWED_PROTOCOLS set to the protocols you chose after
   reading their risk_flags on /defi → Earn. Empty allowlist = deposits blocked.
2. Rates shown are sourced, timestamped, and labelled "variable — not guaranteed".
   Anything above the sanity threshold is excluded as a scam signal, not featured.
3. Accrual is UNREALIZED until you withdraw/claim; only realized yield ever books.

## 6. WEEKLY HYGIENE

- POST /api/goldhunter/vault/reconcile — unverified_count must be 0. Distribution is
  blocked without a fresh, clean reconcile; that is intentional.
- Glance at EVIDENCE.md's END STATE block after any deploy: every flag you did not
  deliberately flip should still read false.
- Tavily: GoldScout stays on-demand (~12 scans/month budget). Shield/GoPlus cover
  safety without spending credits.

## 7. WHAT THIS SYSTEM WILL NEVER DO (if it does, that's an incident)

- Hold a private key, sign, or move funds server-side.
- Book income without a verifiable external settlement reference.
- Execute a trade, deposit, or payout from a schedule, agent, or LLM output.
- Show a projected return, a compounding calculator, or an income promise.
