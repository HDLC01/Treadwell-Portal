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
from cryptography.fernet import Fernet

import bank_crypto
import config


@pytest.fixture
def client(monkeypatch):
    """TestClient over the real app with the DB/auth/email seams stubbed.
    `add_deposit`, `add_message` and `mark_deposit_submitted` calls are captured;
    `set_deposit_status` (the unguarded staff-only setter) stays tripwired so a
    test can assert the customer path never reaches for it.

    PORTAL_BANK_KEY is set to a fresh, real Fernet key (2026-09-29) so the ACH branch's
    bank_crypto.encrypt() calls run for REAL rather than being stubbed out -- a test asserting
    ciphertext is stored, or that it decrypts back to what the customer typed, has to exercise the
    actual encryption, not a mock of it. Tests exercising the fail-closed path override this back
    to empty/garbage per-test."""
    from fastapi.testclient import TestClient
    import main

    monkeypatch.setattr(config, "PORTAL_BANK_KEY", Fernet.generate_key().decode("ascii"))

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
    tc.main = main
    tc.monkeypatch = monkeypatch
    return tc


def _proposal(**over):
    p = {"proposal_id": "test-pid-0001", "project_name": "Test Project"}
    p.update(over)
    return p


def test_deposit_rejected_when_not_required(client):
    """Staff sent this job without a deposit, so there is nothing to pay. Accepting
    bank details here would record money nobody asked for."""
    client.monkeypatch.setattr(client.main, "_require",
                               lambda request, token: _proposal(deposit_required=False))
    r = client.post("/api/portal/tok/deposit", json={"method": "check", "note": "x"})
    assert r.status_code == 400 and r.json()["error"] == "deposit_not_required"
    assert client.calls["deposits"] == [] and client.calls["submitted"] == []


def test_deposit_accepted_when_not_required_but_invoice_exists(client):
    """Staff changed their mind and invoiced manually. The customer must be able to
    pay it — an issued invoice outranks the flag."""
    client.monkeypatch.setattr(client.main, "_require",
                               lambda request, token: _proposal(deposit_required=False,
                                                                deposit_invoice_no="TW-INV-01001"))
    r = client.post("/api/portal/tok/deposit", json={"method": "check", "note": "x"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert len(client.calls["deposits"]) == 1


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


def test_ach_encrypts_numbers_and_derives_masks(client):
    """2026-09-29: the numbers db.add_deposit is handed are no longer the customer's typed
    digits -- they are Fernet ciphertext (bank_crypto.ENC_PREFIX), and the two mask fields are
    derived from the PLAINTEXT before it was ever encrypted. Before this change these two kwargs
    were the literal typed digits ("021000021" / "000123456789"); this test would have failed on
    the ciphertext-prefix assertion against that old behaviour."""
    r = client.post("/api/portal/tok/deposit",
                    json={"method": "ach", "account_name": "Payer LLC",
                          "routing_number": "021000021", "account_number": "000123456789",
                          "account_type": "checking"})
    assert r.status_code == 200 and r.json()["ok"] is True
    rec = client.calls["deposits"][0]
    assert rec["args"][2] == "Payer LLC"                # account_name (positional)
    assert rec["args"][4] == "••••6789"                 # masked_ref derived server-side (positional)
    assert rec["kwargs"]["routing_masked"] == "••••0021"
    routing_ct = rec["kwargs"]["routing_number"]
    account_ct = rec["kwargs"]["account_number"]
    assert routing_ct.startswith(bank_crypto.ENC_PREFIX), "routing must be ciphertext, not typed digits"
    assert account_ct.startswith(bank_crypto.ENC_PREFIX), "account must be ciphertext, not typed digits"
    assert "021000021" not in routing_ct and "000123456789" not in account_ct
    # Round trip: what was stored decrypts back to exactly what the customer typed.
    assert bank_crypto.decrypt(routing_ct) == "021000021"
    assert bank_crypto.decrypt(account_ct) == "000123456789"
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
    """Digit-normalization happens BEFORE encryption -- proven by decrypting what was stored,
    since the ciphertext itself is randomized per call and can't be compared directly."""
    r = client.post("/api/portal/tok/deposit",
                    json={"method": "ach", "account_name": "Payer LLC",
                          "routing_number": "021-000-021", "account_number": "0001 2345 6789",
                          "account_type": "savings"})
    assert r.status_code == 200
    kw = client.calls["deposits"][0]["kwargs"]
    assert bank_crypto.decrypt(kw["routing_number"]) == "021000021"
    assert bank_crypto.decrypt(kw["account_number"]) == "000123456789"


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
        stored = client.calls["deposits"][0]["kwargs"]["routing_number"]
        assert bank_crypto.decrypt(stored) == ok_routing


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


def test_ach_email_masks_account_and_routing_numbers(client):
    """2026-09-29: routing used to be shown IN FULL in the team-notify email ("Routing may be
    full" was the old, deliberate rule) -- now that the numbers are encrypted at rest, the email
    must never carry the plaintext routing number either, only its last-4 mask, same as account."""
    r = client.post("/api/portal/tok/deposit",
                    json={"method": "ach", "account_name": "Payer LLC",
                          "routing_number": "021000021", "account_number": "000123456789",
                          "account_type": "checking"})
    assert r.status_code == 200
    assert len(client.calls["emails"]) == 1
    body = client.calls["emails"][0]["body"]
    assert "000123456789" not in body                   # full account never in the email
    assert "021000021" not in body                       # full routing never in the email either
    assert "••••6789" in body                           # masked account shown
    assert "••••0021" in body                           # masked routing shown


def test_ach_refused_when_bank_key_missing(client):
    """Fail CLOSED, not silent plaintext: with no PORTAL_BANK_KEY, ACH must be refused before
    db.add_deposit is ever called -- never stored in the clear as a fallback. The refusal message
    is customer-facing prose the portal can show as-is, and it names the alternative (check)."""
    client.monkeypatch.setattr(config, "PORTAL_BANK_KEY", "")
    r = client.post("/api/portal/tok/deposit",
                    json={"method": "ach", "account_name": "Payer LLC",
                          "routing_number": "021000021", "account_number": "000123456789",
                          "account_type": "checking"})
    assert r.status_code == 503
    assert "check" in r.json()["error"].lower()
    assert client.calls["deposits"] == []


def test_ach_refused_when_bank_key_invalid(client):
    """Same refusal for a key that is SET but not a valid Fernet key (e.g. truncated by a bad
    deploy) -- key_configured() must catch this, not just an empty string."""
    client.monkeypatch.setattr(config, "PORTAL_BANK_KEY", "not-a-valid-fernet-key")
    r = client.post("/api/portal/tok/deposit",
                    json={"method": "ach", "account_name": "Payer LLC",
                          "routing_number": "021000021", "account_number": "000123456789",
                          "account_type": "checking"})
    assert r.status_code == 503
    assert client.calls["deposits"] == []


def test_check_deposit_unaffected_by_missing_bank_key(client):
    """The other half of 'fail closed, not fail everything': a broken bank key must not take
    down the check-payment path, which never touches bank_crypto at all."""
    client.monkeypatch.setattr(config, "PORTAL_BANK_KEY", "")
    r = client.post("/api/portal/tok/deposit", json={"method": "check", "note": "mailed Friday"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert len(client.calls["deposits"]) == 1


def test_ach_chat_message_never_carries_bank_numbers(client):
    """The customer-visible chat card says a transfer is in flight and nothing else -- no
    typed digits, masked or not, belong in a message thread the customer also reads."""
    r = client.post("/api/portal/tok/deposit",
                    json={"method": "ach", "account_name": "Payer LLC",
                          "routing_number": "021000021", "account_number": "000123456789",
                          "account_type": "checking"})
    assert r.status_code == 200
    body = client.calls["messages"][0]["args"][3]
    assert "021000021" not in body and "000123456789" not in body
    assert "6789" not in body and "0021" not in body


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


# ── list_deposits / get_deposit_bank_details column scrub (2026-09-29) ────────
def test_list_deposits_never_selects_the_number_columns(monkeypatch):
    """THE GUARD for the CRM drawer feed. list_deposits() is what admin_proposal serializes
    straight into the staff JSON response -- if this query selected routing_number/
    account_number, no amount of care in main.py's dict comprehension would matter, because the
    dict would have the ciphertext sitting right there under d.get('routing_number'). Executed for
    real against a stubbed pool, not grepped, so a future edit that re-adds the columns fails here
    even if nobody touches main.py."""
    import db as dbmod

    seen = {}
    monkeypatch.setattr(dbmod, "qall", lambda sql, params=(): seen.update(sql=sql) or [])
    dbmod.list_deposits("p1")
    sql = " ".join(seen["sql"].split())
    assert "routing_number" not in sql
    assert "account_number" not in sql
    assert " id," in sql or sql.strip().startswith("select id,"), "id must be selected for the reveal endpoint to key on"
    assert "routing_masked" in sql


def test_get_deposit_bank_details_is_the_one_query_allowed_to_read_the_numbers(monkeypatch):
    """The mirror image of the guard above: exactly one function may select these columns, and
    this is it -- used only by the SERVICE_TOKEN-gated reveal endpoint."""
    import db as dbmod

    seen = {}
    monkeypatch.setattr(dbmod, "q1", lambda sql, params=(): seen.update(sql=sql, params=params))
    dbmod.get_deposit_bank_details("dep-1")
    sql = " ".join(seen["sql"].split())
    assert "routing_number" in sql and "account_number" in sql
    assert seen["params"] == ("dep-1",)


def test_add_deposit_inserts_routing_masked(monkeypatch):
    """routing_masked has to actually reach the INSERT column list, not just the function
    signature -- a parameter accepted but not wired into the SQL silently drops the value."""
    import db as dbmod

    seen = {}
    monkeypatch.setattr(dbmod, "execute", lambda sql, params=(): seen.update(sql=sql, params=params))
    dbmod.add_deposit("p1", "ach", "Payer LLC", None, "••••6789", None,
                      routing_number="enc:v1:x", account_number="enc:v1:y",
                      account_type="checking", routing_masked="••••0021")
    sql = " ".join(seen["sql"].split())
    assert "routing_masked" in sql
    assert "••••0021" in seen["params"]
