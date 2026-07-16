-- Treadwell Customer Proposal Portal — PRODUCTION database security setup.
--
-- Applied ONCE by a database admin (the `postgres` role), NOT by the portal at
-- boot — in prod the portal connects as the least-privilege `portal_app` role,
-- which has no DDL rights. Version-controlled here so the prod security posture
-- is reproducible. Idempotent — safe to re-run.
--
-- Why this exists: the portal and the proposal tool SHARE one database (one
-- source of truth). `drafts` is owned/written by the proposal tool; the portal
-- only READS it. The portal owns its `portal_*` tables. This role enforces that
-- boundary at the database level — even a full portal compromise cannot read or
-- alter the proposal tool's internal tables (events, profiles) or WRITE drafts.
--
-- Run with:  psql "<admin/postgres connection string>" -f security_prod.sql
-- (or paste into the Supabase SQL Editor).

-- 1) Least-privilege login role for the portal backend. Created WITHOUT a
--    password (inert / cannot authenticate) — set the password at deploy time:
--        alter role portal_app with password '<strong-secret>';
--    then point the portal's DATABASE_URL at the pooler as this role:
--        postgresql://portal_app.<project_ref>:<secret>@aws-0-<region>.pooler.supabase.com:5432/postgres
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'portal_app') then
    create role portal_app with login;
  end if;
end
$$;

-- 2) Schema usage only — NO create privilege, so portal_app cannot run DDL.
grant usage on schema public to portal_app;

-- 3) Read-only on the proposal tool's drafts (the proposal content the portal
--    shows the customer). RLS is enabled on drafts, so a role-scoped SELECT
--    policy is required for portal_app to see rows. This policy applies ONLY to
--    portal_app and does not change access for any other role.
grant select on public.drafts to portal_app;
drop policy if exists portal_app_read_drafts on public.drafts;
create policy portal_app_read_drafts on public.drafts
  for select to portal_app using (true);

-- 4) Full DML on the portal's own tables, plus row policies so the RLS enabled
--    in schema.sql admits portal_app to its own tables while still denying the
--    anon/public REST role.
grant select, insert, update, delete on
  public.portal_proposals, public.portal_questions, public.portal_approvals,
  public.portal_login_codes, public.portal_sessions, public.portal_deposits,
  public.portal_proposal_recipients
  to portal_app;

drop policy if exists portal_app_rw on public.portal_proposals;
create policy portal_app_rw on public.portal_proposals   for all to portal_app using (true) with check (true);
drop policy if exists portal_app_rw on public.portal_questions;
create policy portal_app_rw on public.portal_questions   for all to portal_app using (true) with check (true);
drop policy if exists portal_app_rw on public.portal_approvals;
create policy portal_app_rw on public.portal_approvals   for all to portal_app using (true) with check (true);
drop policy if exists portal_app_rw on public.portal_login_codes;
create policy portal_app_rw on public.portal_login_codes for all to portal_app using (true) with check (true);
drop policy if exists portal_app_rw on public.portal_sessions;
create policy portal_app_rw on public.portal_sessions    for all to portal_app using (true) with check (true);
drop policy if exists portal_app_rw on public.portal_deposits;
create policy portal_app_rw on public.portal_deposits    for all to portal_app using (true) with check (true);
drop policy if exists portal_app_rw on public.portal_proposal_recipients;
create policy portal_app_rw on public.portal_proposal_recipients for all to portal_app using (true) with check (true);

-- V1 revamp: configurable team-notify recipients (portal-owned).
grant select, insert, update, delete on public.portal_notify_recipients to portal_app;
drop policy if exists portal_app_rw on public.portal_notify_recipients;
create policy portal_app_rw on public.portal_notify_recipients for all to portal_app using (true) with check (true);

-- V1 revamp: project contacts collected after the deposit (portal-owned).
grant select, insert, update, delete on public.portal_contacts to portal_app;
drop policy if exists portal_app_rw on public.portal_contacts;
create policy portal_app_rw on public.portal_contacts for all to portal_app using (true) with check (true);

-- NOTE: portal_app is deliberately granted NOTHING on public.events or
-- public.profiles, and only SELECT (no write) on public.drafts. Do not widen.
