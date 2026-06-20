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
