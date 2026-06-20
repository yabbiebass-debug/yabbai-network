# LEGAL CONSENT LAYER — YABBAI.NETWORK (after 001-007, ~12 min)

Real signed acceptance at signup, with an immutable audit trail. Five policy templates
fitted to what your network actually does + a drop-in consent gate.

## ⚖️ Read this first
These are strong, usable TEMPLATES — not legal advice. Before you rely on them, have an
Australian lawyer review them, especially for: NDIS/health/finance clients, Australian
Consumer Law guarantees, and your refund terms. Find/replace these in
`legal/seed_legal_documents.sql` first:
  [LEGAL ENTITY]  [ABN]  [CONTACT EMAIL]  [STATE]

## What's included
- **Terms of Service** — platform use, payment, licence, AI disclaimer, ACL-aware liability.
- **Privacy Policy** — Privacy Act 1988 / APP aligned; covers AI model providers + overseas.
- **Client Services Agreement** — scope, no-outcome-guarantee, regulated-industry clause.
- **Acceptable Use Policy** — bans malware, scraping, spam, relicensing, false guarantees.
- **Refund & Returns** — ACL-aware, digital-goods + retainer terms.

## 1. Database
SQL Editor → run `supabase/migrations/008_legal.sql` (consent tables + audit + helpers).
Then run `legal/seed_legal_documents.sql` (after the find/replace above).
Consent records are IMMUTABLE by design — no update/delete policy — so they stand as proof.

## 2. Wire the gate into signup
The module `legal/yabbai-consent.js` shows pending policies and records signed acceptance.
Host it on your site (e.g. yabbai.network/yabbai-consent.js), then in each surface that has
signup (Portal, Vault, Hub), add after sign-in and BEFORE loading the app:

    <script src="https://yabbai.network/yabbai-consent.js"></script>
    // in the module, after auth resolves:
    const session = await ensureAuth();
    await YabbaiConsent.gate(supa, { context: 'signup' });   // blocks until accepted
    await loadAll();

It checks `my_pending_policies`; if nothing's outstanding it returns instantly, so it's safe
to call on every load (also catches users when you publish a NEW policy version).

## 3. Updating a policy later (the version trail)
Don't edit a published doc's text. Insert a NEW row in legal_documents with the same slug, a
new version (date), active=true, and set the old row active=false. Every user is then
re-prompted to accept the new version on next load, and you keep a record of exactly which
version each user agreed to and when.

## What this gives you legally (plain English)
- Proof of acceptance: who agreed, to which exact version, when, from what IP/device.
- Versioning: you can change terms and still prove what each user originally signed.
- Immutability: consent rows can't be altered or deleted via the app — a clean trail.
This is the foundation; the lawyer review makes it bulletproof for your specific niches.
