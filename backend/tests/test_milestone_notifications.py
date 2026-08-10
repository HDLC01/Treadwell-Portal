"""Every milestone must reach all three channels: the chat thread, the staff
roster, and the CUSTOMER by email.

Before this, no status event emailed the customer at all — they'd approve a
price, or send a deposit, and hear nothing back, and deposit-received did not even
notify the team.

Scheduling was a fourth milestone until 2026-08-11, when it was removed from both
apps at Hanz's request, the customer email included.
"""
import pytest
from fastapi.testclient import TestClient

import main

PROP = {"proposal_id": "p1", "token": "tok", "project_name": "Westport",
        "customer_email": "dana@acme.com", "proposal_status": "approved"}


@pytest.fixture
def wired(monkeypatch):
    """Capture all three channels."""
    box = {"chat": [], "team": [], "customer": []}
    monkeypatch.setattr(main.db, "add_message",
                        lambda pid, kind, who, body, **k: box["chat"].append((body, k.get("msg_type"))))
    monkeypatch.setattr(main.email_sender, "notify_team",
                        lambda subject, body, **k: box["team"].append(subject))
    monkeypatch.setattr(main.email_sender, "send_customer_update",
                        lambda e, url, proj, heading, body, **k: box["customer"].append((e, heading)))
    monkeypatch.setattr(main.email_sender, "proposal_reply_to", lambda t: None)
    monkeypatch.setattr(main.db, "get_recipients", lambda pid: ["dana@acme.com", "ap@acme.com"])
    monkeypatch.setattr(main.db, "get_proposal", lambda pid: dict(PROP))
    monkeypatch.setattr(main, "_admin_ok", lambda request: True)
    return box


def test_deposit_received_reaches_all_three_channels(wired, monkeypatch):
    monkeypatch.setattr(main.db, "set_deposit_status", lambda pid, s: None)
    r = TestClient(main.app).post("/api/admin/proposal/p1/deposit-received")
    assert r.status_code == 200
    assert wired["chat"] and wired["chat"][0][1] == "system"
    assert any("RECEIVED" in s for s in wired["team"])          # team was silent before
    # every recipient, not just the primary
    assert [e for e, _ in wired["customer"]] == ["dana@acme.com", "ap@acme.com"]


def test_customer_email_failure_never_fails_the_action(wired, monkeypatch):
    """A mail hiccup must not break the thing the customer just did.

    This used to fire at /scheduled, which was removed on 2026-08-11 with the rest of scheduling.
    The invariant it guards has nothing to do with scheduling though, so it moved to a milestone
    that still exists rather than being deleted alongside the feature: staff marking a deposit
    received must succeed even when Resend is down, or the money looks unrecorded.
    """
    monkeypatch.setattr(main.db, "set_deposit_status", lambda pid, s: None)
    monkeypatch.setattr(main.email_sender, "send_customer_update",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("resend down")))
    r = TestClient(main.app).post("/api/admin/proposal/p1/deposit-received")
    assert r.json()["ok"] is True


def test_the_scheduled_route_is_gone(wired):
    """Scheduling was removed in both apps on 2026-08-11, the customer email included. Pinned so
    the route cannot come back on its own: a status staff can set but nobody displays would leave
    every job reading one step short of done."""
    r = TestClient(main.app).post("/api/admin/proposal/p1/scheduled")
    assert r.status_code == 404, "the /scheduled endpoint is back"


def test_notify_customer_falls_back_to_the_primary_email(monkeypatch):
    sent = []
    monkeypatch.setattr(main.db, "get_recipients", lambda pid: [])      # no recipient rows
    monkeypatch.setattr(main.email_sender, "proposal_reply_to", lambda t: None)
    monkeypatch.setattr(main.email_sender, "send_customer_update",
                        lambda e, *a, **k: sent.append(e))
    main._notify_customer(dict(PROP), "Heading", "<p>x</p>")
    assert sent == ["dana@acme.com"]
