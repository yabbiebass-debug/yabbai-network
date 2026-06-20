-- =====================================================================
-- YABBAI REALM — 006: THE SELF-SPREADING INBOUND ENGINE (after 001-005)
-- Free diagnostic (the seed) + legal public-signal discovery (fertile soil)
-- + broadened catalog (hobbyist -> startup). No scraping of private data;
-- every lead is volunteered (diagnostic) or a public help request you chose
-- to act on. That's what keeps the soil fertile instead of poisoned.
-- =====================================================================

-- ---------- DIAGNOSTICS (anyone runs this; it's the shareable seed) ----------
create table if not exists diagnostics (
  id          uuid primary key default gen_random_uuid(),
  director    uuid not null references auth.users(id),
  segment     text,                 -- hobbyist | vibecoder | startup | smallbiz | other
  situation   text not null,        -- what they're building / stuck on (their words)
  stack       text,                 -- tools/langs they mentioned
  qa          jsonb not null default '[]'::jsonb,
  fix_plan    text,                 -- the free personalized plan we gave them
  recommended text,                 -- which service slots in
  email       text,
  share_id    text unique,          -- public, shareable result link
  created_at  timestamptz not null default now()
);

-- ---------- DISCOVERY SIGNALS (PUBLIC help requests, manually queued, never scraped private data) ----------
create table if not exists signals (
  id          uuid primary key default gen_random_uuid(),
  director    uuid not null default auth.uid() references auth.users(id) on delete cascade,
  source      text not null,        -- 'reddit','github','forum','indiehackers','x','referral'
  url         text,                 -- the public post
  handle      text,                 -- public username (NOT private contact)
  summary     text,                 -- what help they're asking for
  segment     text,
  status      text not null default 'new' check (status in ('new','reviewed','engaged','converted','skipped')),
  created_at  timestamptz not null default now()
);

-- ---------- EXPANDED CATALOG: help tiers from hobbyist to startup ----------
-- (These complement the agency packages; smaller entry points = wider funnel.)
insert into vault_products
(owner, slug, title, tagline, description, includes, price_aud, badge, sort, config_schema, stripe_link, gumroad_url)
select 'PASTE_DIRECTOR_USER_ID', x.slug, x.title, x.tagline, x.description, x.includes, x.price, x.badge, x.sort, x.cfg::jsonb, 'PASTE_STRIPE_LINK', 'PASTE_GUMROAD_URL'
from (values
  ('ai-unstuck-call','AI Unstuck — 30-min Fix Session',
   'You''re building with AI and hit a wall. We unstick it, live, in 30 minutes.',
   'A focused screen-share for anyone building with AI — hobbyist to founder. Bring your bug, your broken prompt, your stuck deploy, your "it worked yesterday". We diagnose it and get you moving, then send a written recap.',
   array['30-min live screen-share','Diagnosis + working fix or clear path','Written recap with next steps','Any stack — prompts, code, deploys, automations'],
   49,'ENTRY',5,
   '{"goal":"prep for their specific stuck point","ask_about":["what they''re building","exact error or wall","stack/tools","what they''ve tried"]}'),
  ('vibe-coder-rescue','Vibe-Coder Project Rescue',
   'Your AI-built project sprawled into a mess. We refactor it into something shippable.',
   'For vibe-coders whose project grew faster than its structure. We clean the mess (the weeds), keep what works, and hand back a tidy, deployable codebase plus a map of how it fits together.',
   array['Codebase review + cleanup plan','Refactor of the worst tangles','Deploy + structure guidance','Loom walkthrough of the tidied project'],
   199,null,15,
   '{"goal":"scope the rescue","ask_about":["what the project does","what tools built it","where it breaks","hosted where"]}'),
  ('startup-automation-audit','Startup Automation Audit',
   'A founder''s map of every manual task an AI/automation could be doing instead.',
   'For small startups: we audit your operations and hand back a prioritised list of what to automate first, which tools to use, and the rough ROI of each — so you spend build time where it pays.',
   array['Ops + workflow audit','Prioritised automation roadmap','Tool recommendations per task','Quick-win list you can start this week'],
   149,'NEW',25,
   '{"goal":"understand their ops","ask_about":["what the startup does","team size","biggest time-sinks","current tools"]}')
) as x(slug,title,tagline,description,includes,price,badge,sort,cfg)
on conflict (slug) do nothing;

-- =====================================================================
-- RLS
-- =====================================================================
alter table diagnostics enable row level security;
alter table signals     enable row level security;

-- diagnostics written by the public function (service role); director reads
drop policy if exists diag_director_read on diagnostics;
create policy diag_director_read on diagnostics for select using (director = auth.uid());
-- public can read a single result by its share_id (handled in-function; no broad select)

drop policy if exists sig_director on signals;
create policy sig_director on signals
  for all using (director = auth.uid()) with check (director = auth.uid());

do $$ begin alter publication supabase_realtime add table diagnostics; exception when duplicate_object then null; end $$;
do $$ begin alter publication supabase_realtime add table signals; exception when duplicate_object then null; end $$;

-- Director funnel rollup
create or replace view funnel_view with (security_invoker = true) as
select
  (select count(*) from diagnostics where director = auth.uid()) as diagnostics_run,
  (select count(*) from diagnostics where director = auth.uid() and email is not null) as diagnostics_with_email,
  (select count(*) from signals where director = auth.uid() and status = 'new') as signals_to_review,
  (select count(*) from leads where owner = auth.uid() and source in ('diagnostic','signal','vault','widget')) as inbound_leads;
