# PUBLIC-DOOR HARDENING — rate limiting + optional Turnstile (~8 min)

The one pre-revenue security item that saves money BEFORE you have clients: your public
AI endpoints (realm-intake, diagnose, vault-config) call paid AI models. Without limits,
abuse runs up your Anthropic/OpenRouter bill. This caps it per-IP, server-side.

## 1. Database
SQL Editor -> run `supabase/migrations/009_ratelimit.sql`
(rate_hits table + rate_check() sliding-window function; self-pruning, no cron needed).

## 2. Redeploy the three guarded functions
    supabase functions deploy diagnose
    supabase functions deploy realm-intake
    supabase functions deploy vault-config
The shared guard lives in supabase/functions/_shared/ratelimit.ts and is imported by each.

Default caps (per IP, sliding 5-min window) — tune in each function if needed:
  diagnose:      20 / 5 min
  realm-intake:  15 / 5 min
  vault-config:  25 / 5 min
Over the limit returns HTTP 429 with Retry-After. Share-link reads on diagnose are exempt.

## 3. (Optional) Cloudflare Turnstile — only if you want bot challenge on top
Free, privacy-friendly CAPTCHA alternative. Add it later if you see bot abuse:
  - Create a Turnstile widget at Cloudflare -> get site key + secret.
  - supabase secrets set TURNSTILE_SECRET=...   (server-side; the guard auto-enforces when set)
  - Add the Turnstile widget to the public pages and pass its token as `turnstile` in the
    POST body. If TURNSTILE_SECRET is NOT set, the guard simply skips this layer — so you
    can ship rate limiting now and add Turnstile only if needed.

## Why this and not the rest of the review's list (yet)
Rate limiting protects your wallet at zero clients. Email automation, observability, CI,
caching, indexes — all real, all matter at 30+ clients, all premature at 0. Per the Iron
Rule (and the review's own Phase 4 = "Post 3 Clients"): get paid first, harden the rest then.
