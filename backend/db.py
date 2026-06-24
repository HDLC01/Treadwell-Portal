"""Database access for the portal.

Uses psycopg3 against the SAME Postgres the proposal tool uses (one source of
truth). Reads proposal content from `drafts`; reads/writes the `portal_*`
tables. psycopg3 parses jsonb columns into Python objects automatically.
"""
from __future__ import annotations

from typing import Any, Optional

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

import config

_pool: Optional[ConnectionPool] = None


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(config.DATABASE_URL, min_size=1, max_size=8, kwargs={"row_factory": dict_row})
    return _pool


def q1(sql: str, params: tuple = ()) -> Optional[dict[str, Any]]:
    with pool().connection() as conn:
        cur = conn.execute(sql, params)
        return cur.fetchone()


def qall(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with pool().connection() as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall()


def execute(sql: str, params: tuple = ()) -> None:
    with pool().connection() as conn:
        conn.execute(sql, params)


def run_script(sql: str) -> None:
    """Run a multi-statement SQL file. psycopg3's extended protocol rejects
    multiple commands per execute(), so we strip `--` comments (which may
    themselves contain ';') and split on ';'. Our scripts have no `--` or `;`
    inside string literals."""
    import re

    no_comments = re.sub(r"--[^\n]*", "", sql)
    with pool().connection() as conn:
        for chunk in no_comments.split(";"):
            if chunk.strip():
                conn.execute(chunk)


# ── Proposal content (read-only; owned by the proposal tool) ──────────────────
def get_draft_data(proposal_id: str) -> Optional[dict[str, Any]]:
    row = q1("select data from public.drafts where id = %s and deleted_at is null", (proposal_id,))
    return (row or {}).get("data") if row else None


# ── portal_proposals ──────────────────────────────────────────────────────────
def get_proposal_by_token(token: str) -> Optional[dict[str, Any]]:
    return q1("select * from public.portal_proposals where token = %s", (token,))


def get_proposal(proposal_id: str) -> Optional[dict[str, Any]]:
    return q1("select * from public.portal_proposals where proposal_id = %s", (proposal_id,))


def list_proposals_by_email(email: str) -> list[dict[str, Any]]:
    """All proposals tied to a (verified) customer email — the account view."""
    return qall(
        "select * from public.portal_proposals where lower(customer_email) = lower(%s) "
        "order by created_at desc",
        (email,),
    )


def email_has_proposal(email: str) -> bool:
    row = q1("select 1 from public.portal_proposals where lower(customer_email) = lower(%s) limit 1", (email,))
    return row is not None


# ── admin (publish + pipeline) ──────────────────────────────────────────────────
def create_portal_proposal(proposal_id, token, customer_email, customer_name, project_name, pdf_path, published_by) -> None:
    execute(
        "insert into public.portal_proposals "
        "(proposal_id, token, customer_email, customer_name, project_name, pdf_path, published_by) "
        "values (%s,%s,%s,%s,%s,%s,%s)",
        (proposal_id, token, customer_email, customer_name, project_name, pdf_path, published_by),
    )


def update_portal_proposal(proposal_id, customer_email, customer_name, project_name, pdf_path) -> None:
    execute(
        "update public.portal_proposals set customer_email=%s, customer_name=%s, project_name=%s, "
        "pdf_path=coalesce(%s, pdf_path), updated_at=now() where proposal_id=%s",
        (customer_email, customer_name, project_name, pdf_path, proposal_id),
    )


def list_all_portal_proposals() -> list[dict[str, Any]]:
    return qall(
        "select proposal_id, token, customer_email, customer_name, project_name, proposal_status, "
        "deposit_status, schedule_status, approved_total, created_at from public.portal_proposals "
        "order by created_at desc"
    )


def list_deposits(proposal_id: str) -> list[dict[str, Any]]:
    return qall(
        "select method, account_name, bank_name, masked_ref, note, submitted_at "
        "from public.portal_deposits where proposal_id=%s order by submitted_at desc",
        (proposal_id,),
    )


def latest_approval(proposal_id: str) -> Optional[dict[str, Any]]:
    return q1(
        "select name, title, approved_date, total, option_label, signed_at "
        "from public.portal_approvals where proposal_id=%s order by signed_at desc limit 1",
        (proposal_id,),
    )


def mark_viewed(proposal_id: str) -> None:
    execute(
        "update public.portal_proposals set viewed_at = coalesce(viewed_at, now()), "
        "proposal_status = case when proposal_status = 'sent' then 'viewed' else proposal_status end, "
        "updated_at = now() where proposal_id = %s",
        (proposal_id,),
    )


def set_approved(proposal_id: str, total, option_label, name, title, approved_date) -> None:
    execute(
        "update public.portal_proposals set proposal_status='approved', approved_at=now(), "
        "approved_total=%s, approved_option=%s, approved_name=%s, approved_title=%s, approved_date=%s, "
        "updated_at=now() where proposal_id=%s",
        (total, option_label, name, title, approved_date, proposal_id),
    )


def set_deposit_status(proposal_id: str, status: str) -> None:
    execute(
        "update public.portal_proposals set deposit_status=%s, updated_at=now() where proposal_id=%s",
        (status, proposal_id),
    )


def set_schedule_status(proposal_id: str, status: str) -> None:
    execute(
        "update public.portal_proposals set schedule_status=%s, updated_at=now() where proposal_id=%s",
        (status, proposal_id),
    )


# ── Q&A ────────────────────────────────────────────────────────────────────────
def list_questions(proposal_id: str) -> list[dict[str, Any]]:
    return qall(
        "select id, author_kind, author_email, body, created_at "
        "from public.portal_questions where proposal_id=%s order by created_at asc",
        (proposal_id,),
    )


def add_question(proposal_id: str, author_kind: str, author_email: Optional[str], body: str) -> dict[str, Any]:
    return q1(
        "insert into public.portal_questions (proposal_id, author_kind, author_email, body) "
        "values (%s,%s,%s,%s) returning id, author_kind, author_email, body, created_at",
        (proposal_id, author_kind, author_email, body),
    )


# ── Approvals ───────────────────────────────────────────────────────────────────
def add_approval(proposal_id, name, title, approved_date, total, option_label, ip) -> None:
    execute(
        "insert into public.portal_approvals (proposal_id, name, title, approved_date, total, option_label, ip) "
        "values (%s,%s,%s,%s,%s,%s,%s)",
        (proposal_id, name, title, approved_date, total, option_label, ip),
    )


# ── Deposits ─────────────────────────────────────────────────────────────────────
def add_deposit(proposal_id, method, account_name, bank_name, masked_ref, note) -> None:
    execute(
        "insert into public.portal_deposits (proposal_id, method, account_name, bank_name, masked_ref, note) "
        "values (%s,%s,%s,%s,%s,%s)",
        (proposal_id, method, account_name, bank_name, masked_ref, note),
    )


# ── OTP login codes (keyed by email) ──────────────────────────────────────────
def upsert_login_code(email: str, code_hash: str, expires_at) -> None:
    execute(
        "insert into public.portal_login_codes (email, code_hash, expires_at, attempts) "
        "values (%s,%s,%s,0) "
        "on conflict (email) do update set code_hash=excluded.code_hash, "
        "expires_at=excluded.expires_at, attempts=0, created_at=now()",
        (email.lower(), code_hash, expires_at),
    )


def get_login_code(email: str) -> Optional[dict[str, Any]]:
    return q1("select * from public.portal_login_codes where email=%s", (email.lower(),))


def bump_login_attempts(email: str) -> None:
    execute("update public.portal_login_codes set attempts = attempts + 1 where email=%s", (email.lower(),))


def clear_login_code(email: str) -> None:
    execute("delete from public.portal_login_codes where email=%s", (email.lower(),))


# ── Sessions (email-scoped) ─────────────────────────────────────────────────────
def create_session(session_token: str, email: str, expires_at) -> None:
    execute(
        "insert into public.portal_sessions (session_token, email, expires_at) values (%s,%s,%s)",
        (session_token, email.lower(), expires_at),
    )


def get_session(session_token: str) -> Optional[dict[str, Any]]:
    return q1(
        "select * from public.portal_sessions where session_token=%s and expires_at > now()",
        (session_token,),
    )


def delete_session(session_token: str) -> None:
    execute("delete from public.portal_sessions where session_token=%s", (session_token,))


def cleanup_expired() -> None:
    """Purge expired sessions + login codes so the tables don't grow unbounded."""
    execute("delete from public.portal_sessions where expires_at <= now()")
    execute("delete from public.portal_login_codes where expires_at <= now()")
