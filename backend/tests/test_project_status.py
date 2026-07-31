"""The customer's way out: "we're delayed" or "we're not moving forward".

This is the piece that makes the automation humane. A customer who has gone quiet is
usually waiting on a budget or has gone elsewhere — one click to say so stops the
reminders being noise, and tells the estimator something they'd otherwise guess at.
"""
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    import main

    calls = {"paused": [], "closed": [], "resumed": [], "messages": [],
             "followups": [], "notified": []}
    state = {"proposal": None}

    monkeypatch.setattr(main, "_require", lambda request, token: state["proposal"])
    monkeypatch.setattr(main, "_session_email", lambda request: "cust@x.com")
    monkeypatch.setattr(main.ratelimit, "allow_ip", lambda *a, **k: True)
    monkeypatch.setattr(main.db, "pause_followups",
                        lambda pid, until: calls["paused"].append(until))
    monkeypatch.setattr(main.db, "resume_followups",
                        lambda pid: calls["resumed"].append(pid))
    monkeypatch.setattr(main.db, "close_lost",
                        lambda pid, reason: calls["closed"].append(reason) or True)
    monkeypatch.setattr(main.db, "add_message",
                        lambda pid, kind, who, body, **k: calls["messages"].append(
                            {"kind": kind, "body": body, "type": k.get("msg_type"),
                             "meta": k.get("meta")}))
    monkeypatch.setattr(main.db, "add_followup",
                        lambda pid, kind, detail=None, by=None: calls["followups"].append(
                            {"kind": kind, "detail": detail}))
    monkeypatch.setattr(main, "_notify_staff_status",
                        lambda p, subject, body: calls["notified"].append(subject))

    tc = TestClient(main.app)
    tc.calls = calls
    tc.state = state
    tc.main = main
    tc.monkeypatch = monkeypatch
    return tc


def _p(**over):
    p = {"proposal_id": "p1", "token": "tok", "customer_email": "cust@x.com",
         "customer_name": "Cust", "project_name": "Westport",
         "proposal_status": "viewed", "followup_paused_until": None,
         "assigned_estimator": "kyle@wetreadwell.com"}
    p.update(over)
    return p


def post(client, body):
    return client.post("/api/portal/tok/project-status", json=body)


# ── delayed ──────────────────────────────────────────────────────────────────
def test_delayed_pauses_the_cadence_and_tells_the_estimator(client):
    client.state["proposal"] = _p()
    r = post(client, {"status": "delayed", "months": 2})
    assert r.status_code == 200, r.text
    assert r.json()["project_status"]["paused_until"]
    assert len(client.calls["paused"]) == 1
    # The thread records it as the CUSTOMER's statement, not ours.
    msg = client.calls["messages"][0]
    assert msg["kind"] == "customer" and msg["type"] == "status_update"
    assert msg["meta"]["months"] == 2
    assert client.calls["followups"][0]["kind"] == "customer_status"
    assert "delayed" in client.calls["notified"][0].lower()


def test_the_pause_lands_the_right_number_of_months_out(client):
    client.state["proposal"] = _p()
    post(client, {"status": "delayed", "months": 3})
    until = client.calls["paused"][0]
    today = date.today()
    # Roughly three months, allowing for month lengths — the exact arithmetic is
    # covered in test_followup_rules.
    assert (until - today).days >= 85
    assert (until - today).days <= 95


def test_four_means_four_plus_and_reads_that_way(client):
    client.state["proposal"] = _p()
    post(client, {"status": "delayed", "months": 4})
    assert "4+ months" in client.calls["messages"][0]["body"]


def test_only_the_offered_windows_are_accepted(client):
    client.state["proposal"] = _p()
    for bad in (0, 5, 99, -1, "two"):
        r = post(client, {"status": "delayed", "months": bad})
        assert r.status_code == 400, bad
    assert client.calls["paused"] == []


def test_saying_the_same_thing_twice_does_not_re_notify(client):
    """A customer clicking the link again from an older email must not fire a second
    "project delayed" email at the estimator."""
    import followup_rules as fr
    # From the BUSINESS date, not the machine's — this box is a day ahead of Chicago,
    # and the handler quite rightly works in Treadwell's timezone.
    until = fr.add_months(fr.business_today(datetime.now(timezone.utc)), 2)
    client.state["proposal"] = _p(followup_paused_until=until.isoformat())
    r = post(client, {"status": "delayed", "months": 2})
    assert r.status_code == 200
    assert client.calls["notified"] == [] and client.calls["paused"] == []


# ── not moving forward ───────────────────────────────────────────────────────
def test_not_moving_forward_closes_the_opportunity(client):
    client.state["proposal"] = _p()
    r = post(client, {"status": "not_moving_forward", "reason": "price"})
    assert r.status_code == 200 and r.json()["project_status"]["closed"] is True
    assert client.calls["closed"] == ["price"]
    assert "Price" in client.calls["messages"][0]["body"]
    assert "closed" in client.calls["notified"][0].lower()


def test_a_reason_is_optional_but_must_be_one_of_ours_if_given(client):
    client.state["proposal"] = _p()
    assert post(client, {"status": "not_moving_forward"}).status_code == 200
    client.state["proposal"] = _p()
    r = post(client, {"status": "not_moving_forward", "reason": "because"})
    assert r.status_code == 400 and r.json()["error"] == "invalid_reason"


def test_closing_an_already_closed_proposal_is_a_quiet_no_op(client):
    client.state["proposal"] = _p(proposal_status="closed_lost")
    r = post(client, {"status": "not_moving_forward", "reason": "timing"})
    assert r.status_code == 200 and r.json()["project_status"]["closed"] is True
    assert client.calls["closed"] == [] and client.calls["notified"] == []


def test_an_approved_proposal_cannot_be_marked_lost(client):
    """A signed proposal is a win. A stray click on an old email must not erase it."""
    client.state["proposal"] = _p(proposal_status="approved")
    r = post(client, {"status": "not_moving_forward", "reason": "price"})
    assert r.status_code == 400 and r.json()["error"] == "already_approved"
    assert client.calls["closed"] == []


def test_the_customers_free_text_is_captured_but_escaped(client):
    client.state["proposal"] = _p()
    post(client, {"status": "not_moving_forward", "reason": "other",
                  "note": "<script>alert(1)</script>"})
    assert client.calls["followups"][0]["detail"]["note"] == "<script>alert(1)</script>"


# ── resume ───────────────────────────────────────────────────────────────────
def test_a_customer_can_put_the_project_back_on(client):
    client.state["proposal"] = _p(followup_paused_until="2026-12-01")
    r = post(client, {"status": "resume"})
    assert r.status_code == 200 and r.json()["project_status"]["paused_until"] is None
    assert client.calls["resumed"] == ["p1"]


# ── guards ───────────────────────────────────────────────────────────────────
def test_an_unknown_status_is_rejected(client):
    client.state["proposal"] = _p()
    r = post(client, {"status": "maybe"})
    assert r.status_code == 400 and r.json()["error"] == "invalid_status"


def test_an_unauthenticated_caller_gets_nothing(client):
    client.state["proposal"] = None
    assert post(client, {"status": "delayed", "months": 1}).status_code == 401


def test_the_endpoint_is_rate_limited(client):
    """It fans out email to the estimator and the roster, so unlike /questions it is
    worth the cheap IP guard."""
    client.state["proposal"] = _p()
    client.monkeypatch.setattr(client.main.ratelimit, "allow_ip", lambda *a, **k: False)
    r = post(client, {"status": "delayed", "months": 1})
    assert r.status_code == 429 and client.calls["paused"] == []
