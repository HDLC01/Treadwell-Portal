"""Deposit-invoice CLIENT.

The document itself is Kyle's Invoice_Deposit.docx, filled and rendered by the
proposal tool (it owns the template, python-docx and LibreOffice — none of which
ship in this container). Those tests live in that repo. Here we pin the two
things the portal is responsible for: building the field payload, and degrading
sanely when the proposal tool can't be reached.
"""
import pytest

import config
import invoice
import proposals

ROW = {
    "proposal_id": "46e80049-1889-4b0c-994c-4b698ccaa273",
    "project_name": "Nearman Creek Power Station Control Room",
    "customer_name": "Dana Lee",
    "customer_email": "dana@acme.com",
    "approved_total": 14973.0,
}
DRAFT = {"address": "4600 Madison Ave", "city_state": "Kansas City, MO 64112", "job_number": "26.114"}


# ── payload shaping (pure) ───────────────────────────────────────────────────
def test_payload_carries_the_template_fields():
    p = invoice.invoice_payload(ROW, 3743.25, "26.114-01", draft=DRAFT)
    assert p["customer_name"] == "Dana Lee"
    assert p["customer_address"] == "4600 Madison Ave"
    assert p["city_state"] == "Kansas City, MO 64112"
    assert p["job_name"] == "Nearman Creek Power Station Control Room"
    assert p["job_number"] == "26.114"
    assert p["invoice_no"] == "26.114-01"
    assert p["contract_value"] == 14973.0
    assert p["deposit_amount"] == 3743.25
    assert p["total_due"] == 3743.25
    assert p["deposit_pct"] == 25
    assert p["reference"] == proposals.deposit_ref(ROW["proposal_id"])


def test_payload_falls_back_to_the_email_when_there_is_no_name():
    p = invoice.invoice_payload({"proposal_id": "p", "customer_email": "only@x.com"}, 10.0)
    assert p["customer_name"] == "only@x.com"


def test_staff_overrides_win():
    """Edit-before-send is the point: whatever staff typed replaces the derived
    value, including the invoice number and the amounts."""
    p = invoice.invoice_payload(ROW, 3743.25, "26.114-01", draft=DRAFT, overrides={
        "customer_name": "Acme Holdings LLC", "invoice_no": "26.999-07",
        "deposit_amount_text": "$1,000.00",
    })
    assert p["customer_name"] == "Acme Holdings LLC"
    assert p["invoice_no"] == "26.999-07"
    assert p["deposit_amount_text"] == "$1,000.00"


def test_blank_overrides_are_ignored():
    """An empty box on the review form must not wipe a good derived value."""
    p = invoice.invoice_payload(ROW, 3743.25, "26.114-01", draft=DRAFT,
                                overrides={"customer_name": "  ", "job_number": None})
    assert p["customer_name"] == "Dana Lee"
    assert p["job_number"] == "26.114"


def test_filename_is_safe():
    assert invoice.invoice_filename("26.114-01") == "Treadwell-Invoice-26.114-01.pdf"
    assert invoice.invoice_filename("../../etc/pa ss") == "Treadwell-Invoice-....etcpass.pdf"
    assert invoice.invoice_filename(None) == "Treadwell-Invoice-deposit.pdf"


# ── the render call ──────────────────────────────────────────────────────────
def test_render_raises_when_the_proposal_tool_is_unconfigured(monkeypatch):
    monkeypatch.setattr(config, "PROPOSAL_TOOL_URL", "")
    with pytest.raises(invoice.InvoiceUnavailable):
        invoice.render_invoice_pdf({})


def test_render_raises_when_the_proposal_tool_is_unreachable(monkeypatch):
    monkeypatch.setattr(config, "PROPOSAL_TOOL_URL", "http://tool")
    monkeypatch.setattr(config, "SERVICE_TOKEN", "tok")

    def boom(*a, **k):
        raise invoice.httpx.HTTPError("down")

    monkeypatch.setattr(invoice.httpx, "post", boom)
    with pytest.raises(invoice.InvoiceUnavailable):
        invoice.render_invoice_pdf({})


def test_render_rejects_a_non_pdf_response(monkeypatch):
    """A proxy error page must never be emailed to a customer as their invoice."""
    monkeypatch.setattr(config, "PROPOSAL_TOOL_URL", "http://tool")
    monkeypatch.setattr(config, "SERVICE_TOKEN", "tok")

    class _R:
        status_code = 200
        content = b"<html>gateway error</html>"

    monkeypatch.setattr(invoice.httpx, "post", lambda *a, **k: _R())
    with pytest.raises(invoice.InvoiceUnavailable):
        invoice.render_invoice_pdf({})


def test_render_returns_the_pdf_and_sends_the_service_token(monkeypatch):
    monkeypatch.setattr(config, "PROPOSAL_TOOL_URL", "http://tool")
    monkeypatch.setattr(config, "SERVICE_TOKEN", "tok")
    seen = {}

    class _R:
        status_code = 200
        content = b"%PDF-1.4 ok"

    def cap(url, json=None, headers=None, timeout=None):
        seen.update(url=url, json=json, headers=headers)
        return _R()

    monkeypatch.setattr(invoice.httpx, "post", cap)
    assert invoice.render_invoice_pdf({"invoice_no": "26.114-01"}).startswith(b"%PDF")
    assert seen["url"].endswith("/api/admin/deposit-invoice")
    assert seen["headers"]["X-Service-Token"] == "tok"
