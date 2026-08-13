# YABBAI NETWORK — Patch Set v2 (2026-08-13)

Against `yabbai-network-main`. **v2 supersedes v1 — apply this one only.**

---

## Context: the build was already finished

`EVIDENCE.md` in your tree shows the "interrupted" single-run build actually completed:
Workstreams 1–7 done, 20/20 tests passing, and a read-only code-review agent that found
and fixed three medium issues. The settlement boundary, reconcile, janitor, sentinel,
harvester, trigger and earn modules are all present, tested, and dark.

Two earlier assumptions the real tree corrected:
- `/api/vault/reconcile` returning 404 was a **path-prefix artifact** — reconcile is
  mounted under the goldhunter sub-app (`/api/goldhunter/vault/reconcile`), not at the
  gateway root. It was never missing.
- `solders` was **already** removed from `requirements.txt`; janitor imports it lazily
  with a clean 503 fallback. The production reviewer blocker is resolved.

So this is not a rebuild. It is the small set of things the run genuinely left open,
plus the storefront, which never existed.

---

## Files in this pack (10)

### Modified
| Path | Change |
|---|---|
| `backend/defi/earn.py` | LST markets now carry a verified `receipt_mint` (mSOL, JitoSOL, bSOL, INF) and a **resolved** `shield_verdict` from a single batched `shield()` call, replacing the hardcoded `"unknown"`. Lend markets were already correct and are untouched. |
| `backend/revenue_system/unified_server.py` | `/health` `trading_signer` string changed from `"…inject a real one to trade live"` to `"…live signing intentionally unsupported server-side (N1)"`. Cosmetic — removes a line a future agent could read as an instruction. |
| `backend/server.py` | Mounts the storefront router after `realm_router`. |
| `frontend/public/hub/index.html` | Adds a `⬢ Store` chip to the hub nav (amber, matching the existing Wallets/Settings chip pattern). |
| `backend/requirements.txt` | Unchanged — included so the pack is self-consistent. `solders` is absent, as it should be. |

### New
| Path | Purpose |
|---|---|
| `backend/store/__init__.py` | Storefront API: Stripe Checkout → signature-verified webhook → entitlement → time-limited download. Income books **only** through the existing `record_settled_income(rail="stripe")`, which re-verifies the PaymentIntent and refuses duplicates. Uses the shared `network_db` handle. |
| `backend/store/products.json` | Seven-SKU catalog. A SKU with no price **or** no uploaded asset file is automatically unlisted, with the reason exposed in `/api/store/health`. `complete-collection` ships unpriced (`null`) and stays hidden until you set its price. |
| `backend/tests/test_store.py` | Four offline pytest tests (no network, no Mongo — `db` and settlement are monkeypatched). |
| `frontend/public/store/index.html` | The store page: catalog, Stripe redirect, and a post-payment panel that polls for the entitlement and serves the download. Built on the site's existing token system (Unbounded/JetBrains Mono, `--void`/`--panel`/`--line`), amber accent. Copy follows the established brand voice. |
| `OPERATOR_RUNBOOK.md` | Repo root. Exit and withdrawal instructions come **first**, then store go-live, janitor, trading, earn, weekly hygiene, and a list of things the system should never do. |

Also fixed in `backend/store/__init__.py`: checkout `success_url` / `cancel_url` now point
at `/store/index.html` (the real static page). The v1 draft pointed at `/store/thanks`,
a route that does not exist on static hosting — every purchase would have dead-ended.

---

## Verified in-sandbox before delivery

```
compile (ast.parse):  defi/earn.py OK · revenue_system/unified_server.py OK
                      store/__init__.py OK · server.py OK

pytest tests/test_store.py:  4 passed
  - unpriced + asset-less SKUs stay unlisted, with correct reasons
  - tampered webhook signature -> 400 BEFORE any db write, zero income booked
  - paid webhook books exactly one income row, grants entitlement, serves the real file
  - replay of the same webhook -> still one income row, same token (Stripe retry safe)
  - vanished asset after purchase -> 503, never a placeholder download

safety sweep on the patched tree:
  signing terms outside the lazy solders guard ... 0
  income writers outside settlement.py .......... 0
  signer invitation string ...................... 0
  solders in requirements.txt ................... 0
  LST receipt_mint wired ........................ 3 (+INF in the map)
  store router mounted .......................... yes
```

## NOT verified — needs the live environment (N5: reported, not simulated)

- Stripe test-mode purchase round-trip (needs your test keys + a public webhook endpoint).
- LST `shield_verdict` resolving against live Jupiter data (needs deploy).

Both are steps in the runbook, not claims made here.

---

## Return options

**A — GitHub push (free, preferred).** Extract over your clone at matching paths, commit,
push, redeploy.

**B — Emergent file editor.** Paste each file at its path. `backend/store/` and
`frontend/public/store/` are new folders.

**C — Minimal agent run.** Attach this pack and paste:

```
Apply the attached patch set v2 to /app verbatim: 5 modified files at their existing
paths, plus new backend/store/ (2 files), backend/tests/test_store.py,
frontend/public/store/index.html, and OPERATOR_RUNBOOK.md at the repo root. Do not
modify, refactor, or "improve" anything else — the rest of the tree is already
verified per EVIDENCE.md.

Then, in order:
1. py_compile backend/defi/earn.py, backend/revenue_system/unified_server.py,
   backend/store/__init__.py, backend/server.py. Paste results.
2. Run: python -m pytest backend/tests/test_store.py -q. Paste output (expect 4 passed).
3. Re-run the FINAL SWEEP greps from EVIDENCE.md and confirm each is still zero:
   signing terms in backend; income writers outside settlement.py; hub localhost/port
   refs. Paste all three.
4. Deploy.
5. curl https://yabbai.network/api/store/health — paste it. SKUs should be UNLISTED with
   reason "asset file missing" until the operator uploads the product zips. That is the
   correct state, not a failure.
6. curl https://yabbai.network/api/defi/earn/markets — confirm LST entries now carry
   receipt_mint and a resolved shield_verdict (not "unknown"). Paste the LST entries.
7. Load /store/index.html and confirm it renders, and that the hub shows the Store chip.
8. Six-section report: DONE (with evidence) / FILES TOUCHED / DEVIATIONS / BLOCKED /
   QUESTIONS / OPERATOR CHECKLIST.

Rules N1–N5 and R1–R5 unchanged. Do not flip any flag. Do not upload or invent product
assets. Do not send outreach. If a check fails, stop and report rather than working
around it.
```

Note: `CHANGELOG_AND_RETURN.md` is documentation for you — commit it only if you want it
in the repo.

---

## Operator steps — yours alone, no code can do them

1. **Upload the seven product zips** to `STORE_ASSETS_DIR` (default `/app/store_assets`):
   `aos-v2.zip`, `aos-webdesigner.zip`, `aos-developer.zip`, `aos-smm.zip`,
   `orchestrator.zip`, `starter-bundle.zip`, `complete-collection.zip`.
   Until a SKU's file exists it stays unlisted — that is the design, and this is the last
   blocker between you and the first sale.
2. **Stripe, test mode first.** Set `STRIPE_SECRET_KEY=sk_test_…`; create a webhook
   endpoint at `https://yabbai.network/api/store/webhook` for
   `checkout.session.completed`; set `STRIPE_WEBHOOK_SECRET` from it. Also set
   `PUBLIC_BASE_URL=https://yabbai.network` and `STORE_ASSETS_DIR`.
3. **Run the full purchase once in test mode** (card 4242…), confirm the download works,
   then check the ledger holds exactly one `rail=stripe` income row. Switch to live keys.
4. **Set the Complete Collection price** in `products.json` when ready — it lists itself.
5. **Everything on the DeFi side stays dark** until you deliberately set
   `JUPITER_API_KEY`, `DEFI_ALLOWED_MINTS`, `EARN_ALLOWED_PROTOCOLS` and flip flags —
   after reading `OPERATOR_RUNBOOK.md` §1 (how to get out) first.
