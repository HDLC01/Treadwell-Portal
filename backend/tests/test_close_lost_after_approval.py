"""Losing a job the customer had already approved.

Hanz, 2026-08-10: *"In The Customer CRM allow for the projects to be lost even its been
approved and if its lost remove it from the Customer CRM. To remove clutter"*.

`close_lost` used to carry `and proposal_status <> 'approved'` and the admin endpoint turned
the resulting False into `already_approved`, 400. The reasoning was sound as far as it went: a
signed proposal is a win, and a stray click on an old email must not erase it. What it missed
is that a signed job can still die. Financing falls through, the GC loses the bid it was
bidding us into, the customer goes with somebody else. Those rows sat in Approved on the board
for good, and the only way out was hand-written SQL against prod.

So the guard moved from "refuse the move" to "keep the approval". Three halves, and this file
tests all of them, because any one alone is worse than what we had:

  * `close_lost` writes only the closed_* columns. Every approved_* column and every
    portal_approvals row survives, so the name, title, date and total that were agreed are
    still on the row for the board, the drawer and a reopen to read.
  * `reopen_if_closed` hardcoded `proposal_status='sent'`. Harmless while an approved row could
    never be closed; a silent demotion the moment it can. Reactivating a won job would have
    parked it back in Sent and put it back on the chasing cadence, with nothing left on the row
    to say it had ever been won. It now restores 'approved' when approved_at survived.
  * `/approve` keyed its idempotence on `proposal_status == 'approved'` alone, which stopped
    being the same question as "has this been signed" the moment an approval could outlive the
    status. A signed job staff filed as lost showed the customer "Awaiting your approval" and a
    live Approve button again (app.js gates the banner, the badge and the thank-you card on the
    status), and one click re-ran the whole approval: a second portal_approvals row, both
    approval emails again, and the status quietly back to 'approved' with staff never told the
    job they closed had un-closed itself. It now also refuses on a surviving approved_at.

`approved_at` is the flag rather than a new pre_close_status column on purpose: prod DDL on
this database needs the Supabase owner, and reset_for_revision already nulls approved_at
whenever it clears an approval, so a superseded approval stays dead instead of resurrecting.

The customer-side half stays shut. /project-status still refuses outright once the status is
'approved': closing a signed job is a staff judgement, not a button to hand the customer.
"""
import inspect
import pathlib

import pytest
from fastapi.testclient import TestClient

import db
import main


def _sql_of(monkeypatch, fn, *args):
    """The SQL a db helper actually sends, whitespace-flattened. Same shape as
    test_link_click_signal's helper, except these two helpers write via q1 (they need the
    RETURNING row to report whether anything moved), so q1 is what gets stubbed."""
    seen = {}
    monkeypatch.setattr(db, "q1",
                        lambda sql, params=(): seen.update(sql=sql, params=params)
                        or {"proposal_id": "p1"})
    fn(*args)
    return " ".join(seen["sql"].split()), seen["params"]


# ── closing ───────────────────────────────────────────────────────────────────
def test_an_approved_proposal_can_now_be_closed_lost(monkeypatch):
    """THE change. Putting the status guard back is the mutation this kills."""
    sql, _ = _sql_of(monkeypatch, db.close_lost, "p1", "price")
    assert "proposal_status <> 'approved'" not in sql, (
        "close_lost still refuses an approved proposal, which is what Hanz asked us to stop")
    assert "'approved'" not in sql.split(" where ", 1)[1], (
        "the where clause still filters on the approval stage in some other spelling")


@pytest.mark.parametrize("column", [
    "approved_at",
    "approved_total",
    "approved_option",     # covers approved_options too
    "approved_name",
    "approved_title",
    "approved_date",
])
def test_closing_lost_leaves_every_approval_column_alone(monkeypatch, column):
    """The other half of relaxing the guard. If closing lost ever starts clearing these, the
    stray click the old guard was written to stop genuinely does erase a win, and
    reopen_if_closed has nothing left to read to put the row back."""
    sql, _ = _sql_of(monkeypatch, db.close_lost, "p1", "price")
    assert column not in sql, "close_lost writes %s, so the approval is not recoverable" % column


def test_closing_lost_still_records_the_reason_and_when(monkeypatch):
    sql, params = _sql_of(monkeypatch, db.close_lost, "p1", "another_contractor")
    assert "proposal_status='closed_lost'" in sql
    assert "closed_lost_reason=%s" in sql and "closed_at=now()" in sql
    assert params == ("another_contractor", "p1")


def test_no_reason_given_stores_null_rather_than_an_empty_string(monkeypatch):
    """The reason drives the CRM's lost-reason label, and "" would render as a blank chip
    instead of no chip."""
    _, params = _sql_of(monkeypatch, db.close_lost, "p1", "")
    assert params[0] is None


# ── putting it back in play ───────────────────────────────────────────────────
def test_reopening_restores_the_approval_instead_of_demoting_it(monkeypatch):
    """The bug the rest of this file exists to prevent: reactivating a job that was approved
    and then lost must not silently file it back under Sent as an unsigned proposal."""
    sql, params = _sql_of(monkeypatch, db.reopen_if_closed, "p1")
    assert "proposal_status='sent'" not in sql, "reopen still hardcodes 'sent'"
    assert "approved_at is not null" in sql, (
        "reopen does not consult the surviving approval, so a won job comes back unsigned")
    assert "then 'approved'" in sql
    assert params == ("p1",)


def test_reopening_a_proposal_that_was_never_approved_still_lands_in_sent(monkeypatch):
    """Restoring 'viewed' is deliberately NOT attempted: cycle_viewed_at drives which reminder
    track followup_rules picks, and a reopen is a fresh chase."""
    sql, _ = _sql_of(monkeypatch, db.reopen_if_closed, "p1")
    assert "else 'sent'" in sql


def test_reopening_only_ever_touches_a_closed_row(monkeypatch):
    """Without this the case expression would happily rewrite the status of a live proposal."""
    sql, _ = _sql_of(monkeypatch, db.reopen_if_closed, "p1")
    assert "proposal_status='closed_lost'" in sql.split(" where ", 1)[1]


def test_reopening_clears_the_lost_detail(monkeypatch):
    """Leaving closed_at set would keep the card out of the board: followups-core treats a
    non-null closed_at as lost on its own, regardless of the status."""
    sql, _ = _sql_of(monkeypatch, db.reopen_if_closed, "p1")
    assert "closed_lost_reason=null" in sql and "closed_at=null" in sql


def test_an_approval_a_revision_retired_does_not_come_back(monkeypatch):
    """reopen_if_closed reads approved_at, so whatever nulls approved_at is load-bearing for
    it. reset_for_revision is the one place that clears an approval without the row being
    closed, and if it stopped nulling approved_at, reopening a later closed_lost row would
    resurrect an agreement to a price the customer was never shown."""
    seen = {}
    monkeypatch.setattr(db, "q1", lambda sql, params=(): {"proposal_status": "approved"})
    monkeypatch.setattr(db, "execute",
                        lambda sql, params=(): seen.update(sql=sql, params=params))
    db.reset_for_revision("p1", 2)
    sql = " ".join(seen["sql"].split())
    assert "approved_at = case when %s then null else approved_at end" in sql


# ── the callers ───────────────────────────────────────────────────────────────
def _branch(fn, start, end=None):
    """The one branch of an endpoint under test, never the whole function. admin_set_status
    handles delayed, closed_lost and active, and all three call add_followup, so a
    function-wide grep for add_followup would pass with the closed_lost branch gutted."""
    src = inspect.getsource(fn)
    head = src.index(start)
    return src[head:src.index(end, head)] if end else src[head:]


def test_the_admin_endpoint_no_longer_refuses_an_approved_proposal():
    branch = _branch(main.admin_set_status, 'elif status == "closed_lost":',
                     'elif status == "active":')
    assert "already_approved" not in branch, (
        "the staff close still 400s on a signed proposal, so the CRM change cannot work")
    # The whole `if not` line, not just the call: dropping the check entirely would still
    # satisfy a grep for the call, and then a proposal deleted mid-request answers ok:true
    # and the drawer reports a close that never happened.
    assert "if not db.close_lost(proposal_id, reason):" in branch
    assert '"error": "not_found"' in branch


def test_the_admin_close_keeps_the_reason_validation_and_the_audit_trail():
    """Neither of these was in question, and both are easy to lose while editing the branch.
    The reason feeds the CRM's lost-reason chip, and the followup row is the only record of who
    closed it and when."""
    branch = _branch(main.admin_set_status, 'elif status == "closed_lost":',
                     'elif status == "active":')
    assert "_LOST_REASONS" in branch and '"error": "invalid_reason"' in branch
    assert 'db.add_followup(proposal_id, "staff_note"' in branch
    assert '"action": "closed_lost"' in branch


def test_the_publish_path_reopens_before_it_resets():
    """Load-bearing ordering, not a style point. reset_for_revision decides whether to clear
    the approved_* columns by reading proposal_status, so it has to run AFTER reopen_if_closed
    has restored 'approved'. Reversed, a revision sent to an approved-then-lost proposal would
    leave the approved_* columns populated under a 'sent' status, and the customer would never
    be told their earlier agreement no longer stands."""
    src = inspect.getsource(main.admin_publish)
    assert ('existing.get("proposal_status") == "closed_lost" and db.reopen_if_closed(draft_id)'
            in src), "publish no longer reopens a lost opportunity when a new version is sent"
    assert src.index("db.reopen_if_closed(draft_id)") < src.index("db.reset_for_revision(draft_id"), (
        "publish resets the revision before reopening, so the retired approval is not cleared")


@pytest.fixture
def approve_client(monkeypatch):
    """POST /approve with every write and every outbound mail stubbed, so a test can ask
    whether an approval was recorded rather than whether one could have been."""
    calls = {"approvals": [], "approved": [], "messages": [], "team": [],
             "customer": [], "automations": []}
    state = {"proposal": None}
    priced = [{"label": "Base bid", "total": 13265.0, "is_base": True}]

    monkeypatch.setattr(main, "_require", lambda request, token: state["proposal"])
    monkeypatch.setattr(main, "_session_email", lambda request: "cust@x.com")
    monkeypatch.setattr(main.db, "get_pinned_draft_data", lambda p: {"project_name": "Westport"})
    monkeypatch.setattr(main.proposals, "pricing_options", lambda data: priced)
    monkeypatch.setattr(main.proposals, "resolve_selection",
                        lambda data, labels: (priced, 13265.0))
    monkeypatch.setattr(main.db, "add_approval",
                        lambda pid, *a, **k: calls["approvals"].append(pid))
    monkeypatch.setattr(main.db, "set_approved",
                        lambda pid, total, *a, **k: calls["approved"].append(total))
    monkeypatch.setattr(main.db, "add_message",
                        lambda pid, kind, who, body, **k: calls["messages"].append(body))
    monkeypatch.setattr(main.email_sender, "notify_team",
                        lambda subject, body, **k: calls["team"].append(subject))
    monkeypatch.setattr(main.email_sender, "proposal_reply_to", lambda t: None)
    monkeypatch.setattr(main, "_notify_customer",
                        lambda p, heading, body: calls["customer"].append(heading))
    monkeypatch.setattr(main.automations, "run_on_approval",
                        lambda p, project: calls["automations"].append(project))

    tc = TestClient(main.app)
    tc.calls = calls
    tc.state = state
    return tc


def _row(**over):
    p = {"proposal_id": "p1", "token": "tok", "customer_email": "cust@x.com",
         "customer_name": "Cust", "project_name": "Westport", "proposal_status": "viewed",
         "approved_at": None, "deposit_required": True}
    p.update(over)
    return p


def _approve(client):
    return client.post("/api/portal/tok/approve",
                       json={"name": "Dana Reed", "title": "Owner",
                             "option_labels": ["Base bid"], "date": "2026-08-10"})


def test_the_customer_cannot_re_sign_a_job_staff_filed_as_lost(approve_client):
    """The hole relaxing close_lost opened. approved_at outlives the status now, so
    /approve keying idempotence on the status alone let this customer put a closed job back
    into Approved with one click: a second audit row, both approval emails again, and the
    staff decision undone without a word to the estimator who made it."""
    approve_client.state["proposal"] = _row(proposal_status="closed_lost",
                                            approved_at="2026-08-04T15:02:00+00:00")
    r = _approve(approve_client)
    assert r.status_code == 200 and r.json().get("already_approved") is True
    c = approve_client.calls
    assert c["approved"] == [], "the closed job was written back to Approved"
    assert c["approvals"] == [], "a second portal_approvals row was recorded"
    assert c["team"] == [] and c["customer"] == [], "the approval emails went out again"
    assert c["automations"] == []


def test_a_closed_job_that_was_never_signed_can_still_be_approved(approve_client):
    """The guard is deliberately not "closed_lost cannot approve". A customer who told us
    they were out and then changed their mind is welcome back, and that is the ONLY way a
    closed proposal comes back on the customer's own initiative."""
    approve_client.state["proposal"] = _row(proposal_status="closed_lost", approved_at=None)
    r = _approve(approve_client)
    assert r.status_code == 200 and "already_approved" not in r.json()
    assert approve_client.calls["approved"] == [13265.0]
    assert approve_client.calls["approvals"] == ["p1"]


def test_a_double_submit_is_still_a_no_op(approve_client):
    """The original reason this guard exists: two clicks, or a stale tab, must not run the
    approval twice."""
    approve_client.state["proposal"] = _row(proposal_status="approved",
                                            approved_at="2026-08-04T15:02:00+00:00")
    assert _approve(approve_client).json().get("already_approved") is True
    assert approve_client.calls["approved"] == []


def test_the_customers_own_way_out_is_still_shut_once_they_have_signed():
    """Hanz asked for this in the staff CRM. The customer's project-status card is a different
    question: "not moving forward" on a job they already signed is a conversation, not a
    self-service button, and it fans out mail to the estimator."""
    # Scoped to the guard itself. The not_moving_forward branch further down carries its own
    # `"error": "already_approved"`, so grepping the whole function for that string proves
    # nothing about the guard at the top: it passed with the guard deleted.
    guard = _branch(main.api_project_status,
                    'if (p.get("proposal_status") or "") == "approved":',
                    "body = await _body(request)")
    assert '"error": "already_approved"' in guard and "400" in guard


# ── no owner-only migration hiding in here ────────────────────────────────────
def test_the_status_constraint_already_allows_the_transition():
    """Prod DDL on this database needs the Supabase owner, so it matters that this change is
    pure SQL. The check constraint already lists both stages and there is no trigger on
    proposal_status, meaning nothing to migrate."""
    schema = (pathlib.Path(__file__).resolve().parents[1] / "schema.sql").read_text(encoding="utf-8")
    assert "check (proposal_status in ('sent','viewed','approved','closed_lost'))" in schema
