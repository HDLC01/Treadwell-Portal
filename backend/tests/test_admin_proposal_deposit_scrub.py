"""GET /api/admin/proposal/{proposal_id} -- the endpoint that feeds the CRM drawer. This file pins
one thing: the "deposits" array in its JSON response never carries routing_number/account_number,
even if list_deposits() (or some future caller) hands back a row that HAS those keys on it.

That "even if" is the point. test_deposit.py already proves list_deposits' SQL doesn't select the
columns; this file proves the SERIALIZATION layer doesn't blindly re-expose them either, so a
later change that widens list_deposits for some unrelated reason doesn't silently reopen the leak
this whole feature exists to close. Full numbers are only ever available through the one-deposit
reveal endpoint (test_bank_reveal.py).

Every other db read admin_proposal makes is stubbed to an empty/minimal value -- this file is not
trying to pin the rest of the drawer's shape, only the deposits array.
"""
import pytest
from fastapi.testclient import TestClient

import main

PROPOSAL_ROW = {
    "proposal_id": "pid-1", "token": "tok-1",
    "customer_email": "kevin@example.com", "customer_name": "Kevin",
    "project_name": "Test Project", "proposal_status": "sent",
    "deposit_status": "submitted", "schedule_status": "pending",
    "deposit_invoice_no": "TW-INV-01001",   # truthy -> peek_next_invoice_no is never called
}

# A row shaped like what a hypothetical buggy/future list_deposits() might return -- carrying the
# ENCRYPTED numbers on it, exactly the shape that must never reach the response JSON.
POISONED_DEPOSIT = {
    "id": "dep-1", "method": "ach", "account_name": "Payer LLC", "bank_name": None,
    "masked_ref": "••••6789", "routing_masked": "••••0021", "note": None,
    "routing_number": "enc:v1:should-never-appear-in-the-response",
    "account_number": "enc:v1:should-never-appear-in-the-response-either",
    "account_type": "checking", "sent_date": None, "trace_ref": None,
    "sent_to_beneficiary": None, "sent_to_bank": None, "sent_to_routing": None,
    "sent_to_account": None, "check_number": None, "submitted_at": None,
}


@pytest.fixture
def drawer(monkeypatch):
    monkeypatch.setattr(main, "_admin_ok", lambda request: True)
    monkeypatch.setattr(main.db, "get_proposal", lambda pid: dict(PROPOSAL_ROW))
    monkeypatch.setattr(main.db, "latest_approval", lambda pid: None)
    monkeypatch.setattr(main.db, "get_recipients", lambda pid: [])
    monkeypatch.setattr(main.db, "list_contacts", lambda pid: [])
    monkeypatch.setattr(main.db, "list_followups", lambda pid: [])
    monkeypatch.setattr(main.db, "list_views", lambda pid: [])
    monkeypatch.setattr(main.db, "get_followup_recipients", lambda pid: [])
    monkeypatch.setattr(main.db, "list_messages", lambda pid, include_internal=False: [])
    monkeypatch.setattr(main.db, "list_questions", lambda pid: [])
    monkeypatch.setattr(main.db, "list_deposits", lambda pid: [dict(POISONED_DEPOSIT)])
    return TestClient(main.app)


def test_drawer_deposits_never_carry_the_number_columns(drawer):
    r = drawer.get("/api/admin/proposal/pid-1", headers={"x-service-token": "irrelevant"})
    assert r.status_code == 200
    deposits = r.json()["deposits"]
    assert len(deposits) == 1
    dep = deposits[0]
    assert "routing_number" not in dep
    assert "account_number" not in dep
    body_text = r.text
    assert "should-never-appear-in-the-response" not in body_text


def test_drawer_deposits_carry_id_and_both_masks(drawer):
    """The reveal endpoint needs `id` to key on, and the drawer needs both masks to render --
    losing either while removing the plaintext columns would be a display regression, not a
    security fix."""
    r = drawer.get("/api/admin/proposal/pid-1", headers={"x-service-token": "irrelevant"})
    dep = r.json()["deposits"][0]
    assert dep["id"] == "dep-1"
    assert dep["masked_ref"] == "••••6789"
    assert dep["routing_masked"] == "••••0021"
