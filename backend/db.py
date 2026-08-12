"""Database access for the portal.

Uses psycopg3 against the SAME Postgres the proposal tool uses (one source of
truth). Reads proposal content from `drafts`; reads/writes the `portal_*`
tables. psycopg3 parses jsonb columns into Python objects automatically.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

import config

log = logging.getLogger("portal")
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


def get_revision_data(proposal_id: str, revision_no: int) -> Optional[dict[str, Any]]:
    """One snapshot of the project state, as sent. Also owned by the proposal tool."""
    row = q1("select data from public.draft_revisions "
             "where project_id = %s and revision_no = %s", (proposal_id, revision_no))
    return (row or {}).get("data") if row else None


def get_pinned_draft_data(p: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The project state the customer was actually SENT — use this for anything
    customer-facing, not `get_draft_data`.

    The live draft is whatever an estimator last typed, which is not the same thing
    as the proposal in the customer's inbox. Rendering the live blob meant a
    mid-edit save rewrote a proposal that had already gone out, and an approval
    could be recorded against option labels that no longer existed.

    Falls back to the live blob when there is no pin: proposals published before
    revisions existed (current_revision_no NULL), and the should-never-happen case
    of a pin whose snapshot row is gone. Both keep working exactly as before."""
    no = p.get("current_revision_no")
    if no:
        data = get_revision_data(p["proposal_id"], int(no))
        if data is not None:
            return data
        log.warning("proposal %s pins revision %s but no snapshot exists — "
                    "falling back to live draft data", p.get("proposal_id"), no)
    return get_draft_data(p["proposal_id"])


# ── portal_proposals ──────────────────────────────────────────────────────────
def get_proposal_by_token(token: str) -> Optional[dict[str, Any]]:
    return q1("select * from public.portal_proposals where token = %s", (token,))


def get_proposal_by_token_ci(token: str) -> Optional[dict[str, Any]]:
    """Case-insensitive token lookup — fallback for inbound email, where some
    mail systems lowercase the address local part that carries our token."""
    return q1("select * from public.portal_proposals where lower(token) = lower(%s)", (token,))


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


def get_followup_recipients(proposal_id: str) -> list[str]:
    """Only the contacts who should get AUTOMATED follow-ups.

    Separate from get_recipients, which stays the list for everything else — the proposal itself,
    the invoice, a reply, a milestone. Turning follow-ups off for somebody must not stop them
    receiving the thing they are a contact for; it stops them being chased.

    The flag is read through to_jsonb so a database without the migration answers null, which
    `is not false` treats as opted IN. That matters more here than in most places: reading a
    missing column as "off" would silently stop chasing every live bid.
    """
    rows = qall(
        "select email, (to_jsonb(r) ->> 'followups') as followups "
        "from public.portal_proposal_recipients r "
        "where proposal_id = %s order by added_at, id",
        (proposal_id,),
    )
    return [r["email"] for r in rows if str(r.get("followups")).lower() != "false"]


def set_followup_recipient(proposal_id: str, email: str, enabled: bool) -> bool:
    """Turn automated follow-ups on or off for ONE contact. False if there is no such recipient."""
    row = q1(
        "update public.portal_proposal_recipients set followups = %s "
        "where proposal_id = %s and lower(email) = lower(%s) returning id",
        (bool(enabled), proposal_id, (email or "").strip()),
    )
    return bool(row)


def recipients_by_proposal() -> dict[str, list[str]]:
    """{proposal_id: [emails]} for EVERY proposal, in one query.

    The board reads this once per poll instead of calling get_recipients per card. Same shape and
    ordering as get_recipients (added_at, id) so a card and its drawer list the contacts in the
    same order — two orderings of the same two people reads as two different pairs.

    {} on failure: the board must render without it, and every card falls back to its own
    customer_email."""
    try:
        rows = qall(
            "select proposal_id, email from public.portal_proposal_recipients "
            "order by added_at, id", ())
    except Exception as exc:  # noqa: BLE001
        log.warning("recipients unavailable for the board: %s", exc)
        return {}
    out: dict[str, list[str]] = {}
    for r in rows:
        out.setdefault(r["proposal_id"], []).append(r["email"])
    return out


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


def set_recipients(proposal_id: str, emails: list[str], added_by: Optional[str] = None,
                  no_followups: Optional[list[str]] = None) -> None:
    """Replace the recipient set in one transaction: drop rows not in `emails`,
    insert the rest (retained rows keep their added_at). `emails` must be
    non-empty and already lowercased/deduped by the caller.

    `no_followups` is the subset the sender un-ticked on the send page. Written EXPLICITLY for
    every row, not only for the un-ticked ones: a retained row keeps its existing value on
    conflict, so leaving the ticked ones alone would make an earlier opt-out stick after somebody
    ticked the box again and re-sent. None means the caller said nothing about follow-ups (a
    legacy publish), and every row keeps whatever it already had.
    """
    emails = [e.strip().lower() for e in emails if e and e.strip()]
    if not emails:
        return
    off = {e.strip().lower() for e in (no_followups or []) if e and e.strip()}
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
            if no_followups is not None:
                conn.execute(
                    "update public.portal_proposal_recipients set followups = %s "
                    "where proposal_id = %s and lower(email) = %s",
                    (e not in off, proposal_id, e),
                )


# ── admin (publish + pipeline) ──────────────────────────────────────────────────
def create_portal_proposal(proposal_id, token, customer_email, customer_name, project_name, pdf_path,
                           published_by, deposit_required: bool = True,
                           revision_no: Optional[int] = None) -> None:
    execute(
        "insert into public.portal_proposals "
        "(proposal_id, token, customer_email, customer_name, project_name, pdf_path, published_by, "
        "deposit_required, current_revision_no) "
        "values (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (proposal_id, token, customer_email, customer_name, project_name, pdf_path, published_by,
         bool(deposit_required), None if revision_no is None else int(revision_no)),
    )


def update_portal_proposal(proposal_id, customer_email, customer_name, project_name, pdf_path,
                           deposit_required: Optional[bool] = None,
                           revision_no: Optional[int] = None) -> None:
    """Re-publish. `deposit_required=None` PRESERVES the stored value — an older
    proposal tool sends no flag, and a re-send must not silently start requiring a
    deposit on a job that was sent without one. Same for `revision_no`."""
    execute(
        "update public.portal_proposals set customer_email=%s, customer_name=%s, project_name=%s, "
        "pdf_path=coalesce(%s, pdf_path), deposit_required=coalesce(%s, deposit_required), "
        "current_revision_no=coalesce(%s, current_revision_no), "
        "updated_at=now() where proposal_id=%s",
        (customer_email, customer_name, project_name, pdf_path,
         None if deposit_required is None else bool(deposit_required),
         None if revision_no is None else int(revision_no), proposal_id),
    )


def list_all_portal_proposals() -> list[dict[str, Any]]:
    """Every proposal for the staff CRM board, with its estimator and milestones.

    The estimator comes from the DRAFT, not from us: `drafts.owner_email` is
    written once when the project is first saved and never overwritten, so it
    exists for every proposal. `published_by` (who clicked Send) is the fallback
    for rows created before that plumbing landed — it is never backfilled.

    The join deliberately does NOT filter `deleted_at`: trashing the draft does
    not retract the proposal the customer already has, and the board would
    otherwise lose the estimator on exactly those rows. Reading `drafts` is
    already granted to portal_app in prod (security_prod.sql, portal_app_read_drafts).

    `created_at` IS the sent-at: a row cannot exist before the email goes out.
    Note `viewed_at` is FIRST view only (mark_viewed coalesces it)."""
    return qall(
        "select p.proposal_id, p.token, p.customer_email, p.customer_name, p.project_name, "
        "p.proposal_status, p.deposit_status, p.contacts_status, p.schedule_status, "
        "p.approved_total, p.deposit_amount, p.created_at, p.viewed_at, p.approved_at, "
        "p.deposit_requested_at, p.deposit_required, "
        # Per-stage timestamps so each board column can sort by its OWN date.
        "p.last_viewed_at, p.deposit_submitted_at, p.deposit_received_at, "
        "p.contacts_received_at, p.scheduled_at, "
        # Whether the notification email's link was ever followed. Separate from the viewed_*
        # columns on purpose (see mark_link_clicked): it says the email reached a mailbox, not
        # that anybody read the bid, and the board renders it with that caveat.
        #
        # Read through `to_jsonb` rather than as `p.link_clicked_at` because prod cannot apply
        # its own DDL — `APPLY_SCHEMA_ON_BOOT` is false when `IS_PROD`, so an owner runs the
        # ALTERs by hand and the code can arrive first. Named directly, a missing column makes
        # psycopg raise `UndefinedColumn`; this is the ONLY pipeline query, so the staff CRM
        # board AND the Follow-ups page would both die with an error that blames portal
        # reachability, sending somebody to check nginx over one missing ALTER. A jsonb key
        # lookup returns NULL for an absent key instead, so the board simply shows no click
        # badge, and it starts working the moment the column lands — no restart, no cache.
        # Verified against the prod database: absent key -> NULL, present key -> identical value.
        "(to_jsonb(p) ->> 'link_clicked_at')::timestamptz as link_clicked_at, "
        "(to_jsonb(p) ->> 'last_link_clicked_at')::timestamptz as last_link_clicked_at, "
        # Follow-up automation state.
        "p.assigned_estimator, p.followup_enrolled_at, p.followup_disabled_at, "
        "p.followup_paused_until, p.closed_lost_reason, p.closed_at, "
        # The assigned estimator OWNS the follow-up; owner_email is only the fallback
        # for proposals published before assignment was required.
        "coalesce(p.assigned_estimator, d.owner_email, p.published_by) as estimator_email, "
        # Two "last touched" facts the digest and the board both need: when the
        # customer last did anything, and when THIS estimator last chased them.
        "(select max(q.created_at) from public.portal_questions q "
        "   where q.proposal_id = p.proposal_id) as last_message_at, "
        # Has the CUSTOMER ever come back to us, and when last.
        #
        # `last_message_at` above cannot answer this: it is the newest message from either side,
        # so a proposal where we sent the last note looks identical to one the customer answered.
        # The staff board needs to tell "they have never responded" from "they replied and we
        # are mid-conversation", and those call for opposite actions — chase versus answer.
        #
        # Counts ANY customer-authored row, not just msg_type='text'. A customer who answered
        # the status card ("still deciding, ask me in two weeks") has responded just as surely
        # as one who typed a sentence, and filing them under "never replied" would misrepresent
        # them and earn them a chasing email they had already pre-empted.
        "(select max(q.created_at) from public.portal_questions q "
        "   where q.proposal_id = p.proposal_id and q.author_kind = 'customer') "
        "   as customer_replied_at, "
        # OUTREACH only. `staff_note` carries two different things: a note an
        # estimator typed, and the system's own bookkeeping (reassigned, automation
        # on/off, paused, closed). Bookkeeping rows are the ones with an `action`
        # key, and counting them would mean reassigning a proposal — or merely
        # switching its automation on — reads as "somebody chased this", which
        # silently drops it out of the digest for two days and backdates the board's
        # last-activity to an admin click.
        "(select max(f.created_at) from public.portal_followups f "
        "   where f.proposal_id = p.proposal_id "
        "     and f.kind in ('staff_call','staff_email','staff_text','staff_note') "
        "     and f.detail->>'action' is null) "
        "  as last_staff_followup_at "
        "from public.portal_proposals p "
        "left join public.drafts d on d.id = p.proposal_id "
        "order by p.created_at desc"
    )


def unread_counts() -> dict[str, int]:
    """Per-proposal count of customer messages awaiting a staff reply — customer
    text messages newer than the last staff TEXT reply. System/card rows, though
    author_kind='staff', are msg_type!='text' so they never count as a reply.
    One aggregate query for the whole board (no N+1).

    Deliberately excludes 'deposit_submitted': nothing clears it (staff answer a
    deposit by marking it Received, not by typing a chat reply), so counting it
    would pin a badge on the card forever. It reaches staff via the bell feed
    below and via deposit_status instead."""
    rows = qall(
        "select q.proposal_id as pid, count(*) as n "
        "from public.portal_questions q "
        "where q.author_kind='customer' and q.msg_type='text' "
        "and q.id > coalesce((select max(s.id) from public.portal_questions s "
        "  where s.proposal_id=q.proposal_id and s.author_kind='staff' and s.msg_type='text'), 0) "
        "group by q.proposal_id"
    )
    return {r["pid"]: int(r["n"]) for r in rows}


def list_recent_customer_messages(limit: int = 25) -> list[dict[str, Any]]:
    """Newest customer-originated chat rows across ALL proposals, for the staff
    tool's notification bell + toast feed: real customer text (chat + inbound-email
    replies) plus deposit submissions. Staff replies and staff-authored system/card
    rows are excluded. Joined to the proposal for a display title. `id` is the
    monotonic cursor the staff side uses to dedupe toasts.

    'deposit_submitted' is here because a deposit used to reach staff through one
    email and nothing else — the board card sat in 'Approved' looking identical to
    a customer who had paid nothing. msg_type ships to the client so the staff side
    can style it differently from a question."""
    return qall(
        "select q.id, q.proposal_id, q.author_email, q.body, q.msg_type, q.created_at, "
        "p.project_name, p.customer_name "
        "from public.portal_questions q "
        "join public.portal_proposals p on p.proposal_id = q.proposal_id "
        "where q.author_kind='customer' and q.msg_type in ('text','deposit_submitted') "
        "order by q.id desc limit %s",
        (int(limit),),
    )


def list_customer_events(email: str, limit: int = 30) -> list[dict[str, Any]]:
    """Everything worth telling a customer about, across EVERY proposal they can
    reach — staff replies, deposit invoices, and the system lines that record
    approvals, deposits received and scheduling.

    Scoped by the same primary-OR-recipient union as list_proposals_by_email, so
    one customer can never see another's activity."""
    return qall(
        "select q.id, q.proposal_id, q.msg_type, q.body, q.created_at, "
        "       p.project_name, p.token "
        "from public.portal_questions q "
        "join public.portal_proposals p on p.proposal_id = q.proposal_id "
        "where q.author_kind = 'staff' "
        "  and q.msg_type in ('text','deposit_request','system') "
        "  and q.proposal_id in ("
        "    select proposal_id from public.portal_proposals where lower(customer_email) = lower(%s)"
        "    union"
        "    select proposal_id from public.portal_proposal_recipients where lower(email) = lower(%s)"
        "  ) "
        "order by q.id desc limit %s",
        (email, email, int(limit)),
    )


def get_read_state(email: str) -> dict[str, Any]:
    """proposal_id -> last_seen_at for this reader."""
    rows = qall(
        "select proposal_id, last_seen_at from public.portal_read_state where lower(email) = lower(%s)",
        (email,),
    )
    return {r["proposal_id"]: r["last_seen_at"] for r in rows}


def mark_read(email: str, proposal_ids: list[str]) -> None:
    """Stamp 'seen now' for this reader on the given proposals. Per (reader,
    proposal) so two recipients on one proposal keep separate unread counts."""
    for pid in proposal_ids:
        execute(
            "insert into public.portal_read_state (email, proposal_id, last_seen_at, updated_at) "
            "values (%s, %s, now(), now()) "
            "on conflict (lower(email), proposal_id) "
            "do update set last_seen_at = now(), updated_at = now()",
            (email.strip().lower(), pid),
        )


def record_view(proposal_id: str, email: str) -> None:
    """Note that THIS recipient opened the proposal. Best-effort, never raises.

    Hanz, 2026-08-11: "It should then highlight in the CRM who viewed it as well and who
    replied." portal_proposals.viewed_at already says somebody opened it and stays exactly as
    it was — the customer-facing status is shared by design. This answers the other question:
    WHICH of two contacts has looked, which is the difference between chasing the right person
    and chasing nobody.

    `email` comes from the session, never from the client. Conflict target is lower(email)
    because a recipient list is typed by hand and Dana@ and dana@ are one person; without it the
    upsert inserts a second row and the CRM reports two of two viewed when one had.

    Swallows everything, including the table not existing: this runs on the customer's page load
    and a missing migration must cost the CRM a name, not cost the customer their proposal.
    """
    if not proposal_id or not (email or "").strip():
        return
    try:
        execute(
            "insert into public.portal_proposal_views (proposal_id, email) values (%s,%s) "
            "on conflict (proposal_id, lower(email)) do update set "
            "last_viewed_at = now(), view_count = public.portal_proposal_views.view_count + 1",
            (proposal_id, email.strip()),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("could not record the view of %s by %s: %s", proposal_id, email, exc)


def list_views(proposal_id: str) -> list[dict[str, Any]]:
    """Per-recipient view rows for one proposal. [] when the table is missing."""
    try:
        return qall(
            "select email, first_viewed_at, last_viewed_at, view_count "
            "from public.portal_proposal_views where proposal_id=%s order by first_viewed_at asc",
            (proposal_id,),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("view records unavailable for %s: %s", proposal_id, exc)
        return []


def views_by_proposal() -> dict[str, list[str]]:
    """{proposal_id: [emails that have viewed]} for the WHOLE board, in one query.

    One read rather than one per card: the Active Projects board polls every 25 seconds and
    already carries a per-row cost for the test flag. {} when the table is missing, which makes
    the board's "N viewed" line simply not render."""
    try:
        rows = qall("select proposal_id, email from public.portal_proposal_views", ())
    except Exception as exc:  # noqa: BLE001
        log.warning("view records unavailable for the board: %s", exc)
        return {}
    out: dict[str, list[str]] = {}
    for r in rows:
        out.setdefault(r["proposal_id"], []).append(r["email"])
    return out


def list_deposits(proposal_id: str) -> list[dict[str, Any]]:
    return qall(
        "select method, account_name, bank_name, masked_ref, note, sent_date, trace_ref, "
        "sent_to_beneficiary, sent_to_bank, sent_to_routing, sent_to_account, check_number, "
        "routing_number, account_number, account_type, submitted_at, "
        # to_jsonb rather than a bare column so a database that has not had the ALTER yet
        # returns null instead of erroring — the house trick, same as the columns above it
        # were introduced with. Code can therefore ship before the migration.
        "(to_jsonb(d) ->> 'submitted_by') as submitted_by "
        "from public.portal_deposits d where proposal_id=%s order by submitted_at desc",
        (proposal_id,),
    )


class SettingsUnreadable(Exception):
    """The settings row could not be read, and we do not know what it says.

    Distinct from "there is no row yet", which `get_settings` reports as None. The difference
    matters because the editor OVERWRITES the whole row: told "nothing saved", it shows the
    shipped defaults and the next Save replaces somebody's hand-written customer emails with
    them. A read that failed must therefore not be reported as an empty one.
    """


def _is_missing_table(exc: BaseException) -> bool:
    """Is this "the table has not been created here yet" rather than a real failure?

    Prod cannot apply its own DDL (`APPLY_SCHEMA_ON_BOOT` is false when `IS_PROD`), so code
    routinely arrives before its table. That state is normal and means "as shipped". Matched on
    the class where psycopg gives us one, with a string fallback because the same condition can
    surface wrapped by the pool.
    """
    import psycopg

    for e in (exc, getattr(exc, "__cause__", None), getattr(exc, "__context__", None)):
        if e is None:
            continue
        if isinstance(e, psycopg.errors.UndefinedTable):
            return True
        if getattr(e, "sqlstate", None) == "42P01":     # undefined_table
            return True
    text = str(exc).lower()
    return "does not exist" in text and "relation" in text


def get_settings(key: str) -> Optional[dict[str, Any]]:
    """One settings row's value, or None when it has never been saved.

    None is a normal answer, not an error: `followup_settings.merge()` lays stored values over the
    shipped defaults, so an absent row means "the cadence as shipped". That is what lets the code
    deploy before the DDL is applied — the worker keeps sending exactly as it did before.

    A table that does not exist yet is also None, for the same reason. Anything else raises
    `SettingsUnreadable`, because "the read failed" and "nothing is saved" must not look alike to
    a caller that will offer to overwrite the row.
    """
    try:
        row = q1("select value from public.portal_settings where id=%s", (key,))
    except Exception as exc:  # noqa: BLE001
        if _is_missing_table(exc):
            return None
        raise SettingsUnreadable(str(exc)) from exc
    if not row:
        return None
    val = row.get("value")
    return val if isinstance(val, dict) else None


def save_settings(key: str, value: dict[str, Any], by: Optional[str] = None) -> dict[str, Any]:
    """Upsert one settings row, and hand back the audit values it just wrote.

    `updated_by` is recorded because these settings send email to CUSTOMERS: when somebody asks
    why the wording changed, "who and when" has to be answerable without reading a git log that
    contains nothing about it.

    `returning` rather than a second `settings_meta()` read: the editor shows that line straight
    after saving, and a follow-up select would be one more round trip that can fail or hang on a
    path that has already succeeded. The timestamp also has to be the one `now()` actually stored,
    not a second reading taken a moment later.
    """
    row = q1(
        "insert into public.portal_settings (id, value, updated_at, updated_by) "
        "values (%s, %s, now(), %s) "
        "on conflict (id) do update set value = excluded.value, "
        "  updated_at = now(), updated_by = excluded.updated_by "
        "returning updated_at, updated_by",
        (key, Jsonb(value), by),
    )
    return row or {}


def settings_meta(key: str) -> dict[str, Any]:
    """Who last changed this, and when — for the editor to show above the form."""
    row = q1("select updated_at, updated_by from public.portal_settings where id=%s", (key,))
    return row or {}


def latest_approval(proposal_id: str) -> Optional[dict[str, Any]]:
    return q1(
        "select name, title, approved_date, total, option_label, options, signed_at, approver_email "
        "from public.portal_approvals where proposal_id=%s order by signed_at desc limit 1",
        (proposal_id,),
    )


def mark_link_clicked(proposal_id: str) -> None:
    """Somebody followed the link in the notification email.

    A SOFT signal, and kept away from everything that means "the customer read it". It does not
    touch proposal_status, viewed_at or cycle_viewed_at: the landing page serves before any
    login, and Outlook SafeLinks and mail scanners follow links on their own. Moving the status
    on a click would also move cycle_viewed_at, which anchors the follow-up cadence — a scanner
    would quietly change which reminders a customer gets.

    What it is good for is the opposite question. When a proposal has been sitting in Sent for a
    week, "the email was delivered and the link was followed" and "we may have the wrong address"
    are very different problems, and until now the board could not tell them apart."""
    execute(
        "update public.portal_proposals set "
        "link_clicked_at = coalesce(link_clicked_at, now()), "
        "last_link_clicked_at = now() where proposal_id = %s",
        (proposal_id,),
    )


def mark_viewed(proposal_id: str) -> None:
    """Three different "viewed" facts, which is why this writes three columns.

    `viewed_at` is the FIRST view ever (coalesced — the board dates a card by it).
    `last_viewed_at` is every view, so the Viewed column can sort by most recent.
    `cycle_viewed_at` is the first view of the CURRENT send: a revision re-publish
    nulls it, so the follow-up cadence restarts instead of inheriting a clock from
    a version the customer saw weeks ago."""
    execute(
        "update public.portal_proposals set viewed_at = coalesce(viewed_at, now()), "
        "last_viewed_at = now(), "
        "cycle_viewed_at = case when proposal_status = 'sent' "
        "                       then coalesce(cycle_viewed_at, now()) else cycle_viewed_at end, "
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
    # Stamp the received-at the first time it lands there, so the board's "Deposit
    # received" column can sort by deposit date rather than by last-touched.
    execute(
        "update public.portal_proposals set deposit_status=%s, "
        "deposit_received_at = case when %s = 'received' "
        "                           then coalesce(deposit_received_at, now()) "
        "                           else deposit_received_at end, "
        "updated_at=now() where proposal_id=%s",
        (status, status, proposal_id),
    )


def mark_deposit_submitted(proposal_id: str) -> None:
    """Customer-side flip to 'submitted' — they've sent us their payment details.

    Guarded in SQL rather than read-then-write so a customer who resubmits (or a
    concurrent request) can never downgrade a deposit staff already verified as
    'received'. Staff still move the status freely via set_deposit_status."""
    execute(
        "update public.portal_proposals set deposit_status='submitted', "
        "deposit_submitted_at = coalesce(deposit_submitted_at, now()), updated_at=now() "
        "where proposal_id=%s and deposit_status <> 'received'",
        (proposal_id,),
    )


def set_deposit_requested(proposal_id: str) -> None:
    execute(
        "update public.portal_proposals set deposit_requested_at=now(), updated_at=now() where proposal_id=%s",
        (proposal_id,),
    )


def set_deposit_amount(proposal_id: str, amount) -> None:
    """Persist a staff-adjusted deposit amount. Must be written before the invoice
    is rendered: the customer-facing /deposit-invoice.pdf rebuilds the document
    from this column, so leaving it stale would make the downloaded invoice
    disagree with the one that was emailed."""
    execute(
        "update public.portal_proposals set deposit_amount=%s, updated_at=now() where proposal_id=%s",
        (amount, proposal_id),
    )


def peek_next_invoice_no() -> Optional[str]:
    """What the NEXT invoice number will be, without consuming it.

    The staff review form prefills with this so nobody has to guess, and reading
    last_value (rather than calling nextval) means opening the dialog can't burn
    numbers or create gaps."""
    row = q1("select last_value, is_called from public.portal_invoice_seq")
    if not row:
        return None
    nxt = int(row["last_value"]) + (1 if row.get("is_called") else 0)
    return f"TW-INV-{nxt:05d}"


def issue_new_invoice_no(proposal_id: str) -> Optional[str]:
    """Mint a FRESH invoice number, always. Used by the staff resend: per Hanz,
    each resend is a genuinely new invoice that supersedes the last, unlike the
    auto-on-approval path which reuses the number via assign_invoice_no."""
    execute(
        "update public.portal_proposals "
        "set deposit_invoice_no = 'TW-INV-' || to_char(nextval('public.portal_invoice_seq'), 'FM00000'), "
        "    deposit_invoice_issued_at = now(), updated_at = now() "
        "where proposal_id = %s",
        (proposal_id,),
    )
    row = q1("select deposit_invoice_no from public.portal_proposals where proposal_id=%s", (proposal_id,))
    return (row or {}).get("deposit_invoice_no")


def reset_for_revision(proposal_id: str, revision_no: int) -> bool:
    """Point the proposal at a newly sent revision and reopen it for approval.

    A revised estimate means the customer has not agreed to THIS one yet, so a
    'viewed' or 'approved' proposal goes back to 'sent' and the denormalised
    approved_* columns are cleared — otherwise the board would show an approval of
    a price nobody was shown. The portal_approvals rows are deliberately left
    alone: they are the audit record of what was agreed and when.

    Deposit columns are untouched. Money that has already been invoiced or paid is
    a fact about the project, not about which revision is current.

    Returns True when a prior approval was cleared, so the caller can say so in the
    thread."""
    row = q1("select proposal_status from public.portal_proposals where proposal_id = %s",
             (proposal_id,))
    was_approved = (row or {}).get("proposal_status") == "approved"
    execute(
        "update public.portal_proposals set current_revision_no = %s, "
        "proposal_status = case when proposal_status in ('viewed','approved') "
        "                       then 'sent' else proposal_status end, "
        "approved_total = case when %s then null else approved_total end, "
        "approved_option = case when %s then null else approved_option end, "
        "approved_options = case when %s then null else approved_options end, "
        "approved_name = case when %s then null else approved_name end, "
        "approved_title = case when %s then null else approved_title end, "
        "approved_date = case when %s then null else approved_date end, "
        "approved_at = case when %s then null else approved_at end, "
        "updated_at = now() where proposal_id = %s",
        (revision_no, was_approved, was_approved, was_approved, was_approved,
         was_approved, was_approved, was_approved, proposal_id),
    )
    return was_approved


def supersede_proposal_cards(proposal_id: str, replaced_by_rev: int) -> None:
    """Mark earlier proposal cards superseded so the customer can tell at a glance
    which version is current. Same shape as supersede_invoice_cards — the frontends
    already know how to render a superseded card."""
    execute(
        "update public.portal_questions "
        "set meta = coalesce(meta, '{}'::jsonb) || jsonb_build_object('superseded', true, "
        "                                                            'superseded_by', %s::int) "
        "where proposal_id = %s and msg_type = 'proposal_card' "
        "  and coalesce((meta->>'superseded')::boolean, false) = false",
        (int(replaced_by_rev), proposal_id),
    )


# ── Follow-up automation ──────────────────────────────────────────────────────
# A sent proposal is chased on a cadence until it is approved, the customer says
# they are delayed or out, or an estimator takes it off automation. See
# followup_rules.py for the schedule and followup_worker.py for the tick.

def set_assigned_estimator(proposal_id: str, email: Optional[str]) -> None:
    execute("update public.portal_proposals set assigned_estimator=%s, updated_at=now() "
            "where proposal_id=%s", ((email or None), proposal_id))


def enroll_followup(proposal_id: str) -> None:
    """Start (or restart) the cadence for a freshly sent proposal.

    Deliberately does NOT clear `followup_disabled_at`: an estimator taking a
    proposal off automation is a human decision, and sending a revision should not
    quietly undo it. The drawer toggle is how you turn it back on."""
    execute("update public.portal_proposals set followup_enrolled_at=now(), "
            "cycle_viewed_at=null, followup_paused_until=null, updated_at=now() "
            "where proposal_id=%s", (proposal_id,))


def set_followup_enabled(proposal_id: str, enabled: bool) -> None:
    """Estimator toggle. Enabling a proposal that was never enrolled (a legacy row
    published before automation existed) anchors the cadence at opt-in time rather
    than at its original send, which is weeks stale."""
    if enabled:
        execute("update public.portal_proposals set followup_disabled_at=null, "
                "followup_enrolled_at=coalesce(followup_enrolled_at, now()), updated_at=now() "
                "where proposal_id=%s", (proposal_id,))
    else:
        execute("update public.portal_proposals set followup_disabled_at=now(), updated_at=now() "
                "where proposal_id=%s", (proposal_id,))


def pause_followups(proposal_id: str, until) -> None:
    execute("update public.portal_proposals set followup_paused_until=%s, updated_at=now() "
            "where proposal_id=%s", (until, proposal_id))


def resume_followups(proposal_id: str) -> None:
    execute("update public.portal_proposals set followup_paused_until=null, updated_at=now() "
            "where proposal_id=%s", (proposal_id,))


def close_lost(proposal_id: str, reason: Optional[str]) -> bool:
    """Mark the opportunity lost. Works from any stage now, approved included.

    This used to carry `and proposal_status <> 'approved'`, on the grounds that a signed
    proposal is a win and a stray "not moving forward" click must not erase it. Hanz reopened
    that on 2026-08-10: "allow for the projects to be lost even its been approved". A customer
    can sign and the job still die, financing falls through, the GC loses the bid, they go with
    somebody else. Refusing left those rows parked in Approved on the board for good, with no
    way out short of hand-written SQL.

    The original worry is answered by KEEPING the approval rather than by blocking the move.
    Nothing here touches approved_at, approved_total, approved_option/approved_options,
    approved_name, approved_title, approved_date or the portal_approvals audit rows, so what
    was agreed and when is still on the row, and reopen_if_closed reads approved_at back and
    returns it to 'approved'. A mis-click costs a second click, not a lost win.

    The customer's own view is NOT the same claim: app.js gates its approved banner, its
    "Approved" badge and its thank-you card on status === 'approved', so while the job sits
    closed they see the closed receipt from the status card instead of their approval. The
    columns are what survives, not that rendering.

    Note the customer-side ways out both stayed shut: /project-status refuses outright once
    proposal_status is 'approved', and /approve treats a surviving approved_at as already
    approved so this closed row cannot be signed a second time back into play. Closing a
    signed job is a staff judgement, not something to offer the customer in their own portal.

    Returns whether the row actually moved. False now means only that no such proposal_id
    exists."""
    row = q1("update public.portal_proposals set proposal_status='closed_lost', "
             "closed_lost_reason=%s, closed_at=now(), updated_at=now() "
             "where proposal_id=%s "
             "returning proposal_id", ((reason or None), proposal_id))
    return bool(row)


def reopen_if_closed(proposal_id: str) -> bool:
    """A closed-lost proposal goes back in play, at the stage it was at rather than at 'sent'.

    Two callers: a new version being published to a lost opportunity, and staff moving the
    card back to Active from the drawer.

    This hardcoded proposal_status='sent', which was harmless while an approved proposal could
    not be closed at all. The moment close_lost started accepting approved rows (Hanz,
    2026-08-10) it turned into a silent demotion: reactivating a job the customer had signed
    would have dropped it back into Sent, re-enrolled it in the chasing cadence and thrown
    away the fact it was ever won.

    approved_at is what the restore reads because set_approved always stamps it and
    reset_for_revision nulls it whenever it clears an approval, so an approval a later
    revision superseded stays dead instead of resurrecting here.

    A previously-'viewed' row is deliberately still restored as 'sent'. Reopening is a fresh
    chase, and cycle_viewed_at is what followup_rules reads to choose between the not-opened
    and opened reminder tracks, so claiming the current send has been read would pick the
    wrong track.

    The publish path must keep calling this BEFORE reset_for_revision (see admin_publish):
    reset_for_revision decides whether to clear the approved_* columns by reading
    proposal_status, so it needs the restored 'approved' to notice there is an approval to
    retire and to tell the customer their earlier agreement no longer stands."""
    row = q1("update public.portal_proposals set "
             "proposal_status = case when approved_at is not null then 'approved' else 'sent' end, "
             "closed_lost_reason=null, closed_at=null, updated_at=now() "
             "where proposal_id=%s and proposal_status='closed_lost' "
             "returning proposal_id", (proposal_id,))
    return bool(row)


def add_followup(proposal_id: str, kind: str, detail: Optional[dict] = None,
                 created_by: Optional[str] = None) -> dict[str, Any]:
    return q1("insert into public.portal_followups (proposal_id, kind, detail, created_by) "
              "values (%s,%s,%s,%s) returning id, kind, detail, created_by, created_at",
              (proposal_id, kind, Jsonb(detail or {}), created_by))


def list_followups(proposal_id: str, limit: int = 20) -> list[dict[str, Any]]:
    return qall("select id, kind, detail, created_by, created_at from public.portal_followups "
                "where proposal_id=%s order by created_at desc limit %s",
                (proposal_id, limit))


def reserve_followup(proposal_id: str, rule_key: str, detail: dict) -> Optional[int]:
    """Claim the right to send one automated email, or return None if it is already
    claimed.

    This is the whole dedupe: the partial unique index on (proposal_id, rule_key)
    means a crashed tick, a container restart, or two overlapping containers during
    a deploy cannot double-nag a customer. Reserve first, send second — a lost
    reservation costs one missed nudge (the next cadence step covers it), whereas
    sending first would risk sending twice, which is not recoverable."""
    payload = dict(detail or {})
    payload["rule_key"] = rule_key
    row = q1("insert into public.portal_followups (proposal_id, kind, detail) "
             "values (%s,'auto_email',%s) on conflict do nothing returning id",
             (proposal_id, Jsonb(payload)))
    return int(row["id"]) if row else None


def delete_followup(followup_id: int) -> None:
    """Release a reservation whose send failed outright, so the next tick retries."""
    execute("delete from public.portal_followups where id=%s", (followup_id,))


def list_followup_candidates() -> list[dict[str, Any]]:
    """Proposals the cadence should consider this tick.

    Paused rows are deliberately INCLUDED: the rule engine needs them to notice a
    pause that has expired and remind the estimator. Closed-lost is excluded — there
    is nothing left to chase.

    APPROVED is included while its deposit is still outstanding. Hanz, 2026-08-12:
    "followups should be automated until a deposit has been received." It used to be
    excluded here, which is why widening the rule engine alone would have changed
    nothing — an approved proposal never reached it.

    This clause is deliberately coarser than the rules are. `received` is the last
    stop either audience has, so anything short of it is a candidate; whether THIS
    tick owes the customer or only the estimator is followup_rules' decision, made
    against the fresh row. A `deposit_required` of NULL predates the optional-deposit
    column and means the default, which was required — and a FALSE with an invoice
    raised against it anyway (`deposit_requested_at`) is staff deciding after the fact
    that money is due, which is money worth chasing."""
    return qall(
        "select * from public.portal_proposals "
        "where followup_enrolled_at is not null and followup_disabled_at is null "
        "  and (proposal_status in ('sent','viewed') "
        "       or (proposal_status = 'approved' "
        "           and (coalesce(deposit_required, true) "
        "                or deposit_requested_at is not null) "
        "           and coalesce(deposit_status, '') <> 'received')) "
        "order by followup_enrolled_at"
    )


def supersede_invoice_cards(proposal_id: str, replaced_by: str) -> None:
    """Mark earlier deposit_request cards superseded so the customer can tell
    which invoice is current. Only the latest number is stored, so an old card's
    document can't be re-rendered — the frontends drop its download link and
    label it instead."""
    execute(
        "update public.portal_questions "
        "set meta = coalesce(meta, '{}'::jsonb) || jsonb_build_object('superseded', true, "
        "                                                            'superseded_by', %s::text) "
        "where proposal_id = %s and msg_type = 'deposit_request' "
        "  and coalesce((meta->>'superseded')::boolean, false) = false",
        (replaced_by, proposal_id),
    )


def set_invoice_no(proposal_id: str, invoice_no: str) -> None:
    """Persist a staff-edited invoice number. The customer quotes this back, and
    the portal download rebuilds the document from it, so an edit that only lived
    in the emailed PDF would leave the two disagreeing."""
    execute(
        "update public.portal_proposals set deposit_invoice_no=%s, updated_at=now() "
        "where proposal_id=%s",
        (invoice_no, proposal_id),
    )


def assign_invoice_no(proposal_id: str) -> Optional[str]:
    """Issue the deposit invoice number, ONCE. Returns the number (existing or
    newly minted). Idempotent by construction: the `where deposit_invoice_no is
    null` clause means a concurrent/repeat call updates nothing, and the second
    statement reads back whatever is stored — so a re-send or a re-approval can
    never show the customer a second number for the same deposit.

    nextval() is only evaluated for the row being updated, so repeat calls don't
    burn sequence values."""
    execute(
        "update public.portal_proposals "
        "set deposit_invoice_no = 'TW-INV-' || to_char(nextval('public.portal_invoice_seq'), 'FM00000'), "
        "    deposit_invoice_issued_at = now(), updated_at = now() "
        "where proposal_id = %s and deposit_invoice_no is null",
        (proposal_id,),
    )
    row = q1("select deposit_invoice_no from public.portal_proposals where proposal_id=%s", (proposal_id,))
    return (row or {}).get("deposit_invoice_no")


def set_schedule_status(proposal_id: str, status: str) -> None:
    execute(
        "update public.portal_proposals set schedule_status=%s, "
        "scheduled_at = case when %s = 'scheduled' "
        "                    then coalesce(scheduled_at, now()) else scheduled_at end, "
        "updated_at=now() where proposal_id=%s",
        (status, status, proposal_id),
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
            "update public.portal_proposals set contacts_status='received', "
            "contacts_received_at = coalesce(contacts_received_at, now()), "
            "updated_at=now() where proposal_id=%s",
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


def add_feedback(email: str, category: str, body: str,
                 proposal_id: Optional[str] = None) -> dict[str, Any]:
    """Record what a customer told us about the PORTAL — a question about how it works, a
    request, or something broken.

    Kept out of `portal_questions` deliberately: this is about the software, not about a job.
    In the thread it would reach the estimator as if it were a question about their proposal
    and would disappear with the project."""
    return q1(
        "insert into public.portal_feedback (proposal_id, email, category, body) "
        "values (%s,%s,%s,%s) returning id, created_at",
        (proposal_id or None, email, category, body),
    )


def has_email_message(proposal_id: str, email_id: str) -> bool:
    """Dedup for inbound email: has this Resend email_id already been inserted?
    (email_id is stable across Svix retries AND dashboard re-sends; scoping by
    proposal_id keeps the scan on the proposal index.)"""
    return q1(
        "select 1 as x from public.portal_questions "
        "where proposal_id=%s and meta->>'email_id' = %s limit 1",
        (proposal_id, email_id),
    ) is not None


# ── Team notification recipients (configurable roster; falls back to env when empty) ─
def list_notify_recipients() -> list[dict[str, Any]]:
    return qall(
        "select id, email, kind, enabled, added_by, created_at from public.portal_notify_recipients "
        "order by kind, lower(email)"
    )


def add_notify_recipient(email: str, kind: str, added_by: Optional[str] = None,
                         enabled: bool = True) -> None:
    # on-conflict DO NOTHING: a duplicate add must never flip an existing person's
    # green/gray state (you change that with the toggle, not by re-adding).
    execute(
        "insert into public.portal_notify_recipients (email, kind, enabled, added_by) "
        "values (%s,%s,%s,%s) "
        "on conflict (kind, lower(email)) do nothing",
        (email.strip().lower(), kind, enabled, added_by),
    )


def set_notify_recipient_enabled(rid: int, enabled: bool) -> None:
    execute("update public.portal_notify_recipients set enabled=%s where id=%s", (enabled, rid))


def delete_notify_recipient(rid: int) -> None:
    execute("delete from public.portal_notify_recipients where id=%s", (rid,))


# ── Per-project notify overrides ('add' extra person / 'mute' someone for ONE project) ─
def list_notify_overrides(proposal_id: str) -> list[dict[str, Any]]:
    return qall(
        "select email, mode from public.portal_notify_overrides where proposal_id=%s "
        "order by lower(email)",
        (proposal_id,),
    )


def list_all_notify_overrides() -> list[dict[str, Any]]:
    """Every per-project override, for the Notification Sending page's per-project
    view (one fetch instead of one-per-project)."""
    return qall(
        "select proposal_id, email, mode from public.portal_notify_overrides "
        "order by proposal_id, lower(email)"
    )


def set_notify_override(proposal_id: str, email: str, mode: str) -> None:
    execute(
        "insert into public.portal_notify_overrides (proposal_id, email, mode) values (%s,%s,%s) "
        "on conflict (proposal_id, lower(email)) do update set mode = excluded.mode",
        (proposal_id, email.strip().lower(), mode),
    )


def clear_notify_override(proposal_id: str, email: str) -> None:
    execute(
        "delete from public.portal_notify_overrides where proposal_id=%s and lower(email)=lower(%s)",
        (proposal_id, email),
    )


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
def add_deposit(proposal_id, method, account_name, bank_name, masked_ref, note,
                sent_date=None, trace_ref=None,
                sent_to_beneficiary=None, sent_to_bank=None,
                sent_to_routing=None, sent_to_account=None,
                check_number=None, routing_number=None, account_number=None,
                account_type=None, submitted_by=None) -> None:
    """`submitted_by` is which CONTACT paid. Named in the INSERT rather than read through
    to_jsonb like the SELECT does, so this is the one place the migration has to be applied
    first — a write cannot fall back to null the way a read can."""
    execute(
        "insert into public.portal_deposits "
        "(proposal_id, method, account_name, bank_name, masked_ref, note, sent_date, trace_ref, "
        "sent_to_beneficiary, sent_to_bank, sent_to_routing, sent_to_account, check_number, "
        "routing_number, account_number, account_type, submitted_by) "
        "values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (proposal_id, method, account_name, bank_name, masked_ref, note, sent_date, trace_ref,
         sent_to_beneficiary, sent_to_bank, sent_to_routing, sent_to_account, check_number,
         routing_number, account_number, account_type, submitted_by),
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
