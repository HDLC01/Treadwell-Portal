"""The Files screen's notification picks, applied to the send they were chosen for.

Hanz, 2026-08-19: "we need that notifcation sending selection in the Files. so we can select who
receives it first."

TWO ORDERING FACTS MAKE THIS THE ONLY PLACE THE WRITE CAN HAPPEN, and both are load-bearing:

1. `portal_notify_overrides.proposal_id` is a foreign key onto `portal_proposals`. On a FIRST send
   that row does not exist until `create_portal_proposal` runs inside this route, so the browser
   cannot write the overrides beforehand — the endpoint 404s and the FK would refuse the insert.
2. `notify_team` resolves the recipient list by reading the override table at the instant it sends.
   Anything written after it affects the NEXT notification, not this one.

So the picks must land after the row is created and before notify_team runs. A regression that moves
either boundary is silent: the proposal still goes out, the confirmation still goes out, and it goes
to the wrong people.
"""
import pytest
from fastapi.testclient import TestClient

import main

client = TestClient(main.app)


@pytest.fixture
def publish(monkeypatch):
    """A publish that touches no database and no mail server, recording the ORDER of every
    notify-relevant call so the two boundaries above can be asserted rather than assumed."""
    calls = {"order": [], "set": [], "cleared": [], "notified": None}
    proposal = {"token": "tok-1", "customer_email": "cust@acme.com", "proposal_status": "sent",
                "assigned_estimator": "kyle@wetreadwell.com"}
    state = {"exists": True, "overrides": []}

    monkeypatch.setattr(main, "_admin_ok", lambda request: True)
    monkeypatch.setattr(main.db, "get_draft_data",
                        lambda pid: {"contact_email": "cust@acme.com", "project_name": "Oak Grove"})
    monkeypatch.setattr(main.db, "get_proposal",
                        lambda pid: dict(proposal) if state["exists"] else None)

    def created(*a, **k):
        state["exists"] = True
        calls["order"].append("create_row")
    monkeypatch.setattr(main.db, "create_portal_proposal", created)
    monkeypatch.setattr(main.db, "update_portal_proposal",
                        lambda *a, **k: calls["order"].append("update_row"))
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
    monkeypatch.setattr(main.db, "list_notify_overrides", lambda pid: list(state["overrides"]))
    monkeypatch.setattr(main, "_pdf_cache_drop", lambda pid: None)
    monkeypatch.setattr(main.email_sender, "proposal_reply_to", lambda tok: "r@x.com")
    monkeypatch.setattr(main.email_sender, "send_portal_link", lambda to, *a, **k: True)

    def set_override(pid, email, mode):
        calls["set"].append((email, mode))
        calls["order"].append("set_override")
    monkeypatch.setattr(main.db, "set_notify_override", set_override)

    def clear_override(pid, email):
        calls["cleared"].append(email)
        calls["order"].append("clear_override")
    monkeypatch.setattr(main.db, "clear_notify_override", clear_override)

    def notify(heading, body, **kw):
        calls["order"].append("notify_team")
        calls["notified"] = {"heading": heading, **kw}
        return True
    monkeypatch.setattr(main.email_sender, "notify_team", notify)

    def go(**extra):
        body = {"draft_id": "d-1", "by": "sender@wetreadwell.com", "revision_no": 1}
        body.update(extra)
        return client.post("/api/admin/publish", json=body)

    return go, calls, state


def test_an_added_name_is_written_as_an_add(publish):
    go, calls, _ = publish
    r = go(notify_add=["will@wetreadwell.com"])
    assert r.status_code == 200, r.text
    assert ("will@wetreadwell.com", "add") in calls["set"]


def test_a_muted_name_is_written_as_a_mute(publish):
    go, calls, _ = publish
    go(notify_mute=["troy@wetreadwell.com"])
    assert ("troy@wetreadwell.com", "mute") in calls["set"]


def test_the_picks_land_before_the_team_is_told(publish):
    """THE ordering assertion. notify_team reads the override table as it sends, so a pick written
    afterwards changes who hears about the NEXT thing instead of this send.

    Mutation: move the notify block below the notify_team call and this is the only test that
    notices — the proposal still sends, and the confirmation still arrives, just to the old list."""
    go, calls, _ = publish
    go(notify_add=["will@wetreadwell.com"])
    order = calls["order"]
    assert "set_override" in order and "notify_team" in order
    assert order.index("set_override") < order.index("notify_team"), order


def test_the_picks_land_after_the_proposal_row_exists(publish):
    """The FK reason. On a first send there is no portal_proposals row until this route makes one,
    so an override written before it cannot be stored at all.

    Mutation: move the notify block above the create/update branch — the insert then violates the
    foreign key on every first send, which is precisely the send nobody tests by hand."""
    go, calls, state = publish
    state["exists"] = False          # a FIRST send: no proposal row yet
    r = go(notify_add=["will@wetreadwell.com"])
    assert r.status_code == 200, r.text
    order = calls["order"]
    assert "create_row" in order, order
    assert order.index("create_row") < order.index("set_override"), order


def test_a_name_returned_to_the_roster_has_its_override_cleared(publish):
    """The chips show an effective state, so un-ticking somebody the CRM drawer had muted has to
    actually undo it — otherwise the screen says one thing and the mail does another."""
    go, calls, state = publish
    state["overrides"] = [{"email": "greg@wetreadwell.com", "mode": "mute"}]
    go(notify_add=["will@wetreadwell.com"])
    assert "greg@wetreadwell.com" in calls["cleared"]


def test_an_untouched_send_changes_no_overrides_at_all(publish):
    """A send from a page that never showed the control, or one where nothing was clicked, must not
    reconcile anything — the clear-the-rest pass would wipe the CRM drawer's per-project choices."""
    go, calls, state = publish
    state["overrides"] = [{"email": "greg@wetreadwell.com", "mode": "mute"}]
    go()
    assert calls["set"] == [] and calls["cleared"] == []


def test_a_name_in_both_lists_is_muted(publish):
    """A client bug, not a user choice. Mute wins everywhere else in the resolver, so it wins here
    rather than the outcome depending on which loop ran last."""
    go, calls, _ = publish
    go(notify_add=["will@wetreadwell.com"], notify_mute=["will@wetreadwell.com"])
    assert ("will@wetreadwell.com", "mute") in calls["set"]
    assert ("will@wetreadwell.com", "add") not in calls["set"]


def test_a_malformed_address_is_ignored_not_stored(publish):
    """parseaddr turns "not an email" into "not", which would be written into the roster and later
    handed to the mailer as a recipient."""
    go, calls, _ = publish
    go(notify_add=["not an email", "will@wetreadwell.com"])
    assert calls["set"] == [("will@wetreadwell.com", "add")]


def test_a_roster_failure_never_stops_the_proposal(publish, monkeypatch):
    """The customer's proposal has more claim on this request than the roster does."""
    go, calls, _ = publish
    def boom(*a, **k):
        raise RuntimeError("table is gone")
    monkeypatch.setattr(main.db, "set_notify_override", boom)
    r = go(notify_add=["will@wetreadwell.com"])
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
