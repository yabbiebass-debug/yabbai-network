# YABBAI NETWORK V2

One domain · One sign-in · Fifteen surfaces · Twelve agents · One truth ledger

---

## Quick start (5 minutes)

```bash
# 1. Install Python deps
pip install -r requirements_all.txt

# 2. Configure environment
cp .env.example .env
# Edit .env — fill in ADMIN_API_KEY, ANTHROPIC_API_KEY, Supabase keys, Stripe keys

# 3. Start all Python backends
start_all.bat          # Windows
bash start_all.sh      # Mac / Linux

# 4. Open hub/index.html in your browser
#    - Enter your Supabase URL + anon key in the CONFIG block
#    - Enter your ADMIN_API_KEY → click "Connect backends"
#    - All service chips turn green

# 5. (Optional) Unified gateway on port 8080
python yabbai_network_server.py
```

---

## What's in this build

| Surface | Stack | Port | Purpose |
|---------|-------|------|---------|
| Hub | Supabase HTML | Netlify | Single sign-in, all surfaces, civilisation map |
| Mission Control | Supabase HTML | Netlify | Director cockpit — pipeline, gates, cycles |
| Realm OS | Supabase HTML | Netlify | Ecosystem map, widget forge, enquiry stream |
| Agency Floor | Supabase HTML | Netlify | Leads, AI call guides, outcomes |
| Catalog Studio | Supabase HTML | Netlify | AI product builder |
| Client Portal | Supabase HTML | Netlify | Brief → scope → pay → track |
| The Vault | Supabase HTML | Netlify | Template/product store |
| Free Fix-Plan | Supabase HTML | Netlify | Public lead magnet |
| Revenue System | Python FastAPI | 7870 | Truth protocol, gates, income, kill-switch |
| YABBAI AI | Python FastAPI | 7860 | Brain, agents, coding, IDE |
| DeFi Simulator | Python FastAPI | 8002 | Paper trading engine (no live trades) |
| GoldScout | Python FastAPI | 8001 | Opportunity scanner + scam analysis |
| Ops Cockpit | Python FastAPI | 7880 | Truth protocol + compliance + decisions |
| Network Gateway | Python FastAPI | 8080 | Starts all backends + serves hub |
| Widget | JS embed | CDN | AI chat widget for client websites |

---

## Supabase setup

```bash
# Run migrations (in order)
supabase db push   # or run each file in supabase/migrations/ manually

# Deploy edge functions
supabase functions deploy ai-proxy
supabase functions deploy call-guide
supabase functions deploy catalog-agent
supabase functions deploy cycle-runner
supabase functions deploy diagnose
supabase functions deploy realm-intake
supabase functions deploy scope-brief
supabase functions deploy stripe-webhook
supabase functions deploy vault-config

# Set secrets
supabase secrets set ANTHROPIC_API_KEY=sk-ant-...
supabase secrets set ANTHROPIC_MODEL=claude-sonnet-4-6
supabase secrets set STRIPE_SECRET_KEY=sk_live_...
supabase secrets set STRIPE_WEBHOOK_SECRET=whsec_...
supabase secrets set ADMIN_API_KEY=<same key as .env>
```

---

## Safety invariants (enforced in code, 32 tests)

- **Income** = `source_type == payment_processor` AND `reconciled == True`. No exceptions.
- **HIGH actions** (payout / outreach / publish / trade) → queued for human approval. Never auto-executed.
- **Kill-switch** halts all activity. Accessible from the hub nav bar.
- **No live trading** — `NoKeySigner` refuses all broadcasts. Live signer stays on your machine.
- **No fabricated numbers** — missing data raises `DataGap`, shows as flag in dashboard.
- **Daily caps** count both executed AND pending spend. Caps can only be tightened.

Run tests: `python revenue_system/tests/test_safety.py`

---

## Deploy to cloud

**Web surfaces** → Netlify (drag hub/ + each surface folder)
**Python backends** → Railway / Render / Fly (one service per backend, or use the gateway)

Update the `PY` block in `hub/index.html` with deployed backend URLs.

---

## The honest truth

This is operational infrastructure, not an autonomous money-maker. It organises and
constrains the work. Real income enters only through verified payment webhooks.
Every irreversible action waits for your approval. The kill-switch is always one
click away. The ledger only shows what actually happened.

YABBAI.NETWORK · BASHAM Automations, Melbourne
