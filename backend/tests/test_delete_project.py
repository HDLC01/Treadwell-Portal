"""Deleting a project off the staff board, from the drawer's Proposal tab.

Hanz, 2026-08-24: "In the proposals tab under the Active Projects create a 'delete project'
button", and "make sure there is a confirmation dialog". He had two SENT test bids that no control
in either app could take off the Active Projects board.

WHY THE PORTAL HAD TO GROW ANYTHING. The board is built from portal_proposals
(db.list_all_portal_proposals), and its join deliberately ignores the DRAFT's deleted_at, so the
staff tool's own Trash button -- which touches the draft and nothing else -- removed the project
from the Proposals Database and left its card on the board for good. Worse, the board's enrichment
reads drafts.list_drafts(), which skips trashed rows, so the surviving card silently lost its
is_test flag, its bid total and its won mark and could move itself from the Test tab onto Active
with nobody touching it. There was no way to delete or hide a portal_proposals row anywhere in
either repository: 30-odd proxy calls and exactly one DELETE, for a notification recipient.

FOUR THINGS ARE PINNED HERE, and the second is the one with teeth:

  1. the row leaves the board (list_all_portal_proposals);
  2. it leaves the cadence, so nothing keeps emailing the customer from the trash
     (list_followup_candidates AND followup_rules.due_now, which is real code and runs here);
  3. the CUSTOMER is untouched -- their link still resolves, because deleting a project off our
     board is not the same act as revoking what somebody was already sent;
  4. restoring puts the card back and leaves the chase OFF.

The query tests assert the SQL rather than a result set, which is this file's established
compromise for a suite with no database (see test_admin_pipeline.py, which does the same for the
estimator join): the queries are executed for real through db.qall/db.q1, so a rename or a
refactor fails here, and the live behaviour is covered by the staging smoke. Everything else in
this file runs the real handler.
"""
import datetime as dt

import pytest
from fastapi.testclient import TestClient

import followup_rules
import main


def _sql(monkeypatch, fn):
    """Run one of the real db functions and hand back the SQL it executed, whitespace-flattened."""
    seen = {}
    monkeypatch.setattr(main.db, "qall", lambda sql, params=(): seen.update(sql=sql) or [])
    monkeypatch.setattr(main.db, "q1", lambda sql, params=(): seen.update(sql=sql, params=params))
    fn()
    return " ".join(seen["sql"].split()), seen.get("params")


# -- 1. the card leaves the board --------------------------------------------
def test_the_board_query_skips_a_deleted_project(monkeypatch):
    """The predicate, and WHERE it sits. A guard after the order by is a syntax error, and a
    guard that read the draft's column instead of ours would filter the wrong table."""
    sql, _ = _sql(monkeypatch, main.db.list_all_portal_proposals)
    assert "where (to_jsonb(p) ->> 'deleted_at') is null" in sql
    assert sql.index("where (to_jsonb(p)") < sql.index("order by"), (
        "the guard has to be in the WHERE, not trailing after the sort")


def test_the_board_query_reads_the_column_through_to_jsonb(monkeypatch):
    """NOT `p.deleted_at is null`, and this is not style.

    Production cannot apply its own DDL (APPLY_SCHEMA_ON_BOOT is false when IS_PROD), so an owner
    runs the ALTER by hand and the code can land first. A column named directly makes psycopg raise
    UndefinedColumn, and this is the ONLY pipeline query -- the staff board AND the Follow-ups page
    would both die, with an error that blames portal reachability, over one unapplied statement. An
    absent jsonb key reads as NULL instead, which is exactly "not deleted". The same reasoning is
    already load-bearing for link_clicked_at and last_sent_at in this query."""
    sql, _ = _sql(monkeypatch, main.db.list_all_portal_proposals)
    assert "p.deleted_at" not in sql, (
        "named directly, a database without the ALTER returns UndefinedColumn and takes the board "
        "and the Follow-ups page down together")


# -- 2. it stops being chased ------------------------------------------------
def test_the_cadence_query_skips_a_deleted_project(monkeypatch):
    """The one that would have emailed a customer about a project nobody can see.

    followup_worker selects its work through this function, and the automation went live the same
    day the button was asked for. A deleted project whose row still looked enrolled would keep
    chasing from the trash."""
    sql, _ = _sql(monkeypatch, main.db.list_followup_candidates)
    # to_jsonb of the TABLE NAME rather than an alias: aliasing this query would mean qualifying
    # every other column in it for no gain. This line is the only thing pinning that choice --
    # test_followup_deposit_stage.py used to read three columns out of the source text and now
    # executes the clause instead, which is alias-agnostic by design.
    assert "where (to_jsonb(portal_proposals) ->> 'deleted_at') is null" in sql
    # And the enrolment clause is still there: this must NARROW the candidate set, not replace it.
    assert "followup_enrolled_at is not null and followup_disabled_at is null" in sql


def test_deleting_stamps_the_hide_and_stops_the_chase_in_one_statement(monkeypatch):
    """BOTH columns, and the second is not tidying.

    followup_disabled_at is what due_now reads first, so it is the belt to the deleted_at braces.
    Writing it in the route instead would let a future caller do half of it."""
    sql, params = _sql(monkeypatch, lambda: main.db.delete_proposal("p1"))
    assert "set deleted_at = now()" in sql
    assert "followup_disabled_at = coalesce(followup_disabled_at, now())" in sql, (
        "a deleted project that still looks enrolled keeps emailing the customer")
    assert params == ("p1",)


def test_deleting_never_moves_a_disable_stamp_that_already_exists(monkeypatch):
    """coalesce, not now(). The follow-up log dates "when did we stop chasing this" off that
    column, so re-stamping it on a second press would re-date a stop that happened weeks ago."""
    sql, _ = _sql(monkeypatch, lambda: main.db.delete_proposal("p1"))
    assert "followup_disabled_at = now()" not in sql


def test_restoring_leaves_the_chase_off(monkeypatch):
    """THE POINT OF A SEPARATE FUNCTION. Restoring is somebody looking for a project, not asking
    us to start emailing their customer again -- and the cadence would resume against an anchor
    weeks old, so the first tick after a restore would fire immediately."""
    sql, params = _sql(monkeypatch, lambda: main.db.restore_proposal("p1"))
    assert "deleted_at = null" in sql
    assert "followup_disabled_at" not in sql, "restoring must not switch the chasing back on"
    assert params == ("p1",)


def test_due_now_returns_nothing_for_a_deleted_project():
    """The engine itself, over a row shaped exactly as delete_proposal leaves it. Real code, no
    stubs: this is the last line of defence if the candidate query is ever widened again."""
    now = dt.datetime(2026, 8, 24, 12, 0, tzinfo=dt.timezone.utc)
    long_ago = dt.datetime(2026, 6, 1, 12, 0, tzinfo=dt.timezone.utc)
    live = {"proposal_id": "p1", "proposal_status": "sent",
            "followup_enrolled_at": long_ago, "followup_disabled_at": None,
            "followup_paused_until": None, "cycle_viewed_at": None,
            "deposit_required": True, "deposit_status": "pending"}
    # The premise: this row IS due, or the assertion below proves nothing.
    assert followup_rules.due_now(live, now), "fixture is not due, so the test cannot bite"
    deleted = dict(live, deleted_at=now, followup_disabled_at=now)
    assert followup_rules.due_now(deleted, now) == [], (
        "a deleted project is still a follow-up candidate")


# -- 3. the routes -----------------------------------------------------------
@pytest.fixture
def client(monkeypatch):
    """The two new admin routes, with the store recorded rather than reached."""
    calls = {"deleted": [], "restored": [], "followups": [], "cards": [], "resumed": []}
    state = {"proposal": {"proposal_id": "p1", "project_name": "Nearman Creek",
                          "customer_email": "dave@x.com", "token": "tok",
                          "proposal_status": "sent", "deleted_at": None}}

    monkeypatch.setattr(main.config, "SERVICE_TOKEN", "svc")
    monkeypatch.setattr(main.db, "get_proposal", lambda pid: state["proposal"])
    monkeypatch.setattr(main.db, "delete_proposal",
                        lambda pid: calls["deleted"].append(pid) or True)
    monkeypatch.setattr(main.db, "restore_proposal",
                        lambda pid: calls["restored"].append(pid) or True)
    monkeypatch.setattr(main.db, "resume_followups",
                        lambda pid: calls["resumed"].append(pid))
    monkeypatch.setattr(main.db, "add_followup",
                        lambda pid, kind, detail=None, by=None: calls["followups"].append(
                            {"kind": kind, "detail": detail, "by": by}))
    monkeypatch.setattr(main.db, "add_message",
                        lambda pid, who, email, body, **k: calls["cards"].append(
                            {"body": body, "meta": k.get("meta")}))
    tc = TestClient(main.app)
    tc.calls = calls
    tc.state = state
    return tc


def _post(client, what, token="svc", body=None):
    return client.post("/api/admin/proposal/p1/" + what, json=body or {},
                       headers={"X-Service-Token": token})


def test_a_caller_without_the_service_token_is_refused(client):
    """The gate, on both halves. The tool proxies these server-to-server and holds the admin
    check of its own; this is the one that stops anything else reaching them."""
    for what in ("delete", "restore"):
        r = _post(client, what, token="wrong")
        assert r.status_code == 401, what
    assert client.calls["deleted"] == [] and client.calls["restored"] == []


def test_deleting_an_unknown_proposal_is_a_404_and_not_an_error(client):
    """The tool reads this as "there is no portal row", which is the NORMAL answer for a bid
    nobody ever sent -- that project's card comes off the board through the draft half alone. So
    it has to stay a 404 with `not_found`, not a 500 and not a 200."""
    client.state["proposal"] = None
    r = _post(client, "delete")
    assert r.status_code == 404 and r.json()["error"] == "not_found"
    assert client.calls["deleted"] == []


def test_deleting_records_the_act_and_tells_the_next_reader_what_it_did(client):
    r = _post(client, "delete", body={"by": "hanz@wetreadwell.com"})
    assert r.status_code == 200 and r.json()["deleted"] is True
    assert client.calls["deleted"] == ["p1"]
    # Bookkeeping, so `action` is set: last_staff_followup_at counts only rows WITHOUT one, and
    # deleting a project is not somebody chasing a customer.
    assert client.calls["followups"] == [
        {"kind": "staff_note", "detail": {"action": "deleted"}, "by": "hanz@wetreadwell.com"}]
    card = client.calls["cards"][0]
    assert card["meta"] == {"crm": "status_deleted", "internal": True}, (
        "the card must be internal, or deleting a project rings the customer's bell about it")
    assert "Hanz" in card["body"], "the card does not say who did it"
    for phrase in ("off the board", "follow-ups have stopped", "still works"):
        assert phrase in card["body"], phrase


def test_restoring_puts_it_back_and_says_the_reminders_stay_off(client):
    client.state["proposal"]["deleted_at"] = "2026-08-24T12:00:00+00:00"
    r = _post(client, "restore", body={"by": "hanz@wetreadwell.com"})
    assert r.status_code == 200 and r.json() == {"ok": True, "restored": True, "was_deleted": True}
    assert client.calls["restored"] == ["p1"]
    # THE WHOLE POINT: nothing here resumes the cadence.
    assert client.calls["resumed"] == [], "restoring switched the chasing back on"
    assert client.calls["followups"][0]["detail"] == {"action": "restored"}
    assert "stay off" in client.calls["cards"][0]["body"]


def test_restoring_a_live_project_changes_nothing_and_narrates_nothing(client):
    """A no-op worth reporting rather than announcing. Nothing moved, so nothing goes in the
    thread -- otherwise a stray press writes a card saying a project was restored from a place it
    had never been."""
    r = _post(client, "restore")
    assert r.status_code == 200 and r.json()["was_deleted"] is False
    assert client.calls["cards"] == [] and client.calls["followups"] == []


# -- 4. the customer is untouched -------------------------------------------
def test_the_customers_own_page_still_resolves_for_a_deleted_project(monkeypatch):
    """DELETING IS NOT REVOKING, and this is the assertion that holds the line.

    The customer's view reads portal_proposals by TOKEN (get_proposal_by_token) and renders the
    revision that was pinned when the proposal was sent, and neither of those is filtered. Taking a
    project off our board must not break a link somebody was already given -- if a link ever needs
    revoking that is a different feature, with a different word on the button."""
    deleted = {"proposal_id": "p1", "token": "tok", "customer_email": "dave@x.com",
               "proposal_status": "sent", "deposit_status": "pending",
               "contacts_status": "pending", "schedule_status": "pending",
               "deposit_invoice_no": None, "deposit_amount": None, "deposit_required": True,
               "current_revision_no": 2,
               "deleted_at": "2026-08-24T12:00:00+00:00"}
    # The REAL lookup and the REAL access check; only the thread read is stubbed.
    monkeypatch.setattr(main.db, "get_proposal_by_token", lambda tok: deleted)
    monkeypatch.setattr(main, "_session_email", lambda request: "dave@x.com")
    monkeypatch.setattr(main.db, "list_messages", lambda pid, after: [])
    r = TestClient(main.app).get("/api/portal/tok/messages?after=0")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert r.json()["status"]["proposal"] == "sent"


def test_the_token_lookup_is_deliberately_unfiltered(monkeypatch):
    """The other half of the claim above, at the query. A `deleted_at is null` added here would
    turn every deleted project's customer link into a 404 -- silently, weeks later."""
    seen = {}
    monkeypatch.setattr(main.db, "q1", lambda sql, params=(): seen.update(sql=sql))
    main.db.get_proposal_by_token("tok")
    assert "deleted_at" not in seen["sql"]
