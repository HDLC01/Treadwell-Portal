"""Deposit reference code (proposals.deposit_ref, pure logic) + the POST /deposit
endpoint (check vs ACH). The endpoint tests use a FastAPI TestClient with the DB
+ auth + email seams monkeypatched — they run in CI (requirements-dev pulls in the
full runtime deps); the end-to-end path is also covered by the staging smoke."""
import proposals


def test_ref_first_eight_alnum_uppercased():
    assert proposals.deposit_ref("8dbe3385-be1d-4081-bdd5-96a51868187d") == "TW-8DBE3385"


def test_ref_strips_non_alnum():
    assert proposals.deposit_ref("---a.b_c9---") == "TW-ABC9"   # dashes/dots/underscores dropped


def test_ref_is_stable():
    pid = "43f891da-bb9a-40c9-b927-0788058317d9"
    assert proposals.deposit_ref(pid) == proposals.deposit_ref(pid)


def test_ref_empty_or_none_falls_back():
    assert proposals.deposit_ref("") == "TW-DEPOSIT"
    assert proposals.deposit_ref(None) == "TW-DEPOSIT"
    assert proposals.deposit_ref("----") == "TW-DEPOSIT"


# ── POST /api/portal/{token}/deposit ─────────────────────────────────────────
import pytest


@pytest.fixture
def client(monkeypatch):
    """TestClient over the real app with the DB/auth/email seams stubbed.
    `add_deposit` calls are captured; `set_deposit_status` is tripwired so a test
    can assert a customer submission never flips deposit status (staff-only)."""
    from fastapi.testclient import TestClient
    import main

    calls = {"deposits": [], "status_calls": 0}
    # Any token → one fake proposal (bypasses session/DB auth).
    monkeypatch.setattr(main, "_require",
                        lambda request, token: {"proposal_id": "test-pid-0001", "project_name": "Test Project"})
    monkeypatch.setattr(main.db, "add_deposit",
                        lambda *a, **k: calls["deposits"].append({"args": a, "kwargs": k}))
    monkeypatch.setattr(main.db, "add_message", lambda *a, **k: None)
    monkeypatch.setattr(main.db, "set_deposit_status",
                        lambda *a, **k: calls.__setitem__("status_calls", calls["status_calls"] + 1))
    monkeypatch.setattr(main.email_sender, "notify_team", lambda *a, **k: None)

    tc = TestClient(main.app)
    tc.calls = calls
    return tc


def test_check_deposit_records_check_number_and_leaves_status(client):
    r = client.post("/api/portal/tok/deposit",
                    json={"method": "check", "check_number": "  10 42 ",
                          "account_name": "Acme LLC", "bank_name": "First Bank"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert len(client.calls["deposits"]) == 1
    rec = client.calls["deposits"][0]
    assert rec["args"][1] == "check"                    # method (positional)
    assert rec["args"][2] == "Acme LLC"                 # account_name (positional)
    assert rec["kwargs"]["check_number"] == "10 42"     # ends trimmed + inner whitespace collapsed
    # A customer submission must NEVER flip deposit status — staff verify manually.
    assert client.calls["status_calls"] == 0


def test_ach_deposit_passes_sent_to_fields(client):
    r = client.post("/api/portal/tok/deposit",
                    json={"method": "ach", "account_name": "Payer",
                          "sent_to_beneficiary": "Treadwell", "sent_to_routing": "123456789"})
    assert r.status_code == 200
    kw = client.calls["deposits"][0]["kwargs"]
    assert kw["sent_to_beneficiary"] == "Treadwell"
    assert kw["sent_to_routing"] == "123456789"
    assert kw.get("check_number") is None
    assert client.calls["status_calls"] == 0


def test_invalid_method_rejected(client):
    r = client.post("/api/portal/tok/deposit", json={"method": "wire"})
    assert r.status_code == 400
    assert client.calls["deposits"] == []
