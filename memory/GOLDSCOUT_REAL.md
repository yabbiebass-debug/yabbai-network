# GoldScout → Real — deployment runbook

The safe half of "make DeFi real": GoldScout is a **scanner and safety checker**. It
reads chains and rates risk. It never moves funds. This is the piece I said I'd build,
and it's done and tested. The execution/trading side is deliberately NOT in here.

## What changed, and why it's actually "real" now

Before, GoldScout had two honesty problems:

1. **It only searched the news.** The scout hit Tavily and found *articles about*
   airdrops — never live on-chain data. Useful, but not a real DeFi scanner.
2. **It forgot everything on every deploy.** Findings saved to a JSON file, and
   Emergent wipes the app filesystem on every publish. So every scan was lost.

Both are fixed:

- **Real market data** — `core/market.py` pulls live pairs from **DexScreener** (free,
  no key): trending/boosted tokens plus ecosystem searches, with real liquidity, 24h
  volume, price and age. Dust below sane floors is dropped.
- **Real token safety** — `core/safety.py` checks each token against **Solana RPC**
  (is the mint authority renounced? the freeze authority? — the two biggest hard
  signals, straight from chain) and **GoPlus** (honeypot / pausable / mintable / tax
  flags). Free, no key. Merged with the existing scam-text heuristics, plus an optional
  AI second opinion via your own router as a tie-breaker only.
- **Durable storage** — findings, scan history and a watchlist now live in **Mongo**,
  owner-scoped, surviving every publish.

Three things I kept deliberately honest:
- A source we can't reach is reported **`unknown`**, never silently treated as safe.
- A LOW score means **"no hard flags found," not "safe to buy."** Stated on every result.
- Results are framed as **research leads, not buy signals.** No trading logic exists.

## Important: I could not test the *live* API calls

My build sandbox can't reach DexScreener / Solana / GoPlus (locked network), so the
**logic** is fully tested (24 checks, mocked upstreams + in-memory Mongo — see
`backend/tests/test_goldscout_real.py`) but the **live endpoints** are exercised only
once deployed. First real scan is the true integration test. If DexScreener rate-limits
or changes a field, the code degrades honestly rather than crashing — but watch the
first scan's `summary.errors`.

---

# The steps

## Step 1 — Emergent: upload the code

The changed/new files are all backend:

```
backend/goldscout_router.py            (rewritten — new endpoints, Mongo persistence)
backend/goldscout/core/market.py       (new — real DEX scanner)
backend/goldscout/core/safety.py       (new — real on-chain + GoPlus safety)
backend/goldscout/core/store.py        (new — Mongo store)
backend/goldscout/core/scout.py        (edited — async, no more JSON file)
backend/tests/test_goldscout_real.py   (new — the 24-check suite)
```

Upload the zip, then let it deploy. To keep it cheap: tell the agent
**"apply these files as-is, do not run the testing agent, then publish."** The tests
ship in the repo and run free locally — you don't need the credit-heavy iteration loop.

## Step 2 — MongoDB (through the Emergent DB): nothing to paste

Mongo needs **no schema**. The three collections —
`goldscout_findings`, `goldscout_scans`, `goldscout_watchlist` — create themselves on
the first scan. The only action is the index bootstrap, and it's one call. After deploy,
signed in on any surface, open DevTools (F12) → Console:

```js
fetch('/api/goldscout/indexes',{method:'POST',credentials:'include'}).then(r=>r.json()).then(console.log)
```

Expect `{ok:true, indexed:[...]}` once. (At low volume it's a nicety, not a blocker.)

## Step 3 — SQL editor (Supabase): you do NOT need this for GoldScout

Straight answer: **GoldScout runs entirely on Mongo, like the rest of the backend.**
It needs no Supabase tables, and I deliberately didn't add any — dual-writing the same
data to two stores is exactly the split-brain we just spent a session removing.

The only reason to bring GoldScout into Supabase is if you specifically want its scan
history *joinable with your relational data* (clients, orders) for reporting. If you
do, say so and I'll add a clean one-way export + a small `goldscout_scans` schema —
Mongo stays the source of truth, Supabase becomes an optional mirror you trigger. Until
then, skip this step; there's nothing to paste.

---

# Try it once it's live

Signed in, from the Console:

```js
// real market scan (no key needed)
fetch('/api/goldscout/market/scan',{method:'POST',credentials:'include',
  headers:{'Content-Type':'application/json'},
  body:JSON.stringify({queries:['SOL'],deep_safety_top:3})}).then(r=>r.json()).then(console.log)

// real safety on any token (example: USDC mint)
fetch('/api/goldscout/token/safety',{method:'POST',credentials:'include',
  headers:{'Content-Type':'application/json'},
  body:JSON.stringify({token_address:'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v',chain:'solana'})}).then(r=>r.json()).then(console.log)
```

If `market/scan` returns pairs → the live DEX feed works end to end. If `summary.errors`
is non-empty, that's the honest report of what DexScreener did — send it to me.

---

# Not in this build (on purpose)

Turning the **DeFi Simulator** into a live *trader* — real swaps, real funds — is a
different, higher-stakes job, and I won't wire an autonomous agent to move real money
on a fast build. If you ever go there, the safe path uses the rails you already have:
human approval on every trade, testnet first, tiny hard caps, the LOW/MED/HIGH gates
doing real work, kill-switch tested — and a real signer (a private key) that **you**
inject locally and never paste into any chat, form, or agent. GoldScout feeds that
decision by telling you what's real and what's a trap; the pulling of the trigger stays
with you, in your own wallet.
