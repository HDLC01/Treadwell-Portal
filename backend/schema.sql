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
  deposit_status  text not null default 'pending'   check (deposit_status  in ('pending','submitted','received')),
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

-- Deposit intake. masked_ref = last-4 display value (derived server-side). Full
-- customer ACH routing/account numbers live in routing_number/account_number (added
-- in the V1 alter block below) so Treadwell can initiate the debit; those are exposed
-- ONLY via the SERVICE_TOKEN-gated admin endpoint (masked in email + chat).
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
  check (msg_type in ('text','proposal_card','deposit_request','system','deposit_submitted'));
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

-- ACH debit intake (V1): the customer's OWN routing + account numbers, collected so
-- Treadwell can initiate the deposit debit. Full values are stored deliberately and
-- surfaced only through the SERVICE_TOKEN-gated admin endpoint (masked in email/chat).
alter table public.portal_deposits add column if not exists routing_number text;
alter table public.portal_deposits add column if not exists account_number text;
-- Account type the customer selected on the ACH form: 'checking' or 'savings'.
alter table public.portal_deposits add column if not exists account_type text;

-- ── Deposit 'submitted' state (customer has paid; staff have not verified yet) ─
-- Without this the board could not tell "approved, nothing paid" from "customer
-- sent us their money and is waiting" — both read 'pending'.
--
-- The create-table / add-column statements above already carry the widened checks,
-- but they are no-ops on an existing table, so re-add the constraints explicitly.
-- The names are Postgres' auto-generated ones for a column-level check
-- (<table>_<column>_check), which is how both were originally created. Written as
-- plain statements, NOT a `do $$ … $$` block: run_script() splits this file on ';'
-- and a dollar-quoted body would be torn in half. Idempotent (drop-if-exists first);
-- every existing value is still legal, so the re-validation passes.
alter table public.portal_proposals drop constraint if exists portal_proposals_deposit_status_check;
alter table public.portal_proposals add constraint portal_proposals_deposit_status_check
  check (deposit_status in ('pending','submitted','received'));
alter table public.portal_questions drop constraint if exists portal_questions_msg_type_check;
alter table public.portal_questions add constraint portal_questions_msg_type_check
  check (msg_type in ('text','proposal_card','deposit_request','system','deposit_submitted'));

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

-- ── Deposit invoice ───────────────────────────────────────────────────────────
-- The deposit request is a real invoice document (generated on demand from these
-- columns — no blob is stored). The NUMBER is issued once from a sequence and then
-- frozen, so re-sending or re-approving can never show the customer a second
-- invoice number for the same deposit.
create sequence if not exists public.portal_invoice_seq start 1001;
alter table public.portal_proposals add column if not exists deposit_invoice_no text;
alter table public.portal_proposals add column if not exists deposit_invoice_issued_at timestamptz;

-- ── Deposit required? ─────────────────────────────────────────────────────────
-- Staff tick "Require deposit" on the Files page before sending. Direct-customer
-- work defaults to requiring one; GC work usually does not, but the box is free to
-- toggle either way for edge cases. FALSE means the approval automation issues no
-- invoice and the customer never sees a Deposit step.
--
-- Default TRUE, so every row that existed before this column keeps today's
-- behaviour, and a publish from an older proposal tool (which sends no flag) is
-- indistinguishable from today.
alter table public.portal_proposals add column if not exists deposit_required boolean not null default true;

-- ── Which revision the customer was actually SENT ─────────────────────────────
-- Points at a public.draft_revisions row (owned by the proposal tool). The
-- proposal page and its PDF used to render LIVE from drafts.data, which meant any
-- mid-edit save silently rewrote a proposal somebody had already received — and
-- after approval, the numbers they had agreed to. Pinning to the snapshot that was
-- sent makes "what they approved" and "what they saw" provably the same document.
--
-- NULL = published before revisions existed → fall back to live drafts.data, i.e.
-- exactly today's behaviour. Self-heals on that project's next send.
alter table public.portal_proposals add column if not exists current_revision_no int;

-- ── Customer read state (notification bell) ───────────────────────────────────
-- Per (reader, proposal), NOT per session: sessions expire and are replaced, so a
-- re-login would reset the marker. Keyed on email like portal_proposal_recipients,
-- so two people on one proposal each keep their own unread count. Deliberately
-- per-customer — a shared marker would leak one customer's read state to another.
create table if not exists public.portal_read_state (
  email        text not null,
  proposal_id  text not null references public.portal_proposals(proposal_id) on delete cascade,
  last_seen_at timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);
create unique index if not exists portal_read_state_unique_idx
  on public.portal_read_state (lower(email), proposal_id);
create index if not exists portal_read_state_email_idx
  on public.portal_read_state (lower(email));

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
alter table public.portal_read_state enable row level security;

-- ── Proposal Follow-Up System ─────────────────────────────────────────────────
-- The CRM as a virtual sales coordinator: chase every sent proposal on a cadence,
-- let the customer say "delayed" or "not moving forward" instead of going silent,
-- and point each estimator at the handful worth a personal call.
--
-- Assignment lives on the proposal row. The proposal tool requires it at publish,
-- so it is coalesced ahead of drafts.owner_email everywhere the board reads an
-- estimator.
alter table public.portal_proposals add column if not exists assigned_estimator text;

-- NULL = this proposal is not automated. Stamped now() on EVERY publish, so it is
-- both the enrolment marker AND the "sent" cadence anchor. created_at cannot
-- anchor it: a re-publish never moves created_at, so a revision would inherit the
-- original send's clock and fire its reminders immediately.
alter table public.portal_proposals add column if not exists followup_enrolled_at timestamptz;
-- An estimator deliberately removed this proposal from automation. Sticky across
-- re-publishes: a revision send must not silently undo a human's decision.
alter table public.portal_proposals add column if not exists followup_disabled_at timestamptz;
-- The customer asked for time. Automation sleeps until this date (Chicago).
alter table public.portal_proposals add column if not exists followup_paused_until date;
-- Closed-Lost detail; the stage itself is proposal_status below.
alter table public.portal_proposals add column if not exists closed_lost_reason text;
alter table public.portal_proposals add column if not exists closed_at timestamptz;
-- First view of the CURRENT send cycle. viewed_at coalesces (first view EVER, which
-- the board wants), so after a revision it cannot anchor the viewed-track
-- reminders. Set by mark_viewed on the sent->viewed transition; nulled each publish.
alter table public.portal_proposals add column if not exists cycle_viewed_at timestamptz;

-- Stage timestamps. The board sorts each column by its OWN date, and these
-- milestones were previously status flips with no time recorded, so "Deposit
-- received, most recent first" was unanswerable.
alter table public.portal_proposals add column if not exists last_viewed_at timestamptz;
-- Somebody followed the link in the notification email. DELIBERATELY separate from the
-- viewed_* columns and from proposal_status.
--
-- A click is a weaker fact than a view: Outlook SafeLinks and mail scanners follow links
-- without a human reading anything, and the landing page serves before any login. Writing it
-- into proposal_status='viewed' would also move cycle_viewed_at, which is the anchor
-- followup_rules.py uses to switch a customer from the not-opened reminder track to the
-- opened one — so a scanner could silently change which emails a customer receives. These two
-- columns let the board say "the email got through" without claiming anybody read the bid.
alter table public.portal_proposals add column if not exists link_clicked_at timestamptz;
alter table public.portal_proposals add column if not exists last_link_clicked_at timestamptz;
alter table public.portal_proposals add column if not exists deposit_submitted_at timestamptz;
alter table public.portal_proposals add column if not exists deposit_received_at timestamptz;
alter table public.portal_proposals add column if not exists contacts_received_at timestamptz;
alter table public.portal_proposals add column if not exists scheduled_at timestamptz;

-- Closed-Lost is a terminal pipeline stage, not a parallel flag: proposal_status is
-- the single source of stage truth for the board, the drawer and the customer
-- badge, and a second column would force every consumer to consult both.
alter table public.portal_proposals drop constraint if exists portal_proposals_proposal_status_check;
alter table public.portal_proposals add constraint portal_proposals_proposal_status_check
  check (proposal_status in ('sent','viewed','approved','closed_lost'));

-- The customer's project-status answer posts a customer-authored chat row, so it
-- reaches the staff bell the same way a submitted deposit does.
alter table public.portal_questions drop constraint if exists portal_questions_msg_type_check;
alter table public.portal_questions add constraint portal_questions_msg_type_check
  check (msg_type in ('text','proposal_card','deposit_request','system','deposit_submitted','status_update'));

-- Follow-up activity: automated sends, the estimator's own logged outreach, and the
-- customer's status answers. The daily digest reads "time since last estimator
-- follow-up" from the staff_* kinds, and suppresses anything already actioned.
create table if not exists public.portal_followups (
  id           bigint generated always as identity primary key,
  proposal_id  text not null references public.portal_proposals(proposal_id) on delete cascade,
  kind         text not null check (kind in
                 ('auto_email','staff_call','staff_email','staff_text','staff_note','customer_status')),
  detail       jsonb,
  created_by   text,
  created_at   timestamptz not null default now()
);
create index if not exists portal_followups_proposal_idx
  on public.portal_followups (proposal_id, created_at desc);
-- THE dedupe for automated sends: one email per (proposal, rule occurrence),
-- enforced by the database rather than by application bookkeeping. The worker
-- reserves a row before sending, so a crashed tick, a container restart, or two
-- overlapping containers during a deploy cannot double-nag a customer.
create unique index if not exists portal_followups_rule_uidx
  on public.portal_followups (proposal_id, (detail->>'rule_key'))
  where kind = 'auto_email' and (detail->>'rule_key') is not null;
alter table public.portal_followups enable row level security;

-- ── Settings ──────────────────────────────────────────────────────────────
-- Key/value JSON, one row per area of the app. Currently one row: 'followups', holding the
-- chase cadence and the wording of the four automated emails (see followup_settings.py).
--
-- A TABLE rather than environment variables because these are edited by staff in the tool, and
-- an env var needs a redeploy and a person with SSH. Key/value rather than a column per setting
-- because the cadence will grow another knob and an ALTER on every one of them is how a settings
-- table becomes something nobody wants to touch.
--
-- Deliberately NOT seeded. followup_settings.merge() lays stored values over the shipped
-- defaults, so an absent row means "the cadence as shipped" and the worker keeps sending exactly
-- as it did before this table existed. That is what makes the DDL safe to apply after the code.
create table if not exists public.portal_settings (
  id          text primary key,
  value       jsonb not null default '{}'::jsonb,
  updated_at  timestamptz not null default now(),
  updated_by  text
);
alter table public.portal_settings enable row level security;
