"""Treadwell Customer Proposal Portal — FastAPI app (customer side only).

Account model: a customer signs in (email code or Google), proving control of
their email, and gets an EMAIL-scoped session that grants access to every
proposal on that email. The /p/<token> link is a convenient deep-link, not the
access gate. The admin side is the proposal tool; both share one Postgres DB.
"""
from __future__ import annotations

import hmac
import html
import json
import logging
import re
import time
from datetime import date, datetime, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

import automations
import config
import customer_auth as ca
import db
import email_sender
import followup_rules
import followup_settings
import followup_worker
import inbound
import invoice
import proposals
import ratelimit

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("portal")

app = FastAPI(title="Treadwell Customer Proposal Portal", docs_url=None, redoc_url=None)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
BACKEND_DIR = Path(__file__).resolve().parent
ALLOWED_HOST = urlparse(config.PUBLIC_BASE_URL).netloc

CSP = (
    "default-src 'self'; "
    "script-src 'self' https://accounts.google.com https://www.gstatic.com; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "img-src 'self' data:; "
    "connect-src 'self' https://accounts.google.com; "
    "frame-src 'self' https://accounts.google.com; "   # 'self' lets the proposal page embed its own PDF iframe
    "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)

# The official PDF is served same-origin and embedded in an <iframe> on the
# customer's own proposal page. Every other response stays DENY / frame-ancestors
# 'none'; only this one path may be framed, and only by us.
_PDF_CSP = "frame-ancestors 'self'"


@app.on_event("startup")
def _startup() -> None:
    try:
        if config.APPLY_SCHEMA_ON_BOOT:
            db.run_script((BACKEND_DIR / "schema.sql").read_text(encoding="utf-8"))
        if config.DEV_SEED:
            db.run_script((BACKEND_DIR / "staging" / "dev_seed.sql").read_text(encoding="utf-8"))
        db.cleanup_expired()
        log.info("startup ok (schema_apply=%s%s)", config.APPLY_SCHEMA_ON_BOOT,
                 " + dev seed" if config.DEV_SEED else "")
    except Exception as exc:  # noqa: BLE001
        log.error("startup failed: %s", exc)
    # Started here rather than lazily off a request: the proposals that most need
    # chasing are the ones nobody is looking at, so waiting for traffic would mean
    # the quiet ones never get followed up. Guarded internally by the env flag.
    try:
        followup_worker.ensure_started()
    except Exception as exc:  # noqa: BLE001 — never block boot on the worker
        log.error("follow-up worker failed to start: %s", exc)


# ── helpers ───────────────────────────────────────────────────────────────────
def _json(data: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(status_code=status, content=data)


async def _body(request: Request) -> dict:
    try:
        return await request.json()
    except Exception:  # noqa: BLE001 — malformed/empty body
        return {}


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return (fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "")) or ""


def _cap(v, n: int) -> str:
    return (v or "").strip()[:n]


def _iso(v):
    """A timestamp as an ISO string, or None. Tolerates an already-string value
    so a caller never has to know whether psycopg parsed the column."""
    return v.isoformat() if hasattr(v, "isoformat") else (v or None)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _followup_state(p: dict) -> dict:
    """Where this proposal stands in follow-up automation, for the staff board.

    `enrolled` false means nothing is chasing it — either a legacy proposal published
    before automation existed, or one an estimator took off. The board renders paused
    and closed-lost as badges, so both need to travel."""
    return {
        "enrolled": bool(p.get("followup_enrolled_at")),
        "enabled": bool(p.get("followup_enrolled_at")) and not p.get("followup_disabled_at"),
        "paused_until": _iso(p.get("followup_paused_until")),
        "closed_lost_reason": p.get("closed_lost_reason"),
        "closed_at": _iso(p.get("closed_at")),
    }


def _last_activity(p: dict):
    """The most recent thing that happened on this proposal, from either side.

    The board dates cards by it and the digest scores "customer silence" from it, so
    it spans the customer's messages, the estimator's logged outreach and the
    milestones themselves — whichever is latest."""
    stamps = [p.get(k) for k in (
        "last_message_at", "last_staff_followup_at", "scheduled_at", "contacts_received_at",
        "deposit_received_at", "deposit_submitted_at", "approved_at", "last_viewed_at",
        "viewed_at", "created_at")]
    real = [s for s in stamps if hasattr(s, "isoformat")]
    return max(real) if real else None


def _set_session_cookie(resp: Response, token: str) -> None:
    resp.set_cookie(
        config.SESSION_COOKIE, token, max_age=config.SESSION_TTL_HOURS * 3600,
        httponly=True, samesite="lax", secure=config.COOKIE_SECURE, path="/",
    )


def _session_email(request: Request) -> Optional[str]:
    return ca.session_email(request.cookies.get(config.SESSION_COOKIE))


def _can_access(request: Request, proposal: dict) -> bool:
    se = _session_email(request)
    if not se:
        return False
    if se == (proposal.get("customer_email") or "").strip().lower():
        return True                                   # primary contact — no extra query
    return db.email_can_access(proposal["proposal_id"], se)   # added recipient?


# Dot-separated domain labels that exclude '.', so the label class never overlaps
# the '.' separator — linear-time (the old [^@\s]+\.[^@\s]+ form backtracked
# polynomially: a ReDoS on length-bounded but attacker-shaped input).
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)+$")
MAX_RECIPIENTS = 10


def _clean_emails(raw):
    """Validate an optional recipients list from the admin-publish body.
    Returns (None, None) when the key is absent (legacy caller), (list, None)
    when clean (lowercased, trimmed, deduped, order-preserving), or
    (None, error_str) on bad input. The regex forbids whitespace, so a value
    can't smuggle newlines/headers into the Resend `to` list."""
    if raw is None:
        return None, None
    if not isinstance(raw, list):
        return None, "emails_must_be_list"
    out, seen = [], set()
    for e in raw:
        if not isinstance(e, str):
            return None, "invalid_email"
        e = e.strip().lower()
        if not e:
            continue
        if len(e) > 254 or not _EMAIL_RE.match(e):
            return None, "invalid_email"
        if e not in seen:
            seen.add(e)
            out.append(e)
    return out, None


_VALID_CONTACT_ROLES = ("primary", "accounts_payable", "other")
MAX_CONTACTS = 10


def _clean_contacts(raw):
    """Validate the customer contacts payload. Returns (list, None) on success or
    (None, error_code). Requires at least one 'primary' with a name; caps at
    MAX_CONTACTS; validates any supplied email; trims + length-caps every field."""
    if not isinstance(raw, list) or not raw:
        return None, "no_contacts"
    if len(raw) > MAX_CONTACTS:
        return None, "too_many"
    out, has_primary = [], False
    for c in raw:
        if not isinstance(c, dict):
            return None, "invalid_contact"
        role = (c.get("role") or "other").strip().lower()
        if role not in _VALID_CONTACT_ROLES:
            return None, "invalid_role"
        name = _cap(c.get("name"), 120)
        if not name:
            return None, "name_required"
        email = (c.get("email") or "").strip().lower()
        if email and (len(email) > 254 or not _EMAIL_RE.match(email)):
            return None, "invalid_email"
        has_primary = has_primary or role == "primary"
        out.append({"role": role, "name": name, "email": email or None,
                    "phone": _cap(c.get("phone"), 40) or None, "label": _cap(c.get("label"), 120) or None})
    if not has_primary:
        return None, "primary_required"
    return out, None


def _contact(row: dict) -> dict:
    return {"role": row["role"], "name": row["name"], "email": row.get("email"),
            "phone": row.get("phone"), "label": row.get("label")}


def _notify_customer(p: dict, heading: str, body_html: str, *,
                     actor_email: Optional[str] = None,
                     peer_heading: Optional[str] = None,
                     peer_body_html: Optional[str] = None) -> None:
    """Email the milestone to EVERY recipient on the proposal.

    The third channel alongside the chat line and the team email. Best-effort:
    a mail failure must never fail the action the customer just completed.

    TWO VERSIONS WHEN ONE CONTACT ACTED FOR BOTH. Hanz, 2026-08-11: "For example one contact
    sent the deposit it should update on the 2nd contact as well. But, we need to inform the
    other contact of what has been done."

    Everybody used to get the same second-person copy, so "we've recorded your check" landed on
    the contact who had not paid — which reads as either a mistake or a second charge. With
    `actor_email` the person who did it keeps the receipt and everyone else gets the
    third-person heads-up, naming them by first name.

    When the actor is UNKNOWN, everyone gets the peer version if there is one. A receipt must
    never land on somebody who did nothing; the reverse ("Dana approved this" to Dana) is
    merely redundant.
    """
    try:
        pid = p["proposal_id"]
        link = f"{config.PUBLIC_BASE_URL}/p/{p['token']}"
        rt = email_sender.proposal_reply_to(p["token"])
        project = p.get("project_name") or "your project"
        actor = (actor_email or "").strip().lower()
        for e in (db.get_recipients(pid) or [p.get("customer_email")]):
            if not e:
                continue
            # `bool(actor)` is redundant — an empty actor never equals a real address, and
            # empty recipients are skipped above — but it is kept because it states the rule
            # this function turns on: an unknown actor means NOBODY is the actor, so nobody
            # gets a receipt. A mutation removing it is correctly equivalent, not a gap.
            is_actor = bool(actor) and e.strip().lower() == actor
            if peer_body_html and not is_actor:
                email_sender.send_customer_update(e, link, project,
                                                  peer_heading or heading, peer_body_html,
                                                  reply_to=rt, token=p["token"])
            else:
                email_sender.send_customer_update(e, link, project, heading, body_html,
                                                  reply_to=rt, token=p["token"])
    except Exception as exc:  # noqa: BLE001
        log.warning("customer update email failed for %s: %s", p.get("proposal_id"), exc)


def _staff_link(proposal_id: str) -> str:
    """Deep-link a staff notification email into the proposal in the staff tool
    (so staff answer in-portal rather than replying to the email)."""
    return f"{config.PROPOSAL_TOOL_PUBLIC_URL}/portal.html?open={proposal_id}"


def _proposal_card(row: dict) -> dict:
    """One row in the customer's project list (login page + in-portal switcher)."""
    created = row.get("created_at")
    return {
        "token": row["token"],
        "project_name": row.get("project_name") or "Your Proposal",
        "proposal_status": row.get("proposal_status"),
        "deposit_status": row.get("deposit_status"),
        "schedule_status": row.get("schedule_status"),
        "contacts_status": row.get("contacts_status") or "pending",
        "created_at": created.isoformat() if hasattr(created, "isoformat") else created,
        # So the card doesn't say "Deposit due" on a job that never needs one.
        "deposit_required": row.get("deposit_required") is not False,
        "deposit_requested": bool(row.get("deposit_requested_at")),
    }


# ── middleware: CSRF/origin backstop + security headers ───────────────────────
@app.middleware("http")
async def _security(request: Request, call_next):
    # CSRF backstop: state-changing API POSTs must originate from our own site —
    # except the server-to-server endpoints with their own auth (/api/notify:
    # service token; /api/inbound/resend: svix signature).
    if (request.method == "POST" and request.url.path.startswith("/api/")
            and request.url.path not in ("/api/notify", "/api/inbound/resend")):
        ref = request.headers.get("origin") or request.headers.get("referer") or ""
        if ref:
            host = urlparse(ref).netloc
            req_host = request.headers.get("host", "")
            if host not in (req_host, ALLOWED_HOST):
                return _json({"ok": False, "error": "bad_origin"}, 403)
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    path = request.url.path
    if path.startswith("/api/portal/") and path.endswith("/pdf"):
        # The one framable path: the customer's own proposal page embeds it.
        resp.headers["X-Frame-Options"] = "SAMEORIGIN"
        resp.headers["Content-Security-Policy"] = _PDF_CSP
    else:
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Content-Security-Policy"] = CSP
    if config.COOKIE_SECURE:
        resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return resp


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    log.error("unhandled error on %s %s: %s", request.method, request.url.path, exc)
    return _json({"ok": False, "error": "server_error"}, 500)


# ── health, static pages, public config ───────────────────────────────────────
@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


# Always revalidate the app shell + its assets. See the asset() docstring: without
# this, browsers heuristically cached them and customers ran stale JS after a deploy.
_NO_CACHE = {"Cache-Control": "no-cache, must-revalidate"}


@app.get("/")
def root() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "login.html", headers=_NO_CACHE)


@app.get("/p/{token}")
def portal_page(token: str, request: Request) -> FileResponse:
    """The landing page for the link in every notification email.

    Recording the visit answers a question the Follow-ups board could not answer before: a
    proposal that has sat in Sent for a week is a completely different problem depending on
    whether the email was ever opened, and "we might have the wrong address" was
    indistinguishable from "they are thinking about it".

    It is only ever a SOFT signal. This serves before any login, so a click is not a read, and
    `mark_link_clicked` keeps it away from proposal_status and cycle_viewed_at for that reason.
    HEAD is skipped because that is prefetchers and link checkers rather than people.

    Serving the page must not depend on any of this working — a database hiccup here would
    otherwise take the customer's proposal offline, which is far worse than a missing
    timestamp."""
    if request.method == "GET":
        try:
            p = db.get_proposal_by_token(token)
            if p:
                db.mark_link_clicked(p["proposal_id"])
        except Exception:                     # noqa: BLE001 — never block the page
            log.warning("mark_link_clicked failed for token %s", token[:8], exc_info=True)
    return FileResponse(FRONTEND_DIR / "index.html", headers=_NO_CACHE)


@app.get("/api/public-config")
def public_config() -> JSONResponse:
    return _json({"ok": True, "google_client_id": config.GOOGLE_CLIENT_ID or None})


# ── global auth (account login) ───────────────────────────────────────────────
@app.post("/api/auth/request-code")
async def auth_request_code(request: Request) -> JSONResponse:
    if not ratelimit.allow_ip(_client_ip(request), config.RATE_REQUESTS_PER_IP, config.RATE_WINDOW_SEC):
        return _json({"ok": False, "error": "rate_limited"}, 429)
    email = ((await _body(request)).get("email") or "").strip().lower()
    if not email:
        return _json({"ok": False, "error": "Enter your email."}, 400)
    if not db.email_has_proposal(email):
        return _json({"ok": False, "error": "no_project"})  # 200: a normal outcome
    allowed, wait = ratelimit.allow_otp(
        email, config.OTP_REQUESTS_PER_EMAIL, config.RATE_WINDOW_SEC, config.OTP_REQUEST_COOLDOWN_SEC
    )
    if not allowed:
        return _json({"ok": False, "error": "rate_limited", "retry_after": wait}, 429)
    code = ca.issue_code(email)
    email_sender.send_otp(email, code, "your Treadwell proposal")
    return _json({"ok": True, "dev_code": code if config.SHOW_OTP else None})


@app.post("/api/auth/verify-code")
async def auth_verify_code(request: Request) -> JSONResponse:
    if not ratelimit.allow_ip(_client_ip(request), config.RATE_REQUESTS_PER_IP, config.RATE_WINDOW_SEC):
        return _json({"ok": False, "error": "rate_limited"}, 429)
    body = await _body(request)
    email = (body.get("email") or "").strip().lower()
    ok, reason = ca.verify_code(email, (body.get("code") or "").strip())
    if not ok:
        return _json({"ok": False, "error": reason}, 400)
    resp = _json({"ok": True, "proposals": [_proposal_card(r) for r in db.list_proposals_by_email(email)]})
    _set_session_cookie(resp, ca.start_session(email))
    return resp


@app.post("/api/auth/google")
async def auth_google(request: Request) -> JSONResponse:
    if not ratelimit.allow_ip(_client_ip(request), config.RATE_REQUESTS_PER_IP, config.RATE_WINDOW_SEC):
        return _json({"ok": False, "error": "rate_limited"}, 429)
    if not config.GOOGLE_AUTH_ENABLED:
        return _json({"ok": False, "error": "Google sign-in isn't enabled."}, 400)
    email = ca.verify_google_idtoken((await _body(request)).get("credential") or "")
    if not email:
        return _json({"ok": False, "error": "Could not verify your Google sign-in."}, 401)
    if not db.email_has_proposal(email):
        return _json({"ok": False, "error": "no_project", "email": email})  # 200: normal outcome
    resp = _json({"ok": True, "proposals": [_proposal_card(r) for r in db.list_proposals_by_email(email)]})
    _set_session_cookie(resp, ca.start_session(email))
    return resp


@app.post("/api/auth/logout")
def auth_logout(request: Request) -> JSONResponse:
    tok = request.cookies.get(config.SESSION_COOKIE)
    if tok:
        db.delete_session(tok)  # actually revoke, not just drop the cookie
    resp = _json({"ok": True})
    resp.delete_cookie(config.SESSION_COOKIE, path="/")
    return resp


_EVENT_ICONS = {"text": "💬", "deposit_request": "🧾", "system": "🔔"}


def _event(row: dict, seen) -> dict:
    """One bell item. `title` carries the project name because the feed spans
    every project this customer can reach."""
    kind = row.get("msg_type") or "text"
    body = (row.get("body") or "").strip()
    if kind == "text":
        head = "Treadwell replied"
    elif kind == "deposit_request":
        head = "Deposit invoice"
    else:
        head = (body.split(" — ", 1)[0] if " — " in body[:60] else "Update")
        body = body.split(" — ", 1)[1] if " — " in body[:60] else body
    ts = row.get("created_at")
    link = f"/p/{row['token']}" + ("#proposal" if kind == "deposit_request" else "")
    return {
        "id": f"ev:{row.get('id')}",
        "kind": kind,
        "icon": _EVENT_ICONS.get(kind, "•"),
        "title": f"{head} · {row.get('project_name') or 'your project'}",
        "body": body[:240],
        "ts": ts.isoformat() if hasattr(ts, "isoformat") else ts,
        "link": link,
        "unread": bool(ts and (seen.get(row["proposal_id"]) is None or ts > seen[row["proposal_id"]])),
    }


@app.get("/api/me/notifications")
def me_notifications(request: Request) -> JSONResponse:
    """The customer's bell: staff replies, deposit invoices and status changes
    across ALL their projects, newest first, with a per-reader unread count."""
    se = _session_email(request)
    if not se:
        return _json({"ok": True, "authed": False, "items": [], "unread": 0})
    try:
        seen = db.get_read_state(se)
        items = [_event(r, seen) for r in db.list_customer_events(se)]
    except Exception as exc:  # noqa: BLE001 — the bell must never break the page
        log.warning("customer notifications failed for %s: %s", se, exc)
        return _json({"ok": True, "authed": True, "items": [], "unread": 0})
    return _json({"ok": True, "authed": True, "items": items,
                  "unread": sum(1 for i in items if i["unread"])})


@app.post("/api/me/notifications/seen")
async def me_notifications_seen(request: Request) -> JSONResponse:
    """Clear this reader's badge. Per-customer, unlike the staff bell's single
    shared marker — a shared one would leak read state between customers."""
    se = _session_email(request)
    if not se:
        return _json({"ok": False, "error": "unauthorized"}, 401)
    try:
        pids = [r["proposal_id"] for r in db.list_proposals_by_email(se)]
        db.mark_read(se, pids)
    except Exception as exc:  # noqa: BLE001
        log.warning("mark_read failed for %s: %s", se, exc)
        return _json({"ok": False}, 500)
    return _json({"ok": True})


FEEDBACK_CATEGORIES = ("question", "request", "problem", "other")


@app.post("/api/me/feedback")
async def me_feedback(request: Request) -> JSONResponse:
    """What a customer wants from this portal — a question, a request, or a fault.

    Hanz, 2026-08-13: "Here create a Feedback form for the customer of what queries or update
    they want from this system."

    Signed in only, and the address is taken from the SESSION rather than the body: feedback
    that anybody could post under anybody's name is feedback nobody can act on, and an open
    endpoint on a public host is a spam relay.

    Stored AND emailed. Stored because an inbox is where suggestions go to die; emailed because
    nobody would think to read a table. The email is a plain team notification with no project
    threading — this is not about one job, so it must not land in a project's conversation.
    A store failure is fatal (the customer is told it did not save); an EMAIL failure is not,
    because their words are already safe and telling them otherwise would invite a duplicate."""
    se = _session_email(request)
    if not se:
        return _json({"ok": False, "error": "unauthorized"}, 401)
    body = await _body(request)
    category = str(body.get("category") or "other").strip().lower()
    if category not in FEEDBACK_CATEGORIES:
        return _json({"ok": False, "error": "invalid_category"}, 400)
    text = _cap(body.get("body"), 4000)
    if not text:
        return _json({"ok": False, "error": "empty"}, 400)
    # Which project they were looking at, when they were looking at one. Recorded as context,
    # never as ownership — see db.add_feedback.
    #
    # Resolved from the TOKEN through the same gate every per-project route uses, because the
    # page never learns a proposal id and a client-supplied one would let any signed-in customer
    # file feedback against somebody else's job. A token that does not resolve simply costs the
    # context: the feedback is still saved, unattached.
    pid = None
    tok = str(body.get("token") or "").strip()
    if tok:
        p = _require(request, tok)
        if p:
            pid = p.get("proposal_id")
    try:
        row = db.add_feedback(se, category, text, pid)
    except Exception as exc:  # noqa: BLE001 — the customer must know it did not save
        log.error("feedback save failed for %s: %s", se, exc)
        return _json({"ok": False, "error": "save_failed"}, 500)
    try:
        label = {"question": "Question", "request": "Feature request",
                 "problem": "Problem report", "other": "Feedback"}[category]
        email_sender.notify_team(
            f"Portal {label.lower()} from {se}",
            f"<p><strong>{html.escape(se)}</strong> sent {html.escape(label.lower())} about the "
            f"customer portal:</p><blockquote>{html.escape(text)}</blockquote>"
            + (f"<p class=\"muted\">While viewing project {html.escape(pid)}.</p>" if pid else ""),
        )
    except Exception as exc:  # noqa: BLE001 — saved already; a send failure is not the customer's
        log.warning("feedback notify failed for %s: %s", se, exc)
    return _json({"ok": True, "id": row.get("id") if isinstance(row, dict) else None})


@app.get("/api/me/proposals")
def me_proposals(request: Request) -> JSONResponse:
    se = _session_email(request)
    if not se:
        return _json({"ok": True, "authed": False, "proposals": []})
    return _json({"ok": True, "authed": True, "email": se,
                  "proposals": [_proposal_card(r) for r in db.list_proposals_by_email(se)]})


# ── per-proposal (email-scoped access) ────────────────────────────────────────
@app.get("/api/portal/{token}")
def api_get_portal(token: str, request: Request) -> JSONResponse:
    p = db.get_proposal_by_token(token)
    if not p:
        return _json({"ok": False, "error": "not_found"}, 404)
    se = _session_email(request)
    authed = _can_access(request, p)                  # primary OR added recipient
    base = {"ok": True, "authed": authed, "project_name": p.get("project_name") or "Your Proposal",
            "wrong_account": bool(se and not authed)}
    if not authed:
        return _json(base)
    # The snapshot they were SENT, not whatever an estimator has since typed.
    data = db.get_pinned_draft_data(p) or {}
    db.mark_viewed(p["proposal_id"])
    # And WHICH recipient it was. mark_viewed above is untouched: the customer-facing status is
    # shared on purpose, one status for the whole project. This is the staff-side question of
    # which of two contacts has actually opened it. record_view swallows its own failures, so a
    # missing migration costs the CRM a name rather than costing the customer their proposal.
    db.record_view(p["proposal_id"], se)
    p = db.get_proposal(p["proposal_id"])
    vm = proposals.build_view_model(p, data)
    vm["questions"] = [_q(q) for q in db.list_questions(p["proposal_id"])]   # text-only (legacy UI)
    # _customer_msg, not _msg: `mine` has to be decided against the session that is asking, or
    # the second contact on a proposal sees the first contact's reply as their own.
    vm["messages"] = [_customer_msg(m, se) for m in db.list_messages(p["proposal_id"])]
    vm["contacts"] = [_contact(c) for c in db.list_contacts(p["proposal_id"])]
    vm["check_address"] = config.CHECK_ADDRESS
    vm["payable_to"] = config.PAYABLE_TO
    _deps = db.list_deposits(p["proposal_id"])   # newest first
    _latest = _deps[0] if _deps else None
    vm["deposit"] = {
        "due": float(p["deposit_amount"]) if p.get("deposit_amount") is not None else None,
        "ref": proposals.deposit_ref(p["proposal_id"]),
        # `submitted` lets the customer see a "recorded" state on reload instead of a
        # blank form they might resubmit. Derived from the deposit rows, not from
        # deposit_status, so the banner survives staff moving the status either way.
        "submitted": bool(_latest),
        "submitted_method": _latest["method"] if _latest else None,
        # WHO paid, when it was the other contact. Hanz's own example: one contact sends the
        # deposit and the second must see that it is done — without this the banner said "we've
        # recorded your check" to somebody who had not written one. First name only and never an
        # address, the same rule as the chat thread; `_me` lets the client keep saying "your"
        # to the person who actually paid.
        "submitted_by_first_name": (_first_name_of(_latest.get("submitted_by"))
                                    if _latest else ""),
        "submitted_by_me": bool(_latest and se and (_latest.get("submitted_by") or "").strip().lower()
                                == se.strip().lower()),
        # Present once the invoice has been issued — drives the download button on
        # the chat card and the thank-you card.
        "invoice_no": p.get("deposit_invoice_no"),
        # Does this job collect a deposit at all? False hides the whole Deposit
        # step. An issued invoice overrides it (staff can invoice a no-deposit job
        # later), so the UI gates on `required || invoice_no`.
        "required": p.get("deposit_required") is not False,
    }
    if config.PROPOSAL_TOOL_URL:   # official PDF available via on-demand render
        vm["has_pdf"] = True
    # Which version of the document this is. Lets the client notice a revision
    # landing mid-session and re-fetch the PDF instead of showing the old one.
    vm["revision_no"] = p.get("current_revision_no")
    # Where the customer has told us the project stands, and how long it has been
    # sitting — the status card uses both to decide whether offering a way out is
    # helpful or presumptuous.
    vm["project_status"] = {
        "paused_until": _iso(p.get("followup_paused_until")),
        "closed": (p.get("proposal_status") or "") == "closed_lost",
    }
    vm["sent_at"] = _iso(p.get("created_at"))
    base["view"] = vm
    return _json(base)


def _q(row: dict) -> dict:
    return {"author_kind": row["author_kind"], "body": row["body"],
            "created_at": row["created_at"].isoformat() if row.get("created_at") else None}


def _msg(row: dict) -> dict:
    """A chat-thread message (any msg_type), for STAFF. Superset of _q with the id (for
    incremental polling), msg_type, and meta payload.

    Carries the full author address: staff need to know WHICH contact on a multi-recipient
    proposal said something, and they already see every recipient's address in the drawer.
    The customer-facing shape is _customer_msg below, which never ships one.
    """
    return {"id": row.get("id"), "author_kind": row["author_kind"], "body": row["body"],
            "author_email": row.get("author_email"),
            "msg_type": row.get("msg_type") or "text", "meta": row.get("meta"),
            "created_at": row["created_at"].isoformat() if row.get("created_at") else None}


def _first_name_of(email: Optional[str]) -> str:
    """"dana.reed@acme.com" → "Dana". Derived SERVER-side so a peer's raw address never
    reaches another customer's browser."""
    local = str(email or "").split("@")[0]
    first = re.split(r"[._+-]", local)[0]
    return first[:1].upper() + first[1:] if first else ""


def _customer_msg(row: dict, viewer_email: Optional[str]) -> dict:
    """One thread message as a CUSTOMER may see it.

    Two recipients share one proposal, one token and one thread — that part has always been
    true. What was wrong is who each message looked like it came from: `mine` was decided in
    the browser as `author_kind === "customer"`, so EVERY customer message rendered as the
    viewer's own. The second contact on a proposal saw the first contact's reply sitting on
    their own side of the thread, in their own colour, as if they had written it.

    So `mine` is decided here, against the session that is asking. A peer's message carries a
    FIRST NAME only (Hanz: first name in the portal, full address staff-side) and never an
    address — meta is whitelisted for the same reason, because an inbound-email row keeps the
    sender's address in `meta.from`.

    A legacy row with no author_email keeps the old behaviour and reads as the viewer's own.
    Guessing the other way would relabel a customer's own history as somebody else's.
    """
    me = (viewer_email or "").strip().lower()
    author = (row.get("author_email") or "").strip().lower()
    customer = row["author_kind"] == "customer"
    meta = row.get("meta") or {}
    return {
        "id": row.get("id"),
        "author_kind": row["author_kind"],
        "body": row["body"],
        "msg_type": row.get("msg_type") or "text",
        "mine": customer and (not author or author == me),
        "author_first_name": _first_name_of(row.get("author_email")) if customer else "",
        "meta": {k: meta[k] for k in ("source", "revision_no", "superseded", "superseded_by",
                                     "amount", "invoice_no", "reference") if k in meta},
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
    }


def _require(request: Request, token: str):
    """Return the proposal row if the session email may access it, else None."""
    p = db.get_proposal_by_token(token)
    if not p or not _can_access(request, p):
        return None
    return p


@app.post("/api/portal/{token}/questions")
async def api_post_question(token: str, request: Request) -> JSONResponse:
    p = _require(request, token)
    if not p:
        return _json({"ok": False, "error": "unauthorized"}, 401)
    text = _cap((await _body(request)).get("body"), 4000)
    if not text:
        return _json({"ok": False, "error": "empty"}, 400)
    who = _session_email(request)
    row = db.add_message(p["proposal_id"], "customer", who, text, msg_type="text")
    email_sender.notify_team(
        f"New proposal question — {p.get('project_name')}",
        f"<p><strong>{html.escape(who or '')}</strong> asked a question on "
        f"<strong>{html.escape(p.get('project_name') or '')}</strong>:</p>"
        f"<blockquote>{html.escape(text)}</blockquote>",
        reply_link=_staff_link(p["proposal_id"]), proposal_id=p["proposal_id"],
        reply_to=email_sender.proposal_reply_to(p.get("token")),
        token=p.get("token"), project=p.get("project_name"),
        # The estimator who owns this job hears about a customer question whether or not
        # they sit on the org-wide roster. Hanz, 2026-08-13, asking for chat to reach
        # "whoever is set for the notification sending of that project" — with hanz@ and
        # will@ the only enabled roster rows, a question on Kyle's job reached neither Kyle
        # nor anybody who could answer it. A per-project mute still wins.
        assigned_estimator=p.get("assigned_estimator"),
    )
    # `who` is this session, and this row is theirs — but route it through the same serializer
    # so the shape the client appends matches the shape it polls. Easy one to miss.
    return _json({"ok": True, "question": _q(row), "message": _customer_msg(row, who)})


@app.get("/api/portal/{token}/messages")
def api_messages(token: str, request: Request) -> JSONResponse:
    """The chat thread for the customer view + incremental polling. `after` is the
    highest message id the client already has (0 = full thread)."""
    p = _require(request, token)
    if not p:
        return _json({"ok": False, "error": "unauthorized"}, 401)
    try:
        after = int(request.query_params.get("after") or 0)
    except (ValueError, TypeError):
        after = 0
    msgs = [_customer_msg(m, _session_email(request))
            for m in db.list_messages(p["proposal_id"], after)]
    # The client re-renders the whole page when ANY of these changes, so anything a
    # customer would otherwise have to reload to see belongs here. Issuing an
    # invoice or a staff amount edit never moves deposit_status, so without
    # invoice_no/amount the page kept saying "your invoice is on its way" while the
    # invoice sat in the chat below it.
    return _json({"ok": True, "messages": msgs, "status": {
        "proposal": p["proposal_status"], "deposit": p["deposit_status"],
        "contacts": p.get("contacts_status") or "pending", "schedule": p["schedule_status"],
        "invoice_no": p.get("deposit_invoice_no"),
        "deposit_amount": (float(p["deposit_amount"]) if p.get("deposit_amount") is not None else None),
        "deposit_required": p.get("deposit_required") is not False,
        # A revision landing while the customer has the page open changes the whole
        # document — the client re-renders and re-fetches the PDF off this.
        "revision_no": p.get("current_revision_no"),
        # So the status card updates in the tab the customer left open — including
        # when STAFF pause or close it from the drawer.
        "paused_until": _iso(p.get("followup_paused_until")),
        "closed": (p.get("proposal_status") or "") == "closed_lost"}})


@app.post("/api/portal/{token}/approve")
async def api_approve(token: str, request: Request) -> JSONResponse:
    p = _require(request, token)
    if not p:
        return _json({"ok": False, "error": "unauthorized"}, 401)
    # Idempotent: a double-submit (or a re-opened tab) must not re-run the approval
    # email + automations, which would issue a second invoice.
    #
    # approved_at as well as the status, since staff got the power to file a signed job
    # as lost (2026-08-10). That deliberately keeps the approval on the row under a
    # 'closed_lost' status, and keyed on the status alone this guard stopped firing for
    # exactly those rows: the customer's portal went back to reading "Awaiting your
    # approval" with a live Approve button, and one click wrote a second portal_approvals
    # row, re-sent both approval emails and put the job back in Approved, undoing a staff
    # decision nobody told them about. A closed-lost proposal that was NEVER signed stays
    # approvable, since a customer who changes their mind is welcome back, and so does a
    # revised one: reset_for_revision nulls approved_at whenever it retires an approval.
    if p.get("proposal_status") == "approved" or p.get("approved_at"):
        return _json({"ok": True, "already_approved": True})
    body = await _body(request)
    name = _cap(body.get("name"), 120)
    title = _cap(body.get("title"), 120)
    if not name:
        return _json({"ok": False, "error": "Name is required."}, 400)

    # Validate the approval against the SAME snapshot the customer was shown, so the
    # option labels they ticked always exist and the total they agreed to is the
    # total we record. Reading live data here meant a mid-edit rename could orphan
    # their selection between page load and pressing Approve.
    data = db.get_pinned_draft_data(p) or {}
    options = proposals.pricing_options(data)

    # Multi-select (option_labels[]) is the V1 path; option_label (single string)
    # is the legacy body. A single-option proposal auto-selects its only option.
    raw = body.get("option_labels")
    if isinstance(raw, list):
        labels = [_cap(x, 200) for x in raw if isinstance(x, str) and x.strip()]
    else:
        single = _cap(body.get("option_label"), 200)
        labels = [single] if single else []
    if not labels and len(options) == 1:
        labels = [options[0]["label"]]
    try:
        chosen, total = proposals.resolve_selection(data, labels)
    except ValueError:
        return _json({"ok": False, "error": "Please choose at least one option you're approving."}, 400)

    label_list = [o["label"] for o in chosen]
    option_summary = ", ".join(label_list)   # denormalized so legacy consumers keep working
    deposit = proposals.deposit_amount(total)
    try:
        approved_date = date.fromisoformat(body["date"]) if body.get("date") else date.today()
    except (ValueError, TypeError):
        approved_date = date.today()

    approver = _session_email(request)
    db.add_approval(p["proposal_id"], name, title, approved_date, total, option_summary,
                    _client_ip(request), approver, options=label_list)
    db.set_approved(p["proposal_id"], total, option_summary, name, title, approved_date,
                    options=label_list, deposit_amount=deposit)

    project_name = p.get("project_name") or "proposal"
    # A system line in the chat thread records the approval for both sides.
    sel_txt = "; ".join(f"{o['label']} (${o['total']:,.2f})" for o in chosen)
    db.add_message(p["proposal_id"], "staff", None,
                   f"Approved by {name} — {sel_txt}. Total ${total:,.2f}.", msg_type="system")

    # Staff decided at send time whether this job collects a deposit. When it
    # doesn't, every mention of one has to go — promising an invoice that will
    # never arrive is worse than saying nothing.
    deposit_due = p.get("deposit_required") is not False
    email_sender.notify_team(
        f"Proposal APPROVED — {project_name}",
        f"<p><strong>{html.escape(name)}</strong>{(', ' + html.escape(title)) if title else ''} approved "
        f"<strong>{html.escape(option_summary)}</strong> at <strong>${total:,.2f}</strong> on {approved_date}"
        f"{(' (signed in as ' + html.escape(approver) + ')') if approver else ''}.</p>"
        + (f"<p>Auto-calculated deposit (25%): <strong>${deposit:,.2f}</strong>.</p>" if deposit_due
           else "<p>No deposit required for this project — the customer has been asked for "
                "their project contacts.</p>")
        + f"<p>Project: {html.escape(project_name)}.</p>",
        reply_link=_staff_link(p["proposal_id"]), proposal_id=p["proposal_id"],
        reply_to=email_sender.proposal_reply_to(p.get("token")),
        token=p.get("token"), project=p.get("project_name"),
    )
    # Confirm the approval to the customer in writing. They'd just committed to a
    # price and heard nothing back except (later) an invoice.
    _notify_customer(
        p, "Thank you — your proposal is approved",
        f"<p>We've recorded your approval of <strong>{html.escape(project_name)}</strong>"
        f"{(' by ' + html.escape(name)) if name else ''} on {approved_date}.</p>"
        f"<p>Approved: <strong>{html.escape(option_summary)}</strong> — "
        f"<strong>${total:,.2f}</strong>.</p>"
        + (f"<p>A deposit of <strong>${deposit:,.2f}</strong> (25%) reserves your place on our "
           f"schedule; the invoice follows separately.</p>" if deposit_due
           else "<p>No deposit is needed. Next, please add your project contacts so we can "
                "schedule the work.</p>"),
        # The other contacts hear WHO approved rather than "your approval". Named from the typed
        # signature, which every recipient already sees on the proposal itself, falling back to
        # the first name of whoever was signed in.
        actor_email=approver,
        peer_heading="This proposal has been approved",
        peer_body_html=(
            f"<p><strong>{html.escape(name or _first_name_of(approver) or 'Someone on your team')}"
            f"</strong> approved <strong>{html.escape(project_name)}</strong> on {approved_date}.</p>"
            f"<p>Approved: <strong>{html.escape(option_summary)}</strong> — "
            f"<strong>${total:,.2f}</strong>.</p>"
            + (f"<p>A deposit of <strong>${deposit:,.2f}</strong> (25%) reserves the schedule; "
               f"the invoice follows separately.</p>" if deposit_due
               else "<p>No deposit is needed. Next, the project contacts.</p>")),
    )
    if not deposit_due:
        # The contacts prompt normally rides on deposit-received (admin_deposit_received).
        # With no deposit there is no such moment, so ask now — otherwise the thread
        # goes quiet and the project stalls waiting for contacts nobody requested.
        try:
            db.add_message(p["proposal_id"], "staff", None,
                           "Approved — thank you! Please add your project contacts so we can "
                           "schedule the work.", msg_type="system")
        except Exception as exc:  # noqa: BLE001 — the approval itself must still succeed
            log.warning("could not post the contacts prompt for %s: %s", p["proposal_id"], exc)
    try:
        automations.run_on_approval(p, project_name)
    except Exception as exc:  # noqa: BLE001
        log.error("approval automations failed: %s", exc)
    return _json({"ok": True})


_LOST_REASON_LABELS = {
    "price": "Price", "another_contractor": "Selected another contractor",
    "canceled": "Project canceled", "scope_changed": "Scope changed",
    "timing": "Timing", "other": "Other",
}


@app.post("/api/portal/{token}/project-status")
async def api_project_status(token: str, request: Request) -> JSONResponse:
    """The customer tells us where the project actually stands.

    The point of the whole follow-up system: a customer who has gone quiet usually
    isn't ignoring us, they're waiting on a budget or they've gone elsewhere. Give
    them one click to say so and the reminders stop being noise — for them and for
    the estimator.

    Rate-limited by IP: unlike /questions this fans out email to the estimator and
    the roster and mutates pipeline state, so it is worth the cheap guard."""
    p = _require(request, token)
    if not p:
        return _json({"ok": False, "error": "unauthorized"}, 401)
    if not ratelimit.allow_ip(_client_ip(request), config.RATE_REQUESTS_PER_IP,
                              config.RATE_WINDOW_SEC):
        return _json({"ok": False, "error": "rate_limited"}, 429)
    if (p.get("proposal_status") or "") == "approved":
        return _json({"ok": False, "error": "already_approved"}, 400)

    body = await _body(request)
    status = str(body.get("status") or "").strip().lower()
    pid = p["proposal_id"]
    who = _session_email(request) or p.get("customer_email")
    name = p.get("customer_name") or who or "the customer"
    project = p.get("project_name") or "the project"

    if status == "delayed":
        try:
            months = int(body.get("months") or 0)
        except (TypeError, ValueError):
            months = 0
        if months not in _PAUSE_MONTHS:
            return _json({"ok": False, "error": "invalid_months"}, 400)
        until = followup_rules.add_months(followup_rules.business_today(_now_utc()), months)
        # Saying "two months" twice — a second click from an older email — must not
        # fire another notification at the estimator.
        if followup_rules.as_date(p.get("followup_paused_until")) == until:
            return _json({"ok": True, "project_status": {"paused_until": until.isoformat(),
                                                         "closed": False}})
        db.pause_followups(pid, until)
        window = "4+ months" if months == 4 else f"{months} month{'s' if months > 1 else ''}"
        db.add_message(pid, "customer", who,
                       f"Project delayed — revisiting in about {window}.",
                       msg_type="status_update",
                       meta={"status": "delayed", "months": months,
                             "paused_until": until.isoformat()})
        db.add_followup(pid, "customer_status",
                        {"status": "delayed", "months": months, "until": until.isoformat()}, who)
        _notify_staff_status(
            p, f"Project delayed — {project}",
            f"<p><strong>{html.escape(str(name))}</strong> says "
            f"<strong>{html.escape(project)}</strong> is delayed by about "
            f"{html.escape(window)}.</p>"
            f"<p>Automated follow-ups are paused until "
            f"{html.escape(until.isoformat())}, and you'll get a reminder then.</p>")
        return _json({"ok": True, "project_status": {"paused_until": until.isoformat(),
                                                     "closed": False}})

    if status == "not_moving_forward":
        reason = str(body.get("reason") or "").strip().lower() or None
        if reason and reason not in _LOST_REASONS:
            return _json({"ok": False, "error": "invalid_reason"}, 400)
        note = _cap(body.get("note"), 1000) or None
        if (p.get("proposal_status") or "") == "closed_lost":
            return _json({"ok": True, "project_status": {"paused_until": None, "closed": True}})
        if not db.close_lost(pid, reason):
            return _json({"ok": False, "error": "already_approved"}, 400)
        label = _LOST_REASON_LABELS.get(reason or "", "")
        db.add_message(pid, "customer", who,
                       "Not moving forward with this project."
                       + (f" Reason: {label}." if label else ""),
                       msg_type="status_update",
                       meta={"status": "not_moving_forward", "reason": reason, "note": note})
        db.add_followup(pid, "customer_status",
                        {"status": "not_moving_forward", "reason": reason, "note": note}, who)
        _notify_staff_status(
            p, f"Closed–Lost — {project}",
            f"<p><strong>{html.escape(str(name))}</strong> is not moving forward with "
            f"<strong>{html.escape(project)}</strong>.</p>"
            + (f"<p>Reason: <strong>{html.escape(label)}</strong></p>" if label else "")
            + (f"<blockquote>{html.escape(note)}</blockquote>" if note else "")
            + "<p>Follow-ups have stopped and the opportunity is marked Closed–Lost.</p>")
        return _json({"ok": True, "project_status": {"paused_until": None, "closed": True}})

    if status == "resume":
        db.resume_followups(pid)
        db.add_message(pid, "customer", who, "Ready to move forward again.",
                       msg_type="status_update", meta={"status": "resume"})
        db.add_followup(pid, "customer_status", {"status": "resume"}, who)
        _notify_staff_status(
            p, f"Back on — {project}",
            f"<p><strong>{html.escape(str(name))}</strong> says "
            f"<strong>{html.escape(project)}</strong> is ready to move forward again.</p>")
        return _json({"ok": True, "project_status": {"paused_until": None, "closed": False}})

    return _json({"ok": False, "error": "invalid_status"}, 400)


def _notify_staff_status(p: dict, subject: str, body_html: str) -> None:
    """Tell the assigned estimator (and the roster) what the customer just said.

    Best-effort: the customer's answer is already recorded, and an email failure must
    not make their click look broken."""
    pid = p["proposal_id"]
    try:
        # This used to resolve the roster here and PREPEND the estimator itself, which meant a
        # muted estimator was dragged back in — an explicit "don't email me about this job"
        # silently overruled. Both rules now live in one place (_resolve_notify), so chat
        # messages and status updates cannot disagree about who hears from a project.
        email_sender.notify_team(subject, body_html,
                                 reply_link=_staff_link(pid), proposal_id=pid,
                                 reply_to=email_sender.proposal_reply_to(p.get("token")),
                                 token=p.get("token"), project=p.get("project_name"),
                                 assigned_estimator=p.get("assigned_estimator"))
    except Exception as exc:  # noqa: BLE001
        log.warning("status notify failed for %s: %s", pid, exc)


@app.post("/api/portal/{token}/deposit")
async def api_deposit(token: str, request: Request) -> JSONResponse:
    p = _require(request, token)
    if not p:
        return _json({"ok": False, "error": "unauthorized"}, 401)
    # Don't accept money nobody asked for. An invoice existing overrides the flag:
    # staff can always choose to invoice a no-deposit job later, and once they have,
    # the customer must be able to pay it.
    if p.get("deposit_required") is False and not p.get("deposit_invoice_no"):
        return _json({"ok": False, "error": "deposit_not_required"}, 400)
    body = await _body(request)
    method = (body.get("method") or "").strip().lower()
    if method not in ("ach", "check"):
        return _json({"ok": False, "error": "Choose ACH or check."}, 400)
    account_name = _cap(body.get("account_name"), 120) or None
    note = _cap(body.get("note"), 1000) or None

    # ACH: the customer's OWN routing + account numbers (double-entry verified on the
    # client). We store the full numbers so Treadwell can initiate the debit; the team
    # email + chat only ever show the last-4 mask. Normalize to digits before storing.
    routing_number = account_number = masked_ref = account_type = None
    if method == "ach":
        routing_number = "".join(ch for ch in str(body.get("routing_number") or "") if ch.isdigit())
        account_number = "".join(ch for ch in str(body.get("account_number") or "") if ch.isdigit())
        account_type = (str(body.get("account_type") or "").strip().lower() or None)
        if not account_name:
            return _json({"ok": False, "error": "Please enter the account name."}, 400)
        # Length floor only, no exact-length rule: routing formats vary by bank and
        # country and may change, so pinning 9 digits would reject a valid payer.
        # 4 is just enough to reject an empty/garbage field (same floor as account).
        if len(routing_number) < 4:
            return _json({"ok": False, "error": "Routing number must be at least 4 digits."}, 400)
        if len(account_number) < 4:
            return _json({"ok": False, "error": "Account number must be at least 4 digits."}, 400)
        if account_type not in ("checking", "savings"):
            return _json({"ok": False, "error": "Please choose an account type (checking or savings)."}, 400)
        masked_ref = f"••••{account_number[-4:]}"

    db.add_deposit(p["proposal_id"], method, account_name, None, masked_ref, note,
                   routing_number=routing_number, account_number=account_number,
                   account_type=account_type,
                   # Which contact paid. From the session, not from account_name — that is
                   # a bank account holder and can be a company.
                   submitted_by=_session_email(request))
    # Move the board card off 'pending' so staff can see money is in flight — until
    # now the only signal a customer had paid was one email, leaving a paid project
    # indistinguishable from an approved-but-unpaid one. Guarded in SQL so a
    # resubmission can't un-receive a deposit staff already verified.
    db.mark_deposit_submitted(p["proposal_id"])
    project_name = p.get("project_name") or "proposal"
    ref = proposals.deposit_ref(p["proposal_id"])
    # A chat line records it so both sides see the deposit is in flight. Filed as a
    # CUSTOMER-authored 'deposit_submitted' row (it is the customer's action, not
    # ours) — that is what puts it in the staff notification bell feed, which only
    # carries customer-originated rows.
    # (No account details or internal ref in the customer-visible message.)
    who = account_name or "The customer"
    chat = (f"Deposit initiated — {who} provided ACH payment details for {project_name}. "
            "We'll confirm once the transfer clears." if method == "ach"
            else f"Deposit initiated — a check is on its way for {project_name}. "
                 "We'll confirm once it arrives.")
    db.add_message(p["proposal_id"], "customer", _session_email(request), chat,
                   msg_type="deposit_submitted")

    if method == "ach":
        detail = (
            f"<p>Name on account: {html.escape(account_name or '—')} · "
            f"Type: {html.escape((account_type or '—').title())} · "
            f"Routing: {html.escape(routing_number or '—')} · "
            f"Account: {masked_ref or '—'} · Note: {html.escape(note or '—')}</p>"
            f"<p>Full account number is in the proposal's admin view.</p>"
        )
        lead = (f"Customer provided ACH details to pay the deposit for "
                f"<strong>{html.escape(project_name)}</strong> ({html.escape(ref)}).")
        closing = "Initiate the debit, then mark the deposit Received in the proposal tool."
        subject = f"Deposit details — {project_name} ({ref})"
    else:   # check
        detail = f"<p>Note: {html.escape(note or '—')}</p>"
        lead = (f"Paying by check for <strong>{html.escape(project_name)}</strong> "
                f"({html.escape(ref)}) — the memo line will show the project name.")
        closing = "Confirm it arrived, then mark the deposit Received in the proposal tool."
        subject = f"Deposit by check — {project_name} ({ref})"
    email_sender.notify_team(
        subject, f"<p>{lead}</p>" + detail + f"<p>{closing}</p>",
        kind="deposit", reply_link=_staff_link(p["proposal_id"]), proposal_id=p["proposal_id"],
        reply_to=email_sender.proposal_reply_to(p.get("token")),
        token=p.get("token"), project=p.get("project_name"),
    )
    _notify_customer(
        p, "We've received your deposit details",
        f"<p>Thanks — we've recorded your "
        f"{'bank transfer' if method == 'ach' else 'check'} for "
        f"<strong>{html.escape(project_name)}</strong>.</p>"
        f"<p>We'll confirm here as soon as it "
        f"{'clears' if method == 'ach' else 'arrives'}. Nothing else is needed from you "
        f"right now.</p>",
        # THE example Hanz gave. "we've recorded your check" reaching the contact who did not
        # pay reads as either a mistake or a second charge.
        actor_email=_session_email(request),
        peer_heading="The deposit for this project has been sent",
        peer_body_html=(
            f"<p><strong>{html.escape(_first_name_of(_session_email(request)) or 'Someone on your team')}"
            f"</strong> sent the {'bank transfer' if method == 'ach' else 'check'} for "
            f"<strong>{html.escape(project_name)}</strong>.</p>"
            f"<p>We'll confirm here as soon as it "
            f"{'clears' if method == 'ach' else 'arrives'}. Nothing is needed from you.</p>"),
    )
    return _json({"ok": True})


# The upstream render is a full docx + LibreOffice pass (seconds). The customer
# viewer mounts the iframe lazily, but a reload or a second recipient would
# re-trigger it — so memoize the rendered bytes per proposal for a short TTL.
_PDF_CACHE: dict[str, tuple[float, bytes]] = {}
_PDF_TTL = 600.0   # seconds
_PDF_CACHE_MAX = 64   # hard cap — PDFs are multi-MB; the VPS is RAM-constrained
_PDF_HEADERS = {"Content-Disposition": 'inline; filename="proposal.pdf"',
                "Cache-Control": "private, max-age=600"}


def _pdf_cache_put(pid: str, content: bytes) -> None:
    """Store rendered bytes, sweeping expired entries and enforcing a hard cap so
    the cache can't grow unbounded (a bare dict would retain every viewed PDF for
    the life of the process)."""
    now = time.monotonic()
    for k in [k for k, (exp, _) in _PDF_CACHE.items() if exp <= now]:
        _PDF_CACHE.pop(k, None)
    while len(_PDF_CACHE) >= _PDF_CACHE_MAX:
        _PDF_CACHE.pop(next(iter(_PDF_CACHE)), None)   # evict oldest-inserted
    _PDF_CACHE[pid] = (now + _PDF_TTL, content)


def _pdf_cache_drop(pid: str) -> None:
    _PDF_CACHE.pop(pid, None)


@app.post("/api/portal/{token}/contacts")
async def api_contacts(token: str, request: Request) -> JSONResponse:
    p = _require(request, token)
    if not p:
        return _json({"ok": False, "error": "unauthorized"}, 401)
    contacts, err = _clean_contacts((await _body(request)).get("contacts"))
    if err:
        msg = {
            "no_contacts": "Please add at least your primary contact.",
            "primary_required": "A primary contact is required.",
            "name_required": "Each contact needs a name.",
            "invalid_email": "One of the email addresses looks invalid.",
            "invalid_role": "Invalid contact role.",
            "too_many": f"Please list at most {MAX_CONTACTS} contacts.",
        }.get(err, "Please check the contact details.")
        return _json({"ok": False, "error": msg}, 400)
    who = _session_email(request)
    db.replace_contacts(p["proposal_id"], contacts, who)

    names = ", ".join(c["name"] for c in contacts)
    db.add_message(p["proposal_id"], "staff", None,
                   f"Project contacts received ({len(contacts)}): {names}.", msg_type="system")
    project = p.get("project_name") or "your proposal"
    rows = "".join(
        "<li><strong>{}</strong> — {} · {} · {}</li>".format(
            html.escape(c["role"].replace("_", " ").title()), html.escape(c["name"]),
            html.escape(c.get("email") or "—"), html.escape(c.get("phone") or "—"))
        for c in contacts)
    email_sender.notify_team(
        f"Project contacts submitted — {project}",
        f"<p>Contacts for <strong>{html.escape(project)}</strong>:</p><ul>{rows}</ul>",
        reply_link=_staff_link(p["proposal_id"]), proposal_id=p["proposal_id"],
        reply_to=email_sender.proposal_reply_to(p.get("token")),
        token=p.get("token"), project=p.get("project_name"),
    )
    _notify_customer(
        p, "Thanks — we have your project contacts",
        f"<p>We've saved the contacts for <strong>{html.escape(project)}</strong>:</p>"
        f"<ul>{rows}</ul>"
        f"<p>You can update them any time before we schedule the work.</p>",
        actor_email=who,
        peer_heading="The project contacts have been added",
        peer_body_html=(
            f"<p><strong>{html.escape(_first_name_of(who) or 'Someone on your team')}</strong> "
            f"added the contacts for <strong>{html.escape(project)}</strong>:</p>"
            f"<ul>{rows}</ul>"
            f"<p>Anyone on the project can update them before we schedule the work.</p>"),
    )
    return _json({"ok": True})


@app.get("/api/portal/{token}/pdf")
def api_pdf(token: str, request: Request):
    p = _require(request, token)
    if not p:
        return _json({"ok": False, "error": "unauthorized"}, 401)
    pid = p["proposal_id"]
    hit = _PDF_CACHE.get(pid)
    if hit and hit[0] > time.monotonic():
        return Response(content=hit[1], media_type="application/pdf", headers=_PDF_HEADERS)
    # Preferred: render the real Treadwell PDF on demand from the proposal tool.
    if config.PROPOSAL_TOOL_URL and config.SERVICE_TOKEN:
        try:
            # Render the pinned revision, so the downloaded document and the prices
            # on the page can never disagree. Omitted for legacy rows → live draft.
            params = {"draft_id": pid}
            if p.get("current_revision_no"):
                params["revision_no"] = int(p["current_revision_no"])
            r = httpx.get(
                config.PROPOSAL_TOOL_URL + "/api/admin/proposal-pdf",
                params=params,
                headers={"X-Service-Token": config.SERVICE_TOKEN},
                timeout=90,
            )
            if r.status_code == 200:
                _pdf_cache_put(pid, r.content)
                return Response(content=r.content, media_type="application/pdf", headers=_PDF_HEADERS)
            log.info("proposal-pdf upstream %s for %s", r.status_code, pid)
        except Exception as exc:  # noqa: BLE001
            log.warning("proposal-pdf fetch failed: %s", exc)
    if p.get("pdf_path"):  # fallback: a stored Storage URL (prod option)
        return RedirectResponse(p["pdf_path"])
    return _json({"ok": False, "error": "no_pdf"}, 404)


@app.get("/api/portal/{token}/deposit-invoice.pdf")
def api_deposit_invoice_pdf(token: str, request: Request):
    """The deposit invoice document. Rendered on demand from the stored columns
    (no blob storage) — the invoice NUMBER is what's persisted, so the document is
    always reproducible and always matches what was emailed."""
    p = _require(request, token)
    if not p:
        return _json({"ok": False, "error": "unauthorized"}, 401)
    invoice_no = p.get("deposit_invoice_no")
    amount = p.get("deposit_amount")
    if not invoice_no or amount is None:
        return _json({"ok": False, "error": "no_invoice"}, 404)
    try:
        payload = invoice.invoice_payload(p, float(amount), invoice_no,
                                          draft=db.get_pinned_draft_data(p) or {})
        pdf = invoice.render_invoice_pdf(payload)
    except invoice.InvoiceUnavailable as exc:
        log.error("deposit invoice unavailable for %s: %s", p["proposal_id"], exc)
        return _json({"ok": False, "error": "render_failed"}, 502)
    except Exception as exc:  # noqa: BLE001
        log.error("deposit invoice render failed for %s: %s", p["proposal_id"], exc)
        return _json({"ok": False, "error": "render_failed"}, 500)
    return Response(
        content=pdf, media_type="application/pdf",
        headers={"Content-Disposition":
                 f'attachment; filename="{invoice.invoice_filename(invoice_no)}"',
                 "Cache-Control": "private, max-age=0, no-store"},
    )


# ── service endpoint (admin proposal tool -> portal) ──────────────────────────
@app.post("/api/notify")
async def api_notify(request: Request) -> JSONResponse:
    presented = request.headers.get("x-service-token") or ""
    if not config.SERVICE_TOKEN or not hmac.compare_digest(presented, config.SERVICE_TOKEN):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    body = await _body(request)
    p = db.get_proposal(body.get("proposal_id")) if body.get("proposal_id") else None
    if not p:
        return _json({"ok": False, "error": "not_found"}, 404)
    kind = body.get("type")
    link = f"{config.PUBLIC_BASE_URL}/p/{p['token']}"
    primary = (p.get("customer_email") or "").strip().lower()
    project = p.get("project_name") or "your proposal"
    recipients = db.get_recipients(p["proposal_id"]) or ([primary] if primary else [])
    rt = email_sender.proposal_reply_to(p["token"])
    if kind == "published":
        for e in recipients:
            email_sender.send_portal_link(e, p.get("customer_name") or "" if e == primary else "", link, project,
                                          reply_to=rt, token=p["token"])
    elif kind == "reply":
        # Carry the reply TEXT through. Without it this path emailed a bare
        # "Treadwell replied to your question" + button — the same email the
        # staff-drawer path (admin_reply) already sends WITH the snippet.
        msg = _cap(body.get("message") or body.get("body"), 4000) or None
        for e in recipients:
            email_sender.send_reply_notification(e, link, project, reply_to=rt, message=msg,
                                                 token=p["token"])
    else:
        return _json({"ok": False, "error": "unknown_type"}, 400)
    return _json({"ok": True})


# ── inbound email (Resend receiving webhook) → CRM chat thread ─────────────────
def _inbound_body(email_id: str, data: dict) -> tuple[str, dict] | None:
    """Fetch the message from Resend (the webhook carries metadata only) and
    reduce it to the text we store or forward. None on fetch failure — the caller
    answers non-2xx so Svix retries, which is safe before anything is inserted."""
    try:
        r = httpx.get(f"https://api.resend.com/emails/receiving/{email_id}",
                      headers={"Authorization": f"Bearer {config.RESEND_API_KEY}"}, timeout=10)
        r.raise_for_status()
        full = r.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("inbound: body fetch failed for %s: %s", email_id, exc)
        return None
    text = (full.get("text") or "")[:100_000]
    if not text.strip():
        html_body = (full.get("html") or "")[:300_000]
        text = html.unescape(re.sub(r"<[^>]{0,300}>", " ", html_body))
    # Quoted history first, then the sender's own contact block. Both server-side, so the
    # staff CRM and the customer's portal show the same trimmed message rather than each
    # trying to tidy it up in the browser.
    body_txt = _cap(inbound.strip_signature(inbound.strip_quoted(text)), 4000)
    names = [(_cap(a.get("filename"), 120) or "attachment")
             for a in (data.get("attachments") or [])[:10] if isinstance(a, dict)]
    if names:
        body_txt = (body_txt + "\n" + "\n".join(f"[Attachment: {n}]" for n in names)).strip()
    return (body_txt or "(empty email)"), (full if isinstance(full, dict) else {})


def _inbound_forward(to: list[str], subject_line: str, heading: str, banner: str,
                     from_email: str, project: str, subject: str, body_txt: str,
                     link: str | None = None, reply_to: str | None = None,
                     verb: str = "emailed", token: str | None = None) -> None:
    """Forward an inbound email to staff. Best-effort — a mail failure must never
    turn into a webhook error, because by here the CRM insert may already be done
    and a Svix retry would only duplicate work.

    `token` stamps the project into the threading headers. This is the forward staff
    actually hit Reply on — "Customer replied by email" — so without it their answer
    came back to us carrying nothing that named the project, and bounced around the
    roster instead of reaching the customer. Omitted for an UNMATCHED email, which by
    definition has no project, and for an unverified sender, where Reply-To points at
    the sender rather than at us."""
    if not to:
        log.info("inbound: no notify recipients after roster/overrides — forward skipped")
        return
    where = f" on <strong>{html.escape(project)}</strong>" if project else ""
    esc_body = html.escape(body_txt).replace("\n", "<br>")
    body = (
        f"{banner}<p><strong>{html.escape(from_email or 'unknown')}</strong> {verb}{where}"
        f"{(' — ' + html.escape(subject)) if subject else ''}:</p>"
        f"<blockquote>{esc_body}</blockquote>"
    )
    if link:
        body += f'<p><a href="{link}">Open in the proposal tool</a></p>'
    try:
        email_sender._send(to, subject_line, email_sender._wrap(heading, body), reply_to=reply_to,
                           headers=email_sender.project_thread_headers(token))
    except Exception as exc:  # noqa: BLE001
        log.error("inbound: forward failed: %s", exc)


@app.post("/api/inbound/resend")
async def api_inbound_resend(request: Request):
    """Resend `email.received` webhook. Auth = svix signature (no session/token).
    Flow: verify → match a proposal (by the token in the recipient address, or —
    production only — by the sender's own address) → dedup → fetch the body from
    Resend → file it in the chat thread as the customer or as staff → notify.
    Non-2xx makes Svix retry, so only pre-insert failures return errors."""
    if not config.RESEND_WEBHOOK_SECRET:
        return _json({"ok": False, "error": "not_configured"}, 503)
    raw = await request.body()
    if not inbound.verify_svix(
        config.RESEND_WEBHOOK_SECRET,
        request.headers.get("svix-id") or "",
        request.headers.get("svix-timestamp") or "",
        request.headers.get("svix-signature") or "",
        raw,
    ):
        return _json({"ok": False, "error": "bad_signature"}, 401)
    try:
        event = json.loads(raw)
    except ValueError:
        return _json({"ok": False, "error": "bad_json"}, 400)
    if event.get("type") != "email.received":
        return _json({"ok": True, "ignored": "event_type"})

    data = event.get("data") or {}
    email_id = (data.get("email_id") or "").strip()
    if not email_id:
        return _json({"ok": True, "ignored": "no_email_id"})
    rcpts = []
    for key in ("to", "cc", "bcc"):
        v = data.get(key)
        rcpts += v if isinstance(v, list) else ([v] if v else [])
    if data.get("received_for"):
        rcpts.append(data["received_for"])
    from_email = (parseaddr(str(data.get("from") or ""))[1] or "").strip().lower()
    subject = _cap(data.get("subject"), 200)
    # Mail that appears to come from us is a loop — a forward or a customer relay
    # delivered straight back into this webhook. An empty From is equally unusable.
    if inbound.is_own_address(from_email, config.EMAIL_FROM, config.RESEND_INBOUND_DOMAINS):
        log.info("inbound: ignoring mail from ourselves (%r)", from_email)
        return _json({"ok": True, "ignored": "own_address"})

    def _resolve(tok):
        if not tok:
            return None
        return db.get_proposal_by_token(tok) or db.get_proposal_by_token_ci(tok)

    # 1. Token in the recipient address. Every receiving domain we have ever minted
    #    is accepted (primary + legacy), so a Reply-To sitting in an old email still
    #    finds its thread. Tried first because it needs no message fetch.
    token = inbound.find_token(rcpts, config.RESEND_INBOUND_DOMAINS)
    p = _resolve(token)
    matched_by = "address" if p else None
    is_staff = bool(from_email) and from_email in email_sender.staff_emails()
    fetched = None

    if not p:
        # Everything below needs the message itself — the threading headers live in
        # the fetched payload, not the webhook metadata. The fetch is a plain GET,
        # so a Svix retry repeating it is harmless.
        fetched = _inbound_body(email_id, data)
        if fetched is None:
            return _json({"ok": False, "error": "fetch_failed"}, 500)
        body_txt, full = fetched
        headers = full.get("headers")

        # 2. Proposal anchor in In-Reply-To / References. This is what makes a single
        #    clean Reply-To possible: the project rides in a header the customer
        #    never sees, and their mail client quotes it back for us.
        header_token = inbound.find_thread_token(headers)
        p = _resolve(header_token)
        if p:
            matched_by = "header"
            log.info("inbound: matched by thread header (token=%r)", header_token)

        # 3. Nothing identifying in the mail at all — a freshly composed email to our
        #    address. Match the SENDER, primary domain only, and only when it is
        #    unambiguous. Staff are never sender-matched: their address appears on
        #    proposals as a notify recipient, not as the customer.
        if not p and config.INBOUND_SENDER_FALLBACK and not is_staff \
                and inbound.addressed_to_domain(rcpts, config.RESEND_INBOUND_DOMAIN):
            try:
                matches = db.list_proposals_by_email(from_email)
            except Exception as exc:  # noqa: BLE001 — a lookup failure is ambiguous
                log.warning("inbound: sender lookup failed for %r: %s", from_email, exc)
                matches = []
            if len(matches) == 1:
                p, matched_by = matches[0], "sender"

        if not p:
            # 4. Unplaceable. Hand it to a human rather than dropping it — that is
            #    the point of publishing a real address. Gated on the SAME conditions
            #    as sender matching: the environment that owns untokened mail owns
            #    forwarding it too, and only on the primary domain. Otherwise both
            #    environments would forward the same stray email to their own roster,
            #    and staging would forward production customers' mail.
            if not (config.INBOUND_SENDER_FALLBACK
                    and inbound.addressed_to_domain(rcpts, config.RESEND_INBOUND_DOMAIN)):
                log.info("inbound: no proposal match (token=%r, from=%r)", token, from_email)
                return _json({"ok": True, "ignored": "no_match"})
            if inbound.is_auto_reply(subject, headers):
                log.info("inbound: unmatched auto-reply dropped (%r)", from_email)
                return _json({"ok": True, "ignored": "auto_reply"})
            why = ("sent by staff, with no proposal in the message" if is_staff else
                   "nothing in the message identifies a proposal, and no single "
                   "proposal matches this sender")
            log.info("inbound: unmatched email from %r — forwarding (%s)", from_email, why)
            # No pid, so no dedup anchor: a Resend dashboard re-delivery could
            # forward twice. Svix retries only fire on non-2xx, so normal traffic
            # forwards once.
            _inbound_forward(
                email_sender._resolve_notify("general"),
                f"Unmatched email — {from_email or 'unknown sender'}",
                "An email we could not place",
                f"<p><strong>⚠ UNMATCHED — {html.escape(why)}. Not added to any portal "
                f"thread.</strong></p>",
                from_email, "", subject, body_txt, reply_to=from_email or None)
            return _json({"ok": True, "unmatched": True})

    pid = p["proposal_id"]
    if db.has_email_message(pid, email_id):
        return _json({"ok": True, "ignored": "duplicate"})

    if fetched is None:
        fetched = _inbound_body(email_id, data)
        if fetched is None:
            return _json({"ok": False, "error": "fetch_failed"}, 500)
    body_txt, full = fetched
    auto = inbound.is_auto_reply(subject, full.get("headers"))
    project = p.get("project_name") or "proposal"
    meta = {"source": "email", "email_id": email_id, "from": from_email}

    authorized = set(e.lower() for e in (db.get_recipients(pid) or []))
    authorized.add((p.get("customer_email") or "").strip().lower())

    # Now that the project is known, widen the roster check to include the people added
    # to THIS project. The earlier global check (before matching) is deliberately the
    # narrow one: it only gates the sender fallback, where no project exists yet.
    #
    # But NEVER promote one of this proposal's own recipients. A per-project override is
    # typed into a box by hand, so a customer's address can land there by mistake in a
    # way it cannot land on the global roster — and staff is the privileged path that
    # relays a message to every recipient as Treadwell. Being on the proposal wins.
    if not is_staff and from_email and from_email not in authorized:
        is_staff = from_email in email_sender.staff_emails(pid)

    # Staff is tested BEFORE the customer: if one address were somehow on both
    # lists, filing staff as the customer would put our words in their mouth.
    #
    # A staff inbound email can speak AS Treadwell to a customer, so it is the one
    # privileged path here and roster membership alone isn't enough — a From header
    # is forgeable. Require the receiving MTA's SPF+DKIM verdict too. Failing closed
    # just sends the message through the roster forward instead.
    if is_staff and not inbound.sender_authenticated(full.get("headers")):
        log.warning("inbound: %r is on the roster but SPF/DKIM did not both pass — "
                    "refusing the staff path for proposal %s", from_email, pid)
        is_staff = False
    if is_staff:
        if auto:
            log.info("inbound: staff auto-reply ignored (%r)", from_email)
            return _json({"ok": True, "ignored": "auto_reply"})
        # The insert is the idempotency anchor: after this line, retries dedup.
        db.add_message(pid, "staff", from_email, body_txt, msg_type="text", meta=meta)
        # Same outcome as a staff reply typed in the portal: the customer sees it
        # and gets the usual notification. No roster forward — the roster is where
        # this came from. (Attachments arrive as [Attachment: name] markers only.)
        try:
            rt = email_sender.proposal_reply_to(p["token"])
            for e in (db.get_recipients(pid) or [p.get("customer_email")]):
                if e:
                    email_sender.send_reply_notification(
                        e, f"{config.PUBLIC_BASE_URL}/p/{p['token']}", project,
                        reply_to=rt, message=body_txt, token=p["token"])
        except Exception as exc:  # noqa: BLE001 — the thread insert already happened
            log.error("inbound: staff relay to the customer failed: %s", exc)
        return _json({"ok": True, "staff": True})

    # A sender-matched proposal is verified by construction — that match WAS the
    # sender's address appearing on exactly one proposal.
    verified = bool(from_email) and (from_email in authorized or matched_by == "sender")

    if verified:
        db.add_message(pid, "customer", from_email, body_txt, msg_type="text", meta=meta)
    else:
        # Never let an unverified From speak as the customer in the thread —
        # staff still see it via the forward below.
        log.warning("inbound: unverified sender %r for proposal %s", from_email, pid)

    if auto:
        # Keep the record when it's really the customer, but don't page staff for
        # an out-of-office, and never bounce one back at another autoresponder.
        log.info("inbound: auto-reply for %s — no forward (verified=%s)", pid, verified)
        return _json({"ok": True, "verified": verified, "ignored": "auto_reply"})

    # ONE send, governed by the notification roster + this project's overrides — the
    # same switch as every other portal notification (no separate hardcoded list).
    _inbound_forward(
        email_sender._resolve_notify("general", proposal_id=pid),
        f"Customer email reply — {project}", "Customer replied by email",
        "" if verified else "<p><strong>⚠ UNVERIFIED SENDER — not added to the "
                            "portal thread.</strong></p>",
        from_email, project, subject, body_txt, link=_staff_link(pid),
        # Verified: Reply-To is the proposal, so a staff reply from their own inbox
        # comes back here and reaches the customer through the thread. Unverified:
        # reply to the sender — we don't know who they are, so nothing of theirs
        # should be posted on the customer's behalf.
        reply_to=(email_sender.proposal_reply_to(p["token"]) if verified else (from_email or None)),
        token=(p["token"] if verified else None),
        verb="replied by email")
    return _json({"ok": True, "verified": verified})


# ── admin API (proposal tool -> portal; SERVICE_TOKEN-gated, server-to-server) ─
def _admin_ok(request: Request) -> bool:
    presented = request.headers.get("x-service-token") or ""
    return bool(config.SERVICE_TOKEN and hmac.compare_digest(presented, config.SERVICE_TOKEN))


@app.post("/api/admin/publish")
async def admin_publish(request: Request) -> JSONResponse:
    """Publish a proposal to the portal: read the draft (shared DB), mint a token
    (or reuse), upsert the portal_proposals row, email the customer the link."""
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    body = await _body(request)
    draft_id = (body.get("draft_id") or "").strip()
    data = db.get_draft_data(draft_id)
    if data is None:
        return _json({"ok": False, "error": "draft_not_found"}, 404)

    extras, err = _clean_emails(body.get("emails"))
    if err:
        return _json({"ok": False, "error": err}, 400)
    # Which of those contacts should NOT be chased. Cleaned by the same helper, so a malformed
    # address is refused here rather than silently ignored — an unparsed entry would mean somebody
    # un-ticked a box and got chased anyway, which is the failure nobody would notice.
    no_followups, err = _clean_emails(body.get("no_followups"))
    if err:
        return _json({"ok": False, "error": err}, 400)
    contact = (data.get("contact_email") or "").strip().lower()
    # Union semantics: the intake contact is ALWAYS a recipient (the Files-screen
    # modal never removes it — it only adds). `emails` absent → legacy behavior.
    if extras:
        recipients = ([contact] if contact else []) + [e for e in extras if e != contact]
        primary = contact or recipients[0]     # no intake email → first added address is primary
    else:
        recipients = None                       # legacy call: don't touch the extra recipients
        primary = contact
    if not primary:
        return _json({"ok": False, "error": "no_contact_email"}, 400)  # can't publish to nobody
    if recipients is not None and len(recipients) > MAX_RECIPIENTS:
        return _json({"ok": False, "error": "too_many_recipients"}, 400)

    name = _cap(data.get("contact_name"), 120)
    project = _cap(data.get("project_name"), 200) or "Your Proposal"
    pdf_path = (body.get("pdf_path") or "").strip() or None
    by = _cap(body.get("by"), 120) or None
    # Optional personal note the estimator typed on the Done page — shown in the
    # customer's proposal-ready email above the button.
    note = _cap(body.get("message"), 2000) or None
    # Whether this job collects a 25% deposit, decided by staff at send time. None
    # (field absent — an older proposal tool) means "don't change it": on create
    # that lands on the column default TRUE, on update it preserves what was sent.
    rd = body.get("require_deposit")
    require_deposit = None if rd is None else bool(rd)
    # Which snapshot of the project this send represents. Absent → an older proposal
    # tool that doesn't snapshot; the customer view falls back to the live draft,
    # exactly as before.
    raw_rev = body.get("revision_no")
    try:
        rev_no = int(raw_rev) if raw_rev is not None and int(raw_rev) > 0 else None
    except (TypeError, ValueError):
        rev_no = None
    # Who owns chasing this proposal. The proposal tool requires it at send time;
    # absent means an older tool, so the stored value is preserved.
    assigned = (parseaddr(str(body.get("assigned_estimator") or ""))[1] or "").strip().lower()
    if body.get("assigned_estimator") and not assigned:
        return _json({"ok": False, "error": "invalid_estimator"}, 400)

    existing = db.get_proposal(draft_id)
    revised = False
    if existing:
        token = existing["token"]
        # Sending a new version to a lost opportunity puts it back in play — staff
        # would otherwise have to remember to un-close it by hand, and the board
        # would show a live proposal sitting in Closed-lost.
        if existing.get("proposal_status") == "closed_lost" and db.reopen_if_closed(draft_id):
            db.add_message(draft_id, "staff", None,
                           "Re-opened — a new version of this proposal has been sent.",
                           msg_type="system")
        db.update_portal_proposal(draft_id, primary, name, project, pdf_path,
                                  deposit_required=require_deposit, revision_no=rev_no)
        _pdf_cache_drop(draft_id)   # a re-publish may have changed the document — don't serve a stale render
        # A second (or later) revision is a genuinely new document, not a re-send of
        # the same one: reopen it for approval, retire the old card, post a new one.
        if rev_no and rev_no > 1:
            revised = True
            was_approved = db.reset_for_revision(draft_id, rev_no)
            db.supersede_proposal_cards(draft_id, rev_no)
            db.add_message(draft_id, "staff", None,
                           f"Revision {rev_no} of your proposal is ready to review.",
                           msg_type="proposal_card", meta={"revision_no": rev_no})
            if was_approved:
                # Say plainly that the earlier agreement no longer stands, so nobody
                # is left thinking a signed number still applies.
                db.add_message(
                    draft_id, "staff", None,
                    f"Revision {rev_no} replaces the previous version. Your earlier approval "
                    f"has been recorded for reference, and this revision needs a new approval.",
                    msg_type="system")
        elif rev_no:
            # Re-send of the FIRST revision (e.g. adding a recipient). Just re-point.
            db.reset_for_revision(draft_id, rev_no)
    else:
        token = ca.new_proposal_token()
        db.create_portal_proposal(draft_id, token, primary, name, project, pdf_path, by,
                                  deposit_required=True if require_deposit is None else require_deposit,
                                  revision_no=rev_no)
        # Seed the chat thread with the proposal card (first publish only).
        db.add_message(draft_id, "staff", None, "Your proposal is ready to review.",
                       msg_type="proposal_card",
                       meta={"revision_no": rev_no} if rev_no else None)

    # Reconcile the recipient set.
    if recipients is None:                      # legacy call — preserve exact old semantics
        if existing:
            old = (existing.get("customer_email") or "").strip().lower()
            if old and old != primary:
                db.remove_recipient(draft_id, old)   # replaced primary loses access (as today)
        db.add_recipient(draft_id, primary, by)
        send_list = db.get_recipients(draft_id) or [primary]
    else:
        # no_followups rides along so the flag is set in the SAME transaction that writes the
        # recipients. Two calls would leave a window where a contact exists and is about to be
        # chased; the worker runs on its own clock and does not wait for a second write.
        db.set_recipients(draft_id, recipients, by,  # revokes any extra dropped from the list
                          no_followups=no_followups)
        send_list = recipients

    link = f"{config.PUBLIC_BASE_URL}/p/{token}"
    # One send per recipient (keeps _thread_headers per-recipient; recipients
    # never see each other's addresses). Only the primary gets the name greeting.
    rt = email_sender.proposal_reply_to(token)
    emailed = [e for e in send_list
               if email_sender.send_portal_link(e, name if e == primary else "", link, project,
                                                 reply_to=rt, note=note, token=token,
                                                 revised=revised)]

    # Enrol (or re-enrol) in follow-up automation. Stamped AFTER the emails go out so
    # the cadence clock starts from the send the customer actually received, and last
    # so a failure here can never stop a proposal from being delivered.
    try:
        if assigned:
            db.set_assigned_estimator(draft_id, assigned)
        db.enroll_followup(draft_id)
    except Exception as exc:  # noqa: BLE001 — the proposal is sent; automation is secondary
        log.warning("could not enrol %s in follow-ups: %s", draft_id, exc)

    return _json({"ok": True, "token": token, "url": link, "customer_email": primary,
                  "recipients": send_list, "emailed": emailed,
                  "revision_no": rev_no, "revised": revised,
                  "assigned_estimator": assigned or None})


@app.get("/api/admin/pipeline")
def admin_pipeline(request: Request) -> JSONResponse:
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    unread = db.unread_counts()
    # The SAVED cadence, read once for the whole board rather than per row.
    #
    # `next_due_at` used to be called with no cfg, so this column was computed from the shipped
    # constants while the worker used whatever staff had saved. Change "every 3 days" to "every 5"
    # and every date on the board is wrong; raise `max_recurring` and it reads "Nothing scheduled"
    # while emails keep going out. A schedule nobody can see anywhere else has to be the real one.
    #
    # Guarded, because an unreadable settings row must not take down the pipeline the way the
    # unguarded click columns would have — the shipped cadence is the right answer when we cannot
    # read the saved one, and it is what the worker falls back to as well.
    try:
        followup_cfg = followup_settings.merge(db.get_settings(followup_settings.ROW_ID))
    except Exception as exc:  # noqa: BLE001
        log.warning("[pipeline] could not read the cadence, showing the shipped one: %s", exc)
        followup_cfg = followup_settings.defaults()
    # Recipients and who has viewed, both read ONCE for the whole board rather than per card.
    # This endpoint is polled every 25 seconds; a query per row is how a 60-proposal board turns
    # into 120 round-trips. Both degrade to {} on a database without the migration, which makes
    # the "N recipients · M viewed" line simply not render.
    viewed = db.views_by_proposal()
    recips = db.recipients_by_proposal()
    out = []
    for r in db.list_all_portal_proposals():
        out.append({
            "proposal_id": r["proposal_id"], "token": r["token"],
            "customer_email": r["customer_email"], "customer_name": r.get("customer_name"),
            "project_name": r.get("project_name"), "proposal_status": r["proposal_status"],
            "deposit_status": r["deposit_status"], "schedule_status": r["schedule_status"],
            "contacts_status": r.get("contacts_status") or "pending",
            "approved_total": float(r["approved_total"]) if r.get("approved_total") is not None else None,
            "deposit_amount": float(r["deposit_amount"]) if r.get("deposit_amount") is not None else None,
            # Lets the board stop parking no-deposit jobs in a deposit column they
            # can never leave. Legacy rows read as required.
            "deposit_required": r.get("deposit_required") is not False,
            # Who owns the follow-up, and where this proposal stands in automation.
            "assigned_estimator": r.get("assigned_estimator"),
            "followup_state": _followup_state(r),
            # Per-stage dates so each board column sorts by its own milestone rather
            # than by whatever was touched last.
            "last_viewed_at": _iso(r.get("last_viewed_at")),
            # WHICH contacts, not just whether somebody. Only ever more interesting than
            # last_viewed_at on a proposal with more than one recipient, and the card only
            # renders the line in that case. Full addresses: this payload is staff-only.
            "recipients": recips.get(r["proposal_id"]) or (
                [r["customer_email"]] if r.get("customer_email") else []),
            "viewed_by": viewed.get(r["proposal_id"]) or [],
            # Somebody followed the email link. A soft signal, reported separately from
            # `viewed` on purpose (see db.mark_link_clicked): it tells the board that the
            # email reached a mailbox, which is what distinguishes "they haven't decided"
            # from "we may have the wrong address" on a proposal stuck in Sent.
            "link_clicked_at": _iso(r.get("link_clicked_at")),
            "last_link_clicked_at": _iso(r.get("last_link_clicked_at")),
            "deposit_submitted_at": _iso(r.get("deposit_submitted_at")),
            "deposit_received_at": _iso(r.get("deposit_received_at")),
            "contacts_received_at": _iso(r.get("contacts_received_at")),
            "scheduled_at": _iso(r.get("scheduled_at")),
            "last_activity_at": _iso(_last_activity(r)),
            "last_followup_at": _iso(r.get("last_staff_followup_at")),
            # Named separately so the board can say WHAT last happened, not just
            # when: dating a card "Viewed 7/12" when the real event was a customer
            # message on 7/30 reads as a stale deal that nobody has touched.
            "last_message_at": _iso(r.get("last_message_at")),
            # When the CUSTOMER last came back to us — null means they never have. The staff
            # board sorts "seen but never answered" from "mid-conversation" on this, and
            # last_message_at cannot tell them apart (it counts our own messages too).
            "customer_replied_at": _iso(r.get("customer_replied_at")),
            # When the cadence will next email this customer. Computed from the same
            # anchors due_now() uses, so the Follow-ups page can show a schedule that
            # otherwise exists nowhere a human can see. None = nothing is coming
            # (not automated, switched off, approved, closed, or cadence exhausted).
            "next_followup_at": _iso(followup_rules.next_due_at(r, _now_utc(), followup_cfg)),
            "unread": unread.get(r["proposal_id"], 0),   # customer messages awaiting a staff reply
            # Who owns it, and the milestones the board dates a card by. The
            # staff side picks the latest of these — it also owns turning the
            # email into a name, because portal_app is denied `profiles`.
            "estimator_email": r.get("estimator_email"),
            "sent_at": _iso(r.get("created_at")),        # a row can't exist unsent
            "viewed_at": _iso(r.get("viewed_at")),       # FIRST view only
            "approved_at": _iso(r.get("approved_at")),
            "deposit_requested_at": _iso(r.get("deposit_requested_at")),
        })
    return _json({"ok": True, "proposals": out})


# Preview length for the bell/toast — enough to read at a glance, short enough
# that the notification payload stays small.
_RECENT_MSG_PREVIEW = 240


def _recipient_activity(proposal_id: str, proposal: dict, approval: Optional[dict]) -> list:
    """Per-contact activity for the drawer: who opened it, who wrote, who paid, who signed.

    Assembled from what each fact is actually stored against, which is four different places —
    that is why it lives here rather than in a query. All four reads are guarded: this decorates
    a drawer that has to open regardless.

    Only worth rendering when there are two or more contacts, and the frontend gates on that. It
    is still built for one, because a single-contact proposal showing "not viewed" next to the
    only name is a legitimate thing an estimator might want.
    """
    emails = _recipients_or_empty(proposal_id, proposal)
    if not emails:
        return []
    try:
        views = {(v["email"] or "").strip().lower(): v for v in db.list_views(proposal_id)}
    except Exception:  # noqa: BLE001
        views = {}
    # Who is being CHASED. Read as a set of the opted-in, so a database without the migration —
    # where every row reads as opted in — produces every contact marked on, which is the truth.
    try:
        chased = {e.strip().lower() for e in db.get_followup_recipients(proposal_id)}
    except Exception as exc:  # noqa: BLE001
        log.warning("follow-up recipients unavailable for %s: %s", proposal_id, exc)
        chased = {e.strip().lower() for e in emails}
    replied: set = set()
    paid: set = set()
    try:
        for m in db.list_messages(proposal_id):
            if m.get("author_kind") == "customer" and m.get("author_email"):
                replied.add(m["author_email"].strip().lower())
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read the thread for recipient activity on %s: %s", proposal_id, exc)
    try:
        for d in db.list_deposits(proposal_id):
            if d.get("submitted_by"):
                paid.add(d["submitted_by"].strip().lower())
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read deposits for recipient activity on %s: %s", proposal_id, exc)
    approver = ((approval or {}).get("approver_email") or "").strip().lower()

    out = []
    for e in emails:
        k = e.strip().lower()
        v = views.get(k)
        out.append({
            "email": e,
            "name": _first_name_of(e),
            "viewed_at": _iso(v.get("first_viewed_at")) if v else None,
            "last_viewed_at": _iso(v.get("last_viewed_at")) if v else None,
            "view_count": (v.get("view_count") if v else 0) or 0,
            "replied": k in replied,
            "paid": k in paid,
            "followups": k in chased,
            "approved": bool(approver) and k == approver,
        })
    return out


def _recipients_or_empty(proposal_id: str, proposal: dict) -> list:
    """Every address this proposal was sent to, primary first. [] on any failure.

    Best-effort because it only drives labels: the staff drawer has to open even when this
    read does not, and an unnamed bubble is the behaviour it had yesterday."""
    try:
        rows = db.get_recipients(proposal_id) or []
    except Exception as exc:  # noqa: BLE001
        log.warning("recipients unavailable for %s: %s", proposal_id, exc)
        rows = []
    primary = (proposal or {}).get("customer_email")
    # get_recipients usually already includes the primary contact, so this dedupes rather than
    # filters: case-insensitively, because a hand-typed recipient can differ only in case and a
    # two-contact proposal showing three contacts would make the staff bubbles name everyone.
    seen, uniq = set(), []
    for e in ([primary] if primary else []) + list(rows):
        k = str(e or "").strip().lower()
        if k and k not in seen:
            seen.add(k); uniq.append(e)
    return uniq


def _recent_msg(row: dict) -> dict:
    """One recent customer message shaped for the staff notification feed. Body is
    truncated server-side; `created_at` is ISO (matches _msg). `msg_type` lets the
    staff side tell a question apart from a deposit submission (defaults to 'text'
    so an older row without one still renders)."""
    body = (row.get("body") or "").strip()
    if len(body) > _RECENT_MSG_PREVIEW:
        body = body[:_RECENT_MSG_PREVIEW - 1].rstrip() + "…"
    return {
        "id": row.get("id"),
        "proposal_id": row.get("proposal_id"),
        "project_name": row.get("project_name"),
        "customer_name": row.get("customer_name"),
        "author_email": row.get("author_email"),
        "msg_type": row.get("msg_type") or "text",
        "body": body,
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
    }


@app.get("/api/admin/recent-messages")
def admin_recent_messages(request: Request) -> JSONResponse:
    """Newest customer messages across all proposals — drives the staff tool's
    notification bell + bottom-right toast. Customer-originated rows only: chat
    text plus deposit submissions (staff replies and staff system/card rows are
    excluded by the query)."""
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    rows = db.list_recent_customer_messages(limit=25)
    return _json({"ok": True, "messages": [_recent_msg(r) for r in rows]})


@app.get("/api/admin/proposal/{proposal_id}")
def admin_proposal(proposal_id: str, request: Request) -> JSONResponse:
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    p = db.get_proposal(proposal_id)
    if not p:
        return _json({"ok": False, "error": "not_found"}, 404)
    appr = db.latest_approval(proposal_id)
    # So the staff review form can prefill the invoice number instead of leaving
    # it blank; peeking never consumes a sequence value.
    try:
        next_no = p.get("deposit_invoice_no") or db.peek_next_invoice_no()
    except Exception:  # noqa: BLE001 — a missing sequence must not break the drawer
        next_no = p.get("deposit_invoice_no")
    return _json({
        "ok": True,
        "next_invoice_no": next_no,
        "proposal": {
            "proposal_id": p["proposal_id"], "token": p["token"],
            "url": f"{config.PUBLIC_BASE_URL}/p/{p['token']}",
            "customer_email": p["customer_email"], "customer_name": p.get("customer_name"),
            "project_name": p.get("project_name"), "proposal_status": p["proposal_status"],
            "deposit_status": p["deposit_status"], "schedule_status": p["schedule_status"],
            "contacts_status": p.get("contacts_status") or "pending",
            "approved_total": float(p["approved_total"]) if p.get("approved_total") is not None else None,
            "deposit_amount": float(p["deposit_amount"]) if p.get("deposit_amount") is not None else None,
            "deposit_requested_at": p["deposit_requested_at"].isoformat() if p.get("deposit_requested_at") else None,
            "deposit_required": p.get("deposit_required") is not False,
            "assigned_estimator": p.get("assigned_estimator"),
            "followup_state": _followup_state(p),
            "recipients": db.get_recipients(proposal_id),
        },
        "contacts": [_contact(c) for c in db.list_contacts(proposal_id)],
        # Recent follow-up activity for the drawer: what the automation sent, what the
        # estimator logged, and what the customer said about their timeline.
        "followups": [{
            "kind": f["kind"],
            "detail": f.get("detail") or {},
            "by": f.get("created_by"),
            "created_at": _iso(f.get("created_at")),
        } for f in db.list_followups(proposal_id)],
        "approval": ({
            "name": appr["name"], "title": appr.get("title"),
            "date": appr["approved_date"].isoformat() if appr.get("approved_date") else None,
            "total": float(appr["total"]) if appr.get("total") is not None else None,
            "option": appr.get("option_label"), "options": appr.get("options"),
            "approver_email": appr.get("approver_email"),
        } if appr else None),
        # Who the proposal actually went to. The drawer needs it to say WHICH contact wrote a
        # message, and the bubbles only name anyone when there is more than one — a single
        # contact needs no label, which is why the label was removed in the first place.
        # Guarded: a missing recipients read must cost the names, not the drawer.
        "recipients": _recipients_or_empty(proposal_id, p),
        # One row per contact: viewed / replied / paid / approved. This is the "highlight in the
        # CRM who viewed it as well and who replied" half of what Hanz asked for — the peer
        # notifications tell the other CONTACT, this tells the estimator.
        "recipient_activity": _recipient_activity(proposal_id, p, appr),
        "questions": [_q(q) for q in db.list_questions(proposal_id)],   # text-only (legacy drawer)
        "messages": [_msg(m) for m in db.list_messages(proposal_id)],   # full thread (revamped drawer)
        "deposit_ref": proposals.deposit_ref(proposal_id),
        "deposits": [{
            "method": d["method"], "account_name": d.get("account_name"), "bank_name": d.get("bank_name"),
            "masked_ref": d.get("masked_ref"), "note": d.get("note"),
            "routing_number": d.get("routing_number"), "account_number": d.get("account_number"),
            "account_type": d.get("account_type"),
            "sent_date": d["sent_date"].isoformat() if d.get("sent_date") else None,
            "trace_ref": d.get("trace_ref"),
            "sent_to_beneficiary": d.get("sent_to_beneficiary"), "sent_to_bank": d.get("sent_to_bank"),
            "sent_to_routing": d.get("sent_to_routing"), "sent_to_account": d.get("sent_to_account"),
            "check_number": d.get("check_number"),
            "submitted_at": d["submitted_at"].isoformat() if d.get("submitted_at") else None,
        } for d in db.list_deposits(proposal_id)],
    })


# ── follow-up automation (staff-facing) ───────────────────────────────────────
# Kinds an estimator may log. `auto_email` and `customer_status` are minted by the
# server only — letting staff post them would corrupt both the dedupe and the
# digest's "has anyone actually chased this?" signal.
_STAFF_FOLLOWUP_KINDS = ("staff_call", "staff_email", "staff_text", "staff_note")
_LOST_REASONS = ("price", "another_contractor", "canceled", "scope_changed", "timing", "other")
_PAUSE_MONTHS = (1, 2, 3, 4)


# ── the follow-up cadence, as settings ────────────────────────────────────────
# Hanz asked for the chase schedule AND the four customer emails to be editable rather than
# constants in the code. One global cadence, editable by any signed-in staff member (the tool's
# own sign-in is the gate; this endpoint sees only the service token).
@app.get("/api/admin/settings/followups")
def admin_get_followup_settings(request: Request) -> JSONResponse:
    """The current cadence, plus a preview of each email as a customer would receive it.

    Always returns a usable cadence: an absent settings row means "as shipped", which is the
    normal state on any environment where the DDL has not been applied yet."""
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    # Two reads, two separate guards, because they carry different weight.
    #
    # `get_settings` is material: this editor REPLACES the whole row, so if a failed read were
    # reported as "nothing saved", the page would show the shipped defaults, say "never changed",
    # and the next Save would overwrite four hand-written customer emails with boilerplate —
    # attributed to whoever pressed the button. A missing table is the one failure that really
    # does mean "as shipped" (prod cannot apply its own DDL), and `get_settings` returns None for
    # that case alone; anything else raises and is reported as unreadable so the editor can refuse
    # to write over what it cannot see.
    #
    # `settings_meta` is only the "who last changed this" caption. It used to share this try, so a
    # blip on a decorative query threw away a config that had been read perfectly well.
    stored, read_failed = None, False
    try:
        stored = db.get_settings(followup_settings.ROW_ID)
    except Exception as exc:  # noqa: BLE001
        log.error("[settings] could not read follow-up settings: %s", exc)
        read_failed = True
    meta = {}
    if not read_failed:
        try:
            meta = db.settings_meta(followup_settings.ROW_ID)
        except Exception as exc:  # noqa: BLE001 — the caption is not worth failing a page over
            log.warning("[settings] could not read the audit line: %s", exc)
    cfg = followup_settings.merge(stored)
    return _json({
        "ok": True,
        "settings": cfg,
        # `saved` tells the editor whether it is showing somebody's choices or the shipped
        # defaults — without it a fresh install looks identical to an edited one.
        "saved": stored is not None,
        # Set when we could not read the row at all. The editor must then neither claim the
        # cadence has never been changed nor allow a save, because it does not know what it
        # would be replacing.
        "read_failed": read_failed,
        "updated_at": _iso(meta.get("updated_at")),
        "updated_by": meta.get("updated_by") or "",
        # ALL_TEMPLATE_KEYS, not TEMPLATE_KEYS: the editor has a tab for the "Proposal sent"
        # email too, and a tab with no preview renders as a broken panel. TEMPLATE_KEYS stays
        # what the WORKER walks, so the cadence still cannot chase with the sent email.
        "previews": {k: followup_settings.preview(cfg, k)
                     for k in followup_settings.ALL_TEMPLATE_KEYS},
        "tokens": list(followup_settings.TOKENS),
        # The editor labels its tabs from these, so a refusal that names an email ("the
        # “Second reminder” email needs {link}") points at a tab that exists.
        "labels": dict(followup_settings.LABELS),
        # Longer, when-it-fires wording for the heading under the tabs. Separate from labels
        # because labels are quoted verbatim in validation refusals, where a sentence reads
        # badly. Served rather than hardcoded in the editor for the same reason labels are:
        # the message that refuses a save and the heading above the form must not disagree.
        "editor_titles": dict(followup_settings.EDITOR_TITLES),
    })


@app.put("/api/admin/settings/followups")
async def admin_put_followup_settings(request: Request) -> JSONResponse:
    """Save the cadence. Returns what was actually stored, including any clamping.

    Returning the stored values rather than an empty ok is deliberate: numbers get pulled into
    range on the way in, and somebody who typed 2 hours needs to see that they got 4 rather than
    believe their edit took."""
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    body = await _body(request)
    try:
        cfg = followup_settings.validate(body.get("settings") if "settings" in body else body)
    except followup_settings.ValidationError as exc:
        return _json({"ok": False, "error": str(exc)}, 400)
    by = _cap(body.get("by"), 120) or None
    try:
        meta = db.save_settings(followup_settings.ROW_ID, cfg, by) or {}
    except Exception as exc:  # noqa: BLE001
        log.error("[settings] could not save follow-up settings: %s", exc)
        return _json({"ok": False, "error": "Couldn't save that — the settings table may be "
                                            "missing on this environment."}, 500)
    # The same audit fields the GET returns, carried back by the write itself. Without them the
    # editor still read "never changed" underneath the edit it had just stored, until somebody
    # reloaded — the one question that line exists to answer, answered wrongly.
    return _json({
        "ok": True,
        "settings": cfg,
        "saved": True,
        "updated_at": _iso(meta.get("updated_at")),
        "updated_by": meta.get("updated_by") or by or "",
        # ALL_TEMPLATE_KEYS, not TEMPLATE_KEYS: the editor has a tab for the "Proposal sent"
        # email too, and a tab with no preview renders as a broken panel. TEMPLATE_KEYS stays
        # what the WORKER walks, so the cadence still cannot chase with the sent email.
        "previews": {k: followup_settings.preview(cfg, k)
                     for k in followup_settings.ALL_TEMPLATE_KEYS},
    })


@app.post("/api/admin/settings/followups/preview")
async def admin_preview_followup_settings(request: Request) -> JSONResponse:
    """Render the wording being typed, WITHOUT saving it.

    The whole safety net for editable email: an unfilled token or a deleted button is obvious in a
    preview and invisible in a form. Validation errors come back as 400 with the reason, so the
    editor can show "this will not send" before anybody commits it."""
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    body = await _body(request)
    try:
        cfg = followup_settings.validate(body.get("settings") if "settings" in body else body)
    except followup_settings.ValidationError as exc:
        return _json({"ok": False, "error": str(exc)}, 400)
    # ALL_TEMPLATE_KEYS, matching the GET and the PUT. This endpoint is what runs as somebody
    # TYPES, and it was the only one of the three still walking the cadence set — so editing the
    # "Proposal sent" email showed a preview on load, then a blank panel on the first keystroke.
    return _json({"ok": True,
                  "previews": {k: followup_settings.preview(cfg, k)
                               for k in followup_settings.ALL_TEMPLATE_KEYS}})


@app.post("/api/admin/proposal/{proposal_id}/assign")
async def admin_assign(proposal_id: str, request: Request) -> JSONResponse:
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    if not db.get_proposal(proposal_id):
        return _json({"ok": False, "error": "not_found"}, 404)
    body = await _body(request)
    email = (parseaddr(str(body.get("estimator_email") or ""))[1] or "").strip().lower()
    if not email:
        return _json({"ok": False, "error": "invalid_estimator"}, 400)
    by = _cap(body.get("by"), 120) or None
    db.set_assigned_estimator(proposal_id, email)
    db.add_followup(proposal_id, "staff_note", {"action": "reassigned", "to": email}, by)
    return _json({"ok": True, "assigned_estimator": email})


@app.post("/api/admin/proposal/{proposal_id}/followup-automation")
async def admin_followup_automation(proposal_id: str, request: Request) -> JSONResponse:
    """Take a proposal off automation, or put it back on.

    This is the spec's "estimator manually removes the proposal from automation" —
    the deliberate human override, so it is sticky across re-publishes."""
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    p = db.get_proposal(proposal_id)
    if not p:
        return _json({"ok": False, "error": "not_found"}, 404)
    body = await _body(request)
    enabled = bool(body.get("enabled"))
    by = _cap(body.get("by"), 120) or None
    db.set_followup_enabled(proposal_id, enabled)
    db.add_followup(proposal_id, "staff_note",
                    {"action": "automation_on" if enabled else "automation_off"}, by)
    return _json({"ok": True, "followup_state": _followup_state(db.get_proposal(proposal_id) or p)})


@app.post("/api/admin/proposal/{proposal_id}/followups")
async def admin_log_followup(proposal_id: str, request: Request) -> JSONResponse:
    """Record that an estimator chased this one personally.

    The digest reads these: a logged follow-up suppresses the recommendation, and its
    absence is what "no follow-up logged in 9 days" means."""
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    if not db.get_proposal(proposal_id):
        return _json({"ok": False, "error": "not_found"}, 404)
    body = await _body(request)
    kind = str(body.get("kind") or "").strip().lower()
    if kind in ("call", "email", "text", "note"):
        kind = "staff_" + kind          # accept the short form the drawer sends
    if kind not in _STAFF_FOLLOWUP_KINDS:
        return _json({"ok": False, "error": "invalid_kind"}, 400)
    note = _cap(body.get("note"), 2000) or None
    row = db.add_followup(proposal_id, kind, {"note": note}, _cap(body.get("by"), 120) or None)
    return _json({"ok": True, "followup": {
        "kind": row["kind"], "detail": row.get("detail") or {},
        "by": row.get("created_by"), "created_at": _iso(row.get("created_at"))}})


@app.get("/api/admin/proposal/{proposal_id}/followups")
def admin_list_followups(proposal_id: str, request: Request) -> JSONResponse:
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    return _json({"ok": True, "followups": [{
        "kind": f["kind"], "detail": f.get("detail") or {},
        "by": f.get("created_by"), "created_at": _iso(f.get("created_at")),
    } for f in db.list_followups(proposal_id)]})


@app.post("/api/admin/proposal/{proposal_id}/status")
async def admin_set_status(proposal_id: str, request: Request) -> JSONResponse:
    """Staff-side equivalent of the customer's project-status card: pause the chase,
    close the opportunity, or put it back in play. Same db helpers, so the two paths
    can never diverge."""
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    p = db.get_proposal(proposal_id)
    if not p:
        return _json({"ok": False, "error": "not_found"}, 404)
    body = await _body(request)
    status = str(body.get("status") or "").strip().lower()
    by = _cap(body.get("by"), 120) or None

    if status == "delayed":
        try:
            months = int(body.get("months") or 0)
        except (TypeError, ValueError):
            months = 0
        if months not in _PAUSE_MONTHS:
            return _json({"ok": False, "error": "invalid_months"}, 400)
        until = followup_rules.add_months(followup_rules.business_today(_now_utc()), months)
        db.pause_followups(proposal_id, until)
        db.add_followup(proposal_id, "staff_note",
                        {"action": "paused", "months": months, "until": until.isoformat()}, by)
    elif status == "closed_lost":
        reason = str(body.get("reason") or "").strip().lower() or None
        if reason and reason not in _LOST_REASONS:
            return _json({"ok": False, "error": "invalid_reason"}, 400)
        # An approved proposal is closeable from here as of 2026-08-10, per Hanz: a customer
        # can sign and the job still die, so staff need a way to file it as lost. The approval
        # is kept rather than erased (close_lost leaves the approved_* columns and the
        # portal_approvals rows alone) and moving the card back to Active restores 'approved',
        # so the old "a stray click must not clobber a win" objection is covered without
        # blocking the move. The only failure left is the row disappearing between the
        # get_proposal above and this write.
        if not db.close_lost(proposal_id, reason):
            return _json({"ok": False, "error": "not_found"}, 404)
        db.add_followup(proposal_id, "staff_note",
                        {"action": "closed_lost", "reason": reason}, by)
    elif status == "active":
        db.resume_followups(proposal_id)
        if p.get("proposal_status") == "closed_lost":
            db.reopen_if_closed(proposal_id)
        db.add_followup(proposal_id, "staff_note", {"action": "reactivated"}, by)
    else:
        return _json({"ok": False, "error": "invalid_status"}, 400)

    fresh = db.get_proposal(proposal_id) or p
    return _json({"ok": True, "proposal_status": fresh.get("proposal_status"),
                  "followup_state": _followup_state(fresh)})


@app.post("/api/admin/send-digest")
async def admin_send_digest(request: Request) -> JSONResponse:
    """Render and send one estimator's morning follow-up list.

    The proposal tool decides WHO and WHAT — it owns the pipeline scoring and the
    Claude call. This end owns the email: the branded template, the staff deep links
    (only this side knows both base URLs) and Resend. Nothing is looked up here, so a
    digest can be sent for a proposal this request never reads.

    Items are treated as untrusted input all the way to the template, which escapes
    every field — the tool is ours, but a rendered-to-HTML payload is exactly where a
    stray customer-supplied project name would matter."""
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    body = await _body(request)
    email = (parseaddr(str(body.get("estimator_email") or ""))[1] or "").strip().lower()
    if not email or "@" not in email:
        return _json({"ok": False, "error": "invalid_email"}, 400)
    raw = body.get("items")
    if not isinstance(raw, list):
        return _json({"ok": False, "error": "invalid_items"}, 400)
    # Capped here as well as at the source: this endpoint must not be a way to send
    # a hundred-row email, whatever the caller believes it is sending.
    items = [i for i in raw if isinstance(i, dict)][:25]
    if not items:
        return _json({"ok": True, "sent": False, "reason": "empty"})
    ok = email_sender.send_digest(email, items, name=_estimator_name(email),
                                  staff_link=_staff_link)
    return _json({"ok": True, "sent": bool(ok), "items": len(items)})


def _estimator_name(email: str) -> str:
    """A first name for the greeting, read off the address.

    `portal_app` is denied the `profiles` table by design, so the real name isn't
    reachable from here — and "Morning Kyle," off kyle@ is worth more than no
    greeting at all."""
    local = str(email or "").split("@")[0]
    return " ".join(w.capitalize() for w in re.split(r"[._-]+", local) if w)


@app.post("/api/admin/proposal/{proposal_id}/reply")
async def admin_reply(proposal_id: str, request: Request) -> JSONResponse:
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    p = db.get_proposal(proposal_id)
    if not p:
        return _json({"ok": False, "error": "not_found"}, 404)
    body = await _body(request)
    text = _cap(body.get("body"), 4000)
    if not text:
        return _json({"ok": False, "error": "empty"}, 400)
    db.add_question(proposal_id, "staff", _cap(body.get("by"), 120) or "Treadwell", text)
    link = f"{config.PUBLIC_BASE_URL}/p/{p['token']}"
    project = p.get("project_name") or "your proposal"
    rt = email_sender.proposal_reply_to(p["token"])
    for e in (db.get_recipients(proposal_id) or [p["customer_email"]]):
        email_sender.send_reply_notification(e, link, project, reply_to=rt, message=text,
                                             token=p["token"])
    return _json({"ok": True})


@app.post("/api/admin/proposal/{proposal_id}/followup-recipient")
async def admin_followup_recipient(proposal_id: str, request: Request) -> JSONResponse:
    """Add a contact to the follow-up list, or turn one on/off.

    Hanz, 2026-08-12: "on this project container on the follow ups we must have the ability to add
    or remove COntacts who receive the follow ups."

    "Remove" means STOP CHASING, not stop being a contact — the row stays and keeps receiving the
    proposal, the invoice and every reply. Deleting the recipient would revoke their portal access,
    which is a different and much larger decision than "don't nag this person".

    Adding somebody sends them the proposal link, because a recipient who has never been sent one
    cannot reach the portal: the link IS the access. That send uses the editable Proposal sent
    template, same as a publish.
    """
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    p = db.get_proposal(proposal_id)
    if not p:
        return _json({"ok": False, "error": "not_found"}, 404)
    body = await _body(request)
    email = (body.get("email") or "").strip().lower()
    if not email or not _EMAIL_RE.match(email) or len(email) > 254:
        return _json({"ok": False, "error": "invalid_email"}, 400)
    enabled = body.get("enabled") is not False          # absent means "on"
    add = bool(body.get("add"))
    # Same convention as every other admin endpoint here: the staff tool stamps who did it into
    # the body (its own proxy fills it from the signed-in user), because this request arrives on a
    # service token that identifies the APP, not a person.
    by = _cap(body.get("by"), 120) or None

    existing = [e.strip().lower() for e in (db.get_recipients(proposal_id) or [])]
    if add and email not in existing:
        if len(existing) >= MAX_RECIPIENTS:
            return _json({"ok": False, "error": "too_many_recipients"}, 400)
        db.add_recipient(proposal_id, email, by)
        # They cannot reach the portal without the link, so adding somebody and not sending it
        # would put a contact on the list who can never open the thing they are a contact for.
        try:
            email_sender.send_portal_link(
                email, "", f"{config.PUBLIC_BASE_URL}/p/{p['token']}",
                p.get("project_name") or "your project",
                reply_to=email_sender.proposal_reply_to(p["token"]), token=p["token"])
        except Exception as exc:  # noqa: BLE001 — they are on the list; the link can be re-sent
            log.error("could not send the portal link to a newly added contact: %s", exc)
    elif email not in existing:
        return _json({"ok": False, "error": "not_a_recipient"}, 404)

    if not db.set_followup_recipient(proposal_id, email, enabled):
        return _json({"ok": False, "error": "not_a_recipient"}, 404)
    return _json({"ok": True, "email": email, "followups": enabled})


@app.post("/api/admin/proposal/{proposal_id}/deposit-received")
def admin_deposit_received(proposal_id: str, request: Request) -> JSONResponse:
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    if not db.get_proposal(proposal_id):
        return _json({"ok": False, "error": "not_found"}, 404)
    db.set_deposit_status(proposal_id, "received")
    # Prompt the customer, in-thread, for the project contacts we now need.
    #
    # GUARDED, because the money is already recorded by the line above. Unguarded, a database
    # blip on this write returned a 500 from an endpoint that had ALREADY marked the deposit
    # received: the rep saw "Couldn't mark received" on an action that had half succeeded, and
    # if they took that at face value the customer was never asked for contacts and the project
    # stalled with nothing on screen to explain it. Same posture as the approval path's contacts
    # prompt above — the status change is the thing that matters, the message is a courtesy.
    try:
        db.add_message(proposal_id, "staff", None,
                       "Deposit received — thank you! Please add your project contacts so we "
                       "can schedule the work.", msg_type="system")
    except Exception as exc:  # noqa: BLE001 — the deposit is recorded; don't undo it over this
        log.warning("could not post the contacts prompt for %s: %s", proposal_id, exc)
    p = db.get_proposal(proposal_id) or {}
    project = p.get("project_name") or "your project"
    email_sender.notify_team(
        f"Deposit RECEIVED — {project}",
        f"<p>The deposit for <strong>{html.escape(project)}</strong> is marked received. "
        f"The customer has been asked for their project contacts.</p>",
        kind="deposit", reply_link=_staff_link(proposal_id), proposal_id=proposal_id,
        reply_to=email_sender.proposal_reply_to(p.get("token")),
        token=p.get("token"), project=p.get("project_name"),
    )
    _notify_customer(
        p, "Deposit received — thank you",
        f"<p>We've received your deposit for <strong>{html.escape(project)}</strong>.</p>"
        f"<p>Next: add your project contacts so we can schedule the work.</p>",
    )
    return _json({"ok": True})


@app.post("/api/admin/proposal/{proposal_id}/deposit-request")
async def admin_deposit_request(proposal_id: str, request: Request) -> JSONResponse:
    """Staff-triggered deposit invoice: mint/reuse the invoice number, post the
    invoice to the customer chat and email it with the PDF attached. Requires an
    approved proposal.

    Note: approval ALSO issues this automatically (automations.run_on_approval,
    Will's item 15). This endpoint stays for re-sends and for a staff-adjusted
    amount; both paths share automations.issue_deposit_invoice so they can't
    drift. The invoice NUMBER is issued once and reused on a re-send."""
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    p = db.get_proposal(proposal_id)
    if not p:
        return _json({"ok": False, "error": "not_found"}, 404)
    if p.get("proposal_status") != "approved":
        return _json({"ok": False, "error": "not_approved"}, 400)
    body = await _body(request)
    # Amount: explicit override wins; else the stored 25% auto-calc; else derive it.
    amount = None
    try:
        if body.get("amount") is not None:
            amount = round(float(body["amount"]), 2)
    except (TypeError, ValueError):
        return _json({"ok": False, "error": "invalid_amount"}, 400)
    if amount is not None and amount <= 0:
        return _json({"ok": False, "error": "invalid_amount"}, 400)   # no negative/zero deposit requests
    if amount is None:
        amount = (float(p["deposit_amount"]) if p.get("deposit_amount") is not None
                  else proposals.deposit_amount(p.get("approved_total")))

    if amount is None or amount <= 0:
        return _json({"ok": False, "error": "invalid_amount"}, 400)   # nothing to invoice

    # Whatever staff corrected on the review form. Capped and string-coerced here
    # so nothing odd reaches the document renderer.
    overrides = body.get("invoice")
    overrides = ({str(k): _cap(v, 300) for k, v in overrides.items()
                  if isinstance(k, str) and v is not None}
                 if isinstance(overrides, dict) else None)

    project = p.get("project_name") or "your proposal"
    try:
        # Staff resend = a genuinely new invoice that supersedes the last (Hanz's
        # call). The auto path on approval keeps reusing its number.
        result = automations.issue_deposit_invoice(p, project, amount, overrides=overrides,
                                                   new_number=bool(p.get("deposit_invoice_no")))
    except Exception as exc:  # noqa: BLE001
        log.error("manual deposit invoice failed for %s: %s", proposal_id, exc)
        return _json({"ok": False, "error": "invoice_failed"}, 500)
    return _json({"ok": True, **result})


# The /scheduled endpoint was removed on 2026-08-11. Hanz: "We need to remove the schedule
# status on the CRM and on the Customer portal Status", and when asked how far it should go he
# chose to take the notification with it. It used to set schedule_status, post a system message
# to the thread, tell the team, and email every recipient "Your project is scheduled".
#
# Treadwell books the date on the phone, so the customer already knows before any of that
# fires. The staff button, the board column and the customer's tile all went together: a status
# nobody sets is worse than no status, because the board would have shown every job stuck one
# step short of done forever.
#
# schedule_status, scheduled_at and db.set_schedule_status are all still here and untouched, so
# reinstating this is putting the route back rather than a migration.


# ── admin: configurable team-notification recipients (roster) ─────────────────
_MAX_NOTIFY_RECIPIENTS = 40


@app.get("/api/admin/notify-recipients")
def admin_notify_list(request: Request) -> JSONResponse:
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    return _json({"ok": True, "recipients": [
        {"id": r["id"], "email": r["email"], "kind": r["kind"],
         "enabled": bool(r.get("enabled", True)), "added_by": r.get("added_by")}
        for r in db.list_notify_recipients()]})


@app.post("/api/admin/notify-recipients")
async def admin_notify_add(request: Request) -> JSONResponse:
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    body = await _body(request)
    email = (body.get("email") or "").strip().lower()
    kind = (body.get("kind") or "general").strip().lower()
    if kind not in ("general", "deposit"):
        return _json({"ok": False, "error": "invalid_kind"}, 400)
    if len(email) > 254 or not _EMAIL_RE.match(email):
        return _json({"ok": False, "error": "invalid_email"}, 400)
    if len(db.list_notify_recipients()) >= _MAX_NOTIFY_RECIPIENTS:
        return _json({"ok": False, "error": "too_many"}, 400)
    # New recipients start OFF (gray) — added to the roster but not emailed until an
    # admin toggles them green. Adding someone must never silently start sending.
    db.add_notify_recipient(email, kind, _cap(body.get("by"), 120) or None, enabled=False)
    return _json({"ok": True})


@app.patch("/api/admin/notify-recipients/{rid}")
async def admin_notify_toggle(rid: int, request: Request) -> JSONResponse:
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    body = await _body(request)
    db.set_notify_recipient_enabled(rid, bool(body.get("enabled")))
    return _json({"ok": True})


@app.delete("/api/admin/notify-recipients/{rid}")
def admin_notify_delete(rid: int, request: Request) -> JSONResponse:
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    db.delete_notify_recipient(rid)
    return _json({"ok": True})


# ── admin: per-project notification overrides (add extra / mute someone) ──────
@app.get("/api/admin/notify-overrides")
def admin_notify_overrides_all(request: Request) -> JSONResponse:
    """Every per-project override at once — for the Notification Sending page's
    per-project view (avoids one request per project)."""
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    return _json({"ok": True, "overrides": db.list_all_notify_overrides()})


@app.get("/api/admin/proposal/{proposal_id}/notify-overrides")
def admin_notify_overrides_get(proposal_id: str, request: Request) -> JSONResponse:
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    if not db.get_proposal(proposal_id):
        return _json({"ok": False, "error": "not_found"}, 404)
    # Return the roster (enabled state) + this project's overrides so the drawer can
    # show each person's EFFECTIVE state without a second roster fetch.
    return _json({"ok": True,
                  "roster": [{"email": r["email"], "enabled": bool(r.get("enabled", True))}
                             for r in db.list_notify_recipients() if r["kind"] == "general"],
                  "overrides": db.list_notify_overrides(proposal_id)})


@app.put("/api/admin/proposal/{proposal_id}/notify-overrides")
async def admin_notify_overrides_set(proposal_id: str, request: Request) -> JSONResponse:
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    if not db.get_proposal(proposal_id):
        return _json({"ok": False, "error": "not_found"}, 404)
    body = await _body(request)
    email = (body.get("email") or "").strip().lower()
    mode = (body.get("mode") or "").strip().lower()
    if len(email) > 254 or not _EMAIL_RE.match(email):
        return _json({"ok": False, "error": "invalid_email"}, 400)
    if mode == "clear":
        db.clear_notify_override(proposal_id, email)
    elif mode in ("add", "mute"):
        db.set_notify_override(proposal_id, email, mode)
    else:
        return _json({"ok": False, "error": "invalid_mode"}, 400)
    return _json({"ok": True})


# Static assets — mounted last so /api, /, /p win.
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/{asset}")
def asset(asset: str):
    """Serve top-level static assets.

    `no-cache` means "revalidate every time", NOT "don't cache" — the browser
    still stores the file and the ETag turns each check into a cheap 304. Without
    it these responses carried only an ETag/Last-Modified, so browsers applied
    HEURISTIC freshness and kept running yesterday's app.js after a deploy (a
    customer saw the old deposit card with no invoice buttons). Correctness beats
    saving a few hundred bytes on a handful of tiny files."""
    f = FRONTEND_DIR / asset
    if f.is_file() and asset in {"styles.css", "app.js", "auth.js", "login.js",
                                 "projects.js", "shell.js", "favicon.ico"}:
        return FileResponse(f, headers=_NO_CACHE)
    return _json({"ok": False, "error": "not_found"}, 404)
