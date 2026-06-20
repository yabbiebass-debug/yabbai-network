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
