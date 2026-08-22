"""Email via Resend. Graceful: with no RESEND_API_KEY (local dev) it logs the
message to stdout instead of sending, so the full flow is testable offline.
"""
from __future__ import annotations

import base64
import hashlib
import html
import logging
import time

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


DEFAULT_THREAD_SUBJECT = followup_settings.DEFAULT_THREAD_SUBJECT


def customer_thread_subject(project_name: str | None) -> str:
    """The ONE subject every customer email about a project carries.

    Hanz, 2026-08-11: "for all updates to one project can we have it in one email thread?"

    The threading headers were already right — every project email shares the proposal
    anchor. The subject was what kept splitting it. Gmail groups by the References chain
    AND the subject, so "Your Treadwell proposal — X", then "Deposit requested — X", then
    "Checking in on X" produced three conversations about one job no matter what the
    headers said. A constant subject is the right answer for stricter clients too: it is
    what the thread is ABOUT, and the specific event belongs in the heading, which is the
    line Gmail shows as the snippet anyway.

    Deliberately NOT applied to the access code or the morning digest. A code is transient
    and gets its own conversation (see _otp_headers, which Hanz confirmed he wants kept
    separate), and the digest spans every project rather than belonging to one.

    EDITABLE, on the Auto Followups page. It has to be: the four follow-up emails already had
    a per-template "Subject line" field there, and this change is what makes them share one.
    Leaving that field on screen while ignoring it would have meant somebody types a subject,
    saves, and nothing happens — so the field moved up to project level instead of dying.

    Best-effort read. An unreadable settings row falls back to the shipped wording rather than
    failing the send: an email with a slightly different subject is a split thread, an email
    that never goes out is a customer who hears nothing.
    """
    return _thread_subject_template().replace("{project}", project_name or "your project")


_SUBJECT_TTL = 60.0
_subject_cache: tuple[float, str] | None = None


def _thread_subject_template() -> str:
    """The configured template, cached for a minute.

    Cached because this is read on EVERY project email, and a send is not a place to be
    waiting on the database: without it a connection-pool stall costs 30 seconds per email
    before falling back to wording we already had in hand. A minute is short enough that an
    edit on the Auto Followups page shows up on the next send.

    Only SUCCESS is cached. Caching a failure would pin the shipped wording for a minute
    after a single blip, which is the one thing that would actually split a live thread.
    """
    global _subject_cache
    now = time.monotonic()
    if _subject_cache and _subject_cache[0] > now:
        return _subject_cache[1]
    try:
        import db  # local import: avoid a hard DB dependency at module import time
        cfg = followup_settings.merge(db.get_settings(followup_settings.ROW_ID))
    except Exception as exc:  # noqa: BLE001
        log.warning("thread subject unreadable (%s); using the shipped wording", exc)
        return DEFAULT_THREAD_SUBJECT
    template = cfg.get("thread_subject") or DEFAULT_THREAD_SUBJECT
    _subject_cache = (now + _SUBJECT_TTL, template)
    return template


def staff_thread_subject(project_name: str | None) -> str:
    """The customer-facing wording read wrong in a shared bids@ inbox, so staff get their
    own form of the same idea: the project IS the subject, the event is the heading."""
    return f"[Treadwell] {project_name or 'proposal'}"


def project_thread_headers(token: str | None) -> dict[str, str] | None:
    """Threading headers for a STAFF email about one project. None without a token.

    The proposal anchor and nothing else, keyed on the PROJECT with nothing
    recipient-specific in it — unlike `_thread_headers`, the customer equivalent, which
    hashes one address. That property is what lets notify_team mail each person on the
    roster separately (see the loop there) without splitting the conversation: every
    copy carries the same References, so every staff member still gets one thread per
    project, which is how they read it anyway.

    Hanz, 2026-08-11: "When a Treadwell employee replies through email it doesn't
    get captured by the Proposal CRM and doesn't get sent out to the customer."
    This was the cause. The inbound webhook routes a reply by finding a token —
    first in the recipient address, then in these headers. Since INBOUND_REPLY_ADDRESS
    made the visible Reply-To ONE address for every project, the headers became the
    only route left, and staff notifications were the one kind of mail that never
    carried them. So a staff reply matched no proposal, took the "unmatched" branch,
    and was forwarded back to the roster instead of reaching the customer. Customer
    replies were unaffected: send_reply_notification has always passed a token."""
    if not token:
        return None
    anchor = proposal_anchor(token)
    return {"References": anchor, "In-Reply-To": anchor}


def _thread_headers(email: str, token: str | None = None) -> dict[str, str]:
    """Group portal email into inbox threads AND carry the proposal identity.

    WITH A TOKEN: the proposal anchor and nothing else, so a project's mail forms exactly one
    conversation containing only that project.

    Hanz, 2026-08-13: "the treadwell access code should not be the same email thread, only the
    projects." The access code was never the problem — it got its own anchor (`_otp_headers`) on
    2026-08-11. The problem was here: this function still prefixed a PER-RECIPIENT anchor,
    `<treadwell-portal.<sha1(email)>@…>`, to References, and that is the exact Message-ID every
    access code sent BEFORE 2026-08-11 went out on. Gmail threads on References-graph
    connectivity, so a project email sharing that node gets filed into the conversation already
    holding those old codes. The codes were not joining the project thread; the project thread was
    joining the codes. The per-recipient anchor's only documented job was to make the login code
    land beside the proposal link — the opposite of what is wanted now — so it goes.

    It was also a latent cross-project merge: two proposals to the same customer shared that node,
    and only In-Reply-To and the subject kept them apart.

    WITHOUT A TOKEN: no proposal to anchor to, so fall back to the per-recipient mid. Nothing
    reaches this branch today (every customer sender passes a token) — it is the safe default for
    a future caller rather than live behaviour.

    Gmail never un-merges an existing conversation: this stops NEW mail from joining a merged
    thread; threads already merged stay merged."""
    recipient = hashlib.sha1((email or "").strip().lower().encode()).hexdigest()[:24]
    mid = f"<treadwell-portal.{recipient}@wetreadwell.com>"
    if not token:
        return {"References": mid, "In-Reply-To": mid}
    anchor = proposal_anchor(token)
    return {"References": anchor, "In-Reply-To": anchor}


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
    # A FIRST send renders the editable "Proposal sent" template. Hanz, 2026-08-12: "Create the
    # ability to change what the first proposal sent email looks like. from the heading to the
    # content (this would be the global setting for the first proposal sent) Just like the emails
    # for the follow ups." So it goes through exactly the pipeline send_followup uses — same
    # tokens, same blank-line-separated blocks, same {link}-becomes-the-button rule — and the
    # editor's preview is therefore honest about what goes out.
    #
    # A REVISED send does not. Its wording is the only thing telling the customer that the version
    # they already have no longer stands, and the portal has reopened it for approval; letting an
    # edited template silently replace that is how somebody approves the wrong numbers. Asked,
    # Hanz chose first-send-only.
    if not revised:
        tpl = _sent_template()
        # `tpl.get("body")` as well as `tpl`, even though _sent_template already refuses a
        # body-less template: this is the branch that decides whether a customer gets an email
        # with words in it, and the check costs nothing. A test that stubbed the reader straight
        # past its own guard produced a letterhead with no content, which is exactly the outcome
        # worth being defensive about twice.
        if tpl and tpl.get("body"):
            rendered = followup_settings.render(
                tpl, first_name=_first_name(name), project=project_name,
                need="your signed approval", link_html=_LINK_MARK)
            # The button label comes off the RAW template, not off `rendered` — render() fills
            # tokens in the title and body and deliberately returns nothing else, which is the
            # same reason send_followup reads `saved.get("cta")` rather than the rendered dict.
            label = tpl.get("cta") or "View your proposal"
            blocks = [b.strip() for b in rendered["body"].split("\n\n") if b.strip()]
            html_blocks = []
            for block in blocks:
                # The estimator's personal note keeps its position: immediately above the button,
                # which is where it has always been. Inserted as its own block rather than
                # appended, so an edited template that puts the link mid-body still reads right.
                if note_html and block == _LINK_MARK:
                    html_blocks.append(note_html)
                    note_html = ""
                html_blocks.append(_block_html(block, url, label))
            if note_html:                      # a template with no {link} block of its own
                html_blocks.append(note_html)
            return _send([email], customer_thread_subject(project_name),
                         _wrap(rendered["title"], "".join(html_blocks)),
                         _thread_headers(email, token), reply_to=reply_to)

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
    return _send([email], customer_thread_subject(project_name),
                 _wrap("Your revised proposal is ready" if revised else "Your proposal is ready", body),
                 _thread_headers(email, token), reply_to=reply_to)


_SENT_TPL_TTL = 60.0
_sent_tpl_cache: tuple[float, dict] | None = None


def _sent_template() -> dict | None:
    """The saved "Proposal sent" template, cached for a minute. None if it cannot be read.

    None rather than the shipped default on failure, because the caller falls back to the
    hardcoded copy below — which IS the shipped default, and is the one thing guaranteed to
    render. Publishing a proposal must never fail over a settings read.

    Cached for the same reason the thread subject is: this runs once per recipient on every
    publish, and a connection-pool stall would otherwise cost 30 seconds per address. Success
    only, so a single blip cannot pin the fallback for a minute.
    """
    global _sent_tpl_cache
    now = time.monotonic()
    if _sent_tpl_cache and _sent_tpl_cache[0] > now:
        return _sent_tpl_cache[1]
    try:
        import db
        cfg = followup_settings.merge(db.get_settings(followup_settings.ROW_ID))
        tpl = (cfg.get("templates") or {}).get(followup_settings.SENT_KEY)
        if not (tpl and tpl.get("body")):
            return None
    except Exception as exc:  # noqa: BLE001
        log.warning("sent-email template unreadable (%s); using the shipped copy", exc)
        return None
    _sent_tpl_cache = (now + _SENT_TPL_TTL, tpl)
    return tpl


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
        return _send([email], customer_thread_subject(project_name),
                     _wrap(rendered["title"], body_html),
                     _thread_headers(email, token), reply_to=reply_to)

    # Each branch sets a `title` (the heading) and a body. It used to set a `subject` too,
    # one per template, which is exactly what split a chased proposal into four separate
    # Gmail conversations. The subject is now the project — see customer_thread_subject.
    if template == "not_viewed":
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
        title = "Checking in"
        body = (
            f'{greeting}'
            f'<p>Circling back on <strong>{_esc(project_name)}</strong>. It\'s still open on '
            f'our side and we need {need} whenever the timing works.</p>'
            f'{_cta(url, "View your proposal")}'
        )

    if include_status_ask and token:
        body += _status_ask_html(token)
    return _send([email], customer_thread_subject(project_name), _wrap(title, body),
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
    # Hanz, 2026-08-12: 'Change it o jUST "View proposal here"'. It replaced two sentences
    # explaining that you could reply on the page or to the email — both true, neither needed. A
    # reply-by-email nudge under a "View the reply" button was answering a question nobody had
    # asked, and replying to the email works whether or not we say so (the inbound webhook routes
    # it either way — see the staff-reply fix). A link, not a sentence: it is the only thing left
    # on the line, so it should do something.
    nudge = (f'<a href="{url}" style="color:{_BRAND_RED};font-weight:600;'
             f'text-decoration:none">View proposal here</a>')
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
    return _send([email], customer_thread_subject(project_name), _wrap("You have a new reply", body),
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
    return _send([email], customer_thread_subject(project_name), _wrap(heading, body),
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
    atts = [(invoice_filename or "Treadwell-Invoice.pdf", invoice_pdf)] if invoice_pdf else None
    return _send([email], customer_thread_subject(project_name), _wrap("Deposit invoice", body),
                 _thread_headers(email, token), reply_to=reply_to, attachments=atts)


# CRM steps a notification can belong to.
#
# DERIVED FROM THE CALL SITES, not invented. Every notify_team() in main.py names one of these,
# and each id is named after what actually happened to the project. Nothing here fires from
# nowhere: the mapping is pinned by test_notify_step_coverage.py, which walks main.py's syntax
# tree and fails if a call passes a step this tuple does not contain, or passes none at all.
#
# `general` is NOT a step. It is the FLOOR: the list every step resolves on top of (see
# resolve_notify_recipients). Somebody on the general list hears about everything unless a step
# row of their own says otherwise for that one step.
#
# ORDER IS THE PROJECT'S ORDER, because it becomes the column order of the matrix on the
# Notification Sending page, and a grid whose columns run in the order the work happens can be
# read without hunting.
NOTIFY_STEPS: tuple[tuple[str, str, str], ...] = (
    ("sent", "Proposal sent",
     "The proposal was emailed to the customer, including when a delivery failed."),
    ("viewed", "Proposal opened", "The customer opened the proposal."),
    ("question", "Customer question", "The customer asked a question in the project thread."),
    ("status_change", "Customer status",
     "The customer said the project is delayed, not moving forward, or back on."),
    ("approved", "Proposal approved", "The customer approved and signed."),
    ("deposit_submitted", "Deposit sent",
     "The customer sent ACH details, or told us a check is on the way."),
    ("deposit_received", "Deposit received", "Staff marked the deposit received."),
    ("contacts", "Project contacts", "The customer submitted their project contacts."),
    ("feedback", "Portal feedback", "Somebody sent feedback about the customer portal itself."),
)
NOTIFY_STEP_IDS: tuple[str, ...] = tuple(s for s, _, _ in NOTIFY_STEPS)

# The floor's own id. Stored in the same `kind` column as a step, which is what lets one table
# hold both "who is on the team" and "who is an exception on one step".
GENERAL_KIND = "general"

# The two steps that are about money. They inherit the DEPOSIT env fallback rather than the
# general one, which is what the single old 'deposit' kind did.
DEPOSIT_STEPS: frozenset[str] = frozenset({"deposit_submitted", "deposit_received"})

# What those two steps were BOTH called before 2026-08-21, when the column held exactly
# ('general','deposit'). A surviving row fans out to both deposit steps rather than being
# ignored: kylene@ is one of these on prod, and a widening that silently stopped emailing her
# would be the same class of bug as the swap fixed here on 2026-08-20. The schema change migrates
# these into two step rows; this covers the window before it is applied, and any row a human
# writes by hand afterwards.
LEGACY_DEPOSIT_KIND = "deposit"


def steps_payload() -> list[dict[str, object]]:
    """The step vocabulary, for the UI. Served from here so the Notification Sending page's
    columns cannot drift from what the resolver recognises: the page renders whatever this
    returns and keeps no list of its own.

    `required` marks a step that may not be left reaching nobody (UNSILENCEABLE_STEPS). It rides
    along so the column can SAY so before somebody tries, rather than only reporting a refusal
    afterwards. The refusal itself is enforced server-side in main.admin_notify_step_set; this
    flag is the explanation, never the check."""
    return [{"id": s, "label": label, "hint": hint, "required": s in UNSILENCEABLE_STEPS}
            for s, label, hint in NOTIFY_STEPS]


def steps_for_kind(kind: str) -> tuple[str, ...]:
    """Which step buckets a stored row belongs to. One, normally; BOTH deposit steps for a legacy
    'deposit' row; none at all for the floor, which is not a step."""
    if kind == LEGACY_DEPOSIT_KIND:
        return ("deposit_submitted", "deposit_received")
    return (kind,) if kind in NOTIFY_STEP_IDS else ()


def bucket_notify_rows(rows, step) -> tuple[list[str], list[str], list[str]]:
    """Split the roster rows into the three buckets ONE step resolves from:
    (the floor, this step's opt-ins, this step's suppressions).

    Extracted so `_resolve_notify` and `step_reach` cannot form two opinions about which row means
    what. A grid, a guard and a send that each bucket the rows themselves is three chances to
    disagree about who is emailed.

      * the FLOOR - enabled rows whose kind is 'general'. Everybody on the team.
      * OPT-INS   - enabled rows whose kind is this step. Somebody who is not on the team but
                    should hear about this one moment (kylene@ and the deposit).
      * SUPPRESSIONS - rows whose kind is this step and which are switched OFF. Somebody on the
                    team who does not want this one moment. It beats the floor.

    A DISABLED LEGACY 'deposit' ROW IS NEITHER, and is skipped entirely. Under the old vocabulary
    the column held exactly ('general','deposit') and there was no such thing as a suppression, so
    an off row could only ever have meant "an address somebody typed into the Deposit-alerts card
    and never turned green" - merely not on the deposit list. Reading it as a suppression now would
    invent an instruction nobody gave, and because the legacy kind fans out to BOTH money steps it
    would invent it twice. Measured on rows [hanz general on, will general on, hanz kind='deposit'
    enabled=false]: before the widening `deposit` resolved to ['hanz','will']; treating that row as
    a suppression resolved deposit_submitted AND deposit_received to ['will'] alone. Hanz dropped
    from both deposit emails with nobody touching anything, on the deploy. And it was reachable by
    every address ever added to that card and left grey, because adding has always created the row
    off. schema.sql migrates only `where kind='deposit' and enabled` for the same reason.

    An ENABLED step row that is switched off is still a suppression - that is the whole feature.
    The exemption is only for the legacy kind, whose off state predates the concept.

    A row whose kind is neither the floor nor a step anything recognises is IGNORED here (the
    caller still counts it as `configured`, so a value from the future cannot drop the whole roster
    back to the env inbox)."""
    general: list[str] = []
    opt_ins: list[str] = []
    suppressed: list[str] = []
    for r in rows or ():
        row_kind = r.get("kind") or GENERAL_KIND
        enabled = r.get("enabled", True)
        if row_kind == GENERAL_KIND:
            if enabled:                      # gray toggle -> not on the floor
                general.append(r["email"])
            continue
        # A step row. `steps_for_kind` is what makes a legacy 'deposit' row count for BOTH money
        # steps instead of matching nothing once the column widened.
        if step not in steps_for_kind(row_kind):
            continue
        if not enabled and row_kind == LEGACY_DEPOSIT_KIND:
            continue                         # see above: it cannot have meant "suppress"
        (opt_ins if enabled else suppressed).append(r["email"])
    return general, opt_ins, suppressed


# Steps that must never be left reaching nobody. See `step_reach`.
#
# 'sent' carries the DELIVERY-FAILURE alert as well as the good news: admin_publish sends it with
# "That customer has not received the proposal - open the project and send it again." Every other
# step reports something that happened; this one is also the only warning that something did not,
# and it fires precisely when nobody is watching the project. A suppression that emptied it would
# be a silent failure hidden behind a successful-looking click, so the write is REFUSED rather
# than merely flagged - see main.py's admin_notify_step_set.
UNSILENCEABLE_STEPS: frozenset[str] = frozenset({"sent"})


def step_reach(rows, step) -> list[str]:
    """Who this step reaches ORG-WIDE: the floor plus its opt-ins, minus its suppressions.

    Deliberately no per-project adds or mutes, and no env fallback: this answers "is this step
    configured to reach somebody", which is a question about the roster, not about one job. A step
    that only reaches somebody because one project happens to carry an override is still a step
    nobody set up."""
    general, opt_ins, suppressed = bucket_notify_rows(rows, step)
    off = {e.strip().lower() for e in suppressed if e}
    out, seen = [], set()
    for e in general + opt_ins:
        key = (e or "").strip().lower()
        if not key or key in off or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def resolve_notify_recipients(general_rows, step_rows, step, env_general, env_deposit,
                              adds=(), mutes=(), configured=None, suppressed=()) -> list[str]:
    """Pure recipient resolution for team notifications, fully driven by the UI-managed roster
    (not hardcoded env).

    THE FLOOR. Every step resolves to the enabled GENERAL rows PLUS that step's own enabled rows.
    A step nobody has configured therefore still reaches the team, because an alert that reaches
    nobody is worse than one that reaches too many. That was already true of deposits (the
    2026-08-20 note below) and it is now true of all nine steps.

    THE EXCEPTION. `suppressed` is the addresses whose row FOR THIS STEP is switched off, and it
    beats the floor. Without it, the only way to stop somebody hearing about one moment would be
    to take them off the team entirely, which stops the other eight as well: a cliff, not a knob.
    It is also what keeps the screen honest, because on the matrix every green cell receives and
    every grey cell does not, one rule, readable straight off the grid. What the floor still
    guarantees is the case it exists for: nothing has been SAID about this person and this step.

    A suppression is a STEP row that is off. A GENERAL row that is off is not a suppression: it is
    simply not on the floor, and says nothing about any individual step.

    Then per-project overrides apply: union `adds`, subtract `mutes` (mute wins over add),
    case-insensitive, order-preserving, deduped. A per-project mute outranks everything here,
    step opt-ins included, because it is the narrowest and most deliberate thing anybody can say:
    not me, not this job.

    `configured` tells apart two empty states: an UNCONFIGURED roster (no rows at all, e.g. a
    fresh install) falls back to the env list, but a CONFIGURED roster whose enabled buckets are
    empty (everyone toggled off) sends to NOBODY and must not resurrect the env default inbox.
    `configured=None` infers from the rows passed, for back-compat with callers that pass only the
    five base args.

    `step` is 'general' (or anything unrecognised) for a caller that names no step, and resolves
    to the floor alone, which is exactly where the seven un-named call sites used to land.
    """
    if configured is None:
        configured = bool(general_rows or step_rows)
    if not configured:
        # Fresh install: the env lists are all there is. The two money steps inherit the deposit
        # env list, which is what the single old 'deposit' kind did.
        base = list(env_deposit if step in DEPOSIT_STEPS or step == LEGACY_DEPOSIT_KIND
                    else env_general)
    elif step in NOTIFY_STEP_IDS or step == LEGACY_DEPOSIT_KIND:
        # ADDITIVE, not a swap. This once read `list(deposit_rows or general_rows)`, which let the
        # deposit bucket REPLACE the general roster, so the first deposit-kind row anybody added
        # would have silently stopped every general recipient hearing about deposits. A step alert
        # is MORE people than the floor, not DIFFERENT people: whoever is added for one moment
        # joins the people already told. Rejected alternative: keep the replace semantics and
        # auto-add the money person as a per-project override instead, which hardcodes a named
        # human into the codebase and writes a row per project.
        base = list(general_rows) + list(step_rows)
    else:
        # No step named, or one nothing recognises: the floor, and only the floor. Deliberately
        # NOT additive in the other direction, so somebody added for the deposit alone does not
        # start receiving approvals and questions because of it.
        base = list(general_rows)
    mute_set = {m.strip().lower() for m in (mutes or []) if m}
    # PRECEDENCE, widest to narrowest, later winning: the floor, then this step's opt-ins and
    # suppressions (org-wide, about one moment), then this project's adds, then its mutes.
    #
    # So a step suppression is subtracted from the BASE and a per-project add can still bring
    # somebody back. That is not a loophole, it is the rule that already governed everybody: being
    # the assigned estimator has always reached somebody who is not on the roster at all, and a
    # step row saying "not this moment" is a weaker statement than being absent altogether. The
    # narrow way to stop a specific job reaching you is the per-project mute, which is exactly
    # what it is for and still outranks every line above it.
    step_off = {m.strip().lower() for m in (suppressed or []) if m}
    base = [e for e in base if (e or "").strip().lower() not in step_off]
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


def _resolve_notify(kind: str, proposal_id: str | None = None,
                    assigned_estimator: str | None = None) -> list[str]:
    """Resolve recipients for ONE CRM step from the roster plus this proposal's per-project
    overrides. On DB failure, fall back to env (don't go silent just because the table was
    momentarily unreachable).

    `kind` is a step id from NOTIFY_STEPS. The parameter keeps its old name because it is the
    keyword nine call sites and every test already pass, and because it is still literally the
    row's `kind` column; what widened is the vocabulary, from ('general','deposit') to the nine
    moments the CRM actually emails about.

    THREE BUCKETS come out of one table, keyed on that column — the floor, this step's opt-ins,
    this step's suppressions. `bucket_notify_rows` does the splitting and documents each one,
    including the row shape that is deliberately NONE of them: a DISABLED LEGACY 'deposit' row,
    which predates the concept of a suppression and so cannot have meant one.

    A row whose kind is neither the floor nor a step it recognises is IGNORED for resolution but
    still counted as `configured`, so a value from the future cannot silently drop the whole
    roster back to the env inbox.

    `assigned_estimator` is folded in as a per-project ADD, which is exactly what it is: the
    person who owns THIS job hears about it whether or not they sit on the org-wide roster. Hanz,
    2026-08-13, asking for chat messages to reach "whoever is set for the notification sending of
    that project" — and on that date the enabled roster was hanz@ + will@ only, so a job assigned
    to Kyle emailed neither Kyle nor anybody who knew about it.

    Routed through `adds` rather than prepended by the caller so that a per-project MUTE still
    wins: somebody who explicitly silenced one job does not get dragged back in by being its
    estimator. That also collapses two rules into one — the status-update path used to prepend the
    estimator itself, unconditionally, and therefore ignored mutes."""
    general: list[str] = []
    step_rows: list[str] = []
    suppressed: list[str] = []
    adds: list[str] = []
    mutes: list[str] = []
    configured = False
    try:
        import db  # local import: avoid a hard DB dependency at module import time
        rows = db.list_notify_recipients()
        configured = bool(rows)
        general, step_rows, suppressed = bucket_notify_rows(rows, kind)
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
    est = (assigned_estimator or "").strip()
    if est:
        adds = list(adds) + [est]
    return resolve_notify_recipients(general, step_rows, kind, config.NOTIFY_EMAILS,
                                     config.DEPOSIT_NOTIFY_EMAILS, adds=adds, mutes=mutes,
                                     configured=configured, suppressed=suppressed)


def staff_emails(proposal_id: str | None = None) -> set[str]:
    """Lowercase addresses of everyone on the notification roster (enabled rows,
    both kinds) — the allowlist for "this inbound email came from staff".

    With `proposal_id`, this project's per-project ADDS count too. They have to: an
    override is how somebody gets a project's notification emails without being on
    the global roster, so leaving them out meant the exact person we had just mailed
    could not reply to it. Mutes are NOT subtracted — muting somebody stops this
    project's mail reaching them, it does not make them a customer.

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
    def _adds() -> set[str]:
        """This project's per-project additions; {} on any failure — a missing override
        must narrow the allowlist, never widen or break it."""
        if not proposal_id:
            return set()
        try:
            import db
            return {o["email"].strip().lower() for o in db.list_notify_overrides(proposal_id)
                    if o.get("mode") == "add" and o.get("email")}
        except Exception as exc:  # noqa: BLE001
            log.warning("staff-roster override lookup failed for %s (%s)", proposal_id, exc)
            return set()

    try:
        import db  # local import: avoid a hard DB dependency at module import time
        rows = db.list_notify_recipients()
        if rows:
            return {r["email"].strip().lower() for r in rows
                    if r.get("enabled", True) and r.get("email")} | _adds()
    except Exception as exc:  # noqa: BLE001 — DB down / table missing → env fallback
        log.warning("staff-roster lookup failed (%s); using env fallback", exc)
    return {e.strip().lower() for e in [*config.NOTIFY_EMAILS,
                                        *config.DEPOSIT_NOTIFY_EMAILS] if e} | _adds()


def notify_team(subject: str, body_html: str, kind: str = "general",
                recipients: list[str] | None = None, reply_link: str | None = None,
                proposal_id: str | None = None, reply_to: str | None = None,
                token: str | None = None, project: str | None = None,
                assigned_estimator: str | None = None) -> list[str]:
    """Email the internal team, ONE MESSAGE PER PERSON. `recipients` (explicit) wins;
    otherwise resolve by `kind` — a CRM STEP id from NOTIFY_STEPS, naming which moment this is —
    from the UI-managed roster, applying this proposal's per-project overrides (`proposal_id`).
    EVERY call site names its step. Seven of them used to default to `general`, which is why the
    only knob anybody had was "everything or nothing"; test_notify_step_coverage.py walks the
    syntax tree and fails the next call that forgets. `reply_link` appends a "Reply in Portal"
    button that deep-links staff to the proposal in the staff tool. `reply_to` (the
    proposal's inbound address) makes a plain reply from a staff inbox land in the
    thread too, so the button is the convenient path rather than the only one — but
    ONLY together with `token`, which carries the project in the threading headers.
    Reply-To alone gets the reply to our inbox; the token is what tells us which
    project it belongs to.

    Returns the addresses that were DELIVERED, in roster order — `[]` when nothing
    landed. This used to be a single bool, and the one caller that BRANCHES on it is
    unaffected: followup_worker._send_staff wraps the call in `bool()`, and an empty
    list is falsy for the same reason a False was — nothing went out, so the cadence
    reservation is released and the next tick retries. Returning the list rather than
    that bool is the point of the loop below: per-address delivery status is the whole
    reason this is not BCC, and discarding it here would leave a future "the roster
    copy to Kyle bounced" with nothing to read. The nine other call sites (main.py)
    ignore the return entirely."""
    # The call site passes the EVENT ("Proposal APPROVED — Nearman Creek"). That stays the
    # heading inside the email; the outgoing subject becomes the project, so every update
    # about one job lands in one conversation. Without a project there is no thread to join.
    heading = subject
    if project and token:
        subject = staff_thread_subject(project)
    # A step nobody recognises resolves to the FLOOR and says so in the log. Said out loud
    # because that is the failure this whole feature exists to end: seven of these call sites
    # spent months defaulting to `general` with nothing anywhere reporting it, so the roster
    # could not be configured per moment and nobody could tell. A typo now leaves a line in the
    # log rather than a silent, wider send. `recipients` given explicitly means the caller has
    # already decided (followup_worker passes its own list), so no step is expected there.
    if (recipients is None and kind != GENERAL_KIND
            and kind not in NOTIFY_STEP_IDS and kind != LEGACY_DEPOSIT_KIND):
        log.warning("notify: %r names step %r, which is not in NOTIFY_STEPS — resolving to the "
                    "general floor", heading, kind)
    to = (recipients if recipients is not None
          else _resolve_notify(kind, proposal_id, assigned_estimator))
    if not to:
        log.info("notify: no recipients after roster/overrides — skipped (%r)", subject)
    if reply_link:
        body_html += (
            f'<p style="margin-top:16px"><a href="{reply_link}" style="background:{_BRAND_RED};color:#fff;'
            f'padding:10px 18px;border-radius:8px;text-decoration:none;font-weight:700">Reply in Portal</a></p>'
        )
    # Only promise reply-by-email when a reply can actually be routed. Reply-To without
    # a token lands the message in our inbox with nothing to match it to, which is the
    # bug this line used to advertise.
    if reply_to and token:
        body_html += ('<p style="color:#64748b;font-size:13px;margin-top:12px">Replying to this email '
                      'posts your message to the customer\'s portal thread and notifies them.</p>')

    # ONE SEND PER RECIPIENT, not one send addressed to the whole roster.
    #
    # Hanz, 2026-08-19: "for sending out multiple emails to the staff and customers can it be BCC
    # so we dont see the cross talk of the emails of the receivers". This was the only path with
    # that problem: the roster was resolved into a list and handed to _send once, and _send puts
    # the list straight into Resend's "to" — so every staff notification published every
    # colleague's address, plus whoever a per-project add or an assignment had folded in, in a To
    # header they could all read. The two customer paths already loop (admin_publish over
    # send_portal_link, admin_reply over send_reply_notification).
    #
    # BCC was what he asked for and is REJECTED. It needs something in To, and an empty or
    # self-addressed To reads as machine mail and costs deliverability; and a BCC send comes back
    # as ONE pass/fail for the batch, so a dead address is indistinguishable from a working one.
    # Per-recipient keeps a verdict per address — which is exactly what makes admin_publish's
    # "Proposal sent, with failures" email possible on the customer side — and it leaves one rule
    # for all outbound mail instead of two.
    #
    # THREADING SURVIVES, which is the thing worth checking before believing any of the above:
    # project_thread_headers takes only `token` and derives the anchor from the PROJECT, with
    # nothing recipient-specific in it (unlike _thread_headers, which hashes an address). So all N
    # copies carry identical References/In-Reply-To and each person's client files them into the
    # one conversation for that job, exactly as before. Built ONCE, out here, so a later edit
    # cannot quietly make the anchor per-recipient — and so can the body, which must be byte-equal
    # in every copy.
    headers = project_thread_headers(token)
    wrapped = _wrap(heading, body_html)
    delivered: list[str] = []
    for addr in to:
        # One dead address must never silence the rest of the team, so each send is its own
        # attempt. _send already swallows transport errors and returns False; the try is for the
        # failure it cannot swallow, because five of the main.py call sites are NOT wrapped and a
        # raise here would 500 a customer's route over a staff email.
        try:
            if _send([addr], subject, wrapped, reply_to=reply_to, headers=headers):
                delivered.append(addr)
        except Exception as exc:  # noqa: BLE001
            log.error("notify: sending %r to %s raised: %s", subject, addr, exc)
    if len(delivered) < len(to):
        # The per-address status the loop exists to produce, said out loud. Nothing else surfaces
        # WHICH colleague missed a notification — the batch used to report one verdict for all.
        log.warning("notify: %r reached %d of %d (missed: %s)", subject, len(delivered), len(to),
                    ", ".join(a for a in to if a not in delivered))
    return delivered
