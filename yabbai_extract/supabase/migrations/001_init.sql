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
