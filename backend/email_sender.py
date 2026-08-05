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
import followup_settings

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


# ── Automated follow-ups ──────────────────────────────────────────────────────
# Sent by followup_worker on the cadence in followup_rules. Every one carries the
# per-proposal Reply-To and thread anchor, so a customer who just replies lands in
# the right chat thread instead of in a black hole.

def _status_ask_html(token: str) -> str:
    """"Has your timeline changed?" — the customer's way out.

    From the recurring stage onward, every follow-up offers this. It is also the
    polite unsubscribe: a customer who is not moving forward can say so in one click
    instead of ignoring us until we stop, and staff learn why rather than guessing."""
    base = f"{config.PUBLIC_BASE_URL}/p/{token}#status"
    return (
        f'<div style="margin:22px 0 6px;padding:14px 16px;background:#f8fafc;'
        f'border:1px solid #e2e8f0;border-radius:10px">'
        f'<p style="margin:0 0 10px;font-weight:600">Has your timeline changed?</p>'
        f'<p style="margin:0 0 12px;color:#475569;font-size:14px">'
        f'Let us know and we\'ll stop the reminders.</p>'
        f'<a href="{base}" style="display:inline-block;margin-right:8px;padding:9px 14px;'
        f'border:1px solid #cbd5e1;border-radius:8px;text-decoration:none;color:#0f172a;'
        f'font-weight:600;font-size:14px">Project delayed</a>'
        f'<a href="{base}" style="display:inline-block;padding:9px 14px;border:1px solid #cbd5e1;'
        f'border-radius:8px;text-decoration:none;color:#0f172a;font-weight:600;font-size:14px">'
        f'Not moving forward</a>'
        f'</div>'
    )


def _cta(url: str, label: str) -> str:
    return (f'<p style="margin:20px 0"><a href="{url}" style="background:{_BRAND_RED};color:#fff;'
            f'padding:12px 20px;border-radius:8px;text-decoration:none;font-weight:700">'
            f'{label}</a></p>')


# Stands in for {link} while an edited body is escaped, so the anchor markup is added AFTER the
# escaping rather than being mangled by it.
#
# Wrapped in control characters rather than being a word like TWLINK, which somebody could type
# and have silently turned into a link they never asked for. `_clean_text` strips control
# characters from every stored body, so this cannot appear in a saved template at all. It exists
# only in memory, between render and escape. Written as escapes, never as raw bytes in source.
_LINK_MARK = "\u0001TWLINK\u0001"


def _inline_link(url: str, label: str) -> str:
    """A link inside a sentence. Not the button.

    A big red block button dropped mid-paragraph reads as a mistake, so an inline mention gets an
    ordinary underlined link in the brand colour instead. Both routes end up clickable, which is
    the part that matters."""
    return (f'<a href="{url}" style="color:{_BRAND_RED};font-weight:600">{label}</a>')


def _block_html(block: str, url: str, label: str) -> str:
    """One blank-line-separated block of an edited body, as email HTML.

    Escape first, then place the link, so a `{link}` written into a sentence produces a real
    anchor instead of escaped source text. A block that is ONLY the link keeps the branded button;
    anywhere else it becomes an inline link.
    """
    if block.lstrip().startswith("<"):
        return block                                   # already-built markup (the shipped bodies)
    if block.strip() == _LINK_MARK:
        return _cta(url, label)
    html = _esc(block).replace("\n", "<br>")
    if _LINK_MARK in html:
        html = html.replace(_LINK_MARK, _inline_link(url, label))
    return f"<p>{html}</p>"


def send_followup(email: str, url: str, project_name: str, template: str, *,
                  name: str = "", deposit_required: bool = True,
                  reply_to: str | None = None, token: str | None = None,
                  include_status_ask: bool = False,
                  templates: dict | None = None) -> bool:
    """One automated follow-up. `template` names which of the four; `templates` is the wording
    staff have saved (followup_settings), and None means use the wording as shipped.

    The deposit sentence is conditional: promising "signed proposal and deposit" on a
    job sent without a deposit requirement would be wrong, and GC work usually is."""
    greeting = f'<p>Hi {_esc(_first_name(name) or "there")},</p>' if name else ""
    need = ("your signed approval and the deposit" if deposit_required
            else "your signed approval")

    # Saved wording wins when there is any. The hardcoded versions below stay as the fallback
    # rather than being deleted: an absent, partial or hand-broken settings row must still send a
    # well-worded email, and "as shipped" is a better failure mode than a blank body.
    saved = (templates or {}).get(template) if isinstance(templates, dict) else None
    if isinstance(saved, dict) and saved.get("body"):
        # The link is rendered as a MARKER first, and turned into markup per block below.
        #
        # It used to be substituted as finished HTML before the escape pass, which worked only
        # while `{link}` sat alone on its own line. Written into a sentence — "just click {link}
        # when you get a moment" — the block no longer began with "<", so the whole thing went
        # through _esc and the customer received the anchor tag as visible source text with
        # nothing clickable in the email at all. The editor inserts the token at the caret, so
        # mid-sentence is exactly what its own UI invites.
        label = saved.get("cta") or "View your proposal"
        rendered = followup_settings.render(
            saved,
            first_name=_first_name(name) or "there",
            project=project_name,
            need=need,
            link_html=_LINK_MARK,
        )
        body_html = "".join(
            _block_html(block, url, label)
            for block in [b.strip() for b in rendered["body"].split("\n\n")] if block
        )
        if include_status_ask and token:
            body_html += _status_ask_html(token)
        return _send([email], rendered["subject"], _wrap(rendered["title"], body_html),
                     _thread_headers(email, token), reply_to=reply_to)

    if template == "not_viewed":
        subject = f"Your Treadwell proposal for {project_name} is ready when you are"
        title = "Your proposal is waiting"
        body = (
            f'{greeting}'
            f'<p>We sent over the proposal for <strong>{_esc(project_name)}</strong> and '
            f'wanted to make sure it reached you.</p>'
            f'{_cta(url, "View your proposal")}'
            f'<p style="color:#64748b">Any questions at all, just reply to this email — '
            f'it comes straight to us.</p>'
        )
    elif template == "next_steps":
        subject = f"Next steps for {project_name}"
        title = "Getting you on the schedule"
        body = (
            f'{greeting}'
            f'<p>Thanks for taking a look at the proposal for '
            f'<strong>{_esc(project_name)}</strong>.</p>'
            f'<p>Whenever you\'re ready, we need {need} before we can book your dates.</p>'
            f'{_cta(url, "Review and approve")}'
            f'<p style="color:#64748b">If anything needs changing first, reply and tell us — '
            f'we\'d rather adjust it than have it sit.</p>'
        )
    elif template == "second_nudge":
        subject = f"Quick reminder — {project_name}"
        title = "Still holding your spot"
        body = (
            f'{greeting}'
            f'<p>Just a nudge that the proposal for <strong>{_esc(project_name)}</strong> '
            f'is still pending. We need {need} to schedule the work.</p>'
            f'{_cta(url, "Review and approve")}'
            f'<p style="color:#64748b">Happy to walk through it or price an alternative — '
            f'a reply is enough.</p>'
        )
    else:   # "checkin" — the recurring stage
        subject = f"Checking in on {project_name}"
        title = "Checking in"
        body = (
            f'{greeting}'
            f'<p>Circling back on <strong>{_esc(project_name)}</strong>. It\'s still open on '
            f'our side and we need {need} whenever the timing works.</p>'
            f'{_cta(url, "View your proposal")}'
        )

    if include_status_ask and token:
        body += _status_ask_html(token)
    return _send([email], subject, _wrap(title, body),
                 _thread_headers(email, token), reply_to=reply_to)


# ── the morning digest ────────────────────────────────────────────────────────
# One email per estimator, at most five proposals, ranked and reasoned by the
# proposal tool's digest_worker. This module only renders and sends: the scoring is
# arithmetic over there so "why is this first?" has a stable answer, and the sentence
# on each row was written from those same facts.

def _digest_row(it: dict, staff_url: str) -> str:
    """One proposal. The project name is the link — an estimator reading this on a
    phone at 6 AM taps the name, not a button at the end of five rows."""
    name = _esc(str(it.get("project_name") or "Proposal"))
    customer = _esc(str(it.get("customer") or ""))
    reason = _esc(str(it.get("reason") or ""))
    stage = _esc(str(it.get("stage") or ""))
    total = it.get("total")
    money = "${:,.0f}".format(float(total)) if isinstance(total, (int, float)) and total else ""
    streak = int(it.get("streak") or 1)
    meta = " · ".join(x for x in (customer, stage, money) if x)
    # Said in words, because "3rd morning" is the difference between a reminder and
    # a duplicate email nobody trusts.
    again = (f'<span style="display:inline-block;margin-left:6px;padding:1px 7px;border-radius:999px;'
             f'background:#fef3c7;color:#78350f;font-size:11px;font-weight:700">'
             f'{streak}rd morning running</span>' if streak >= 3
             else '<span style="display:inline-block;margin-left:6px;padding:1px 7px;border-radius:999px;'
                  'background:#f1f5f9;color:#475569;font-size:11px;font-weight:700">again today</span>'
             if streak == 2 else "")
    more = it.get("and_more")
    tail = (f'<p style="margin:6px 0 0;color:#64748b;font-size:13px">'
            f'…and {int(more)} more over the bar that didn\'t fit in this email.</p>'
            if isinstance(more, int) and more > 0 else "")
    # Built out here, not inline: an f-string expression can't contain a backslash on
    # Python 3.11, which is what the container runs. It parses fine on a newer local
    # interpreter, so the tests pass and the deploy crashes on import.
    meta_html = (f'<p style="margin:0 0 6px;color:#64748b;font-size:13px">{meta}</p>'
                 if meta else "")
    return (
        f'<div style="padding:14px 0;border-top:1px solid #e2e8f0">'
        f'<p style="margin:0 0 3px;font-size:15px;font-weight:700">'
        f'<a href="{staff_url}" style="color:#0f172a;text-decoration:none">{name}</a>{again}</p>'
        f'{meta_html}'
        f'<p style="margin:0;color:#334155;font-size:14px">{reason}</p>'
        f'{tail}</div>'
    )


def send_digest(email: str, items: list[dict], *, name: str = "",
                staff_link=None) -> bool:
    """The 6 AM list. `staff_link(proposal_id)` builds the CRM deep link.

    Returns False without sending when there is nothing to chase — an empty digest
    every morning is how a daily email becomes one people filter away, and then the
    one that matters goes unread too."""
    if not items:
        log.info("digest: nothing to chase for %s — not sending", email)
        return False
    n = len(items)
    greeting = f'<p>Morning {_esc(_first_name(name) or "")},</p>' if name else "<p>Morning,</p>"
    rows = "".join(_digest_row(it, staff_link(it.get("proposal_id")) if staff_link else "#")
                   for it in items)
    body = (
        f'{greeting}'
        f'<p>{n} proposal{"" if n == 1 else "s"} worth a follow-up today.</p>'
        f'{rows}'
        f'<hr style="border:none;border-top:1px solid #e2e8f0;margin:20px 0 14px">'
        f'<p style="color:#64748b;font-size:13px">Logging a call or a text in the CRM takes it '
        f'off tomorrow\'s list. Automatic customer follow-ups keep running either way.</p>'
    )
    subject = f"{n} proposal{'' if n == 1 else 's'} to follow up today"
    return _send([email], subject, _wrap("Your follow-ups for today", body),
                 _digest_headers(email))


def _digest_headers(email: str) -> dict:
    """Its own thread, like the OTP. Threading the digest onto a proposal's
    conversation would bury the customer's actual messages under a daily email —
    and there is no single proposal it belongs to anyway."""
    anchor = hashlib.sha1(f"tw-digest:{email.lower()}".encode()).hexdigest()[:16]
    return {"References": f"<treadwell-digest.{anchor}@wetreadwell.com>"}


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
