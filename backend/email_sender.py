"""Email via Resend. Graceful: with no RESEND_API_KEY (local dev) it logs the
message to stdout instead of sending, so the full flow is testable offline.
"""
from __future__ import annotations

import base64
import hashlib
import html
import logging

import httpx

import config

log = logging.getLogger("portal.email")

_RESEND_URL = "https://api.resend.com/emails"


def _esc(s) -> str:
    """HTML-escape any value dropped into an email body (customer free-text notes,
    replies, names) so it can't break the markup or inject."""
    return html.escape(str(s if s is not None else ""))


def _first_name(name) -> str:
    """Customer greeting uses the FIRST name only (per Hanz). Empty → '' so the
    caller can fall back to a generic greeting."""
    return (str(name or "").strip().split() or [""])[0]


def proposal_anchor(token: str) -> str:
    """The Message-ID that identifies a PROPOSAL inside the mail headers.

    This is how a reply finds its project without putting a routing token in the
    visible address — customers reply to one clean address, and the identity rides
    in References/In-Reply-To, which every mail client echoes back untouched
    (verified against a real Gmail reply: our anchor came back in `references`)."""
    return f"<tw-proposal.{token}@wetreadwell.com>"


def _thread_headers(email: str, token: str | None = None) -> dict[str, str]:
    """Group portal email into inbox threads AND carry the proposal identity.

    Two anchors, both echoed back on reply:
    - a per-recipient anchor, so the login code lands in the same conversation as
      the proposal link (and is never shown on a web page);
    - a per-proposal anchor (when `token` is given), which is what the inbound
      webhook reads to route a reply to the right project.

    In-Reply-To gets the proposal anchor when we have one, so a customer with
    several projects gets a thread per project rather than one merged pile."""
    recipient = hashlib.sha1((email or "").strip().lower().encode()).hexdigest()[:24]
    mid = f"<treadwell-portal.{recipient}@wetreadwell.com>"
    if not token:
        return {"References": mid, "In-Reply-To": mid}
    anchor = proposal_anchor(token)
    # RFC 5322: References is a space-separated list, oldest first.
    return {"References": f"{mid} {anchor}", "In-Reply-To": anchor}


def _send(to: list[str], subject: str, html: str, headers: dict[str, str] | None = None,
          reply_to: str | None = None,
          attachments: list[tuple[str, bytes]] | None = None) -> bool:
    """`attachments` is a list of (filename, raw bytes) — base64-encoded here into
    Resend's attachment format. Kept as raw bytes at the call site so callers
    never deal with encoding."""
    to = [t for t in to if t]
    if not to:
        return False
    if not config.RESEND_API_KEY:
        log.warning("[email:dev] would send to=%s subject=%r attachments=%s\n%s",
                    to, subject, [n for n, _ in (attachments or [])], html)
        return True
    try:
        payload: dict = {"from": config.EMAIL_FROM, "to": to, "subject": subject, "html": html}
        if attachments:
            payload["attachments"] = [
                {"filename": name, "content": base64.b64encode(blob).decode("ascii")}
                for name, blob in attachments if blob
            ]
        # Explicit per-message reply_to (e.g. the per-proposal inbound-capture
        # address) wins over the global EMAIL_REPLY_TO fallback.
        effective_reply_to = reply_to or config.EMAIL_REPLY_TO
        if effective_reply_to:
            payload["reply_to"] = effective_reply_to
        if headers:
            payload["headers"] = headers
        resp = httpx.post(
            _RESEND_URL,
            headers={"Authorization": f"Bearer {config.RESEND_API_KEY}"},
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("email send failed (to=%s): %s", to, exc)
        return False


def _logo_html() -> str:
    """Letterhead mark at the top of every email.

    PNG, not the SVG the apps use: Gmail, Outlook desktop and Outlook.com all
    refuse inline SVG. The src is built from PUBLIC_BASE_URL at CALL time (not
    import time) so a re-pointed base URL is honoured — a relative or localhost
    src renders as a broken image in the recipient's inbox. width/height are
    real ATTRIBUTES because Outlook ignores CSS sizing on images, and border:0
    kills the border Outlook draws on linked images. alt matters more than usual
    here: most clients block images by default, so "Treadwell" is what the
    majority of recipients actually see. No srcset — Outlook drops it anyway,
    and the 320px asset is already >2x the 150px display box, so it stays crisp
    on retina without a second source.
    """
    return (
        f'<div style="text-align:center;margin:0 0 20px">'
        f'<img src="{config.PUBLIC_BASE_URL}/static/img/treadwell-mark.png" alt="Treadwell" '
        f'width="150" height="90" '
        f'style="display:block;border:0;margin:0 auto;width:150px;height:auto">'
        f'</div>'
    )


def _wrap(title: str, body_html: str) -> str:
    return (
        f'<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'
        f'max-width:520px;margin:0 auto;color:#0f172a">'
        f'{_logo_html()}'
        f'<h2 style="color:#0f172a;margin:0 0 12px">{title}</h2>{body_html}'
        f'<hr style="border:none;border-top:1px solid #e2e8f0;margin:24px 0">'
        f'{_SIGNATURE_HTML}</div>'
    )


# Footer / signatory on EVERY outgoing email (single choke-point — _wrap wraps
# them all). Copied from Kyle's real signature: Arial 8pt, the TREADWELL word and
# the "l" separators in navy (#000087/#000090), everything else grey. Colours are
# set inline and explicitly so dark-mode clients tint them as little as possible —
# the navy is what reads as "violet" in Gmail's dark theme.
_SIG_NAVY = "#000087"
_SIG_BAR = "#000090"
_SIG_GREY = "#595959"
_SIG_ADDR = "#666666"
# The red on every CTA button and the estimator-note rule. This is the UI red the
# apps already use everywhere, deliberately NOT the brand PDF's #E52B2E — the logo
# artwork carries the brand red, the interface does not. Named so the five buttons
# below can't drift apart.
_BRAND_RED = "#C8102E"
_SIGNATURE_HTML = (
    f'<p style="font-family:Arial,sans-serif;font-size:8pt;line-height:1.6;margin:0">'
    f'<b><span style="color:{_SIG_NAVY}">TREADWELL</span></b> '
    f'<b><span style="color:{_SIG_BAR}">l</span></b> '
    f'<span style="color:{_SIG_GREY}">913.396.6216</span> '
    f'<b><span style="color:{_SIG_BAR}">l</span></b> '
    f'<span style="color:{_SIG_ADDR}">1707 E. 123rd Ter, Olathe, KS 66061</span><br>'
    f'<span style="color:{_SIG_GREY}">Epoxy Flooring + Polished Concrete + Gypsum Underlayments</span>'
    f'</p>'
)


def _otp_headers(email: str) -> dict[str, str]:
    """A thread anchor for login codes ONLY, separate from the proposal thread.

    Access codes are transient noise: a customer may request several while reading
    one proposal, and threading them in with the proposal, replies and invoice
    buried the conversation under a pile of expired codes. Codes now thread with
    each other (one tidy "access code" conversation per recipient) and never with
    the proposal."""
    anchor = hashlib.sha1((email or "").strip().lower().encode()).hexdigest()[:24]
    mid = f"<treadwell-otp.{anchor}@wetreadwell.com>"
    return {"References": mid, "In-Reply-To": mid}


def send_otp(email: str, code: str, project_name: str) -> bool:
    body = (
        f'<p>Use this code to view your proposal for <strong>{project_name}</strong>:</p>'
        f'<p style="font-size:30px;font-weight:800;letter-spacing:6px;margin:16px 0">{code}</p>'
        f'<p style="color:#64748b">This code expires in {config.OTP_TTL_MINUTES} minutes.</p>'
    )
    return _send([email], "Your Treadwell proposal access code", _wrap("Your access code", body),
                 _otp_headers(email))


def proposal_reply_to(token: str) -> str | None:
    """The Reply-To we put on customer email, or None when receiving isn't set up.

    Prefers INBOUND_REPLY_ADDRESS — ONE clean, human-readable address for every
    proposal (e.g. proposals@notify.wetreadwell.com). Routing does not depend on
    this address: the proposal travels in the Message-ID headers (see
    `proposal_anchor`), so nothing legible has to be sacrificed to make a reply
    land in the right thread.

    Without INBOUND_REPLY_ADDRESS it falls back to the older token@domain form,
    which routes fine but shows the customer a wall of random characters."""
    if config.INBOUND_REPLY_ADDRESS:
        return config.INBOUND_REPLY_ADDRESS
    if not (config.RESEND_INBOUND_DOMAIN and token):
        return None
    return f"{token}@{config.RESEND_INBOUND_DOMAIN}"


def send_portal_link(email: str, name: str, url: str, project_name: str,
                     reply_to: str | None = None, note: str | None = None,
                     token: str | None = None, revised: bool = False) -> bool:
    """`revised` marks a re-send that carries genuinely different numbers, so the
    customer isn't left wondering whether this is the same proposal again. It also
    tells them the earlier version no longer stands — the portal has reopened it for
    approval, and silently resending "your proposal is ready" would hide that."""
    # Greet by FIRST name only; `note` is the estimator's optional personal message
    # (entered on the Done page before sending) shown above the button.
    note_html = ""
    if note and str(note).strip():
        note_html = (
            f'<p style="margin:16px 0;padding:12px 14px;background:#f8fafc;'
            f'border-left:3px solid {_BRAND_RED};white-space:pre-wrap">{_esc(note)}</p>'
        )
    lead = ("A revised proposal for <strong>%s</strong> is ready to review. It replaces the "
            "version we sent previously." % _esc(project_name)) if revised else \
           ("Your proposal for <strong>%s</strong> is ready to review." % _esc(project_name))
    body = (
        f'<p>Hi {_esc(_first_name(name) or "there")},</p>'
        f'<p>{lead}</p>'
        f'{note_html}'
        f'<p style="margin:20px 0"><a href="{url}" style="background:{_BRAND_RED};color:#fff;'
        f'padding:12px 20px;border-radius:8px;text-decoration:none;font-weight:700">'
        f'{"View the revised proposal" if revised else "View your proposal"}</a></p>'
        f'<p style="color:#64748b">You can view it, ask questions, and approve it right on the page.</p>'
    )
    subject = (f"Your revised Treadwell proposal — {project_name}" if revised
               else f"Your Treadwell proposal — {project_name}")
    return _send([email], subject,
                 _wrap("Your revised proposal is ready" if revised else "Your proposal is ready", body),
                 _thread_headers(email, token), reply_to=reply_to)


def send_reply_notification(email: str, url: str, project_name: str,
                            reply_to: str | None = None, message: str | None = None,
                            token: str | None = None) -> bool:
    # Only advertise reply-by-email when inbound capture is armed (reply_to set);
    # otherwise steer to the portal so nothing dead-ends.
    nudge = ("You can reply right on your proposal page, or simply reply to this email."
             if reply_to else
             "Reply right on your proposal page (button above) so our team sees it fastest.")
    # Show the actual reply TEXT in the email (Will's ask) — not just a button.
    msg_html = ""
    if message and str(message).strip():
        msg_html = (
            f'<blockquote style="margin:12px 0;padding:8px 14px;border-left:3px solid #cbd5e1;'
            f'color:#334155;white-space:pre-wrap">{_esc(message)}</blockquote>'
        )
    body = (
        f'<p>Treadwell replied to your question on the proposal for <strong>{_esc(project_name)}</strong>:</p>'
        f'{msg_html}'
        f'<p style="margin:20px 0"><a href="{url}" style="background:{_BRAND_RED};color:#fff;'
        f'padding:12px 20px;border-radius:8px;text-decoration:none;font-weight:700">View the reply</a></p>'
        f'<p style="color:#64748b;font-size:13px">{nudge}</p>'
    )
    return _send([email], f"New reply on your proposal — {project_name}", _wrap("You have a new reply", body),
                 _thread_headers(email, token), reply_to=reply_to)


def send_customer_update(email: str, url: str, project_name: str, heading: str,
                         body_html: str, reply_to: str | None = None,
                         token: str | None = None) -> bool:
    """Confirm a milestone to the CUSTOMER — approval, deposit, contacts, dates.

    Every one of these already posted a chat line and (mostly) emailed the team,
    but the customer got nothing, so from their side the project went quiet at
    exactly the moments they'd want acknowledgement. `body_html` is trusted
    markup built by the caller; anything user-supplied must be escaped first."""
    body = (
        f'{body_html}'
        f'<p style="margin:20px 0"><a href="{url}" style="background:{_BRAND_RED};color:#fff;'
        f'padding:12px 20px;border-radius:8px;text-decoration:none;font-weight:700">'
        f'View your project</a></p>'
    )
    return _send([email], f"{heading} — {project_name}", _wrap(heading, body),
                 _thread_headers(email, token), reply_to=reply_to)


def send_deposit_request(email: str, url: str, project_name: str, amount: float | None = None,
                         reply_to: str | None = None, invoice_no: str | None = None,
                         invoice_pdf: bytes | None = None, invoice_filename: str | None = None,
                         reference: str | None = None, token: str | None = None) -> bool:
    """The deposit invoice email. When `invoice_pdf` is supplied the actual
    invoice rides along as an attachment, and the body names its number — so the
    customer receives a document, not just a promise of one."""
    amt = f" of <strong>${amount:,.2f}</strong>" if amount is not None else ""
    inv = f" (invoice <strong>{_esc(invoice_no)}</strong>)" if invoice_no else ""
    attached = ("<p>Your invoice is attached as a PDF.</p>" if invoice_pdf else
                "<p>Your deposit invoice will follow shortly.</p>")
    ref = (f'<p style="color:#475569;font-size:13px">Include reference '
           f'<strong>{_esc(reference)}</strong> with your payment.</p>') if reference else ""
    body = (
        f'<p>Thank you for approving your proposal for <strong>{_esc(project_name)}</strong>.</p>'
        f'<p>A deposit{amt}{inv} reserves your place on our schedule.</p>'
        f'{attached}'
        f'<p>You can pay by ACH straight from the portal (fastest), or mail a check.</p>'
        f'<p style="margin:20px 0"><a href="{url}" style="background:{_BRAND_RED};color:#fff;'
        f'padding:12px 20px;border-radius:8px;text-decoration:none;font-weight:700">Pay your deposit</a></p>'
        f'{ref}'
    )
    subject = (f"Invoice {invoice_no} — deposit for {project_name}" if invoice_no
               else f"Deposit requested — {project_name}")
    atts = [(invoice_filename or "Treadwell-Invoice.pdf", invoice_pdf)] if invoice_pdf else None
    return _send([email], subject, _wrap("Deposit invoice", body),
                 _thread_headers(email, token), reply_to=reply_to, attachments=atts)


def resolve_notify_recipients(general_rows, deposit_rows, kind, env_general, env_deposit,
                              adds=(), mutes=(), configured=None) -> list[str]:
    """Pure recipient resolution for team notifications, fully driven by the
    UI-managed roster (not hardcoded env). Base list: when the roster is CONFIGURED
    (any rows exist), a 'deposit' alert prefers deposit-kind rows then general rows,
    a 'general' alert uses general rows. Then per-project overrides apply: union
    `adds`, subtract `mutes` (mute wins over add) — case-insensitive, order-preserving,
    deduped.

    `configured` tells apart two empty states: an UNCONFIGURED roster (no rows at all,
    e.g. fresh install) falls back to the env list, but a CONFIGURED roster whose
    enabled bucket is empty (everyone toggled off) sends to NOBODY — it must NOT
    resurrect the env default inbox. `configured=None` infers from the rows passed
    (back-compat for callers that pass only the 5 base args)."""
    if configured is None:
        configured = bool(general_rows or deposit_rows)
    if kind == "deposit":
        base = list(deposit_rows or general_rows) if configured else list(env_deposit)
    else:
        base = list(general_rows) if configured else list(env_general)
    mute_set = {m.strip().lower() for m in (mutes or []) if m}
    out, seen = [], set()
    for e in list(base) + list(adds or []):
        if not e:
            continue
        key = e.strip().lower()
        if key in seen or key in mute_set:
            continue
        seen.add(key)
        out.append(e)
    return out


def _resolve_notify(kind: str, proposal_id: str | None = None) -> list[str]:
    """Resolve recipients from the roster (enabled rows only) plus this proposal's
    per-project overrides. On DB failure, fall back to env (don't go silent just
    because the table was momentarily unreachable)."""
    general, deposit, adds, mutes = [], [], [], []
    configured = False
    try:
        import db  # local import: avoid a hard DB dependency at module import time
        rows = db.list_notify_recipients()
        configured = bool(rows)
        for r in rows:
            if not r.get("enabled", True):   # gray toggle → excluded
                continue
            (deposit if r.get("kind") == "deposit" else general).append(r["email"])
    except Exception as exc:  # noqa: BLE001 — DB down / table missing → env fallback
        log.warning("notify-recipient lookup failed (%s); using env fallback", exc)
        configured = False
    if proposal_id:
        # Separate try: an overrides-fetch failure must NOT discard the roster we
        # just loaded (or it would silently fall back to the env list).
        try:
            import db
            for o in db.list_notify_overrides(proposal_id):
                (adds if o.get("mode") == "add" else mutes).append(o["email"])
        except Exception as exc:  # noqa: BLE001 — ignore overrides, keep the roster
            log.warning("notify-override lookup failed (%s); ignoring per-project overrides", exc)
    return resolve_notify_recipients(general, deposit, kind, config.NOTIFY_EMAILS,
                                     config.DEPOSIT_NOTIFY_EMAILS, adds=adds, mutes=mutes,
                                     configured=configured)


def staff_emails() -> set[str]:
    """Lowercase addresses of everyone on the notification roster (enabled rows,
    both kinds) — the allowlist for "this inbound email came from staff".

    Deliberately narrower than "any @wetreadwell.com address": a From header is
    forgeable, and the inbound webhook's signature proves the message came from
    Resend, NOT that the sender is who they claim. The roster is the UI-managed
    set of people who receive the notifications we now put a proposal Reply-To
    on, so it is exactly closed under the intended workflow. Residual risk: an
    attacker who knows both a roster address and a live token address could still
    forge From and speak as Treadwell — DMARC (p=reject) on the sending domain is
    the mitigation, and it lives in DNS, not here.

    On DB failure, fall back to the env lists (same posture as _resolve_notify:
    don't lose the allowlist because the table blinked)."""
    try:
        import db  # local import: avoid a hard DB dependency at module import time
        rows = db.list_notify_recipients()
        if rows:
            return {r["email"].strip().lower() for r in rows
                    if r.get("enabled", True) and r.get("email")}
    except Exception as exc:  # noqa: BLE001 — DB down / table missing → env fallback
        log.warning("staff-roster lookup failed (%s); using env fallback", exc)
    return {e.strip().lower() for e in [*config.NOTIFY_EMAILS, *config.DEPOSIT_NOTIFY_EMAILS] if e}


def notify_team(subject: str, body_html: str, kind: str = "general",
                recipients: list[str] | None = None, reply_link: str | None = None,
                proposal_id: str | None = None, reply_to: str | None = None) -> bool:
    """Email the internal team. `recipients` (explicit) wins; otherwise resolve by
    `kind` from the UI-managed roster, applying this proposal's per-project overrides
    (`proposal_id`). `reply_link` appends a "Reply in Portal" button that deep-links
    staff to the proposal in the staff tool. `reply_to` (the proposal's inbound
    address) makes a plain reply from a staff inbox land in the thread too, so the
    button is the convenient path rather than the only one."""
    to = recipients if recipients is not None else _resolve_notify(kind, proposal_id)
    if not to:
        log.info("notify: no recipients after roster/overrides — skipped (%r)", subject)
    if reply_link:
        body_html += (
            f'<p style="margin-top:16px"><a href="{reply_link}" style="background:{_BRAND_RED};color:#fff;'
            f'padding:10px 18px;border-radius:8px;text-decoration:none;font-weight:700">Reply in Portal</a></p>'
        )
    if reply_to:
        body_html += ('<p style="color:#64748b;font-size:13px;margin-top:12px">Replying to this email '
                      'posts your message to the customer\'s portal thread and notifies them.</p>')
    return _send(to, subject, _wrap(subject, body_html), reply_to=reply_to)
