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
