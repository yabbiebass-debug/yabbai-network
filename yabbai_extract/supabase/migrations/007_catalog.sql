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
