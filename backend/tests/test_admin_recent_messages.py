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
    assert m["msg_type"] == "text"                       # default for pre-msg_type rows


def test_recent_msg_carries_msg_type():
    """The staff side styles a deposit differently from a question, so the kind has
    to survive the serializer — it used to drop off here."""
    m = main._recent_msg({"id": 9, "body": "Deposit initiated — …",
                          "msg_type": "deposit_submitted", "created_at": None})
    assert m["msg_type"] == "deposit_submitted"


# ── the feed query itself ────────────────────────────────────────────────────
def test_feed_query_selects_deposit_submissions(monkeypatch):
    """A deposit submission is the one non-chat thing that must reach the bell: it
    used to arrive as author_kind='staff'/'system' and was filtered straight out,
    leaving one email as the only signal a customer had paid. Asserted against the
    SQL because there is no DB in unit tests (the live query is covered by the
    staging smoke) — the point is that the predicate can't silently narrow again."""
    seen = {}
    monkeypatch.setattr(main.db, "qall",
                        lambda sql, params=(): seen.update(sql=sql, params=params) or [])
    main.db.list_recent_customer_messages(limit=5)
    sql = " ".join(seen["sql"].split())
    assert "q.author_kind='customer'" in sql             # staff rows still excluded
    assert "q.msg_type in ('text','deposit_submitted')" in sql
    assert "q.msg_type" in sql.split("select")[1]        # kind is returned, not just filtered on
    assert seen["params"] == (5,)


def test_unread_counts_ignores_deposit_submissions(monkeypatch):
    """Nothing clears a deposit the way a staff reply clears a question, so counting
    it here would pin the board badge on forever. It reaches staff via the bell feed
    and deposit_status instead."""
    seen = {}
    monkeypatch.setattr(main.db, "qall",
                        lambda sql, params=(): seen.update(sql=sql) or [])
    main.db.unread_counts()
    assert "deposit_submitted" not in seen["sql"]
    assert "q.msg_type='text'" in " ".join(seen["sql"].split())


# ── endpoint auth gate ───────────────────────────────────────────────────────
import pytest


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(main.config, "SERVICE_TOKEN", "secret-tok")
    monkeypatch.setattr(main.db, "list_recent_customer_messages",
                        lambda limit=25: [
                            {"id": 8, "proposal_id": "p8", "project_name": "P8",
                             "customer_name": "D", "author_email": "d@x.com",
                             "msg_type": "deposit_submitted",
                             "body": "Deposit initiated — a check is on its way for P8.",
                             "created_at": dt.datetime(2026, 7, 24, 9, 5, 0)},
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
    assert len(j["messages"]) == 2
    assert j["messages"][1]["id"] == 7
    assert j["messages"][1]["body"] == "hello"
    assert j["messages"][1]["project_name"] == "P7"
    assert j["messages"][1]["msg_type"] == "text"


def test_recent_messages_include_deposit_submissions(client):
    """End of the chain the deposit travels: endpoint → staff bell. Newest-first,
    so the deposit leads."""
    j = client.get("/api/admin/recent-messages", headers={"X-Service-Token": "secret-tok"}).json()
    dep = j["messages"][0]
    assert dep["id"] == 8 and dep["msg_type"] == "deposit_submitted"
    assert dep["project_name"] == "P8"
    assert "Deposit initiated" in dep["body"]
