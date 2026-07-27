"""Deposit invoice — the portal's CLIENT for the real Treadwell document.

The invoice is Kyle's `Invoice_Deposit.docx`, filled and rendered by the proposal
tool: that repo already carries python-docx, the template, and LibreOffice, none
of which ship in this container. Exactly the same split as the proposal PDF
(`/api/admin/proposal-pdf`), just for deposits.

This module owns two things:
  * `invoice_payload` — turning a proposal row (plus any staff edits) into the
    field set the template expects. Pure, so the money/text is testable here.
  * `render_invoice_pdf` — the SERVICE_TOKEN-gated call that returns PDF bytes.

The invoice NUMBER is persisted on the proposal, so a document is always
reproducible and always matches what was emailed.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Optional

import httpx

import config
import proposals

log = logging.getLogger("portal.invoice")

_ENDPOINT = "/api/admin/deposit-invoice"


class InvoiceUnavailable(RuntimeError):
    """The proposal tool couldn't render the document (unconfigured or down).
    Callers decide whether that's fatal — the deposit email, for instance, still
    goes out, just without the attachment."""


def invoice_filename(invoice_no: Optional[str]) -> str:
    safe = "".join(c for c in (invoice_no or "deposit") if c.isalnum() or c in "-_.")
    return f"Treadwell-Invoice-{safe or 'deposit'}.pdf"


def _money(v) -> str:
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return ""


def invoice_payload(proposal_row: dict[str, Any], amount: float,
                    invoice_no: Optional[str] = None,
                    draft: Optional[dict[str, Any]] = None,
                    overrides: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Fields for the template. `overrides` is whatever staff edited on the review
    screen and wins over everything derived — that's what makes edit-before-send
    meaningful. Pure: no I/O."""
    d = draft or {}
    pid = proposal_row.get("proposal_id") or ""
    project = (proposal_row.get("project_name") or "").strip()
    payload: dict[str, Any] = {
        "customer_name": (proposal_row.get("customer_name")
                          or proposal_row.get("customer_email") or "").strip(),
        "customer_address": (d.get("address") or "").strip(),
        "city_state": (d.get("city_state") or "").strip(),
        "job_name": project,
        "job_number": (d.get("job_number") or "").strip(),
        "invoice_no": invoice_no or "",
        "invoice_date": date.today().isoformat(),
        "contract_value": proposal_row.get("approved_total"),
        "deposit_amount": amount,
        "total_due": amount,
        "deposit_pct": round(proposals.DEPOSIT_PCT * 100),
        "reference": proposals.deposit_ref(pid),
    }
    for k, v in (overrides or {}).items():
        if v is not None and str(v).strip() != "":
            payload[k] = v
    return payload


def render_invoice_pdf(payload: dict[str, Any], *, timeout: float = 90.0) -> bytes:
    """PDF bytes for the invoice. Raises InvoiceUnavailable when the proposal tool
    isn't configured or the render fails, so a caller can degrade gracefully."""
    if not (config.PROPOSAL_TOOL_URL and config.SERVICE_TOKEN):
        raise InvoiceUnavailable("PROPOSAL_TOOL_URL / SERVICE_TOKEN not configured")
    try:
        r = httpx.post(config.PROPOSAL_TOOL_URL + _ENDPOINT, json=payload,
                       headers={"X-Service-Token": config.SERVICE_TOKEN}, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        raise InvoiceUnavailable(f"proposal tool unreachable: {exc}") from exc
    if r.status_code != 200:
        raise InvoiceUnavailable(f"proposal tool returned {r.status_code}")
    if not r.content.startswith(b"%PDF"):
        raise InvoiceUnavailable("proposal tool did not return a PDF")
    return r.content
