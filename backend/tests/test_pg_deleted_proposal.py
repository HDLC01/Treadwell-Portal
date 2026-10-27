"""A deleted proposal through the REAL app and a REAL Postgres: delete it, then open its link.

test_deleted_proposal.py proves the gate per route with the database recorded; this file runs the
same story with nothing stubbed but email and the network: schema.sql applied to a scratch
database, rows written by db.create_portal_proposal, the deletion done by db.delete_proposal (the
function the staff delete route calls), a real customer session from customer_auth.start_session,
and every request through main.app.

HOW TO RUN: see test_pg_scope_groups.py (a throwaway local Postgres named by PORTAL_TEST_PG_URL).
Skipped without it.
"""
from __future__ import annotations

import pytest

import pg_fixture
from pg_fixture import needs_pg

pytestmark = [needs_pg, pytest.mark.realdb, pytest.mark.realwording]

if pg_fixture.PG_URL:  # imported only when the tests will run
    from fastapi.testclient import TestClient
    from psycopg.types.json import Jsonb

    import config
    import customer_auth as ca
    import db
    import email_sender
    import main

DANA = "dana@customer.example"
ONLY_DELETED = "olly@customer.example"


@pytest.fixture(scope="module")
def pgdb():
    with pg_fixture.scratch_portal_db("del") as pool:
        yield pool


@pytest.fixture
def app(pgdb, monkeypatch):
    db.execute("truncate public.portal_proposals, public.portal_sessions, public.drafts, "
               "public.draft_revisions restart identity cascade")
    sent = []
    monkeypatch.setattr(email_sender, "_send", lambda *a, **k: sent.append(a[:2]) or True)
    monkeypatch.setattr(email_sender, "notify_team", lambda *a, **k: sent.append(a[:1]) or True)
    monkeypatch.setattr(config, "PROPOSAL_TOOL_URL", "")
    for pid, email in (("live-a", DANA), ("gone-b", DANA), ("gone-c", ONLY_DELETED)):
        db.execute("insert into public.drafts (id, data) values (%s, %s)",
                   (pid, Jsonb({"project_name": "Acme HQ"})))
        db.create_portal_proposal(pid, "tok-" + pid, email, "Dana", "Acme HQ", None,
                                  "kyle@wetreadwell.com", True, None)
        db.add_message(pid, "staff", "kyle@wetreadwell.com", "Here is your proposal.",
                       msg_type="text")
    db.delete_proposal("gone-b")
    db.delete_proposal("gone-c")
    client = TestClient(main.app)
    client.cookies.set(config.SESSION_COOKIE, ca.start_session(DANA))
    client.sent = sent
    return client


def _row(pid):
    return db.q1("select proposal_status, approved_at, deleted_at from public.portal_proposals "
                 "where proposal_id = %s", (pid,))


def test_the_deleted_proposals_page_data_is_gone_and_the_live_one_is_not(app):
    r = app.get("/api/portal/tok-gone-b")
    assert r.status_code == 410, r.text
    assert r.json() == {"ok": False, "error": "proposal_gone",
                        "message": main.PROPOSAL_GONE_MESSAGE}
    live = app.get("/api/portal/tok-live-a")
    assert live.status_code == 200, live.text
    assert live.json()["authed"] is True and "view" in live.json()


def test_approving_a_deleted_proposal_is_refused_and_writes_nothing(app):
    approvals = db.q1("select count(*) as n from public.portal_approvals")["n"]
    messages = db.q1("select count(*) as n from public.portal_questions")["n"]
    r = app.post("/api/portal/tok-gone-b/approve",
                 json={"name": "Dana Customer", "title": "Owner", "options": ["Base bid"],
                       "consent": True, "signature": "Dana Customer"})
    assert r.status_code == 410 and r.json()["error"] == "proposal_gone"
    assert db.q1("select count(*) as n from public.portal_approvals")["n"] == approvals
    assert db.q1("select count(*) as n from public.portal_questions")["n"] == messages
    row = _row("gone-b")
    assert row["proposal_status"] == "sent" and row["approved_at"] is None
    assert app.sent == [], "an email went out for a deleted proposal"


def test_a_question_on_a_deleted_proposal_is_refused_and_not_stored(app):
    before = db.q1("select count(*) as n from public.portal_questions")["n"]
    r = app.post("/api/portal/tok-gone-b/questions", json={"body": "Still on?"})
    assert r.status_code == 410
    assert db.q1("select count(*) as n from public.portal_questions")["n"] == before


def test_the_landing_link_records_no_click_on_a_deleted_proposal(app):
    assert app.get("/p/tok-gone-b").status_code == 200
    row = db.q1("select (to_jsonb(p) ->> 'link_clicked_at') as c from public.portal_proposals p "
                "where proposal_id = 'gone-b'")
    assert row["c"] is None
    app.get("/p/tok-live-a")
    row = db.q1("select (to_jsonb(p) ->> 'link_clicked_at') as c from public.portal_proposals p "
                "where proposal_id = 'live-a'")
    assert row["c"] is not None, "the live control did not record, so the check above proves nothing"


def test_the_customers_list_and_bell_skip_the_deleted_proposal(app):
    cards = app.get("/api/me/proposals").json()["proposals"]
    assert [c["token"] for c in cards] == ["tok-live-a"]
    links = {i["link"] for i in app.get("/api/me/notifications").json()["items"]}
    assert links == {"/p/tok-live-a"}, links


def test_a_customer_whose_only_proposal_was_deleted_gets_no_code_and_an_empty_list(app):
    assert db.email_has_proposal(ONLY_DELETED) is False
    assert db.email_has_proposal(DANA) is True
    r = app.post("/api/auth/request-code", json={"email": ONLY_DELETED})
    assert r.status_code == 200 and r.json() == {"ok": False, "error": "no_project"}
    # A session that predates the deletion: an empty list, not an error.
    app.cookies.set(config.SESSION_COOKIE, ca.start_session(ONLY_DELETED))
    r = app.get("/api/me/proposals")
    assert r.status_code == 200
    assert r.json()["authed"] is True and r.json()["proposals"] == []


def test_an_added_recipient_of_only_a_deleted_proposal_gets_no_code(app):
    db.add_recipient("gone-c", "second@customer.example")
    assert db.email_has_proposal("second@customer.example") is False
    db.add_recipient("live-a", "second@customer.example")
    assert db.email_has_proposal("second@customer.example") is True


def test_restoring_brings_the_link_back(app):
    """Staff restore goes by proposal_id and must keep working; once restored, the link opens."""
    assert db.restore_proposal("gone-b") is True
    r = app.get("/api/portal/tok-gone-b")
    assert r.status_code == 200 and r.json()["authed"] is True
