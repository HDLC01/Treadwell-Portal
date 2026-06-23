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
  ip           text
);

-- Email one-time codes (customer auth). One active row per proposal.
create table if not exists public.portal_login_codes (
  proposal_id  text primary key references public.portal_proposals(proposal_id) on delete cascade,
  email        text not null,
  code_hash    text not null,
  expires_at   timestamptz not null,
  attempts     int not null default 0,
  created_at   timestamptz not null default now()
);

-- Issued customer sessions (opaque cookie -> this row; revocable).
create table if not exists public.portal_sessions (
  session_token text primary key,
  proposal_id   text not null references public.portal_proposals(proposal_id) on delete cascade,
  email         text not null,
  expires_at    timestamptz not null,
  created_at    timestamptz not null default now()
);
create index if not exists portal_sessions_proposal_idx on public.portal_sessions(proposal_id);

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
