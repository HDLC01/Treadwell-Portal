"""Sending a proposal emails the staff who need to know it went out — and when it did not.

Hanz, 2026-08-19: "When a proposal has been sent it does not email the estimator and the people in
notification sending. To confirm that the email has been sent."

Before this, the only record that a proposal went out was the response payload the sender's own
browser showed for a few seconds. The estimator who owns the job and the roster on the Notification
Sending page found out when the customer replied — or never. The bad day was worse: a send whose
delivery FAILED looked identical to a successful one from every screen except that one response.

The confirmation is resolved through notify_team, so every rule that governs the other staff
notifications applies to this one unchanged: the enabled roster, the per-project adds (including
the creator, written on this same route), the assigned estimator folded in as an add, and mutes
winning over everything. These tests therefore assert on WHAT is handed to notify_team, not on a
re-implementation of its resolution — that resolution has its own tests.
"""
import pytest
from fastapi.testclient import TestClient

import main

client = TestClient(main.app)


@pytest.fixture
def publish(monkeypatch):
    """A publish that touches no database and no mail server, recording every notify_team call
    and letting a test choose which customer emails 'deliver'."""
    calls = {"notify": [], "emails": []}
    state = {"send_ok": lambda to: True}
    proposal = {"token": "tok-1", "customer_email": "cust@acme.com", "proposal_status": "sent",
                "assigned_estimator": "stored-est@wetreadwell.com"}

    monkeypatch.setattr(main, "_admin_ok", lambda request: True)
    monkeypatch.setattr(main.db, "get_draft_data",
                        lambda pid: {"contact_email": "cust@acme.com", "project_name": "Oak Grove"})
    monkeypatch.setattr(main.db, "get_proposal", lambda pid: dict(proposal))
    monkeypatch.setattr(main.db, "update_portal_proposal", lambda *a, **k: None)
    monkeypatch.setattr(main.db, "reset_for_revision", lambda *a, **k: False)
    monkeypatch.setattr(main.db, "supersede_proposal_cards", lambda *a, **k: None)
    monkeypatch.setattr(main.db, "add_message", lambda *a, **k: {"id": 1})
    monkeypatch.setattr(main.db, "set_recipients", lambda *a, **k: None)
    monkeypatch.setattr(main.db, "add_recipient", lambda *a, **k: None)
    monkeypatch.setattr(main.db, "remove_recipient", lambda *a, **k: None)
    monkeypatch.setattr(main.db, "get_recipients", lambda pid: ["cust@acme.com"])
    monkeypatch.setattr(main.db, "set_assigned_estimator", lambda *a, **k: None)
    monkeypatch.setattr(main.db, "enroll_followup", lambda pid: None)
    monkeypatch.setattr(main.db, "mark_last_sent", lambda pid: None)
    monkeypatch.setattr(main.db, "add_notify_override_if_absent", lambda *a, **k: None)
    monkeypatch.setattr(main, "_pdf_cache_drop", lambda pid: None)
    monkeypatch.setattr(main.email_sender, "proposal_reply_to", lambda tok: "reply@notify.x")
    monkeypatch.setattr(main.email_sender, "send_portal_link",
                        lambda to, *a, **k: (calls["emails"].append(to),
                                             state["send_ok"](to))[1])
    monkeypatch.setattr(main.email_sender, "notify_team",
                        lambda heading, body_html, **kw: calls["notify"].append(
                            {"heading": heading, "body": body_html, **kw}) or True)

    def go(**extra):
        body = {"draft_id": "d-1", "by": "sender@wetreadwell.com", "revision_no": 1}
        body.update(extra)
        return client.post("/api/admin/publish", json=body)

    return go, calls, state


def test_a_send_emails_the_team_that_it_went_out(publish):
    go, calls, _ = publish
    r = go(assigned_estimator="kyle@wetreadwell.com")
    assert r.status_code == 200, r.text
    assert len(calls["notify"]) == 1, "the send confirmation did not go out exactly once"
    n = calls["notify"][0]
    assert n["heading"] == "Proposal sent — Oak Grove"
    assert "cust@acme.com" in n["body"], "the confirmation does not say who received it"
    assert "sender@wetreadwell.com" in n["body"], "the confirmation does not say who sent it"


def test_the_confirmation_carries_every_hook_the_roster_rules_need(publish):
    """kind/adds/mutes are notify_team's business, but only if this call HANDS it the project and
    the estimator. Dropping one of these kwargs silently narrows who hears about the send —
    exactly the class of quiet miss this feature exists to end."""
    go, calls, _ = publish
    go(assigned_estimator="kyle@wetreadwell.com")
    n = calls["notify"][0]
    assert n["proposal_id"] == "d-1", "per-project adds and mutes cannot apply without the id"
    assert n["assigned_estimator"] == "kyle@wetreadwell.com", (
        "the estimator Hanz named first would not be folded in")
    assert n["token"] == "tok-1" and n["project"] == "Oak Grove", (
        "without token+project the email cannot join the project's staff thread")
    assert "portal.html?open=d-1" in (n["reply_link"] or ""), (
        "no deep link back into the staff tool")
    assert n["reply_to"] == "reply@notify.x"


def test_an_older_tool_still_credits_the_stored_estimator(publish):
    """A publish from a tool that predates assigned_estimator must fall back to the estimator
    already stored on the proposal row, not silently notify nobody who owns the job."""
    go, calls, _ = publish
    go()                                        # no assigned_estimator in the body
    assert calls["notify"][0]["assigned_estimator"] == "stored-est@wetreadwell.com"


def test_a_failed_delivery_is_the_loudest_version_not_a_missing_one(publish):
    """The sender's browser is the only other place that knows delivery failed, and it has
    navigated away. Total failure must still email the team, and must SAY it failed."""
    go, calls, state = publish
    state["send_ok"] = lambda to: False
    r = go()
    assert r.status_code == 200
    n = calls["notify"][0]
    assert "did NOT send" in n["heading"], n["heading"]
    assert "no email was delivered" in n["body"]
    assert "send it again" in n["body"]


def test_a_partial_failure_names_the_address_that_missed(publish):
    go, calls, state = publish
    state["send_ok"] = lambda to: to != "second@acme.com"
    go(emails=["second@acme.com"])
    n = calls["notify"][0]
    assert "with failures" in n["heading"], n["heading"]
    assert "second@acme.com" in n["body"], "the failed address is not named"
    assert "cust@acme.com" in n["body"], "the delivered address disappeared from the story"


def test_a_broken_confirmation_can_never_stop_a_proposal(publish, monkeypatch):
    """The customer's email has already gone out by this point. A confirmation failure is a
    logging problem, never a 500 that makes staff re-send a proposal the customer already has."""
    go, calls, _ = publish
    def boom(*a, **k):
        raise RuntimeError("smtp fell over")
    monkeypatch.setattr(main.email_sender, "notify_team", boom)
    r = go()
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True


def test_a_later_revision_says_which_revision_went_out(publish):
    go, calls, _ = publish
    go(revision_no=3)
    n = calls["notify"][0]
    assert "revision 3" in n["body"].lower(), (
        "a revision send reads identically to a first send")
