-- Treadwell Customer Proposal Portal — portal-owned tables.
--
-- These live in the SAME database as the proposal tool's `drafts` table (one
-- source of truth). This file is safe to run against prod: it only creates the
-- portal_* tables (it never touches `drafts`). Idempotent.

-- A published proposal — the customer-facing record for one drafts row.
create table if not exists public.portal_proposals (
  proposal_id     text primary key,                 -- = drafts.id
  token           text not null unique,             -- unguessable URL token
  customer_email  text not null,
  customer_name   text,
  project_name    text,
  pdf_path        text,                             -- Supabase Storage path / URL of the official PDF
  proposal_status text not null default 'sent'      check (proposal_status in ('sent','viewed','approved')),
  deposit_status  text not null default 'pending'   check (deposit_status  in ('pending','received')),
  schedule_status text not null default 'pending'   check (schedule_status in ('pending','scheduled')),
  approved_total  numeric,
  approved_option text,
  approved_name   text,
  approved_title  text,
  approved_date   date,
  published_by    text,
  created_at      timestamptz not null default now(),
  viewed_at       timestamptz,
  approved_at     timestamptz,
  updated_at      timestamptz not null default now()
);

-- The Q&A thread for a proposal (customer asks; staff answers from the admin tool).
create table if not exists public.portal_questions (
  id           bigint generated always as identity primary key,
  proposal_id  text not null references public.portal_proposals(proposal_id) on delete cascade,
  author_kind  text not null check (author_kind in ('customer','staff')),
  author_email text,
  body         text not null,
  created_at   timestamptz not null default now(),
  notified_at  timestamptz
);
create index if not exists portal_questions_proposal_idx on public.portal_questions(proposal_id, created_at);

-- The approval capture (signed acceptance).
create table if not exists public.portal_approvals (
  id           bigint generated always as identity primary key,
  proposal_id  text not null references public.portal_proposals(proposal_id) on delete cascade,
  name         text not null,
  title        text,
  approved_date date,
  total        numeric,
  option_label text,
  signed_at    timestamptz not null default now(),
  ip           text,
  approver_email text                               -- which verified recipient clicked Approve
);

-- Email one-time codes (customer auth) — keyed by EMAIL (account login, not
-- per-proposal). One active code per email.
create table if not exists public.portal_login_codes (
  email        text primary key,
  code_hash    text not null,
  expires_at   timestamptz not null,
  attempts     int not null default 0,
  created_at   timestamptz not null default now()
);

-- Issued customer sessions — EMAIL-scoped (grants access to every proposal on
-- that email). Opaque cookie -> this row; revocable.
create table if not exists public.portal_sessions (
  session_token text primary key,
  email         text not null,
  expires_at    timestamptz not null,
  created_at    timestamptz not null default now()
);
create index if not exists portal_sessions_email_idx on public.portal_sessions(email);
create index if not exists portal_proposals_email_idx on public.portal_proposals(lower(customer_email));

-- Deposit intake. NEVER store raw bank numbers — only a masked reference.
create table if not exists public.portal_deposits (
  id            bigint generated always as identity primary key,
  proposal_id   text not null references public.portal_proposals(proposal_id) on delete cascade,
  method        text not null check (method in ('ach','check')),
  account_name  text,
  bank_name     text,
  masked_ref    text,                              -- e.g. "••••6789" — last 4 only
  note          text,
  submitted_at  timestamptz not null default now()
);

-- Every email allowed to access a proposal — INCLUDES the primary customer_email.
-- Reconciled on each publish (see admin_publish). Backfilled below so existing
-- proposals keep working. This is what lets a proposal be sent to (and opened +
-- approved by) more than one person; auth lookups union this with customer_email.
create table if not exists public.portal_proposal_recipients (
  id           bigint generated always as identity primary key,
  proposal_id  text not null references public.portal_proposals(proposal_id) on delete cascade,
  email        text not null,
  added_by     text,
  added_at     timestamptz not null default now()
);
create unique index if not exists portal_recipients_unique_idx
  on public.portal_proposal_recipients (proposal_id, lower(email));
create index if not exists portal_recipients_email_idx
  on public.portal_proposal_recipients (lower(email));

-- Audit which verified email approved (approvals predate multi-recipient; add for
-- existing DBs — the create-table above already has it for fresh installs).
alter table public.portal_approvals add column if not exists approver_email text;

-- Backfill: every existing proposal's primary contact is a recipient. Idempotent
-- (the unique index makes the ON CONFLICT a no-op on re-run / staging reboot).
insert into public.portal_proposal_recipients (proposal_id, email)
select proposal_id, lower(customer_email) from public.portal_proposals
on conflict do nothing;

-- ── V1 revamp: unified chat thread + configurable team-notify recipients ──────
-- portal_questions becomes the single chat thread. msg_type distinguishes plain
-- customer/staff text from system-generated cards; meta carries per-type payload
-- (e.g. a deposit_request's amount). Existing rows default to 'text', so the
-- current thread is unchanged. System/card rows use author_kind='staff' (the
-- author_kind check has no 'system' value — msg_type is the real discriminator).
alter table public.portal_questions add column if not exists msg_type text not null default 'text'
  check (msg_type in ('text','proposal_card','deposit_request','system'));
alter table public.portal_questions add column if not exists meta jsonb;

-- Backfill one proposal_card per published proposal so existing threads open with
-- the proposal card at the top (created_at = the proposal's, so it sorts first).
-- Idempotent via the not-exists guard.
insert into public.portal_questions (proposal_id, author_kind, body, msg_type, created_at)
select p.proposal_id, 'staff', 'Your proposal is ready to review.', 'proposal_card', p.created_at
from public.portal_proposals p
where not exists (
  select 1 from public.portal_questions q
  where q.proposal_id = p.proposal_id and q.msg_type = 'proposal_card'
);

-- Configurable internal notification recipients (question / approval / deposit
-- alerts). Net-new: previously env-only. notify_team reads this and falls back to
-- the env lists when empty. Seeded ONLY when the table has never held rows (a
-- not-exists guard, NOT on-conflict) so staff deletions survive a boot re-run.
create table if not exists public.portal_notify_recipients (
  id         bigint generated always as identity primary key,
  email      text not null,
  kind       text not null default 'general' check (kind in ('general','deposit')),
  enabled    boolean not null default true,   -- green (receives portal notifs) / gray (off)
  added_by   text,
  created_at timestamptz not null default now()
);
-- Existing installs: add the on/off toggle column (the create-table above is a
-- no-op there). Governs whether a roster member actually receives notifications.
alter table public.portal_notify_recipients add column if not exists enabled boolean not null default true;
create unique index if not exists portal_notify_recipients_unique_idx
  on public.portal_notify_recipients (kind, lower(email));
insert into public.portal_notify_recipients (email, kind)
select v.email, 'general'
from (values ('hanz@wetreadwell.com'), ('will@wetreadwell.com')) as v(email)
where not exists (select 1 from public.portal_notify_recipients);

-- Per-project notification overrides: assign an extra person to ONE project's
-- notifications ('add'), or let someone opt OUT of one project ('mute'). Applied
-- on top of the enabled roster at send time (mute wins over add). Mirrors
-- portal_proposal_recipients (the customer-side per-project scoping table).
create table if not exists public.portal_notify_overrides (
  id           bigint generated always as identity primary key,
  proposal_id  text not null references public.portal_proposals(proposal_id) on delete cascade,
  email        text not null,
  mode         text not null check (mode in ('add','mute')),
  created_at   timestamptz not null default now()
);
create unique index if not exists portal_notify_overrides_unique_idx
  on public.portal_notify_overrides (proposal_id, lower(email));

-- ── V1 revamp: multi-select pricing → summed approval + 25% deposit ───────────
-- A customer may now approve MULTIPLE published options. approved_options holds
-- the selected label list (jsonb) and approved_total their server-computed sum.
-- approved_option (text) is kept as a denormalized ", "-joined summary so every
-- existing consumer (customer banner, staff drawer, board) keeps working.
-- deposit_amount is the auto-calc (25% of approved_total). Pre-revamp rows have
-- approved_options null → single-option fallback everywhere.
alter table public.portal_proposals add column if not exists approved_options jsonb;
alter table public.portal_proposals add column if not exists deposit_amount numeric;
alter table public.portal_proposals add column if not exists deposit_requested_at timestamptz;
alter table public.portal_approvals add column if not exists options jsonb;

-- Inbound email capture: one chat row per received email (meta.email_id is the
-- Resend received-email id). Partial unique index = idempotency backstop for
-- concurrent webhook retries; the handler also checks before inserting.
create unique index if not exists portal_questions_email_uidx
  on public.portal_questions ((meta->>'email_id'))
  where meta->>'email_id' is not null;

-- Deposit confirmation (customer-push bank transfer): when the customer tells us
-- they've sent the transfer, capture the date they sent it and an optional bank
-- trace/confirmation number to help staff match it on the statement.
alter table public.portal_deposits add column if not exists sent_date date;
alter table public.portal_deposits add column if not exists trace_ref text;

-- Self-recorded transfer: the customer types WHERE they sent the deposit (the
-- destination account details) so staff can reconcile it. No pre-configured
-- Treadwell bank details live in the app anymore.
alter table public.portal_deposits add column if not exists sent_to_beneficiary text;
alter table public.portal_deposits add column if not exists sent_to_bank text;
alter table public.portal_deposits add column if not exists sent_to_routing text;
alter table public.portal_deposits add column if not exists sent_to_account text;

-- Pay-by-check: the customer records the check number off the cheque they mailed
-- (we never ask for the MICR routing/account — staff read those off the physical
-- cheque on arrival). `account_name` reuse = the name printed on the check.
alter table public.portal_deposits add column if not exists check_number text;

-- ── V1 revamp: contact collection (tracker step between Deposit and Schedule) ──
-- After the deposit, the customer supplies project contacts (primary required,
-- plus optional accounts-payable / billing). contacts_status gates the new
-- 4-step tracker (Proposal → Deposit → Contact info → Schedule).
alter table public.portal_proposals add column if not exists contacts_status text
  not null default 'pending' check (contacts_status in ('pending','received'));
create table if not exists public.portal_contacts (
  id           bigint generated always as identity primary key,
  proposal_id  text not null references public.portal_proposals(proposal_id) on delete cascade,
  role         text not null default 'other' check (role in ('primary','accounts_payable','other')),
  name         text not null,
  email        text,
  phone        text,
  label        text,
  submitted_by text,
  created_at   timestamptz not null default now()
);
create index if not exists portal_contacts_proposal_idx on public.portal_contacts(proposal_id);

-- ── Row Level Security ────────────────────────────────────────────────────────
-- Enable RLS on every portal_* table so they are NOT exposed through the public
-- (anon) REST API of the shared database. Idempotent: ENABLE on an already-
-- enabled table is a no-op. The portal's backend is unaffected because it
-- connects either as the table owner (local/staging — owners bypass RLS) or as a
-- least-privilege role with explicit policies (prod — see security_prod.sql);
-- only the anon/public API path is denied. Matches drafts/events/profiles, which
-- already have RLS enabled.
alter table public.portal_proposals   enable row level security;
alter table public.portal_questions   enable row level security;
alter table public.portal_approvals   enable row level security;
alter table public.portal_login_codes enable row level security;
alter table public.portal_sessions    enable row level security;
alter table public.portal_deposits    enable row level security;
alter table public.portal_proposal_recipients enable row level security;
alter table public.portal_notify_recipients enable row level security;
alter table public.portal_notify_overrides enable row level security;
alter table public.portal_contacts enable row level security;
