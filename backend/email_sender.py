"""Email via Resend. Graceful: with no RESEND_API_KEY (local dev) it logs the
message to stdout instead of sending, so the full flow is testable offline.
"""
from __future__ import annotations

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


def _thread_headers(email: str) -> dict[str, str]:
    """Group every portal email to one customer into a single inbox thread, so the
    login code always lands in the same conversation as the proposal link (and is
    never shown on a web page). A stable per-recipient anchor in References/In-Reply-To
    makes Gmail and most clients thread the proposal, the code, and reply notices
    together."""
    anchor = hashlib.sha1((email or "").strip().lower().encode()).hexdigest()[:24]
    mid = f"<treadwell-portal.{anchor}@wetreadwell.com>"
    return {"References": mid, "In-Reply-To": mid}


def _send(to: list[str], subject: str, html: str, headers: dict[str, str] | None = None,
          reply_to: str | None = None) -> bool:
    to = [t for t in to if t]
    if not to:
        return False
    if not config.RESEND_API_KEY:
        log.warning("[email:dev] would send to=%s subject=%r\n%s", to, subject, html)
        return True
    try:
        payload: dict = {"from": config.EMAIL_FROM, "to": to, "subject": subject, "html": html}
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


def _wrap(title: str, body_html: str) -> str:
    return (
        f'<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'
        f'max-width:520px;margin:0 auto;color:#0f172a">'
        f'<h2 style="color:#0f172a;margin:0 0 12px">{title}</h2>{body_html}'
        f'<hr style="border:none;border-top:1px solid #e2e8f0;margin:24px 0">'
        f'{_SIGNATURE_HTML}</div>'
    )


# Footer / signatory on EVERY outgoing email (single choke-point — _wrap wraps
# them all). Address first, then the tagline, per Will.
_SIGNATURE_HTML = (
    '<p style="color:#64748b;font-size:12px;line-height:1.6;margin:0">'
    '1707 E. 123rd Ter, Olathe, KS 66061<br>'
    'Treadwell — commercial epoxy &amp; polished concrete.</p>'
)


def send_otp(email: str, code: str, project_name: str) -> bool:
    body = (
        f'<p>Use this code to view your proposal for <strong>{project_name}</strong>:</p>'
        f'<p style="font-size:30px;font-weight:800;letter-spacing:6px;margin:16px 0">{code}</p>'
        f'<p style="color:#64748b">This code expires in {config.OTP_TTL_MINUTES} minutes.</p>'
    )
    return _send([email], "Your Treadwell proposal access code", _wrap("Your access code", body),
                 _thread_headers(email))


def proposal_reply_to(token: str) -> str | None:
    """The per-proposal inbound-capture Reply-To (token@receiving-domain), or
    None when inbound receiving isn't configured. An email reply to this address
    routes through Resend's webhook back into the proposal's CRM thread."""
    if not (config.RESEND_INBOUND_DOMAIN and token):
        return None
    return f"{token}@{config.RESEND_INBOUND_DOMAIN}"


def send_portal_link(email: str, name: str, url: str, project_name: str,
                     reply_to: str | None = None, note: str | None = None) -> bool:
    # Greet by FIRST name only; `note` is the estimator's optional personal message
    # (entered on the Done page before sending) shown above the button.
    note_html = ""
    if note and str(note).strip():
        note_html = (
            f'<p style="margin:16px 0;padding:12px 14px;background:#f8fafc;'
            f'border-left:3px solid #0ea5e9;white-space:pre-wrap">{_esc(note)}</p>'
        )
    body = (
        f'<p>Hi {_esc(_first_name(name) or "there")},</p>'
        f'<p>Your proposal for <strong>{_esc(project_name)}</strong> is ready to review.</p>'
        f'{note_html}'
        f'<p style="margin:20px 0"><a href="{url}" style="background:#0ea5e9;color:#fff;'
        f'padding:12px 20px;border-radius:8px;text-decoration:none;font-weight:700">View your proposal</a></p>'
        f'<p style="color:#64748b">You can view it, ask questions, and approve it right on the page.</p>'
    )
    return _send([email], f"Your Treadwell proposal — {project_name}", _wrap("Your proposal is ready", body),
                 _thread_headers(email), reply_to=reply_to)


def send_reply_notification(email: str, url: str, project_name: str,
                            reply_to: str | None = None, message: str | None = None) -> bool:
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
        f'<p style="margin:20px 0"><a href="{url}" style="background:#0ea5e9;color:#fff;'
        f'padding:12px 20px;border-radius:8px;text-decoration:none;font-weight:700">View the reply</a></p>'
        f'<p style="color:#64748b;font-size:13px">{nudge}</p>'
    )
    return _send([email], f"New reply on your proposal — {project_name}", _wrap("You have a new reply", body),
                 _thread_headers(email), reply_to=reply_to)


def send_deposit_request(email: str, url: str, project_name: str, amount: float | None = None,
                         reply_to: str | None = None) -> bool:
    amt = f" of <strong>${amount:,.2f}</strong>" if amount is not None else ""
    body = (
        f'<p>Thank you for approving your proposal for <strong>{project_name}</strong>.</p>'
        f'<p>A deposit{amt} reserves your place on our schedule. Open your proposal for the '
        f'bank-transfer instructions and the reference to include with your payment.</p>'
        f'<p style="margin:20px 0"><a href="{url}" style="background:#0ea5e9;color:#fff;'
        f'padding:12px 20px;border-radius:8px;text-decoration:none;font-weight:700">View your proposal</a></p>'
    )
    return _send([email], f"Deposit requested — {project_name}", _wrap("Deposit requested", body),
                 _thread_headers(email), reply_to=reply_to)


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


def notify_team(subject: str, body_html: str, kind: str = "general",
                recipients: list[str] | None = None, reply_link: str | None = None,
                proposal_id: str | None = None) -> bool:
    """Email the internal team. `recipients` (explicit) wins; otherwise resolve by
    `kind` from the UI-managed roster, applying this proposal's per-project overrides
    (`proposal_id`). `reply_link` appends a "Reply in Portal" button that deep-links
    staff to the proposal in the staff tool (so they answer in-portal, not by email)."""
    to = recipients if recipients is not None else _resolve_notify(kind, proposal_id)
    if not to:
        log.info("notify: no recipients after roster/overrides — skipped (%r)", subject)
    if reply_link:
        body_html += (
            f'<p style="margin-top:16px"><a href="{reply_link}" style="background:#0ea5e9;color:#fff;'
            f'padding:10px 18px;border-radius:8px;text-decoration:none;font-weight:700">Reply in Portal</a></p>'
        )
    return _send(to, subject, _wrap(subject, body_html))
