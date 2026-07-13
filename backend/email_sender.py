"""Email via Resend. Graceful: with no RESEND_API_KEY (local dev) it logs the
message to stdout instead of sending, so the full flow is testable offline.
"""
from __future__ import annotations

import hashlib
import logging

import httpx

import config

log = logging.getLogger("portal.email")

_RESEND_URL = "https://api.resend.com/emails"


def _thread_headers(email: str) -> dict[str, str]:
    """Group every portal email to one customer into a single inbox thread, so the
    login code always lands in the same conversation as the proposal link (and is
    never shown on a web page). A stable per-recipient anchor in References/In-Reply-To
    makes Gmail and most clients thread the proposal, the code, and reply notices
    together."""
    anchor = hashlib.sha1((email or "").strip().lower().encode()).hexdigest()[:24]
    mid = f"<treadwell-portal.{anchor}@wetreadwell.com>"
    return {"References": mid, "In-Reply-To": mid}


def _send(to: list[str], subject: str, html: str, headers: dict[str, str] | None = None) -> bool:
    to = [t for t in to if t]
    if not to:
        return False
    if not config.RESEND_API_KEY:
        log.warning("[email:dev] would send to=%s subject=%r\n%s", to, subject, html)
        return True
    try:
        payload: dict = {"from": config.EMAIL_FROM, "to": to, "subject": subject, "html": html}
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
        f'<p style="color:#64748b;font-size:12px">Treadwell — commercial epoxy &amp; polished concrete.</p></div>'
    )


def send_otp(email: str, code: str, project_name: str) -> bool:
    body = (
        f'<p>Use this code to view your proposal for <strong>{project_name}</strong>:</p>'
        f'<p style="font-size:30px;font-weight:800;letter-spacing:6px;margin:16px 0">{code}</p>'
        f'<p style="color:#64748b">This code expires in {config.OTP_TTL_MINUTES} minutes.</p>'
    )
    return _send([email], "Your Treadwell proposal access code", _wrap("Your access code", body),
                 _thread_headers(email))


def send_portal_link(email: str, name: str, url: str, project_name: str) -> bool:
    body = (
        f'<p>Hi {name or "there"},</p>'
        f'<p>Your proposal for <strong>{project_name}</strong> is ready to review.</p>'
        f'<p style="margin:20px 0"><a href="{url}" style="background:#0ea5e9;color:#fff;'
        f'padding:12px 20px;border-radius:8px;text-decoration:none;font-weight:700">View your proposal</a></p>'
        f'<p style="color:#64748b">You can view it, ask questions, and approve it right on the page.</p>'
    )
    return _send([email], f"Your Treadwell proposal — {project_name}", _wrap("Your proposal is ready", body),
                 _thread_headers(email))


def send_reply_notification(email: str, url: str, project_name: str) -> bool:
    body = (
        f'<p>Treadwell replied to your question on the proposal for <strong>{project_name}</strong>.</p>'
        f'<p style="margin:20px 0"><a href="{url}" style="background:#0ea5e9;color:#fff;'
        f'padding:12px 20px;border-radius:8px;text-decoration:none;font-weight:700">View the reply</a></p>'
    )
    return _send([email], f"New reply on your proposal — {project_name}", _wrap("You have a new reply", body),
                 _thread_headers(email))


def notify_team(subject: str, body_html: str, recipients: list[str] | None = None) -> bool:
    return _send(recipients or config.NOTIFY_EMAILS, subject, _wrap(subject, body_html))
