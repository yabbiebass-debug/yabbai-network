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
