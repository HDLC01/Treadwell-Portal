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


def request_deposit(proposal_row: dict, project_name: str) -> None:
    """Auto-send the deposit invoice/request to the customer the moment a proposal
    is approved (Will's item 15) — same effect as the manual "Request deposit"
    admin action. Re-reads the proposal so it sees the deposit amount just written
    by set_approved. No-ops when there's no positive amount or the deposit was
    already requested/received (so a re-approval can't double-invoice). Non-fatal:
    any failure is logged; approval must still succeed."""
    pid = proposal_row["proposal_id"]
    try:
        import db
        import email_sender

        fresh = db.get_proposal(pid) or proposal_row
        if (fresh.get("deposit_status") or "").lower() in ("requested", "received"):
            log.info("[deposit:skip] already %s for %s", fresh.get("deposit_status"), pid)
            return
        amount = fresh.get("deposit_amount")
        if amount is None or float(amount) <= 0:
            log.info("[deposit:skip] no positive amount for %s", pid)
            return
        amount = float(amount)
        token = fresh.get("token") or proposal_row.get("token")
        db.add_message(pid, "staff", None,
                       f"Deposit requested: ${amount:,.2f}. Your deposit invoice will follow shortly.",
                       msg_type="deposit_request", meta={"amount": amount})
        db.set_deposit_requested(pid)
        link = f"{config.PUBLIC_BASE_URL}/p/{token}"
        rt = email_sender.proposal_reply_to(token)
        for e in (db.get_recipients(pid) or [fresh.get("customer_email")]):
            email_sender.send_deposit_request(e, link, project_name, amount, reply_to=rt)
        log.info("[deposit] auto-requested $%.2f for %s", amount, pid)
    except Exception as exc:  # noqa: BLE001
        log.error("[deposit] auto-request on approval failed for %s: %s", pid, exc)


def run_on_approval(proposal_row: dict, project_name: str) -> None:
    create_dropbox_folder(project_name, proposal_row["proposal_id"])
    request_deposit(proposal_row, project_name)   # Will #15: auto deposit invoice on approval
    # Phase 2: Basis Board status -> Approved; Foundation project; Ops hand-off.
