"""Post-approval automations. Graceful: each step logs and no-ops when its
integration isn't configured, so approval never fails because (e.g.) Dropbox
creds are absent. Basis Board status write + Foundation + Operations hand-off
are Phase 2 (see plan).
"""
from __future__ import annotations

import logging

import config

log = logging.getLogger("portal.automations")


def create_dropbox_folder(project_name: str, proposal_id: str) -> None:
    """Create the project's Dropbox folder on approval. Reuses the proposal
    tool's dropbox_client pattern (copied into this repo) when configured."""
    if not config.DROPBOX_ENABLED:
        log.info("[dropbox:skip] not configured — would create folder for %r (%s)", project_name, proposal_id)
        return
    try:
        import dropbox_folder  # local copy; created when DROPBOX_* are set

        dropbox_folder.ensure_project_folder(project_name)
        log.info("[dropbox] created/ensured folder for %r", project_name)
    except Exception as exc:  # noqa: BLE001
        log.error("[dropbox] folder creation failed for %r: %s", project_name, exc)


def issue_deposit_invoice(proposal_row: dict, project_name: str,
                          amount: float | None = None,
                          overrides: dict | None = None,
                          new_number: bool = False) -> dict:
    """Issue the deposit invoice: mint the number, generate the PDF, post it to
    the chat thread, and email it to every recipient with the PDF attached.

    THE single implementation — both the automatic path (on approval) and the
    manual staff "Request deposit" action call this, so the document, the chat
    card, and the email can never drift apart.

    `amount` overrides the stored 25% auto-calc (staff may adjust). Raises on
    failure; callers decide whether that's fatal.
    Returns {"amount", "invoice_no"}.
    """
    import db
    import email_sender
    import invoice as invoice_mod
    import proposals

    pid = proposal_row["proposal_id"]
    fresh = db.get_proposal(pid) or proposal_row
    stored = fresh.get("deposit_amount")
    if amount is None:
        amount = stored
    amount = float(amount)
    # A staff override has to be persisted BEFORE rendering: the customer-facing
    # /deposit-invoice.pdf rebuilds from the stored column, so skipping this would
    # make the downloadable invoice disagree with the emailed one.
    if stored is None or float(stored) != amount:
        db.set_deposit_amount(pid, amount)

    # Approval reuses the number (idempotent); a staff RESEND mints a fresh one
    # that supersedes the last, per Hanz — so one deposit can carry several.
    prior_no = fresh.get("deposit_invoice_no")
    invoice_no = db.issue_new_invoice_no(pid) if new_number else db.assign_invoice_no(pid)
    # Re-read so the document shows the issued-at stamp just written.
    fresh = db.get_proposal(pid) or fresh
    filename = invoice_mod.invoice_filename(invoice_no)
    reference = proposals.deposit_ref(pid)
    # The document is rendered by the proposal tool (it owns the template +
    # LibreOffice). If that's down the deposit request still goes out — just
    # without the attachment — rather than blocking the customer on our plumbing.
    try:
        payload = invoice_mod.invoice_payload(fresh, amount, invoice_no,
                                              draft=db.get_draft_data(pid) or {},
                                              overrides=overrides)
        pdf = invoice_mod.render_invoice_pdf(payload)
        # A staff-edited invoice number is what the customer will quote back, so
        # persist it — otherwise the portal download would rebuild the document
        # under the auto-generated number instead.
        edited_no = (overrides or {}).get("invoice_no")
        if edited_no and str(edited_no).strip() and str(edited_no).strip() != invoice_no:
            invoice_no = str(edited_no).strip()
            db.set_invoice_no(pid, invoice_no)
            filename = invoice_mod.invoice_filename(invoice_no)
    except invoice_mod.InvoiceUnavailable as exc:
        log.error("[deposit] invoice render unavailable for %s: %s", pid, exc)
        pdf = None

    # A new number replaces the old one — mark the earlier cards so the customer
    # can tell at a glance which invoice is current.
    if new_number and prior_no and prior_no != invoice_no:
        try:
            db.supersede_invoice_cards(pid, invoice_no)
        except Exception as exc:  # noqa: BLE001 — labelling must not block the send
            log.warning("[deposit] could not supersede prior cards for %s: %s", pid, exc)

    db.add_message(
        pid, "staff", None,
        f"Deposit invoice {invoice_no} — ${amount:,.2f} due. "
        f"Download it below, or pay by ACH or check.",
        msg_type="deposit_request",
        meta={"amount": amount, "invoice_no": invoice_no, "reference": reference},
    )
    db.set_deposit_requested(pid)

    token = fresh.get("token") or proposal_row.get("token")
    link = f"{config.PUBLIC_BASE_URL}/p/{token}"
    rt = email_sender.proposal_reply_to(token)
    for e in (db.get_recipients(pid) or [fresh.get("customer_email")]):
        email_sender.send_deposit_request(e, link, project_name, amount, reply_to=rt,
                                          invoice_no=invoice_no, invoice_pdf=pdf,
                                          invoice_filename=filename, reference=reference,
                                          token=token)
    log.info("[deposit] issued invoice %s for $%.2f (%s)", invoice_no, amount, pid)
    return {"amount": amount, "invoice_no": invoice_no}


def request_deposit(proposal_row: dict, project_name: str) -> None:
    """Auto-issue the deposit invoice the moment a proposal is approved (Will's
    item 15). Re-reads the proposal so it sees the deposit amount just written by
    set_approved. No-ops when there's no positive amount or the deposit was
    already invoiced/received, so a re-approval can't double-invoice. Non-fatal:
    any failure is logged; approval must still succeed."""
    pid = proposal_row["proposal_id"]
    try:
        import db

        fresh = db.get_proposal(pid) or proposal_row
        # Guard on deposit_requested_at, NOT deposit_status: the status check
        # constraint only permits 'pending'/'received', so a 'requested' status
        # never exists and the old check let a re-approval issue a 2nd invoice.
        if fresh.get("deposit_requested_at") or fresh.get("deposit_invoice_no"):
            log.info("[deposit:skip] already invoiced (%s) for %s",
                     fresh.get("deposit_invoice_no") or fresh.get("deposit_requested_at"), pid)
            return
        if (fresh.get("deposit_status") or "").lower() == "received":
            log.info("[deposit:skip] deposit already received for %s", pid)
            return
        amount = fresh.get("deposit_amount")
        if amount is None or float(amount) <= 0:
            log.info("[deposit:skip] no positive amount for %s", pid)
            return
        issue_deposit_invoice(fresh, project_name, float(amount))
    except Exception as exc:  # noqa: BLE001
        log.error("[deposit] auto-invoice on approval failed for %s: %s", pid, exc)


def run_on_approval(proposal_row: dict, project_name: str) -> None:
    create_dropbox_folder(project_name, proposal_row["proposal_id"])
    request_deposit(proposal_row, project_name)   # Will #15: auto deposit invoice on approval
    # Phase 2: Basis Board status -> Approved; Foundation project; Ops hand-off.
