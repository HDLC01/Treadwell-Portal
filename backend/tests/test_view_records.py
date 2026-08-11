"""Who VIEWED it and who PAID, shown in the CRM rather than only notified about.

Hanz, 2026-08-11: "It should then highlight in the CRM who viewed it as well and who replied."

test_peer_attribution.py covers telling the other CONTACT what happened. This file is the other
half: telling the ESTIMATOR which of two contacts has actually opened the proposal, which is the
difference between chasing the right person and chasing nobody.

WHAT IS AND IS NOT REPLACED. portal_proposals.viewed_at / last_viewed_at already record that
SOMEBODY opened it, and they are untouched — the customer-facing status is deliberately one
status for the whole project, which is the thing Hanz asked to keep. portal_proposal_views
answers a different question and sits alongside.

EVERY READ IS GUARDED, and that is the point rather than defensiveness: this decorates a board
and a drawer that both have to work on a database where the migration has not been applied yet.
The reads degrade to "nothing recorded", which is also exactly what a pre-migration database
returns — so the degraded path is the one the tests exercise by default.

The one place that CANNOT degrade is the deposit INSERT, which names submitted_by. That is why
the DDL is applied before the portal deploys, and why db.list_deposits reads the column through
to_jsonb while db.add_deposit names it directly.
"""
import inspect
import pathlib
import re

import pytest

import db
import main

PID = "pid-0001"
TOKEN = "tok-abc-123"
A = "dana.reed@acme.com"
B = "ap@acme.com"
PROP = {"proposal_id": PID, "token": TOKEN, "project_name": "Westport", "customer_email": A,
        "proposal_status": "sent", "deposit_status": "pending", "contacts_status": "pending",
        "schedule_status": "pending", "deposit_amount": None, "deposit_required": True}
MAIN = pathlib.Path(__file__).resolve().parents[1] / "main.py"

# This file tests the view/recipient helpers themselves, so it needs the REAL ones rather
# than conftest's autouse stubs — those exist to keep other tests off a database that is not
# there, and here they would replace the functions under test.
pytestmark = pytest.mark.realdb


def _code():
    """main.py with comment lines stripped — this change is explained by quoting itself."""
    return "\n".join(l for l in MAIN.read_text(encoding="utf-8").splitlines()
                     if not l.strip().startswith("#"))


# ── recording a view ─────────────────────────────────────────────────────────
def test_the_view_is_recorded_against_the_SESSION():
    """The whole value is that it says who was signed in when the page was fetched. Anything
    client-supplied would make it a claim rather than a record."""
    code = _code()
    assert 'db.record_view(p["proposal_id"], se)' in code, "the view is not recorded at all"
    assert "se = _session_email(request)" in code[:code.index("db.record_view")]


def test_the_shared_status_is_left_alone():
    """mark_viewed still runs, and first. One status for the project is what Hanz asked to keep;
    this table is additional, not a replacement."""
    code = _code()
    assert 'db.mark_viewed(p["proposal_id"])' in code
    assert code.index("db.mark_viewed") < code.index("db.record_view")


def test_recording_never_breaks_the_customers_page(monkeypatch):
    """It runs on the customer's page load. A missing migration must cost the CRM a name, not
    cost the customer their proposal."""
    monkeypatch.setattr(db, "execute",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("relation missing")))
    db.record_view(PID, A)          # must not raise


@pytest.mark.parametrize("pid,email", [(PID, ""), (PID, "   "), ("", A), (PID, None)])
def test_nothing_is_written_without_both_halves(monkeypatch, pid, email):
    """A row keyed on an empty email is a phantom recipient in every count, for ever."""
    calls = []
    monkeypatch.setattr(db, "execute", lambda *a, **k: calls.append(a))
    db.record_view(pid, email)
    assert calls == []


def test_the_upsert_is_keyed_case_insensitively_and_counts_repeats():
    """A recipient list is typed by hand. Without lower(email) in the conflict target, Dana@ and
    dana@ insert two rows and the CRM reports two of two viewed when one had."""
    src = inspect.getsource(db.record_view)
    assert "on conflict (proposal_id, lower(email))" in src
    assert "last_viewed_at = now()" in src
    assert "view_count + 1" in src, "a second open does not bump the count"


def test_the_reads_return_empty_rather_than_raising(monkeypatch):
    monkeypatch.setattr(db, "qall",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("relation missing")))
    assert db.list_views(PID) == []
    assert db.views_by_proposal() == {}
    assert db.recipients_by_proposal() == {}


def test_the_board_reads_group_by_proposal(monkeypatch):
    monkeypatch.setattr(db, "qall", lambda *a, **k: [
        {"proposal_id": "p1", "email": A}, {"proposal_id": "p1", "email": B},
        {"proposal_id": "p2", "email": A}])
    assert db.views_by_proposal() == {"p1": [A, B], "p2": [A]}
    assert db.recipients_by_proposal() == {"p1": [A, B], "p2": [A]}


def test_recipients_are_ordered_the_same_way_everywhere():
    """A card and its drawer listing the same two people in different orders reads as two
    different pairs. Both order by added_at, id."""
    assert "order by added_at, id" in inspect.getsource(db.recipients_by_proposal)
    assert "order by added_at, id" in inspect.getsource(db.get_recipients)


# ── what the drawer shows ────────────────────────────────────────────────────
def test_recipient_activity_reports_each_contact_separately(monkeypatch):
    import datetime
    when = datetime.datetime(2026, 8, 11, 9, 30)
    monkeypatch.setattr(main.db, "get_recipients", lambda pid: [A, B])
    monkeypatch.setattr(main.db, "list_views", lambda pid: [
        # UPPERCASE on purpose: the match has to be case-insensitive here too, not only in SQL.
        {"email": A.upper(), "first_viewed_at": when, "last_viewed_at": when, "view_count": 3}])
    monkeypatch.setattr(main.db, "list_messages", lambda pid, after=0: [
        {"author_kind": "customer", "author_email": B, "body": "x", "msg_type": "text",
         "meta": {}, "created_at": when}])
    monkeypatch.setattr(main.db, "list_deposits", lambda pid: [{"submitted_by": B}])
    got = {r["email"]: r for r in main._recipient_activity(PID, dict(PROP),
                                                          {"approver_email": A})}
    assert got[A]["viewed_at"] and got[A]["view_count"] == 3, "the view did not match on case"
    assert (got[A]["approved"], got[A]["replied"], got[A]["paid"]) == (True, False, False)
    assert got[B]["viewed_at"] is None, "B has not opened it and must not read as viewed"
    assert (got[B]["approved"], got[B]["replied"], got[B]["paid"]) == (False, True, True)
    assert got[A]["name"] == "Dana" and got[B]["name"] == "Ap"


def test_a_staff_message_does_not_mark_a_contact_as_having_replied(monkeypatch):
    """Only customer-authored rows count. Otherwise every contact reads as having replied the
    moment Treadwell answers, and the column stops meaning anything."""
    import datetime
    monkeypatch.setattr(main.db, "get_recipients", lambda pid: [A])
    monkeypatch.setattr(main.db, "list_views", lambda pid: [])
    monkeypatch.setattr(main.db, "list_deposits", lambda pid: [])
    monkeypatch.setattr(main.db, "list_messages", lambda pid, after=0: [
        {"author_kind": "staff", "author_email": "kyle@wetreadwell.com", "body": "x",
         "msg_type": "text", "meta": {}, "created_at": datetime.datetime(2026, 8, 11)}])
    assert main._recipient_activity(PID, dict(PROP), None)[0]["replied"] is False


def test_recipient_activity_survives_every_read_failing(monkeypatch):
    """Four separate reads decorate a drawer that has to open. Each one guarded."""
    def boom(*a, **k):
        raise RuntimeError("down")
    monkeypatch.setattr(main.db, "get_recipients", lambda pid: [A, B])
    monkeypatch.setattr(main.db, "list_views", boom)
    monkeypatch.setattr(main.db, "list_messages", boom)
    monkeypatch.setattr(main.db, "list_deposits", boom)
    got = main._recipient_activity(PID, dict(PROP), None)
    assert [r["email"] for r in got] == [A, B]
    assert all(r["viewed_at"] is None and not r["replied"] and not r["paid"] for r in got)


def test_no_recipients_means_no_rows(monkeypatch):
    monkeypatch.setattr(main.db, "get_recipients", lambda pid: [])
    assert main._recipient_activity(PID, {}, None) == []


def test_an_absent_approval_marks_nobody_as_the_approver(monkeypatch):
    """`approver_email` missing must not make the first contact read as having signed."""
    monkeypatch.setattr(main.db, "get_recipients", lambda pid: [A, B])
    monkeypatch.setattr(main.db, "list_views", lambda pid: [])
    monkeypatch.setattr(main.db, "list_messages", lambda pid, after=0: [])
    monkeypatch.setattr(main.db, "list_deposits", lambda pid: [])
    for approval in (None, {}, {"approver_email": None}, {"approver_email": ""}):
        got = main._recipient_activity(PID, dict(PROP), approval)
        assert not any(r["approved"] for r in got), approval


# ── who paid ─────────────────────────────────────────────────────────────────
def test_the_deposit_records_which_contact_paid():
    """From the session, not from account_name — that is a bank account HOLDER and can be a
    company, so it cannot identify a recipient."""
    code = _code()
    i = code.index("db.add_deposit(")
    call = code[i:code.index("\n    #", i) if "\n    #" in code[i:i + 600] else i + 600]
    assert "submitted_by=_session_email(request)" in call, call[:300]


def test_the_read_survives_a_database_without_the_column_but_the_write_does_not():
    """to_jsonb on the SELECT means code can ship before the migration; the INSERT names the
    column and cannot. That asymmetry IS the deploy order, recorded where a reader will find it:
    DDL first, then the portal."""
    assert "to_jsonb(d) ->> 'submitted_by'" in inspect.getsource(db.list_deposits)
    assert "submitted_by" in inspect.getsource(db.add_deposit)


def test_the_customer_sees_a_first_name_and_never_an_address():
    """Same rule as the chat thread. The deposit banner is the other place a peer's address could
    reach a customer."""
    assert main._first_name_of(A) == "Dana"
    assert "@" not in main._first_name_of(A)
    code = _code()
    assert '"submitted_by_first_name"' in code
    assert '"submitted_by_me"' in code, (
        "without this the contact who DID pay is told somebody else paid")
    i = code.index('"submitted_by_first_name"')
    assert '"submitted_by":' not in code[i - 400:i + 400], (
        "the raw payer address is in the customer payload")


# ── cost ─────────────────────────────────────────────────────────────────────
def test_the_board_reads_recipients_and_views_ONCE_for_the_whole_page():
    """This endpoint is polled every 25 seconds. A query per card is how a 60-proposal board
    becomes 120 round-trips — the shape of an outage this system has already had once."""
    code = _code()
    i = code.index("def admin_pipeline")
    # Bounded by the next TOP-LEVEL def, not by the next @app. route: _recipient_activity is
    # defined between admin_pipeline and the following route and legitimately calls list_views
    # per proposal, so a slice to the next decorator swept it in and this failed on the wrong
    # function.
    m = re.search(r"\n(?:def |@app\.)", code[i + 1:])
    body = code[i:i + 1 + m.start()] if m else code[i:]
    assert body.count("db.views_by_proposal()") == 1
    assert body.count("db.recipients_by_proposal()") == 1
    loop = body[body.index("for r in db.list_all_portal_proposals()"):]
    for per_row in ("db.views_by_proposal", "db.recipients_by_proposal", "db.list_views("):
        assert per_row not in loop, "%s is called once per card" % per_row


def test_the_ddl_grants_and_a_policy_together():
    """RLS on with a grant but NO policy reads as locked down and is broken on PROD only: the
    portal connects there as portal_app, which does not bypass RLS, so every read returns zero
    rows while staging (broad role) looks fine. That exact pair has shipped wrong here before,
    which is why schema.sql says so in a comment and why this asserts it."""
    schema = (pathlib.Path(__file__).resolve().parents[1] / "schema.sql").read_text(encoding="utf-8")
    assert "create table if not exists public.portal_proposal_views" in schema
    assert "alter table public.portal_proposal_views enable row level security" in schema
    assert "grant select, insert, update, delete on public.portal_proposal_views to portal_app" in schema
    assert "create policy portal_app_rw on public.portal_proposal_views" in schema
    assert "portal_proposal_views_unique_idx" in schema, "no unique index to upsert against"
    assert "add column if not exists submitted_by" in schema

    prod = (pathlib.Path(__file__).resolve().parents[1] / "security_prod.sql").read_text(encoding="utf-8")
    assert "on public.portal_proposal_views to portal_app" in prod, (
        "prod applies security_prod.sql by hand as the owner; a grant only in schema.sql never "
        "runs there, and the table reads as empty")


def test_the_ddl_is_additive_only():
    """It is applied to a live database. Nothing here may drop or rewrite anything."""
    schema = (pathlib.Path(__file__).resolve().parents[1] / "schema.sql").read_text(encoding="utf-8")
    tail = schema[schema.index("create table if not exists public.portal_proposal_views"):]
    lowered = tail.lower()
    for danger in ("drop table", "drop column", "truncate", "delete from", "alter column"):
        assert danger not in lowered, "the new DDL contains %r" % danger
