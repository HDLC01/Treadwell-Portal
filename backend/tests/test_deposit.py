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
    `add_deposit`, `add_message` and `mark_deposit_submitted` calls are captured;
    `set_deposit_status` (the unguarded staff-only setter) stays tripwired so a
    test can assert the customer path never reaches for it."""
    from fastapi.testclient import TestClient
    import main

    calls = {"deposits": [], "status_calls": 0, "submitted": [], "messages": [], "emails": []}
    # Any token → one fake proposal (bypasses session/DB auth).
    monkeypatch.setattr(main, "_require",
                        lambda request, token: {"proposal_id": "test-pid-0001", "project_name": "Test Project"})
    monkeypatch.setattr(main.db, "add_deposit",
                        lambda *a, **k: calls["deposits"].append({"args": a, "kwargs": k}))
    monkeypatch.setattr(main.db, "add_message",
                        lambda *a, **k: calls["messages"].append({"args": a, "kwargs": k}))
    monkeypatch.setattr(main.db, "mark_deposit_submitted",
                        lambda pid: calls["submitted"].append(pid))
    monkeypatch.setattr(main.db, "set_deposit_status",
                        lambda *a, **k: calls.__setitem__("status_calls", calls["status_calls"] + 1))
    monkeypatch.setattr(main.email_sender, "notify_team",
                        lambda subject, body, *a, **k: calls["emails"].append({"subject": subject, "body": body}))

    tc = TestClient(main.app)
    tc.calls = calls
    return tc


def test_check_deposit_minimal_records_note_and_marks_submitted(client):
    r = client.post("/api/portal/tok/deposit", json={"method": "check", "note": "mailed Friday"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert len(client.calls["deposits"]) == 1
    rec = client.calls["deposits"][0]
    assert rec["args"][1] == "check"                    # method (positional)
    assert rec["args"][5] == "mailed Friday"            # note (positional)
    assert rec["kwargs"].get("routing_number") is None
    assert rec["kwargs"].get("account_number") is None
    # Visible to staff: the board card leaves 'pending'. Only via the guarded
    # helper — never the raw setter, which could overwrite a verified 'received'.
    assert client.calls["submitted"] == ["test-pid-0001"]
    assert client.calls["status_calls"] == 0


def test_ach_stores_full_numbers_and_derives_mask(client):
    r = client.post("/api/portal/tok/deposit",
                    json={"method": "ach", "account_name": "Payer LLC",
                          "routing_number": "021000021", "account_number": "000123456789",
                          "account_type": "checking"})
    assert r.status_code == 200 and r.json()["ok"] is True
    rec = client.calls["deposits"][0]
    assert rec["args"][2] == "Payer LLC"                # account_name (positional)
    assert rec["args"][4] == "••••6789"                 # masked_ref derived server-side (positional)
    assert rec["kwargs"]["routing_number"] == "021000021"
    assert rec["kwargs"]["account_number"] == "000123456789"
    assert client.calls["submitted"] == ["test-pid-0001"]
    assert client.calls["status_calls"] == 0


def test_deposit_chat_row_is_customer_authored_deposit_submitted(client):
    """The chat line is what carries the deposit into the staff bell feed, which
    only selects customer-authored rows — so author_kind/msg_type are load-bearing,
    not cosmetic."""
    r = client.post("/api/portal/tok/deposit", json={"method": "check"})
    assert r.status_code == 200
    assert len(client.calls["messages"]) == 1
    args, kwargs = client.calls["messages"][0]["args"], client.calls["messages"][0]["kwargs"]
    assert args[0] == "test-pid-0001"
    assert args[1] == "customer"                        # author_kind (positional)
    assert kwargs["msg_type"] == "deposit_submitted"
    assert "Deposit initiated" in args[3]               # body (positional)


def test_resubmission_cannot_downgrade_a_received_deposit(client):
    """A customer who resends details after staff verified the money must not
    un-receive it. The guard lives in SQL (mark_deposit_submitted's where-clause),
    so the endpoint's contract is simply that it never calls the raw setter."""
    for _ in range(2):
        r = client.post("/api/portal/tok/deposit", json={"method": "check"})
        assert r.status_code == 200
    assert client.calls["submitted"] == ["test-pid-0001", "test-pid-0001"]
    assert client.calls["status_calls"] == 0


def test_ach_normalizes_separators(client):
    r = client.post("/api/portal/tok/deposit",
                    json={"method": "ach", "account_name": "Payer LLC",
                          "routing_number": "021-000-021", "account_number": "0001 2345 6789",
                          "account_type": "savings"})
    assert r.status_code == 200
    kw = client.calls["deposits"][0]["kwargs"]
    assert kw["routing_number"] == "021000021"
    assert kw["account_number"] == "000123456789"


def test_ach_bad_routing_rejected(client):
    for bad in ("123", "ab-", ""):                      # under 4 digits, no digits, empty
        client.calls["deposits"].clear()
        r = client.post("/api/portal/tok/deposit",
                        json={"method": "ach", "account_name": "Payer LLC",
                              "routing_number": bad, "account_number": "000123456789"})
        assert r.status_code == 400, bad
        assert client.calls["deposits"] == []
    assert client.calls["status_calls"] == 0


def test_ach_off_length_routing_accepted(client):
    # Exact-length cap lifted per Hanz ("don't limit the number to 9 digits ...
    # because it might change") — routing formats vary by bank/country, so only the
    # 4-digit floor survives. 8/10/12 digits were all rejected before.
    for ok_routing in ("12345678", "0210000210", "021000021000"):
        client.calls["deposits"].clear()
        r = client.post("/api/portal/tok/deposit",
                        json={"method": "ach", "account_name": "Payer LLC",
                              "routing_number": ok_routing, "account_number": "000123456789",
                              "account_type": "checking"})
        assert r.status_code == 200, ok_routing
        assert client.calls["deposits"][0]["kwargs"]["routing_number"] == ok_routing


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
    # account (previously rejected) is now accepted. The routing number lost its
    # exact-9 rule the same way and for the same reason (the format might change);
    # both now keep only a 4-digit floor to reject an empty/garbage field.
    client.calls["deposits"].clear()
    r = client.post("/api/portal/tok/deposit",
                    json={"method": "ach", "account_name": "Payer LLC",
                          "routing_number": "021000021", "account_number": "012345678901234567",
                          "account_type": "checking"})
    assert r.status_code == 200
    assert len(client.calls["deposits"]) == 1


def test_ach_account_type_required_and_stored(client):
    base = {"method": "ach", "account_name": "Payer LLC",
            "routing_number": "021000021", "account_number": "000123456789"}
    # Missing / invalid account type is rejected.
    for at in (None, "", "bogus"):
        client.calls["deposits"].clear()
        body = dict(base) if at is None else {**base, "account_type": at}
        r = client.post("/api/portal/tok/deposit", json=body)
        assert r.status_code == 400, at
        assert client.calls["deposits"] == []
    # Valid choice is stored (checking/savings).
    for at in ("checking", "savings"):
        client.calls["deposits"].clear()
        r = client.post("/api/portal/tok/deposit", json={**base, "account_type": at})
        assert r.status_code == 200, at
        assert client.calls["deposits"][0]["kwargs"]["account_type"] == at


def test_ach_email_masks_account_number(client):
    r = client.post("/api/portal/tok/deposit",
                    json={"method": "ach", "account_name": "Payer LLC",
                          "routing_number": "021000021", "account_number": "000123456789",
                          "account_type": "checking"})
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


def test_mark_deposit_submitted_guards_received_in_sql(monkeypatch):
    """The no-downgrade rule is a where-clause, not a read-then-write, so two
    concurrent requests can't race past it. Asserted on the SQL because there is no
    DB in unit tests (the live behaviour is covered by the staging smoke)."""
    import db as dbmod

    seen = {}
    monkeypatch.setattr(dbmod, "execute", lambda sql, params=(): seen.update(sql=sql, params=params))
    dbmod.mark_deposit_submitted("p1")
    sql = " ".join(seen["sql"].split())
    assert "deposit_status='submitted'" in sql
    assert "deposit_status <> 'received'" in sql        # a verified deposit is never downgraded
    assert seen["params"] == ("p1",)
