-- YABBAI.NETWORK V2 — consolidated Supabase schema
-- Run in Supabase SQL Editor (project gecwxvwziktvaiwdhzeg). Order preserved 001->009.


-- ============================================================
-- 001_init.sql
-- ============================================================
-- =====================================================================
-- YABBAI MISSION CONTROL — production schema v1
-- Real data. RLS everywhere. Director gates enforced in the database.
-- Paste this whole file into Supabase SQL Editor, or `supabase db push`.
-- =====================================================================

-- ---------- LEADS ----------
create table if not exists leads (
  id          uuid primary key default gen_random_uuid(),
  owner       uuid not null default auth.uid() references auth.users(id) on delete cascade,
  biz         text not null,
  niche       text,
  suburb      text,
  pain        text,
  email       text,
  phone       text,
  fit         text check (fit in ('Hot','Warm','Cold')),
  score       int  check (score between 0 and 100),
  stage       text not null default 'New'
              check (stage in ('New','Outreach drafted','Contacted','Replied','Awaiting close','Won','Dead')),
  source      text not null default 'manual',
  notes       text,
  created_at  timestamptz not null default now()
);

-- ---------- CLIENTS ----------
create table if not exists clients (
  id            uuid primary key default gen_random_uuid(),
  owner         uuid not null default auth.uid() references auth.users(id) on delete cascade,
  lead_id       uuid references leads(id),
  biz           text not null,
  package       text check (package in ('Starter','Growth','Full Ops')),
  setup_fee     numeric not null default 0,
  setup_paid    boolean not null default false,
  tier          text check (tier in ('Maintain','Optimize','Scale')),
  mrr           numeric not null default 0,
  status        text not null default 'Onboarding'
                check (status in ('Onboarding','QA','Ship pending','Active','Paused','Churned')),
  build_pct     int not null default 0 check (build_pct between 0 and 100),
  health        text not null default 'ok' check (health in ('ok','risk')),
  stripe_customer_id text,
  created_via   text not null check (created_via in ('director','stripe_checkout')),
  created_at    timestamptz not null default now()
);

-- ---------- APPROVALS (the Director gates) ----------
create table if not exists approvals (
  id            uuid primary key default gen_random_uuid(),
  owner         uuid not null default auth.uid() references auth.users(id) on delete cascade,
  type          text not null check (type in ('outreach','contract','ship','fork','save','spend')),
  title         text not null,
  detail        text,
  payload       jsonb not null default '{}'::jsonb,
  agent         text not null default 'SYSTEM',
  license       text,
  license_class text check (license_class in ('permissive','copyleft','blocked')),
  status        text not null default 'pending' check (status in ('pending','approved','rejected')),
  decided_at    timestamptz,
  created_at    timestamptz not null default now()
);

-- ---------- ACTIONS (execution ledger: nothing side-effectful without an approved row) ----------
create table if not exists actions (
  id           uuid primary key default gen_random_uuid(),
  owner        uuid not null default auth.uid() references auth.users(id) on delete cascade,
  approval_id  uuid not null references approvals(id),
  kind         text not null,
  result       jsonb not null default '{}'::jsonb,
  executed_at  timestamptz not null default now()
);

-- ---------- CYCLES + EVENTS (the log) ----------
create table if not exists cycles (
  id          uuid primary key default gen_random_uuid(),
  owner       uuid not null default auth.uid() references auth.users(id) on delete cascade,
  n           int not null,
  summary     jsonb not null default '{}'::jsonb,
  started_at  timestamptz not null default now()
);

create table if not exists events (
  id          uuid primary key default gen_random_uuid(),
  owner       uuid not null default auth.uid() references auth.users(id) on delete cascade,
  cycle_n     int not null default 0,
  agent       text not null,
  message     text not null,
  created_at  timestamptz not null default now()
);

-- =====================================================================
-- ROW LEVEL SECURITY — ownership-scoped on every table
-- =====================================================================
alter table leads     enable row level security;
alter table clients   enable row level security;
alter table approvals enable row level security;
alter table actions   enable row level security;
alter table cycles    enable row level security;
alter table events    enable row level security;

do $$
declare t text;
begin
  foreach t in array array['leads','clients','approvals','actions','cycles','events'] loop
    execute format('drop policy if exists "%1$s_owner_all" on %1$s', t);
    execute format(
      'create policy "%1$s_owner_all" on %1$s for all using (owner = auth.uid()) with check (owner = auth.uid())', t);
  end loop;
end $$;

-- =====================================================================
-- GATE ENFORCEMENT — in the database, not just the UI
-- =====================================================================

-- Gate 1: a blocked-license approval can NEVER transition to approved.
create or replace function trg_block_blocked() returns trigger
language plpgsql as $$
begin
  if new.status = 'approved' and new.license_class = 'blocked' then
    raise exception 'License policy: proprietary/unknown-license items cannot be approved. Cloning a closed product is not a fork.';
  end if;
  return new;
end $$;
drop trigger if exists block_blocked on approvals;
create trigger block_blocked before update on approvals
for each row execute function trg_block_blocked();

-- Gate 2: no execution without an approved approval (ledger integrity).
create or replace function trg_require_approved() returns trigger
language plpgsql as $$
declare st text;
begin
  select status into st from approvals where id = new.approval_id and owner = new.owner;
  if st is distinct from 'approved' then
    raise exception 'Execution blocked: approval % is not approved.', new.approval_id;
  end if;
  return new;
end $$;
drop trigger if exists require_approved on actions;
create trigger require_approved before insert on actions
for each row execute function trg_require_approved();

-- Gate 3 (CLOSE is human): created_via is constrained to director | stripe_checkout.
-- The cycle-runner has no code path that inserts clients; this column makes any
-- attempt auditable and the check constraint rejects unknown origins.

-- =====================================================================
-- TRANSITION FUNCTIONS — atomic, invoker rights (RLS applies)
-- =====================================================================

-- Director approves a pending item; state transition happens server-side.
create or replace function approve_approval(p_id uuid) returns void
language plpgsql security invoker set search_path = public as $$
declare a approvals;
begin
  select * into a from approvals
   where id = p_id and owner = auth.uid() and status = 'pending'
   for update;
  if not found then raise exception 'Approval not found or already decided.'; end if;
  if a.license_class = 'blocked' then
    raise exception 'License policy: blocked items cannot be approved.';
  end if;

  update approvals set status = 'approved', decided_at = now() where id = p_id;

  if a.type = 'outreach' then
    update leads set stage = 'Contacted'
     where id = (a.payload->>'lead_id')::uuid and owner = auth.uid();
  elsif a.type = 'contract' then
    update leads set stage = 'Awaiting close'
     where id = (a.payload->>'lead_id')::uuid and owner = auth.uid();
  elsif a.type = 'ship' then
    update clients set status = 'Active'
     where id = (a.payload->>'client_id')::uuid and owner = auth.uid();
  elsif a.type = 'save' then
    update clients set health = 'ok'
     where id = (a.payload->>'client_id')::uuid and owner = auth.uid();
  end if;

  insert into actions(owner, approval_id, kind) values (auth.uid(), p_id, a.type);
  insert into events(owner, agent, message)
  values (auth.uid(), 'DIRECTOR', 'approved: ' || a.title);
end $$;

create or replace function reject_approval(p_id uuid) returns void
language plpgsql security invoker set search_path = public as $$
begin
  update approvals set status = 'rejected', decided_at = now()
   where id = p_id and owner = auth.uid() and status = 'pending';
  if not found then raise exception 'Approval not found or already decided.'; end if;
  insert into events(owner, agent, message)
  values (auth.uid(), 'DIRECTOR', 'rejected approval ' || p_id::text);
end $$;

-- The human close: the ONLY user-side path that creates a client.
create or replace function close_deal(
  p_lead_id uuid, p_package text, p_tier text, p_fee numeric, p_mrr numeric
) returns uuid
language plpgsql security invoker set search_path = public as $$
declare cid uuid;
begin
  update leads set stage = 'Won'
   where id = p_lead_id and owner = auth.uid() and stage in ('Awaiting close','Replied');
  if not found then raise exception 'Lead not found or not ready to close.'; end if;

  insert into clients(owner, lead_id, biz, package, setup_fee, tier, mrr, created_via)
  select auth.uid(), l.id, l.biz, p_package, p_fee, p_tier, p_mrr, 'director'
    from leads l where l.id = p_lead_id
  returning id into cid;

  insert into events(owner, agent, message)
  values (auth.uid(), 'DIRECTOR',
          'closed ' || (select biz from leads where id = p_lead_id) ||
          ' — ' || p_package || ' $' || p_fee || ' + ' || p_tier || ' $' || p_mrr || '/mo');
  return cid;
end $$;

-- Mark a lead replied (from your inbox) → queues the contract gate.
create or replace function mark_replied(p_lead_id uuid) returns void
language plpgsql security invoker set search_path = public as $$
declare l leads;
begin
  select * into l from leads where id = p_lead_id and owner = auth.uid();
  if not found then raise exception 'Lead not found.'; end if;
  update leads set stage = 'Replied' where id = p_lead_id;
  insert into approvals(owner, type, title, detail, agent, payload)
  values (auth.uid(), 'contract',
          'Proposal → ' || l.biz,
          'Lead replied positive. Approve to send proposal; you take the close call.',
          'PITCHER', jsonb_build_object('lead_id', l.id));
end $$;

-- =====================================================================
-- REALTIME — live UI on approvals + events
-- =====================================================================
do $$ begin
  alter publication supabase_realtime add table approvals;
exception when duplicate_object then null; end $$;
do $$ begin
  alter publication supabase_realtime add table events;
exception when duplicate_object then null; end $$;

-- =====================================================================
-- VIEWS — money at a glance
-- =====================================================================
create or replace view money_view with (security_invoker = true) as
select
  coalesce(sum(mrr) filter (where status = 'Active'), 0)        as mrr,
  coalesce(sum(setup_fee) filter (where setup_paid), 0)         as cash_collected,
  count(*) filter (where status = 'Active')                     as active_clients
from clients where owner = auth.uid();


-- ============================================================
-- 002_portal.sql
-- ============================================================
-- =====================================================================
-- YABBAI REALM PORTAL — schema extension v2 (run AFTER 001_init.sql)
-- Clients get their own auth users; RLS gives each side its own window
-- into the SAME rows. That symmetry is the Mutual Ledger.
-- =====================================================================

-- ---------- ORDERS (the front door) ----------
create table if not exists orders (
  id            uuid primary key default gen_random_uuid(),
  client_user   uuid not null default auth.uid() references auth.users(id) on delete cascade,
  director      uuid not null references auth.users(id),
  brief         text not null,
  scope         jsonb not null default '{}'::jsonb,   -- AI-mapped plan (catalog-bound)
  package       text check (package in ('Starter','Growth','Full Ops')),
  fee           numeric,
  tier          text check (tier in ('Maintain','Optimize','Scale')),
  mrr           numeric,
  status        text not null default 'Draft'
                check (status in ('Draft','Scoped','Quoted','Paid','Building','Shipped','Live','Declined')),
  kind          text not null default 'build' check (kind in ('build','upgrade_request')),
  client_id     uuid references clients(id),          -- linked once payment creates the client
  created_at    timestamptz not null default now()
);

-- ---------- VALUE LEDGER (both sides read the same truth) ----------
create table if not exists value_events (
  id          uuid primary key default gen_random_uuid(),
  order_id    uuid not null references orders(id) on delete cascade,
  director    uuid not null references auth.users(id),
  client_user uuid not null references auth.users(id),
  kind        text not null check (kind in ('hours_saved','leads_generated','bookings','revenue_assisted','uptime_days')),
  amount      numeric not null,
  note        text,
  created_at  timestamptz not null default now()
);

-- ---------- FLEET UPDATES (one build, every retainer client benefits) ----------
create table if not exists fleet_updates (
  id          uuid primary key default gen_random_uuid(),
  director    uuid not null default auth.uid() references auth.users(id),
  title       text not null,
  detail      text,
  shipped_at  timestamptz not null default now()
);

-- ---------- REFERRALS (client growth funds client credit) ----------
create table if not exists referrals (
  id          uuid primary key default gen_random_uuid(),
  referrer    uuid not null default auth.uid() references auth.users(id) on delete cascade,
  code        text not null unique,
  referred_email text,
  status      text not null default 'sent' check (status in ('sent','joined','converted')),
  credit      numeric not null default 0,
  created_at  timestamptz not null default now()
);

-- =====================================================================
-- RLS — two roles, one table, symmetric visibility
-- =====================================================================
alter table orders        enable row level security;
alter table value_events  enable row level security;
alter table fleet_updates enable row level security;
alter table referrals     enable row level security;

drop policy if exists orders_client on orders;
create policy orders_client on orders
  for all using (client_user = auth.uid()) with check (client_user = auth.uid());
drop policy if exists orders_director on orders;
create policy orders_director on orders
  for all using (director = auth.uid()) with check (director = auth.uid());

drop policy if exists ve_client_read on value_events;
create policy ve_client_read on value_events for select using (client_user = auth.uid());
drop policy if exists ve_director_all on value_events;
create policy ve_director_all on value_events
  for all using (director = auth.uid()) with check (director = auth.uid());
-- NOTE: only the Director can WRITE value events; both sides READ the same rows.

drop policy if exists fleet_read_all on fleet_updates;
create policy fleet_read_all on fleet_updates for select using (auth.uid() is not null);
drop policy if exists fleet_director_write on fleet_updates;
create policy fleet_director_write on fleet_updates
  for insert with check (director = auth.uid());

drop policy if exists ref_owner on referrals;
create policy ref_owner on referrals
  for all using (referrer = auth.uid()) with check (referrer = auth.uid());

-- =====================================================================
-- GATES — quotes are catalog-bound and Director-confirmed
-- =====================================================================
-- Fees/MRR may only ever hold catalog values. The AI maps; it cannot price.
create or replace function trg_catalog_prices() returns trigger
language plpgsql as $$
begin
  if new.fee is not null and new.fee not in (1500,4000,8000) then
    raise exception 'Fee must come from the fixed catalog (1500/4000/8000).';
  end if;
  if new.mrr is not null and new.mrr not in (500,1500,3500) then
    raise exception 'Retainer must come from the fixed catalog (500/1500/3500).';
  end if;
  return new;
end $$;
drop trigger if exists catalog_prices on orders;
create trigger catalog_prices before insert or update on orders
for each row execute function trg_catalog_prices();

-- Client accepts a scoped order → Quoted (still unpaid, still non-binding).
create or replace function accept_quote(p_order uuid) returns void
language plpgsql security invoker set search_path = public as $$
begin
  update orders set status = 'Quoted'
   where id = p_order and client_user = auth.uid() and status = 'Scoped';
  if not found then raise exception 'Order not found or not ready.'; end if;
end $$;

-- Realtime for live portal updates
do $$ begin
  alter publication supabase_realtime add table orders;
exception when duplicate_object then null; end $$;
do $$ begin
  alter publication supabase_realtime add table fleet_updates;
exception when duplicate_object then null; end $$;

-- Client-side ledger rollup
create or replace view my_value_view with (security_invoker = true) as
select
  coalesce(sum(amount) filter (where kind = 'hours_saved'), 0)     as hours_saved,
  coalesce(sum(amount) filter (where kind = 'leads_generated'), 0) as leads_generated,
  coalesce(sum(amount) filter (where kind = 'bookings'), 0)        as bookings,
  coalesce(sum(amount) filter (where kind = 'revenue_assisted'),0) as revenue_assisted
from value_events where client_user = auth.uid();


-- ============================================================
-- 003_endusers.sql
-- ============================================================
-- =====================================================================
-- YABBAI REALM — 003: THE END-USER LAYER (run after 001 + 002)
-- The widget your clients' customers touch. Every enquiry auto-writes
-- the Mutual Ledger — the ecosystem starts feeding itself with real data.
-- =====================================================================

-- gen_random_bytes (widget keys) requires pgcrypto — enable it first.
create extension if not exists pgcrypto;

-- The ledger must accept value born at the end-user layer: widget enquiries
-- arrive before any order exists and before the client has a portal login.
alter table value_events alter column order_id drop not null;
alter table value_events alter column client_user drop not null;

create table if not exists widgets (
  id           uuid primary key default gen_random_uuid(),
  director     uuid not null default auth.uid() references auth.users(id) on delete cascade,
  client_id    uuid references clients(id) on delete set null,
  client_user  uuid references auth.users(id),
  key          text not null unique,                 -- public embed key
  biz          text not null,
  greeting     text not null default 'G''day — how can we help?',
  booking_url  text,
  accent       text not null default '#9945FF',
  active       boolean not null default true,
  created_at   timestamptz not null default now()
);

create table if not exists enquiries (
  id           uuid primary key default gen_random_uuid(),
  widget_id    uuid not null references widgets(id) on delete cascade,
  director     uuid not null references auth.users(id),
  client_user  uuid references auth.users(id),
  name         text,
  contact      text,
  message      text not null,
  ai_reply     text,
  want_booking boolean not null default false,
  status       text not null default 'new' check (status in ('new','replied','booked','closed')),
  created_at   timestamptz not null default now()
);

alter table widgets   enable row level security;
alter table enquiries enable row level security;

drop policy if exists w_director on widgets;
create policy w_director on widgets
  for all using (director = auth.uid()) with check (director = auth.uid());
drop policy if exists w_client_read on widgets;
create policy w_client_read on widgets for select using (client_user = auth.uid());

drop policy if exists e_director on enquiries;
create policy e_director on enquiries
  for all using (director = auth.uid()) with check (director = auth.uid());
drop policy if exists e_client_rw on enquiries;
create policy e_client_rw on enquiries
  for select using (client_user = auth.uid());
drop policy if exists e_client_status on enquiries;
create policy e_client_status on enquiries
  for update using (client_user = auth.uid()) with check (client_user = auth.uid());

-- NOTE: end users are anonymous. They never touch these tables directly —
-- the realm-intake function (service role, key-checked) is their only door.

do $$ begin
  alter publication supabase_realtime add table enquiries;
exception when duplicate_object then null; end $$;

-- Director helper: spin up a widget for a client in one call.
create or replace function create_widget(p_client uuid, p_booking text default null)
returns widgets language plpgsql security invoker set search_path = public as $$
declare c clients; w widgets;
begin
  select * into c from clients where id = p_client and owner = auth.uid();
  if not found then raise exception 'Client not found.'; end if;
  insert into widgets(director, client_id, biz, booking_url, key)
  values (auth.uid(), c.id, c.biz, p_booking,
          'yw_' || encode(gen_random_bytes(9), 'hex'))
  returning * into w;
  return w;
end $$;


-- ============================================================
-- 004_vault.sql
-- ============================================================
-- =====================================================================
-- YABBAI VAULT — 004: the template store (run after 001-003)
-- Public gallery (no login to browse/configure). Sessions are inbound
-- leads: every configurator run with an email lands in YOUR pipeline.
-- =====================================================================

create table if not exists vault_products (
  id            uuid primary key default gen_random_uuid(),
  owner         uuid not null references auth.users(id) on delete cascade,
  slug          text not null unique,
  title         text not null,
  tagline       text,
  description   text,
  includes      text[] not null default '{}',
  price_aud     numeric not null default 0,
  stripe_link   text,            -- your Stripe Payment Link
  gumroad_url   text,            -- marketplace listing (MoR handles global tax)
  badge         text,            -- e.g. 'BEST SELLER', 'NEW'
  config_schema jsonb not null default '{}'::jsonb,  -- drives the LLM configurator
  active        boolean not null default true,
  sort          int not null default 100,
  created_at    timestamptz not null default now()
);

create table if not exists vault_sessions (
  id          uuid primary key default gen_random_uuid(),
  owner       uuid not null references auth.users(id),
  product_id  uuid not null references vault_products(id) on delete cascade,
  qa          jsonb not null default '[]'::jsonb,
  guide       text,
  email       text,
  created_at  timestamptz not null default now()
);

alter table vault_products enable row level security;
alter table vault_sessions enable row level security;

-- The world can browse what's live; only you can manage it.
drop policy if exists vp_public_read on vault_products;
create policy vp_public_read on vault_products for select using (active = true);
drop policy if exists vp_owner_all on vault_products;
create policy vp_owner_all on vault_products
  for all using (owner = auth.uid()) with check (owner = auth.uid());

-- Sessions: written only by the vault-config function (service role); you read them.
drop policy if exists vs_owner_read on vault_sessions;
create policy vs_owner_read on vault_sessions for select using (owner = auth.uid());


-- ============================================================
-- 005_staff.sql
-- ============================================================
-- =====================================================================
-- YABBAI REALM — 005: THE STAFFED AGENCY LAYER (run after 001-004)
-- Roles (director/manager/caller), lead assignment, call records with
-- per-lead AI guides, and the outcome -> MRR pipeline. Real humans only:
-- no row here is created by a generator. Leads come from real imports or
-- inbound; calls are logged by real callers.
-- =====================================================================

-- ---------- STAFF (who works the realm) ----------
create table if not exists staff (
  id          uuid primary key default gen_random_uuid(),
  director    uuid not null default auth.uid() references auth.users(id) on delete cascade,
  user_id     uuid references auth.users(id),          -- set when they accept the invite
  email       text not null,
  name        text,
  role        text not null default 'caller' check (role in ('director','manager','caller')),
  status      text not null default 'invited' check (status in ('invited','active','paused')),
  invite_code text unique,
  commission_pct numeric not null default 0,           -- % of setup fee on closes they source
  created_at  timestamptz not null default now(),
  unique(director, email)
);

-- ---------- ASSIGNMENTS (which caller owns which real lead) ----------
create table if not exists assignments (
  id          uuid primary key default gen_random_uuid(),
  director    uuid not null references auth.users(id),
  lead_id     uuid not null references leads(id) on delete cascade,
  staff_id    uuid not null references staff(id) on delete cascade,
  assigned_at timestamptz not null default now(),
  unique(lead_id)
);

-- ---------- CALLS (every real outreach attempt, logged by a real caller) ----------
create table if not exists calls (
  id          uuid primary key default gen_random_uuid(),
  director    uuid not null references auth.users(id),
  lead_id     uuid not null references leads(id) on delete cascade,
  staff_id    uuid references staff(id),
  guide       text,                                    -- per-lead AI script used
  channel     text not null default 'call' check (channel in ('call','email','dm','sms')),
  outcome     text check (outcome in ('no_answer','not_interested','callback','interested','booked_meeting','closed_won')),
  notes       text,
  created_at  timestamptz not null default now()
);

-- ---------- IMPORT BATCHES (provenance — every lead traces to a real source) ----------
create table if not exists import_batches (
  id          uuid primary key default gen_random_uuid(),
  director    uuid not null default auth.uid() references auth.users(id),
  source      text not null,                           -- 'csv_maps','apollo','clay','manual','vault','widget'
  filename    text,
  rows_in     int not null default 0,
  rows_added  int not null default 0,
  rows_dupe   int not null default 0,
  created_at  timestamptz not null default now()
);

-- leads gains provenance + assignment visibility
alter table leads add column if not exists batch_id uuid references import_batches(id);
alter table leads add column if not exists assigned_staff uuid references staff(id);

-- =====================================================================
-- RLS — director sees all; manager sees the team's; caller sees only theirs
-- =====================================================================
alter table staff         enable row level security;
alter table assignments   enable row level security;
alter table calls         enable row level security;
alter table import_batches enable row level security;

-- helper: is the current user a manager/director in this director's org?
create or replace function is_staff_role(p_director uuid, p_roles text[])
returns boolean language sql stable security definer set search_path = public as $$
  select exists(
    select 1 from staff s
    where s.director = p_director and s.user_id = auth.uid()
      and s.role = any(p_roles) and s.status = 'active'
  ) or p_director = auth.uid();
$$;

-- STAFF table
drop policy if exists staff_director on staff;
create policy staff_director on staff
  for all using (director = auth.uid()) with check (director = auth.uid());
drop policy if exists staff_self_read on staff;
create policy staff_self_read on staff
  for select using (user_id = auth.uid());

-- ASSIGNMENTS
drop policy if exists asg_director_mgr on assignments;
create policy asg_director_mgr on assignments
  for all using (is_staff_role(director, array['director','manager']))
  with check (is_staff_role(director, array['director','manager']));
drop policy if exists asg_caller_read on assignments;
create policy asg_caller_read on assignments
  for select using (staff_id in (select id from staff where user_id = auth.uid()));

-- CALLS: caller writes/reads their own; manager+director read all in org
drop policy if exists calls_caller on calls;
create policy calls_caller on calls
  for all using (staff_id in (select id from staff where user_id = auth.uid()))
  with check (staff_id in (select id from staff where user_id = auth.uid()));
drop policy if exists calls_mgr_read on calls;
create policy calls_mgr_read on calls
  for select using (is_staff_role(director, array['director','manager']));

-- IMPORT BATCHES
drop policy if exists imp_director_mgr on import_batches;
create policy imp_director_mgr on import_batches
  for all using (is_staff_role(director, array['director','manager']))
  with check (director = auth.uid());

-- Callers need to read+update the leads assigned to them
drop policy if exists leads_caller_rw on leads;
create policy leads_caller_rw on leads
  for select using (assigned_staff in (select id from staff where user_id = auth.uid()));
drop policy if exists leads_caller_update on leads;
create policy leads_caller_update on leads
  for update using (assigned_staff in (select id from staff where user_id = auth.uid()))
  with check (assigned_staff in (select id from staff where user_id = auth.uid()));
drop policy if exists leads_mgr_all on leads;
create policy leads_mgr_all on leads
  for all using (is_staff_role(owner, array['director','manager']))
  with check (is_staff_role(owner, array['director','manager']));

-- =====================================================================
-- FUNCTIONS
-- =====================================================================

-- Invite a teammate (director only)
create or replace function invite_staff(p_email text, p_name text, p_role text, p_commission numeric default 0)
returns staff language plpgsql security invoker set search_path = public as $$
declare s staff;
begin
  if p_role not in ('manager','caller') then raise exception 'Role must be manager or caller.'; end if;
  insert into staff(director, email, name, role, commission_pct, invite_code)
  values (auth.uid(), lower(p_email), p_name, p_role, coalesce(p_commission,0),
          'inv_' || encode(gen_random_bytes(7),'hex'))
  returning * into s;
  return s;
end $$;

-- Accept an invite (the teammate, after signing in, binds their user to the staff row)
create or replace function accept_invite(p_code text)
returns void language plpgsql security invoker set search_path = public as $$
begin
  update staff set user_id = auth.uid(), status = 'active'
   where invite_code = p_code and user_id is null;
  if not found then raise exception 'Invite not found or already used.'; end if;
end $$;

-- Assign a real lead to a caller (director/manager)
create or replace function assign_lead(p_lead uuid, p_staff uuid)
returns void language plpgsql security invoker set search_path = public as $$
declare dir uuid;
begin
  select owner into dir from leads where id = p_lead;
  if not is_staff_role(dir, array['director','manager']) then raise exception 'Not permitted.'; end if;
  insert into assignments(director, lead_id, staff_id) values (dir, p_lead, p_staff)
    on conflict (lead_id) do update set staff_id = excluded.staff_id, assigned_at = now();
  update leads set assigned_staff = p_staff where id = p_lead;
end $$;

-- Log a real call outcome → moves the lead and (on close) hands to the human close gate
create or replace function log_call(p_lead uuid, p_outcome text, p_notes text, p_guide text default null, p_channel text default 'call')
returns void language plpgsql security invoker set search_path = public as $$
declare dir uuid; sid uuid;
begin
  select owner into dir from leads where id = p_lead;
  select id into sid from staff where user_id = auth.uid() and director = dir limit 1;
  insert into calls(director, lead_id, staff_id, guide, channel, outcome, notes)
  values (dir, p_lead, sid, p_guide, p_channel, p_outcome, p_notes);

  update leads set stage = case p_outcome
    when 'no_answer'      then 'Contacted'
    when 'not_interested' then 'Dead'
    when 'callback'       then 'Contacted'
    when 'interested'     then 'Replied'
    when 'booked_meeting' then 'Replied'
    when 'closed_won'     then 'Awaiting close'   -- still gated: Director confirms + payment
    else stage end,
    last_contact_at = now()
  where id = p_lead;

  insert into events(owner, agent, message)
  values (dir, coalesce((select name from staff where id = sid),'CALLER'),
          'logged ' || p_outcome || ' on ' || (select biz from leads where id = p_lead));
end $$;

-- add last_contact_at if missing
alter table leads add column if not exists last_contact_at timestamptz;

-- realtime
do $$ begin alter publication supabase_realtime add table calls; exception when duplicate_object then null; end $$;
do $$ begin alter publication supabase_realtime add table assignments; exception when duplicate_object then null; end $$;

-- Manager dashboard rollup
create or replace view team_view with (security_invoker = true) as
select s.id staff_id, s.name, s.role, s.status,
  count(distinct a.lead_id) assigned,
  count(distinct c.id) filter (where c.created_at > now() - interval '7 days') calls_7d,
  count(distinct c.id) filter (where c.outcome = 'closed_won') wins
from staff s
left join assignments a on a.staff_id = s.id
left join calls c on c.staff_id = s.id
where s.director = auth.uid()
group by s.id, s.name, s.role, s.status;


-- ============================================================
-- 006_inbound.sql
-- ============================================================
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


-- ============================================================
-- 007_catalog.sql
-- ============================================================
-- =====================================================================
-- YABBAI REALM — 007: THE SCALABLE CATALOG ENGINE (after 001-006)
-- Ideation -> spec -> build -> white-hat audit -> LIST. A product cannot
-- go active until (a) it has a real deliverable asset AND (b) it passes
-- audit. The "never sell air" rule is enforced in the database, not the UI.
-- =====================================================================

-- Extend vault_products with the fulfillment + audit gate
alter table vault_products add column if not exists fulfillment text
  not null default 'concept'
  check (fulfillment in ('concept','building','deliverable_now'));
alter table vault_products add column if not exists asset_ref text;     -- path/url to the REAL deliverable
alter table vault_products add column if not exists audit_status text
  not null default 'unaudited'
  check (audit_status in ('unaudited','passed','flagged'));
alter table vault_products add column if not exists audit_notes text;
alter table vault_products add column if not exists origin text default 'manual'; -- manual|ideation

-- ---------- IDEATION SPECS (skills -> sellable product ideas) ----------
create table if not exists product_specs (
  id           uuid primary key default gen_random_uuid(),
  owner        uuid not null default auth.uid() references auth.users(id) on delete cascade,
  title        text not null,
  pitch        text,
  audience     text,            -- who buys it
  build_effort text,            -- 'have it' | 'hours' | 'days'
  price_band   numeric,
  spec         jsonb not null default '{}'::jsonb,   -- includes, deliverables, config schema
  status       text not null default 'idea' check (status in ('idea','approved','built','rejected')),
  created_at   timestamptz not null default now()
);

-- ---------- AUDITS (the white-hat layer: every product reviewed before listing) ----------
create table if not exists product_audits (
  id          uuid primary key default gen_random_uuid(),
  owner       uuid not null default auth.uid() references auth.users(id) on delete cascade,
  product_id  uuid references vault_products(id) on delete cascade,
  verdict     text not null check (verdict in ('pass','flag')),
  checks      jsonb not null default '{}'::jsonb,  -- security/licence/quality/claims results
  summary     text,
  created_at  timestamptz not null default now()
);

alter table product_specs  enable row level security;
alter table product_audits enable row level security;
drop policy if exists ps_owner on product_specs;
create policy ps_owner on product_specs for all using (owner = auth.uid()) with check (owner = auth.uid());
drop policy if exists pa_owner on product_audits;
create policy pa_owner on product_audits for all using (owner = auth.uid()) with check (owner = auth.uid());

-- =====================================================================
-- THE NO-AIR GATE (database-enforced)
-- A product may only be set active=true if it is deliverable_now AND audit passed.
-- =====================================================================
create or replace function trg_no_air() returns trigger
language plpgsql as $$
begin
  if new.active = true then
    if new.fulfillment <> 'deliverable_now' then
      raise exception 'Cannot list: product is "%", not deliverable_now. No selling air.', new.fulfillment;
    end if;
    if new.audit_status <> 'passed' then
      raise exception 'Cannot list: product has not passed white-hat audit (status: %).', new.audit_status;
    end if;
    if new.asset_ref is null or length(trim(new.asset_ref)) = 0 then
      raise exception 'Cannot list: no deliverable asset attached (asset_ref empty).';
    end if;
  end if;
  return new;
end $$;
drop trigger if exists no_air on vault_products;
create trigger no_air before insert or update on vault_products
for each row execute function trg_no_air();

-- Promote an audited, deliverable product to live (the only blessed path to active)
create or replace function list_product(p_id uuid) returns void
language plpgsql security invoker set search_path = public as $$
begin
  update vault_products set active = true
   where id = p_id and owner = auth.uid();   -- trigger enforces the three conditions
  if not found then raise exception 'Product not found.'; end if;
end $$;

-- Record an audit verdict and sync the product's audit_status
create or replace function record_audit(p_id uuid, p_verdict text, p_checks jsonb, p_summary text)
returns void language plpgsql security invoker set search_path = public as $$
begin
  insert into product_audits(owner, product_id, verdict, checks, summary)
  values (auth.uid(), p_id, p_verdict, coalesce(p_checks,'{}'::jsonb), p_summary);
  update vault_products
     set audit_status = case when p_verdict = 'pass' then 'passed' else 'flagged' end,
         audit_notes = p_summary
   where id = p_id and owner = auth.uid();
end $$;

-- Turn an approved spec into a draft product (concept fulfillment until the asset is built)
create or replace function spec_to_product(p_spec uuid) returns uuid
language plpgsql security invoker set search_path = public as $$
declare sp product_specs; pid uuid; new_slug text;
begin
  select * into sp from product_specs where id = p_spec and owner = auth.uid();
  if not found then raise exception 'Spec not found.'; end if;
  new_slug := lower(regexp_replace(sp.title,'[^a-zA-Z0-9]+','-','g')) || '-' || substr(md5(random()::text),1,4);
  insert into vault_products(owner, slug, title, tagline, description, includes, price_aud,
                             config_schema, active, fulfillment, origin)
  values (auth.uid(), new_slug, sp.title, sp.pitch,
          coalesce(sp.spec->>'description', sp.pitch),
          coalesce((select array_agg(value::text) from jsonb_array_elements_text(sp.spec->'includes')), '{}'),
          coalesce(sp.price_band, 0),
          coalesce(sp.spec->'config_schema','{}'::jsonb),
          false, 'concept', 'ideation')
  returning id into pid;
  update product_specs set status = 'built' where id = p_spec;
  return pid;
end $$;

-- Catalog progress view (toward the big number, honestly)
create or replace view catalog_view with (security_invoker = true) as
select
  count(*)                                                   as total,
  count(*) filter (where active)                             as live,
  count(*) filter (where fulfillment = 'deliverable_now')    as deliverable,
  count(*) filter (where fulfillment = 'building')           as building,
  count(*) filter (where fulfillment = 'concept')            as concept,
  count(*) filter (where audit_status = 'passed')            as audited
from vault_products where owner = auth.uid();


-- ============================================================
-- 008_legal.sql
-- ============================================================
-- =====================================================================
-- YABBAI.NETWORK — 008: LEGAL CONSENT LAYER (run after 001-007)
-- Records exactly who accepted which policy version, when, from where.
-- This audit trail is what makes "signed at signup" legally meaningful.
-- =====================================================================

-- Versioned policy documents (so you can update terms and prove which
-- version each user agreed to).
create table if not exists legal_documents (
  id          uuid primary key default gen_random_uuid(),
  slug        text not null,                 -- 'terms','privacy','client-agreement','aup','refund'
  version     text not null,                 -- e.g. '2026-06-15'
  title       text not null,
  body        text not null,
  effective   date not null default current_date,
  active      boolean not null default true,
  created_at  timestamptz not null default now(),
  unique(slug, version)
);

-- Acceptance records — the signature. One row per user per document version.
create table if not exists consents (
  id           uuid primary key default gen_random_uuid(),
  user_id      uuid not null default auth.uid() references auth.users(id) on delete cascade,
  doc_slug     text not null,
  doc_version  text not null,
  accepted_at  timestamptz not null default now(),
  ip           text,                          -- captured client-side, best-effort
  user_agent   text,
  context      text,                          -- 'signup','reaccept','checkout'
  unique(user_id, doc_slug, doc_version)
);

alter table legal_documents enable row level security;
alter table consents        enable row level security;

-- Anyone signed in can READ active policies (they need to see what they're agreeing to)
drop policy if exists legal_read on legal_documents;
create policy legal_read on legal_documents for select using (active = true);
-- Only the owner (you) manages policy text — handled via service role / SQL, not the app.

-- Users can see + create their OWN consent records; nobody can alter or delete them
drop policy if exists consent_own_read on consents;
create policy consent_own_read on consents for select using (user_id = auth.uid());
drop policy if exists consent_own_insert on consents;
create policy consent_own_insert on consents for insert with check (user_id = auth.uid());
-- (no update/delete policy => consent records are immutable once written: a clean audit trail)

-- Helper: record acceptance of the current active set in one call
create or replace function accept_policies(p_slugs text[], p_context text, p_ip text, p_ua text)
returns int language plpgsql security invoker set search_path = public as $$
declare s text; v text; n int := 0;
begin
  foreach s in array p_slugs loop
    select version into v from legal_documents where slug = s and active = true
      order by effective desc limit 1;
    if v is not null then
      insert into consents(user_id, doc_slug, doc_version, context, ip, user_agent)
      values (auth.uid(), s, v, p_context, p_ip, p_ua)
      on conflict (user_id, doc_slug, doc_version) do nothing;
      n := n + 1;
    end if;
  end loop;
  return n;
end $$;

-- Has the current user accepted the latest of a given doc?
create or replace function has_accepted(p_slug text)
returns boolean language sql stable security invoker set search_path = public as $$
  select exists(
    select 1 from consents c
    join legal_documents d on d.slug = c.doc_slug and d.version = c.doc_version
    where c.user_id = auth.uid() and c.doc_slug = p_slug and d.active = true
  );
$$;

-- View: outstanding policies the user still needs to accept (for re-consent prompts)
create or replace view my_pending_policies with (security_invoker = true) as
select d.slug, d.version, d.title
from legal_documents d
where d.active = true
  and not exists (
    select 1 from consents c
    where c.user_id = auth.uid() and c.doc_slug = d.slug and c.doc_version = d.version
  );


-- ============================================================
-- 009_ratelimit.sql
-- ============================================================
-- =====================================================================
-- YABBAI.NETWORK — 009: PUBLIC-DOOR RATE LIMITING (after 001-008)
-- Protects the open AI endpoints (realm-intake, diagnose, vault-config)
-- from abuse that would run up your AI bill. Per-IP sliding window,
-- enforced server-side. This is the ONE pre-revenue hardening item that
-- saves money before you have clients, not after.
-- =====================================================================

create table if not exists rate_hits (
  id        bigserial primary key,
  bucket    text not null,            -- e.g. 'diagnose' or 'intake'
  ip        text not null,
  at        timestamptz not null default now()
);
create index if not exists rate_hits_lookup on rate_hits (bucket, ip, at);

-- No RLS needed: only service-role (the functions) ever touches this table.
alter table rate_hits enable row level security;  -- deny-all by default; service role bypasses

-- Atomically record a hit and report whether the caller is over the limit.
-- Returns true if ALLOWED, false if over the limit. Also prunes old rows cheaply.
create or replace function rate_check(p_bucket text, p_ip text, p_limit int, p_window_secs int)
returns boolean
language plpgsql security definer set search_path = public as $$
declare cnt int;
begin
  -- prune this bucket+ip's expired rows (keeps the table small without a cron)
  delete from rate_hits
   where bucket = p_bucket and ip = p_ip and at < now() - make_interval(secs => p_window_secs);

  select count(*) into cnt
    from rate_hits
   where bucket = p_bucket and ip = p_ip and at >= now() - make_interval(secs => p_window_secs);

  if cnt >= p_limit then
    return false;  -- over the limit
  end if;

  insert into rate_hits(bucket, ip) values (p_bucket, p_ip);
  return true;
end $$;

-- Occasional global prune (call from any function opportunistically, or a scheduled task).
create or replace function rate_prune()
returns void language sql security definer set search_path = public as $$
  delete from rate_hits where at < now() - interval '1 hour';
$$;

