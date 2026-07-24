"""GET /api/admin/recent-messages — the cross-project customer-message feed that
drives the staff tool's notification bell + toast. The DB query itself (customer
text only, newest-first) is exercised by the staging smoke; here we pin the pure
serializer shape/truncation and the SERVICE_TOKEN gate."""
import datetime as dt

import main


# ── _recent_msg (pure) ───────────────────────────────────────────────────────
def test_recent_msg_shape():
    row = {"id": 42, "proposal_id": "pid-1", "project_name": "Warehouse Floor",
           "customer_name": "Dana Lee", "author_email": "dana@acme.com",
           "body": "  When can you start?  ", "created_at": dt.datetime(2026, 7, 24, 15, 30, 0)}
    m = main._recent_msg(row)
    assert m["id"] == 42
    assert m["proposal_id"] == "pid-1"
    assert m["project_name"] == "Warehouse Floor"
    assert m["customer_name"] == "Dana Lee"
    assert m["author_email"] == "dana@acme.com"
    assert m["body"] == "When can you start?"           # stripped
    assert m["created_at"].startswith("2026-07-24T15:30:00")


def test_recent_msg_truncates_long_body():
    long_body = "x" * 500
    m = main._recent_msg({"id": 1, "body": long_body, "created_at": None})
    assert len(m["body"]) == main._RECENT_MSG_PREVIEW    # capped
    assert m["body"].endswith("…")
    assert m["created_at"] is None


def test_recent_msg_handles_missing_fields():
    m = main._recent_msg({})
    assert m["id"] is None and m["proposal_id"] is None
    assert m["body"] == ""
    assert m["created_at"] is None


# ── endpoint auth gate ───────────────────────────────────────────────────────
import pytest


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(main.config, "SERVICE_TOKEN", "secret-tok")
    monkeypatch.setattr(main.db, "list_recent_customer_messages",
                        lambda limit=25: [
                            {"id": 7, "proposal_id": "p7", "project_name": "P7",
                             "customer_name": "C", "author_email": "c@x.com",
                             "body": "hello", "created_at": dt.datetime(2026, 7, 24, 9, 0, 0)},
                        ])
    return TestClient(main.app)


def test_recent_messages_requires_service_token(client):
    r = client.get("/api/admin/recent-messages")                       # no header
    assert r.status_code == 401
    r = client.get("/api/admin/recent-messages", headers={"X-Service-Token": "wrong"})
    assert r.status_code == 401


def test_recent_messages_returns_feed(client):
    r = client.get("/api/admin/recent-messages", headers={"X-Service-Token": "secret-tok"})
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True
    assert len(j["messages"]) == 1
    assert j["messages"][0]["id"] == 7
    assert j["messages"][0]["body"] == "hello"
    assert j["messages"][0]["project_name"] == "P7"
