"""Whoever BUILT the estimate is on that project's notifications by default.

Will, via Hanz on 2026-08-13: "There are set members for the global notification. And this
estimator or treadwell employee created an estimate, by default this estimator should be
included."

Three different people can be involved in one send, and only one of them was covered before:

* the ASSIGNED estimator — folded in at notify time from `assigned_estimator` (2026-08-13). Who
  owns chasing the job.
* the SENDER — whoever pressed Send. Recorded as `by` for the audit trail, not notified.
* the CREATOR — whose estimate this is. Nobody. RJ could price a bid, hand it to Kyle, and hear
  nothing further about a job he built.

The creator is written as a per-project ADD rather than resolved invisibly at send time, so that
it appears on the Notification Sending page beside the hand-added people — a rule nobody can see
is a rule somebody has to be told about — and so that it can be MUTED, with the mute surviving
every later publish.
"""
import pytest
from fastapi.testclient import TestClient

import email_sender
import main

client = TestClient(main.app)


@pytest.fixture
def publish(monkeypatch):
    """A publish that touches no database and no mail server, recording the roster writes."""
    calls = {"added": [], "upserted": [], "emails": []}
    proposal = {"token": "tok-1", "customer_email": "cust@acme.com", "proposal_status": "sent"}

    monkeypatch.setattr(main, "_admin_ok", lambda request: True)
    # The route reads the project's data from the DB, not from the body — stub it or every test
    # here spends 30 seconds timing out against a connection pool it should never reach.
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
    monkeypatch.setattr(main, "_pdf_cache_drop", lambda pid: None)
    monkeypatch.setattr(main.db, "add_notify_override_if_absent",
                        lambda pid, email: calls["added"].append((pid, email)))
    monkeypatch.setattr(main.db, "set_notify_override",
                        lambda pid, email, mode: calls["upserted"].append((pid, email, mode)))
    monkeypatch.setattr(main.email_sender, "send_portal_link",
                        lambda to, *a, **k: calls["emails"].append(to) or True)
    monkeypatch.setattr(main.email_sender, "proposal_reply_to", lambda tok: "r@x.com")

    def go(**extra):
        body = {"draft_id": "d-1", "by": "sender@wetreadwell.com",
                "data": {"contact_email": "cust@acme.com", "project_name": "Oak Grove"},
                "revision_no": 1}
        body.update(extra)
        return client.post("/api/admin/publish", json=body)

    return go, calls


def test_the_creator_is_added_to_the_projects_notifications(publish):
    go, calls = publish
    r = go(created_by="rj@wetreadwell.com")
    assert r.status_code == 200, r.text
    assert calls["added"] == [("d-1", "rj@wetreadwell.com")]


def test_the_creator_is_recorded_as_an_ADD_that_cannot_stomp_a_mute(publish):
    """Via the insert-if-absent helper, never the upsert. This runs on every publish, so an
    upsert would turn a deliberate mute back into an add the next time the proposal went out."""
    go, calls = publish
    go(created_by="rj@wetreadwell.com")
    assert calls["upserted"] == [], (
        "the creator went through set_notify_override, which overwrites an existing mute")


def test_an_address_that_is_not_an_address_is_ignored(publish):
    go, calls = publish
    for junk in ("", "   ", "not an email", None):
        calls["added"].clear()
        assert go(created_by=junk).status_code == 200
        assert calls["added"] == [], junk


def test_a_display_name_is_reduced_to_the_address(publish):
    """`by` fields have arrived as "RJ <rj@…>" before now, and a stored override has to be the
    bare address or the resolver's case-insensitive dedupe never matches it."""
    go, calls = publish
    go(created_by="RJ Buchanan <RJ@WeTreadwell.com>")
    assert calls["added"] == [("d-1", "rj@wetreadwell.com")]


def test_a_missing_creator_changes_nothing(publish):
    """Drafts predating owner stamping carry no owner. They must publish exactly as before."""
    go, calls = publish
    r = go()
    assert r.status_code == 200
    assert calls["added"] == []
    assert calls["emails"] == ["cust@acme.com"], "the customer send was disturbed"


def test_a_roster_write_failure_does_not_stop_the_send(publish, monkeypatch):
    """The proposal is the point. A notifications table that is momentarily unreachable must not
    be able to hold up a customer's document."""
    go, calls = publish
    monkeypatch.setattr(main.db, "add_notify_override_if_absent",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    r = go(created_by="rj@wetreadwell.com")
    assert r.status_code == 200, r.text
    assert calls["emails"] == ["cust@acme.com"]


def test_the_creator_is_added_on_a_revision_too(publish):
    """Projects that existed before this shipped pick it up on their next send, which is the
    only way they ever would."""
    go, calls = publish
    r = go(created_by="rj@wetreadwell.com", revision_no=3)
    assert r.status_code == 200, r.text
    assert calls["added"] == [("d-1", "rj@wetreadwell.com")]


# ── how it then behaves at notify time ────────────────────────────────────────
def test_a_creator_add_reaches_the_recipient_list():
    """The add is not special-cased anywhere downstream: it flows through the same per-project
    path as a hand-added person."""
    out = email_sender.resolve_notify_recipients(
        ["hanz@wetreadwell.com"], [], "general", ["env@x.com"], ["env@x.com"],
        adds=["rj@wetreadwell.com"], mutes=[], configured=True)
    assert out == ["hanz@wetreadwell.com", "rj@wetreadwell.com"]


def test_a_muted_creator_stays_muted():
    """The reason the row is written insert-if-absent. Somebody who asked to be left off one job
    must not be dragged back in by having built it."""
    out = email_sender.resolve_notify_recipients(
        ["hanz@wetreadwell.com"], [], "general", ["env@x.com"], ["env@x.com"],
        adds=["rj@wetreadwell.com"], mutes=["RJ@wetreadwell.com"], configured=True)
    assert out == ["hanz@wetreadwell.com"]


def test_a_creator_who_is_already_on_the_roster_is_not_emailed_twice():
    out = email_sender.resolve_notify_recipients(
        ["hanz@wetreadwell.com", "RJ@wetreadwell.com"], [], "general", [], [],
        adds=["rj@wetreadwell.com"], mutes=[], configured=True)
    assert out == ["hanz@wetreadwell.com", "RJ@wetreadwell.com"]


def test_the_creator_hears_about_a_deposit_as_well():
    """A deposit alert goes to the general roster PLUS the deposit bucket (see
    test_notify_recipients.py). The person who priced the job is exactly who wants to know the
    money arrived, so the add has to apply to both kinds."""
    out = email_sender.resolve_notify_recipients(
        ["hanz@wetreadwell.com"], ["money@wetreadwell.com"], "deposit", [], [],
        adds=["rj@wetreadwell.com"], mutes=[], configured=True)
    assert out == ["hanz@wetreadwell.com", "money@wetreadwell.com", "rj@wetreadwell.com"]


def test_the_insert_is_an_add_that_does_nothing_on_conflict(monkeypatch):
    """Run the helper and read the statement it actually issues.

    Not `inspect.getsource`: that includes the docstring, which says the word 'add' — so a check
    for "'add'" in the source passed happily while the VALUES clause said 'mute'. The mutation
    that proved it is the reason this test executes the function instead.

    The two clauses are the whole guarantee. An `on conflict do update` here silently un-mutes
    people on the next publish, and a 'mute' value would silence the very person being added."""
    import db

    seen = {}
    monkeypatch.setattr(db, "execute", lambda sql, args=None: seen.update(sql=sql, args=args))
    db.add_notify_override_if_absent("d-1", "  RJ@WeTreadwell.com ")
    stmt = " ".join(seen["sql"].split())
    assert "values (%s,%s,'add')" in stmt, stmt
    assert "'mute'" not in stmt, stmt
    assert "on conflict (proposal_id, lower(email)) do nothing" in stmt, stmt
    assert "do update" not in stmt, stmt
    # Normalised on the way in, so the resolver's case-insensitive dedupe and any later mute
    # both match the row that was written.
    assert seen["args"] == ("d-1", "rj@wetreadwell.com"), seen["args"]
