"""Customer notification bell.

Two things matter most: the feed is scoped to the caller's own email (one
customer must never see another's activity), and the read marker is PER READER —
unlike the staff bell's single shared marker, which would leak read state
between customers if copied here.
"""
import datetime as dt

import pytest
from fastapi.testclient import TestClient

import main

NOW = dt.datetime(2026, 7, 28, 12, 0, 0, tzinfo=dt.timezone.utc)
OLD = NOW - dt.timedelta(days=2)


def _row(i, kind, body, pid="p1", when=NOW):
    return {"id": i, "proposal_id": pid, "msg_type": kind, "body": body,
            "created_at": when, "project_name": "Westport", "token": "tok123"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "_session_email", lambda request: "dana@acme.com")
    return TestClient(main.app)


def test_signed_out_gets_an_empty_feed(monkeypatch):
    monkeypatch.setattr(main, "_session_email", lambda request: None)
    j = TestClient(main.app).get("/api/me/notifications").json()
    assert j == {"ok": True, "authed": False, "items": [], "unread": 0}


def test_feed_shapes_each_event_kind(client, monkeypatch):
    monkeypatch.setattr(main.db, "get_read_state", lambda e: {})
    monkeypatch.setattr(main.db, "list_customer_events", lambda e, limit=30: [
        _row(3, "text", "Thanks — we can start Monday."),
        _row(2, "deposit_request", "Deposit invoice TW-INV-01001 — $3,743.25 due."),
        _row(1, "system", "Deposit received — thank you! Please add your project contacts."),
    ])
    items = client.get("/api/me/notifications").json()["items"]
    assert items[0]["title"] == "Treadwell replied · Westport"
    assert items[1]["title"] == "Deposit invoice · Westport"
    assert items[2]["title"] == "Deposit received · Westport"      # split on the em-dash
    assert items[2]["body"].startswith("thank you!")
    assert items[1]["link"] == "/p/tok123#proposal"                 # invoices open the proposal view


def test_unread_is_per_reader_and_respects_last_seen(client, monkeypatch):
    monkeypatch.setattr(main.db, "get_read_state", lambda e: {"p1": NOW - dt.timedelta(hours=1)})
    monkeypatch.setattr(main.db, "list_customer_events", lambda e, limit=30: [
        _row(2, "text", "new one", when=NOW),          # after last_seen  → unread
        _row(1, "text", "old one", when=OLD),          # before           → read
    ])
    j = client.get("/api/me/notifications").json()
    assert [i["unread"] for i in j["items"]] == [True, False]
    assert j["unread"] == 1


def test_everything_is_unread_when_never_seen(client, monkeypatch):
    monkeypatch.setattr(main.db, "get_read_state", lambda e: {})
    monkeypatch.setattr(main.db, "list_customer_events", lambda e, limit=30: [_row(1, "text", "hi")])
    assert client.get("/api/me/notifications").json()["unread"] == 1


def test_feed_is_scoped_to_the_callers_email(client, monkeypatch):
    """The query must be asked for the SESSION's email — never a client-supplied
    one — or a customer could read another's feed."""
    seen = {}
    monkeypatch.setattr(main.db, "get_read_state", lambda e: seen.setdefault("read", e) and {} or {})
    monkeypatch.setattr(main.db, "list_customer_events",
                        lambda e, limit=30: seen.setdefault("events", e) or [])
    client.get("/api/me/notifications?email=someone@else.com")
    assert seen["events"] == "dana@acme.com"
    assert seen["read"] == "dana@acme.com"


def test_a_broken_feed_never_breaks_the_page(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(main.db, "get_read_state", boom)
    j = client.get("/api/me/notifications").json()
    assert j["ok"] is True and j["items"] == [] and j["unread"] == 0


def test_seen_marks_only_the_callers_own_proposals(client, monkeypatch):
    captured = {}

    def _by_email(e):
        captured["email"] = e
        return [{"proposal_id": "p1"}, {"proposal_id": "p2"}]

    monkeypatch.setattr(main.db, "list_proposals_by_email", _by_email)
    monkeypatch.setattr(main.db, "mark_read",
                        lambda e, pids: captured.update(marked=(e, pids)))
    assert client.post("/api/me/notifications/seen").json()["ok"] is True
    assert captured["marked"] == ("dana@acme.com", ["p1", "p2"])


def test_seen_requires_a_session(monkeypatch):
    monkeypatch.setattr(main, "_session_email", lambda request: None)
    assert TestClient(main.app).post("/api/me/notifications/seen").status_code == 401


# The two scheduling tests that sat here were removed on 2026-08-11 with the /scheduled
# endpoint itself. One checked that flipping the schedule status posted a system line to the
# thread; the other checked that a failing chat write did not fail the status change.
#
# The second looked like a general invariant worth re-pointing at another milestone, and at the
# time it was not: /scheduled and /approve were the only endpoints that wrapped add_message in a
# try, so the same test aimed at deposit-received would have failed honestly rather than passed.
#
# It IS the invariant now. Marking a deposit received set the status before an unguarded chat
# write, so a database blip left the money recorded while the request 500s — the rep reads
# "Couldn't mark received" on an action that half succeeded. Guarded on 2026-08-11 and pinned by
# test_milestone_notifications.py::test_the_money_is_never_undone_by_a_failed_chat_write.
#
# test_milestone_notifications.py::test_the_scheduled_route_is_gone pins the removal.

