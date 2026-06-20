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
