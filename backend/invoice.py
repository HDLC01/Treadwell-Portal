"""Deposit invoice: the customer-facing document for the 25% deposit.

Two halves, deliberately split so the money/text logic is testable without
parsing PDF bytes:

  * `invoice_fields(...)` — pure. Turns a proposal row + amount into the exact
    strings that appear on the document (number, dates, bill-to, line item,
    total, remit-to, reference).
  * `build_deposit_invoice_pdf(...)` — renders those fields with reportlab.

Rendered on demand from stored columns (no blob storage), the same lazy
approach the proposal PDF uses. The invoice NUMBER is not derived here — it is
issued once by `db.assign_invoice_no` so a re-send can never show the customer
a second number for one deposit.
"""
from __future__ import annotations

import io
from datetime import date, datetime, timezone
from typing import Any, Optional

import config
import proposals

# Treadwell red, matching the proposal template + portal brand.
_RED = (0.784, 0.063, 0.180)          # #C8102E
_INK = (0.059, 0.090, 0.165)          # #0f172a
_MUTED = (0.42, 0.45, 0.50)

_COMPANY = "TREADWELL"
_TAGLINE = "Epoxy Flooring · Polished Concrete · Gypsum Underlayments"


def _money(v) -> str:
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return "$0.00"


def _fmt_date(d) -> str:
    """Business-facing date. Accepts date/datetime/ISO string/None (→ today)."""
    if isinstance(d, datetime):
        d = d.date()
    elif isinstance(d, str):
        try:
            d = datetime.fromisoformat(d.replace("Z", "+00:00")).date()
        except ValueError:
            d = None
    if not isinstance(d, date):
        d = datetime.now(timezone.utc).date()
    return f"{d:%b} {d.day}, {d.year}"


def invoice_fields(proposal_row: dict[str, Any], amount: float,
                   invoice_no: Optional[str] = None) -> dict[str, str]:
    """Every string that lands on the invoice. Pure — no DB, no I/O.

    `amount` is passed in (not recomputed) so the document always matches what
    the chat card and the email said; the 25% derivation lives in
    `proposals.deposit_amount` and is only shown here as context on the line."""
    pid = proposal_row.get("proposal_id") or ""
    project = (proposal_row.get("project_name") or "Your project").strip()
    approved_total = proposal_row.get("approved_total")
    pct = int(round(proposals.DEPOSIT_PCT * 100))

    desc = f"Deposit ({pct}%) — {project}"
    if approved_total is not None:
        desc += f"\nApproved proposal total {_money(approved_total)}"

    return {
        "company": _COMPANY,
        "tagline": _TAGLINE,
        "remit_to": config.CHECK_ADDRESS,
        "invoice_no": invoice_no or "TW-INV-DRAFT",
        "issued": _fmt_date(proposal_row.get("deposit_invoice_issued_at")),
        "due": "On receipt",
        "bill_to_name": (proposal_row.get("approved_name")
                         or proposal_row.get("customer_name")
                         or proposal_row.get("customer_email") or "Customer"),
        "bill_to_project": project,
        "description": desc,
        "amount": _money(amount),
        "total_due": _money(amount),
        "reference": proposals.deposit_ref(pid),
    }


def invoice_filename(invoice_no: Optional[str]) -> str:
    safe = "".join(c for c in (invoice_no or "deposit") if c.isalnum() or c in "-_")
    return f"Treadwell-Invoice-{safe}.pdf"


def build_deposit_invoice_pdf(proposal_row: dict[str, Any], amount: float,
                              invoice_no: Optional[str] = None) -> bytes:
    """Render the deposit invoice. Returns PDF bytes (never writes to disk)."""
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.units import inch
    from reportlab.pdfgen import canvas

    f = invoice_fields(proposal_row, amount, invoice_no)
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=LETTER)
    c.setTitle(f"Treadwell Invoice {f['invoice_no']}")
    W, H = LETTER
    L, R = 0.9 * inch, W - 0.9 * inch
    y = H - 0.95 * inch

    # ── header: wordmark left, INVOICE block right ──
    c.setFillColorRGB(*_RED)
    c.setFont("Helvetica-Bold", 23)
    c.drawString(L, y, f["company"])
    c.setFillColorRGB(*_INK)
    c.setFont("Helvetica-Bold", 15)
    c.drawRightString(R, y, "INVOICE")
    y -= 15
    c.setFillColorRGB(*_MUTED)
    c.setFont("Helvetica", 8)
    c.drawString(L, y, f["tagline"])
    c.setFont("Helvetica", 9.5)
    c.drawRightString(R, y, f["invoice_no"])
    y -= 16
    c.setStrokeColorRGB(*_RED)
    c.setLineWidth(2)
    c.line(L, y, R, y)

    # ── bill-to (left) + dates (right) ──
    y -= 26
    top = y
    c.setFillColorRGB(*_MUTED)
    c.setFont("Helvetica-Bold", 8)
    c.drawString(L, y, "BILL TO")
    c.setFillColorRGB(*_INK)
    c.setFont("Helvetica-Bold", 10.5)
    y -= 15
    c.drawString(L, y, f["bill_to_name"])
    c.setFont("Helvetica", 10)
    y -= 13
    c.drawString(L, y, f["bill_to_project"])

    ry = top
    for label, value in (("Issued", f["issued"]), ("Due", f["due"]), ("Reference", f["reference"])):
        c.setFillColorRGB(*_MUTED)
        c.setFont("Helvetica", 9)
        c.drawRightString(R - 1.35 * inch, ry, label)
        c.setFillColorRGB(*_INK)
        c.setFont("Helvetica-Bold" if label == "Reference" else "Helvetica", 9.5)
        c.drawRightString(R, ry, value)
        ry -= 14

    # ── line items ──
    y = min(y, ry) - 30
    c.setStrokeColorRGB(0.85, 0.86, 0.88)
    c.setLineWidth(0.8)
    c.line(L, y, R, y)
    y -= 13
    c.setFillColorRGB(*_MUTED)
    c.setFont("Helvetica-Bold", 8)
    c.drawString(L, y, "DESCRIPTION")
    c.drawRightString(R, y, "AMOUNT")
    y -= 8
    c.line(L, y, R, y)

    y -= 18
    lines = f["description"].split("\n")
    c.setFillColorRGB(*_INK)
    c.setFont("Helvetica", 10.5)
    c.drawString(L, y, lines[0])
    c.drawRightString(R, y, f["amount"])
    for extra in lines[1:]:
        y -= 13
        c.setFillColorRGB(*_MUTED)
        c.setFont("Helvetica", 9)
        c.drawString(L, y, extra)

    # ── total ──
    y -= 20
    c.setStrokeColorRGB(0.85, 0.86, 0.88)
    c.line(L, y, R, y)
    y -= 20
    c.setFillColorRGB(*_INK)
    c.setFont("Helvetica-Bold", 12)
    c.drawRightString(R - 1.35 * inch, y, "TOTAL DUE")
    c.setFillColorRGB(*_RED)
    c.setFont("Helvetica-Bold", 14)
    c.drawRightString(R, y, f["total_due"])

    # ── payment instructions ──
    y -= 44
    c.setFillColorRGB(*_MUTED)
    c.setFont("Helvetica-Bold", 8)
    c.drawString(L, y, "HOW TO PAY")
    y -= 15
    c.setFillColorRGB(*_INK)
    c.setFont("Helvetica", 9.5)
    for line in (
        "ACH transfer (preferred, fastest): open your proposal in the Treadwell portal and",
        "submit the deposit form. Or mail a check to:",
        f"    {f['remit_to']}",
        f"Include reference {f['reference']} with your payment.",
    ):
        c.drawString(L, y, line)
        y -= 13

    y -= 12
    c.setFillColorRGB(*_MUTED)
    c.setFont("Helvetica-Oblique", 8.5)
    c.drawString(L, y, "Your project is scheduled once the deposit is received. Questions? Reply to this email "
                       "or message us in the portal.")

    c.showPage()
    c.save()
    return buf.getvalue()
