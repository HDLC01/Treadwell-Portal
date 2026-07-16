"""Database access for the portal.

Uses psycopg3 against the SAME Postgres the proposal tool uses (one source of
truth). Reads proposal content from `drafts`; reads/writes the `portal_*`
tables. psycopg3 parses jsonb columns into Python objects automatically.
"""
from __future__ import annotations

from typing import Any, Optional

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
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
    """All proposals tied to a (verified) email — the account/dashboard view.
    Matches the primary customer_email OR any added recipient, so a person sent
    several proposals (as primary on some, extra recipient on others) sees them
    all. Both legs are index-served (customer_email idx + recipients email idx)."""
    return qall(
        "select * from public.portal_proposals where proposal_id in ("
        "  select proposal_id from public.portal_proposals where lower(customer_email) = lower(%s)"
        "  union"
        "  select proposal_id from public.portal_proposal_recipients where lower(email) = lower(%s)"
        ") order by created_at desc",
        (email, email),
    )


def email_has_proposal(email: str) -> bool:
    row = q1(
        "select 1 from public.portal_proposals where lower(customer_email) = lower(%s) "
        "union all "
        "select 1 from public.portal_proposal_recipients where lower(email) = lower(%s) limit 1",
        (email, email),
    )
    return row is not None


def email_can_access(proposal_id: str, email: str) -> bool:
    """True if `email` is the primary contact OR an added recipient of this proposal."""
    row = q1(
        "select 1 from public.portal_proposals "
        "where proposal_id = %s and lower(customer_email) = lower(%s) "
        "union all "
        "select 1 from public.portal_proposal_recipients "
        "where proposal_id = %s and lower(email) = lower(%s) limit 1",
        (proposal_id, email, proposal_id, email),
    )
    return row is not None


# ── portal_proposal_recipients (multi-recipient access) ──────────────────────
def get_recipients(proposal_id: str) -> list[str]:
    return [r["email"] for r in qall(
        "select email from public.portal_proposal_recipients "
        "where proposal_id = %s order by added_at, id",
        (proposal_id,),
    )]


def add_recipient(proposal_id: str, email: str, added_by: Optional[str] = None) -> None:
    execute(
        "insert into public.portal_proposal_recipients (proposal_id, email, added_by) "
        "values (%s,%s,%s) on conflict do nothing",
        (proposal_id, email.strip().lower(), added_by),
    )


def remove_recipient(proposal_id: str, email: str) -> None:
    execute(
        "delete from public.portal_proposal_recipients "
        "where proposal_id = %s and lower(email) = lower(%s)",
        (proposal_id, email),
    )


def set_recipients(proposal_id: str, emails: list[str], added_by: Optional[str] = None) -> None:
    """Replace the recipient set in one transaction: drop rows not in `emails`,
    insert the rest (retained rows keep their added_at). `emails` must be
    non-empty and already lowercased/deduped by the caller."""
    emails = [e.strip().lower() for e in emails if e and e.strip()]
    if not emails:
        return
    with pool().connection() as conn:
        conn.execute(
            "delete from public.portal_proposal_recipients "
            "where proposal_id = %s and lower(email) <> all(%s)",
            (proposal_id, emails),
        )
        for e in emails:
            conn.execute(
                "insert into public.portal_proposal_recipients (proposal_id, email, added_by) "
                "values (%s,%s,%s) on conflict do nothing",
                (proposal_id, e, added_by),
            )


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
        "deposit_status, contacts_status, schedule_status, approved_total, deposit_amount, created_at "
        "from public.portal_proposals order by created_at desc"
    )


def unread_counts() -> dict[str, int]:
    """Per-proposal count of customer messages awaiting a staff reply — customer
    text messages newer than the last staff TEXT reply. System/card rows, though
    author_kind='staff', are msg_type!='text' so they never count as a reply.
    One aggregate query for the whole board (no N+1)."""
    rows = qall(
        "select q.proposal_id as pid, count(*) as n "
        "from public.portal_questions q "
        "where q.author_kind='customer' and q.msg_type='text' "
        "and q.id > coalesce((select max(s.id) from public.portal_questions s "
        "  where s.proposal_id=q.proposal_id and s.author_kind='staff' and s.msg_type='text'), 0) "
        "group by q.proposal_id"
    )
    return {r["pid"]: int(r["n"]) for r in rows}


def list_deposits(proposal_id: str) -> list[dict[str, Any]]:
    return qall(
        "select method, account_name, bank_name, masked_ref, note, submitted_at "
        "from public.portal_deposits where proposal_id=%s order by submitted_at desc",
        (proposal_id,),
    )


def latest_approval(proposal_id: str) -> Optional[dict[str, Any]]:
    return q1(
        "select name, title, approved_date, total, option_label, options, signed_at, approver_email "
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


def set_approved(proposal_id: str, total, option_label, name, title, approved_date,
                 options=None, deposit_amount=None) -> None:
    execute(
        "update public.portal_proposals set proposal_status='approved', approved_at=now(), "
        "approved_total=%s, approved_option=%s, approved_options=%s, deposit_amount=%s, "
        "approved_name=%s, approved_title=%s, approved_date=%s, "
        "updated_at=now() where proposal_id=%s",
        (total, option_label, Jsonb(options) if options is not None else None, deposit_amount,
         name, title, approved_date, proposal_id),
    )


def set_deposit_status(proposal_id: str, status: str) -> None:
    execute(
        "update public.portal_proposals set deposit_status=%s, updated_at=now() where proposal_id=%s",
        (status, proposal_id),
    )


def set_deposit_requested(proposal_id: str) -> None:
    execute(
        "update public.portal_proposals set deposit_requested_at=now(), updated_at=now() where proposal_id=%s",
        (proposal_id,),
    )


def set_schedule_status(proposal_id: str, status: str) -> None:
    execute(
        "update public.portal_proposals set schedule_status=%s, updated_at=now() where proposal_id=%s",
        (status, proposal_id),
    )


# ── Project contacts (collected after the deposit) ──────────────────────────────
def list_contacts(proposal_id: str) -> list[dict[str, Any]]:
    return qall(
        "select id, role, name, email, phone, label, submitted_by, created_at "
        "from public.portal_contacts where proposal_id=%s "
        "order by case role when 'primary' then 0 when 'accounts_payable' then 1 else 2 end, id",
        (proposal_id,),
    )


def replace_contacts(proposal_id: str, contacts: list[dict[str, Any]], submitted_by: Optional[str] = None) -> None:
    """Atomically replace the whole contact set and flip contacts_status to
    'received'. `contacts` is a list of {role, name, email, phone, label} dicts.
    Delete + inserts + status update share one transaction (the connection
    context commits on success, rolls back on error)."""
    with pool().connection() as conn:
        conn.execute("delete from public.portal_contacts where proposal_id=%s", (proposal_id,))
        for c in contacts:
            conn.execute(
                "insert into public.portal_contacts (proposal_id, role, name, email, phone, label, submitted_by) "
                "values (%s,%s,%s,%s,%s,%s,%s)",
                (proposal_id, c.get("role") or "other", c.get("name"),
                 c.get("email"), c.get("phone"), c.get("label"), submitted_by),
            )
        conn.execute(
            "update public.portal_proposals set contacts_status='received', updated_at=now() where proposal_id=%s",
            (proposal_id,),
        )


# ── Chat thread (portal_questions is the unified message thread) ────────────────
def list_questions(proposal_id: str) -> list[dict[str, Any]]:
    """Plain text messages only — the back-compat set the current customer view
    and the staff drawer render as Q&A bubbles. Cards/system messages come from
    list_messages (msg_type-aware consumers)."""
    return qall(
        "select id, author_kind, author_email, body, created_at "
        "from public.portal_questions where proposal_id=%s and msg_type='text' order by created_at asc, id asc",
        (proposal_id,),
    )


def list_messages(proposal_id: str, after_id: int = 0) -> list[dict[str, Any]]:
    """The full chat thread (all msg_types) for the chat-first UI + polling.
    `after_id` > 0 returns only newer rows (monotonic id) for incremental polls."""
    return qall(
        "select id, author_kind, author_email, body, msg_type, meta, created_at "
        "from public.portal_questions where proposal_id=%s and id > %s order by created_at asc, id asc",
        (proposal_id, int(after_id or 0)),
    )


def add_message(proposal_id: str, author_kind: str, author_email: Optional[str], body: str,
                msg_type: str = "text", meta: Optional[dict] = None) -> dict[str, Any]:
    return q1(
        "insert into public.portal_questions (proposal_id, author_kind, author_email, body, msg_type, meta) "
        "values (%s,%s,%s,%s,%s,%s) "
        "returning id, author_kind, author_email, body, msg_type, meta, created_at",
        (proposal_id, author_kind, author_email, body, msg_type, Jsonb(meta) if meta is not None else None),
    )


def add_question(proposal_id: str, author_kind: str, author_email: Optional[str], body: str) -> dict[str, Any]:
    """Back-compat wrapper — a plain text message."""
    return add_message(proposal_id, author_kind, author_email, body, msg_type="text")


# ── Team notification recipients (configurable; falls back to env when empty) ───
def list_notify_recipients() -> list[dict[str, Any]]:
    return qall(
        "select id, email, kind, added_by, created_at from public.portal_notify_recipients "
        "order by kind, lower(email)"
    )


def add_notify_recipient(email: str, kind: str, added_by: Optional[str] = None) -> None:
    execute(
        "insert into public.portal_notify_recipients (email, kind, added_by) values (%s,%s,%s) "
        "on conflict (kind, lower(email)) do nothing",
        (email.strip().lower(), kind, added_by),
    )


def delete_notify_recipient(rid: int) -> None:
    execute("delete from public.portal_notify_recipients where id=%s", (rid,))


# ── Approvals ───────────────────────────────────────────────────────────────────
def add_approval(proposal_id, name, title, approved_date, total, option_label, ip,
                 approver_email=None, options=None) -> None:
    execute(
        "insert into public.portal_approvals "
        "(proposal_id, name, title, approved_date, total, option_label, ip, approver_email, options) "
        "values (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (proposal_id, name, title, approved_date, total, option_label, ip, approver_email,
         Jsonb(options) if options is not None else None),
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
