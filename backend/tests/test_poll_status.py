"""The customer poll's `status` block — the contract that decides what a customer
can see without reloading the page.

The client re-renders the whole view when any field here changes, so a field left
out of this payload is a field that goes stale on screen. That is not theoretical:
issuing a deposit invoice never touches deposit_status, so before invoice_no was
added the page kept saying "your invoice is on its way" while the invoice was
sitting in the chat thread underneath.
"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def poll(monkeypatch):
    import main

    def _run(proposal):
        monkeypatch.setattr(main, "_require", lambda request, token: proposal)
        monkeypatch.setattr(main.db, "list_messages", lambda pid, after: [])
        r = TestClient(main.app).get("/api/portal/tok/messages?after=0")
        assert r.status_code == 200, r.text
        return r.json()["status"]

    return _run


def _p(**over):
    p = {"proposal_id": "p1", "proposal_status": "approved", "deposit_status": "pending",
         "contacts_status": "pending", "schedule_status": "pending",
         "deposit_invoice_no": None, "deposit_amount": None, "deposit_required": True}
    p.update(over)
    return p


def test_poll_status_carries_every_field_the_ui_renders_from(poll):
    st = poll(_p())
    for k in ("proposal", "deposit", "contacts", "schedule",
              "invoice_no", "deposit_amount", "deposit_required"):
        assert k in st, k


def test_issuing_an_invoice_changes_the_poll_key(poll):
    """The staleness bug this exists to prevent: deposit_status is identical before
    and after invoicing, so only invoice_no can tell the page to re-render."""
    before = poll(_p())
    after = poll(_p(deposit_invoice_no="TW-INV-01001", deposit_amount=3316.25))
    assert before["invoice_no"] is None
    assert after["invoice_no"] == "TW-INV-01001"
    assert after["deposit_amount"] == 3316.25
    assert before["deposit"] == after["deposit"]          # the old key would not have moved
    assert before != after


def test_deposit_required_is_reported_and_defaults_true(poll):
    assert poll(_p())["deposit_required"] is True
    assert poll(_p(deposit_required=False))["deposit_required"] is False
    # Legacy row read through a dict without the key → required, matching the
    # column default. Never silently "no deposit".
    legacy = _p()
    legacy.pop("deposit_required")
    assert poll(legacy)["deposit_required"] is True


def test_deposit_amount_is_json_safe(poll):
    """Postgres hands back Decimal; JSON cannot serialise it. Coerced to float."""
    from decimal import Decimal
    assert poll(_p(deposit_amount=Decimal("3316.25")))["deposit_amount"] == 3316.25
