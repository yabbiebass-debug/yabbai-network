-- =====================================================================
-- VAULT SEED — your launch catalog. These are products YOU ALREADY OWN
-- from builds in hand. Find/replace PASTE_DIRECTOR_USER_ID (Auth → Users),
-- run in SQL Editor, then fill stripe_link / gumroad_url as you list them.
-- =====================================================================

insert into vault_products
(owner, slug, title, tagline, description, includes, price_aud, badge, sort, config_schema, stripe_link, gumroad_url)
values
('PASTE_DIRECTOR_USER_ID','zapier-agency-box',
 'Zapier Agency-in-a-Box',
 'The complete 6-system automation agency: blueprints, AI agent prompts, offer sheet.',
 'Everything needed to run (or sell) a done-for-you automation agency on Zapier: seven Zap blueprints covering lead capture, outreach, billing, delivery, retention and reporting, three production AI-agent prompts, the full offer & pricing structure, and the build order that gets money flowing first.',
 array['7 Zap blueprints (trigger→action chains)','3 AI agent system prompts (Qualifier, Outreach Writer, Ops Reporter)','Offer + pricing sheet ($1.5k–$8k packages, retainer tiers)','CRM table schemas with exact field types','Week-by-week build order'],
 79,'FLAGSHIP',10,
 '{"goal":"tailor the agency kit to the buyer''s niche, country and tools","ask_about":["their niche/industry focus","country & currency","current tools (CRM, email, PM)","solo or team","first service they want to sell"]}'::jsonb,
 'PASTE_STRIPE_LINK','PASTE_GUMROAD_URL'),

('PASTE_DIRECTOR_USER_ID','client-onboarding-kit',
 'Client Onboarding Kit',
 'Payment → onboarded on autopilot: 4 emails, intake form, contract scopes, 11-step checklist.',
 'The exact post-payment machine: a 4-email welcome sequence, a 16-question intake form, contract scope-of-work language per package tier, and the 11-step gated onboarding checklist. Written for service businesses; reads human, not corporate.',
 array['4-email welcome sequence (instant, reminder, agreement, kickoff)','16-question client intake form','Contract scope blocks per package + retainer tier','11-step onboarding checklist with gates','Plain-English, AU-toned copy throughout'],
 39,null,20,
 '{"goal":"rewrite the kit in the buyer''s voice and offer","ask_about":["business name & what they sell","package names & prices","brand tone (formal ↔ casual)","booking link & e-sign tool","delivery timeline promises"]}'::jsonb,
 'PASTE_STRIPE_LINK','PASTE_GUMROAD_URL'),

('PASTE_DIRECTOR_USER_ID','au-niche-site-pack',
 'AU Small-Business Site Pack (6 templates)',
 'Six conversion-first single-file sites: tradie, clinic, gym, restaurant, artist EPK, portfolio.',
 'Six complete single-file HTML sites built for Australian small service businesses — fast, phone-first, enquiry-driven. No build tools, no dependencies: open, edit the marked sections, deploy anywhere in an afternoon.',
 array['6 single-file HTML templates','Marked EDIT-ME sections (copy, colours, services)','Mobile-first, enquiry-form ready','Works on Netlify/any static host','Licence: use on unlimited client projects'],
 59,null,30,
 '{"goal":"pick the right template and pre-write their edits","ask_about":["business type & name","suburb/city","top 3 services","brand colours","phone & booking preference"]}'::jsonb,
 'PASTE_STRIPE_LINK','PASTE_GUMROAD_URL'),

('PASTE_DIRECTOR_USER_ID','realm-boilerplate',
 'Agency OS Boilerplate (Supabase + Stripe)',
 'The full agency operating system: schema, RLS, edge functions, three wired frontends.',
 'A production agency stack: Postgres schema with row-level security and database-enforced approval gates, five Deno edge functions (AI proxy with local fallback, agent cycle runner, Stripe webhook, public intake, AI configurator), and three wired single-file frontends — director console, client portal, public store. Deploy script included.',
 array['4 SQL migrations (schema, RLS, gate triggers, views)','5 edge functions (TypeScript/Deno)','3 wired frontends (console, portal, store)','One-command deploy.sh + verify.sh','Go-live runbook'],
 149,'NEW',40,
 '{"goal":"map the boilerplate onto their stack and product","ask_about":["what they sell (service/SaaS/agency)","Supabase experience level","payment products & prices","branding (name, colours)","what to rename the three surfaces"]}'::jsonb,
 'PASTE_STRIPE_LINK','PASTE_GUMROAD_URL'),

('PASTE_DIRECTOR_USER_ID','cold-outreach-kit',
 'First-Client Outreach Kit',
 'Where to find AU service-business clients + the exact emails, DMs and follow-ups.',
 'The manual that lands clients one and two: where your ideal clients actually are (Maps, groups, directories), five niche-specific cold emails, the 2-line DM, the follow-up cadence that gets most of the replies, a $500 pilot-offer script, and the 15-minute sales call structure.',
 array['Prospect-sourcing playbook (AU-specific)','5 niche cold-email templates','DM + 2-touch follow-up scripts','Pilot offer + upsell path','15-minute call structure'],
 19,null,50,
 '{"goal":"localise the outreach to their niche and city","ask_about":["their service","target niche(s)","city/region","price point of first offer","email or DM preference"]}'::jsonb,
 'PASTE_STRIPE_LINK','PASTE_GUMROAD_URL'),

('PASTE_DIRECTOR_USER_ID','mission-control-ui',
 'Mission Control UI Kit',
 'The cyberpunk ops-console frontend: cycle ring, approval queue, live KPIs — one file.',
 'A striking dark operations dashboard in a single HTML file: animated eight-stage cycle ring with gate locks, approval card queue, pipeline and client tables, live KPI bar. Vanilla JS, CSS variables for instant rebranding, demo engine included so it runs out of the box.',
 array['Single-file HTML/CSS/JS (no build step)','Animated SVG cycle ring component','Approval queue + KPI bar patterns','Design tokens — rebrand in minutes','Demo data engine included'],
 35,null,60,
 '{"goal":"retheme and relabel the console for their product","ask_about":["product/company name","colour palette","the 8 stage names of THEIR pipeline","what the 3 gates should be","KPI labels"]}'::jsonb,
 'PASTE_STRIPE_LINK','PASTE_GUMROAD_URL'),

('PASTE_DIRECTOR_USER_ID','basham-stem-pack',
 'BASHAM Stem Pack Vol. 1',
 'Dark Melbourne rap stems + loops — mixed, labelled, royalty-cleared for your tracks.',
 'Producer-ready stems and loops from the BASHAM vault: drums, 808s, melodic loops and textures in the dark AusRap lane. Key/BPM labelled, mixed flat for headroom, cleared for commercial use in your releases (credit appreciated, never required).',
 array['Stems + loops, key/BPM labelled','24-bit WAV, mixed flat','Drums, 808s, melodics, textures','Royalty-cleared for commercial release','Bonus: 2-pass loudness mastering cheat-sheet'],
 25,null,70,
 '{"goal":"point them to the right stems for their track","ask_about":["their genre/vibe reference","target BPM range","DAW they use","vocal-led or instrumental","mood (dark/hype/moody)"]}'::jsonb,
 'PASTE_STRIPE_LINK','PASTE_GUMROAD_URL');
