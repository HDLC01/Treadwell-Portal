"""The deposit invoice document: the field shaping (pure) plus a smoke test that
reportlab actually produces a PDF. Field text is asserted here rather than by
parsing PDF bytes — the renderer only draws what invoice_fields() returns."""
import datetime as dt

import config
import invoice
import proposals


ROW = {
    "proposal_id": "46e80049-1889-4b0c-994c-4b698ccaa273",
    "project_name": "Nearman Creek Power Station Control Room",
    "approved_total": 14973.0,
    "approved_name": "Hanz de la Cruz",
    "customer_email": "hanz@wetreadwell.com",
    "deposit_invoice_issued_at": dt.datetime(2026, 7, 27, 13, 36, 44),
}


def test_fields_money_and_reference():
    f = invoice.invoice_fields(ROW, 3743.25, "TW-INV-01001")
    assert f["invoice_no"] == "TW-INV-01001"
    assert f["amount"] == "$3,743.25"
    assert f["total_due"] == "$3,743.25"
    assert f["reference"] == proposals.deposit_ref(ROW["proposal_id"]) == "TW-46E80049"
    assert f["remit_to"] == config.CHECK_ADDRESS
    assert f["due"] == "On receipt"
    assert f["issued"] == "Jul 27, 2026"


def test_fields_line_item_shows_pct_and_approved_total():
    f = invoice.invoice_fields(ROW, 3743.25, "TW-INV-01001")
    assert "Deposit (25%)" in f["description"]
    assert "Nearman Creek" in f["description"]
    assert "$14,973.00" in f["description"]        # context for how the deposit was derived


def test_fields_bill_to_falls_back_to_email():
    row = {"proposal_id": "p1", "project_name": "P", "customer_email": "only@x.com"}
    assert invoice.invoice_fields(row, 100.0)["bill_to_name"] == "only@x.com"


def test_fields_amount_is_passed_through_not_recomputed():
    """A staff-adjusted amount must appear verbatim, so the document always
    matches the chat card and the email."""
    f = invoice.invoice_fields(ROW, 500.0, "TW-INV-1")
    assert f["total_due"] == "$500.00"


def test_fields_tolerate_missing_everything():
    f = invoice.invoice_fields({}, 0)
    assert f["invoice_no"] == "TW-INV-DRAFT"
    assert f["reference"] == "TW-DEPOSIT"
    assert f["total_due"] == "$0.00"
    assert f["issued"]                                  # defaults to today, never blank


def test_build_pdf_returns_a_real_pdf():
    blob = invoice.build_deposit_invoice_pdf(ROW, 3743.25, "TW-INV-01001")
    assert blob.startswith(b"%PDF-")
    assert blob.rstrip().endswith(b"%%EOF")
    assert len(blob) > 1000


def test_filename_is_safe():
    assert invoice.invoice_filename("TW-INV-01001") == "Treadwell-Invoice-TW-INV-01001.pdf"
    # Path separators, dots and spaces can never leak into the Content-Disposition
    # header (only alphanumerics, '-' and '_' survive).
    assert invoice.invoice_filename("../../etc/pa ss") == "Treadwell-Invoice-etcpass.pdf"
    assert invoice.invoice_filename(None) == "Treadwell-Invoice-deposit.pdf"
