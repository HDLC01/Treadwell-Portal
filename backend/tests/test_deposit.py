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

    calls = {"deposits": [], "status_calls": 0, "emails": []}
    # Any token → one fake proposal (bypasses session/DB auth).
    monkeypatch.setattr(main, "_require",
                        lambda request, token: {"proposal_id": "test-pid-0001", "project_name": "Test Project"})
    monkeypatch.setattr(main.db, "add_deposit",
                        lambda *a, **k: calls["deposits"].append({"args": a, "kwargs": k}))
    monkeypatch.setattr(main.db, "add_message", lambda *a, **k: None)
    monkeypatch.setattr(main.db, "set_deposit_status",
                        lambda *a, **k: calls.__setitem__("status_calls", calls["status_calls"] + 1))
    monkeypatch.setattr(main.email_sender, "notify_team",
                        lambda subject, body, *a, **k: calls["emails"].append({"subject": subject, "body": body}))

    tc = TestClient(main.app)
    tc.calls = calls
    return tc


def test_check_deposit_minimal_records_note_and_leaves_status(client):
    r = client.post("/api/portal/tok/deposit", json={"method": "check", "note": "mailed Friday"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert len(client.calls["deposits"]) == 1
    rec = client.calls["deposits"][0]
    assert rec["args"][1] == "check"                    # method (positional)
    assert rec["args"][5] == "mailed Friday"            # note (positional)
    assert rec["kwargs"].get("routing_number") is None
    assert rec["kwargs"].get("account_number") is None
    # A customer submission must NEVER flip deposit status — staff verify manually.
    assert client.calls["status_calls"] == 0


def test_ach_stores_full_numbers_and_derives_mask(client):
    r = client.post("/api/portal/tok/deposit",
                    json={"method": "ach", "account_name": "Payer LLC",
                          "routing_number": "021000021", "account_number": "000123456789"})
    assert r.status_code == 200 and r.json()["ok"] is True
    rec = client.calls["deposits"][0]
    assert rec["args"][2] == "Payer LLC"                # account_name (positional)
    assert rec["args"][4] == "••••6789"                 # masked_ref derived server-side (positional)
    assert rec["kwargs"]["routing_number"] == "021000021"
    assert rec["kwargs"]["account_number"] == "000123456789"
    assert client.calls["status_calls"] == 0


def test_ach_normalizes_separators(client):
    r = client.post("/api/portal/tok/deposit",
                    json={"method": "ach", "account_name": "Payer LLC",
                          "routing_number": "021-000-021", "account_number": "0001 2345 6789"})
    assert r.status_code == 200
    kw = client.calls["deposits"][0]["kwargs"]
    assert kw["routing_number"] == "021000021"
    assert kw["account_number"] == "000123456789"


def test_ach_bad_routing_rejected(client):
    for bad in ("12345678", "0210000210", ""):          # 8 digits, 10 digits, empty
        client.calls["deposits"].clear()
        r = client.post("/api/portal/tok/deposit",
                        json={"method": "ach", "account_name": "Payer LLC",
                              "routing_number": bad, "account_number": "000123456789"})
        assert r.status_code == 400, bad
        assert client.calls["deposits"] == []
    assert client.calls["status_calls"] == 0


def test_ach_short_account_rejected(client):
    for bad in ("123", ""):                               # under 4 digits still rejected
        client.calls["deposits"].clear()
        r = client.post("/api/portal/tok/deposit",
                        json={"method": "ach", "account_name": "Payer LLC",
                              "routing_number": "021000021", "account_number": bad})
        assert r.status_code == 400, bad
        assert client.calls["deposits"] == []


def test_ach_long_account_accepted(client):
    # Upper cap removed per Will ("don't limit the account number") — an 18-digit
    # account (previously rejected) is now accepted. Routing still must be 9 digits.
    client.calls["deposits"].clear()
    r = client.post("/api/portal/tok/deposit",
                    json={"method": "ach", "account_name": "Payer LLC",
                          "routing_number": "021000021", "account_number": "012345678901234567"})
    assert r.status_code == 200
    assert len(client.calls["deposits"]) == 1


def test_ach_email_masks_account_number(client):
    r = client.post("/api/portal/tok/deposit",
                    json={"method": "ach", "account_name": "Payer LLC",
                          "routing_number": "021000021", "account_number": "000123456789"})
    assert r.status_code == 200
    assert len(client.calls["emails"]) == 1
    body = client.calls["emails"][0]["body"]
    assert "000123456789" not in body                   # full account never in the email
    assert "••••6789" in body                           # masked account shown
    assert "021000021" in body                          # routing may be full


def test_invalid_method_rejected(client):
    r = client.post("/api/portal/tok/deposit", json={"method": "wire"})
    assert r.status_code == 400
    assert client.calls["deposits"] == []
