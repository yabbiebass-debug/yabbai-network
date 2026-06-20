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
