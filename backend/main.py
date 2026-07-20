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
from datetime import date
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
import inbound
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


def _staff_link(proposal_id: str) -> str:
    """Deep-link a staff notification email into the proposal in the staff tool
    (so staff answer in-portal rather than replying to the email)."""
    return f"{config.PROPOSAL_TOOL_PUBLIC_URL}/portal.html?open={proposal_id}"


def _proposal_card(row: dict) -> dict:
    return {
        "token": row["token"],
        "project_name": row.get("project_name") or "Your Proposal",
        "proposal_status": row.get("proposal_status"),
        "deposit_status": row.get("deposit_status"),
        "schedule_status": row.get("schedule_status"),
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


@app.get("/")
def root() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "login.html")


@app.get("/p/{token}")
def portal_page(token: str) -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


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
    data = db.get_draft_data(p["proposal_id"]) or {}
    db.mark_viewed(p["proposal_id"])
    p = db.get_proposal(p["proposal_id"])
    vm = proposals.build_view_model(p, data)
    vm["questions"] = [_q(q) for q in db.list_questions(p["proposal_id"])]   # text-only (legacy UI)
    vm["messages"] = [_msg(m) for m in db.list_messages(p["proposal_id"])]   # full chat thread (chat UI)
    vm["contacts"] = [_contact(c) for c in db.list_contacts(p["proposal_id"])]
    vm["check_address"] = config.CHECK_ADDRESS
    vm["payable_to"] = config.PAYABLE_TO
    _deps = db.list_deposits(p["proposal_id"])   # newest first
    _latest = _deps[0] if _deps else None
    vm["deposit"] = {
        "due": float(p["deposit_amount"]) if p.get("deposit_amount") is not None else None,
        "ref": proposals.deposit_ref(p["proposal_id"]),
        # No pre-configured Treadwell bank details are shown; the customer records
        # where they sent the transfer themselves (see the ACH form).
        # so the customer sees a "recorded" state on reload instead of a blank
        # form they might resubmit (deposit_status only flips when staff confirm).
        "submitted": bool(_latest),
        "submitted_method": _latest["method"] if _latest else None,
        "submitted_sent_date": (_latest["sent_date"].isoformat()
                                if _latest and _latest.get("sent_date") else None),
        "submitted_check_number": _latest.get("check_number") if _latest else None,
    }
    if config.PROPOSAL_TOOL_URL:   # official PDF available via on-demand render
        vm["has_pdf"] = True
    base["view"] = vm
    return _json(base)


def _q(row: dict) -> dict:
    return {"author_kind": row["author_kind"], "body": row["body"],
            "created_at": row["created_at"].isoformat() if row.get("created_at") else None}


def _msg(row: dict) -> dict:
    """A chat-thread message (any msg_type). Superset of _q with the id (for
    incremental polling), msg_type, and meta payload."""
    return {"id": row.get("id"), "author_kind": row["author_kind"], "body": row["body"],
            "msg_type": row.get("msg_type") or "text", "meta": row.get("meta"),
            "created_at": row["created_at"].isoformat() if row.get("created_at") else None}


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
    )
    return _json({"ok": True, "question": _q(row), "message": _msg(row)})


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
    msgs = [_msg(m) for m in db.list_messages(p["proposal_id"], after)]
    return _json({"ok": True, "messages": msgs, "status": {
        "proposal": p["proposal_status"], "deposit": p["deposit_status"],
        "contacts": p.get("contacts_status") or "pending", "schedule": p["schedule_status"]}})


@app.post("/api/portal/{token}/approve")
async def api_approve(token: str, request: Request) -> JSONResponse:
    p = _require(request, token)
    if not p:
        return _json({"ok": False, "error": "unauthorized"}, 401)
    body = await _body(request)
    name = _cap(body.get("name"), 120)
    title = _cap(body.get("title"), 120)
    if not name:
        return _json({"ok": False, "error": "Name is required."}, 400)

    data = db.get_draft_data(p["proposal_id"]) or {}
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

    email_sender.notify_team(
        f"Proposal APPROVED — {project_name}",
        f"<p><strong>{html.escape(name)}</strong>{(', ' + html.escape(title)) if title else ''} approved "
        f"<strong>{html.escape(option_summary)}</strong> at <strong>${total:,.2f}</strong> on {approved_date}"
        f"{(' (signed in as ' + html.escape(approver) + ')') if approver else ''}.</p>"
        f"<p>Auto-calculated deposit (25%): <strong>${deposit:,.2f}</strong>.</p>"
        f"<p>Project: {html.escape(project_name)}.</p>",
        reply_link=_staff_link(p["proposal_id"]), proposal_id=p["proposal_id"],
    )
    try:
        automations.run_on_approval(p, project_name)
    except Exception as exc:  # noqa: BLE001
        log.error("approval automations failed: %s", exc)
    return _json({"ok": True})


@app.post("/api/portal/{token}/deposit")
async def api_deposit(token: str, request: Request) -> JSONResponse:
    p = _require(request, token)
    if not p:
        return _json({"ok": False, "error": "unauthorized"}, 401)
    body = await _body(request)
    method = (body.get("method") or "").strip().lower()
    if method not in ("ach", "check"):
        return _json({"ok": False, "error": "Choose ACH or check."}, 400)
    account_name = _cap(body.get("account_name"), 120) or None
    bank_name = _cap(body.get("bank_name"), 120) or None
    note = _cap(body.get("note"), 1000) or None
    trace_ref = _cap(body.get("trace_ref"), 60) or None
    # Customer-recorded destination ("where you sent it"). This is Treadwell's own
    # receiving account (self-reported by the customer for reconciliation), not the
    # customer's account — the source account number is still never collected.
    sent_to_beneficiary = _cap(body.get("sent_to_beneficiary"), 120) or None
    sent_to_bank = _cap(body.get("sent_to_bank"), 120) or None
    sent_to_routing = _cap(body.get("sent_to_routing"), 40) or None
    sent_to_account = _cap(body.get("sent_to_account"), 40) or None
    # account_last4 is optional and only ever stored masked (never the full number).
    last4 = "".join(ch for ch in (body.get("account_last4") or "") if ch.isdigit())[-4:]
    masked_ref = f"••••{last4}" if last4 else None
    try:
        sent_date = date.fromisoformat(body["sent_date"]) if body.get("sent_date") else None
    except (ValueError, TypeError):
        sent_date = None
    # Pay-by-check: the check number off the mailed cheque. Collapse inner
    # whitespace (_cap only trims the ends) so it stays clean in the email + chat.
    check_number = _cap(" ".join(str(body.get("check_number") or "").split()), 40) or None

    db.add_deposit(p["proposal_id"], method, account_name, bank_name, masked_ref, note,
                   sent_date=sent_date, trace_ref=trace_ref,
                   sent_to_beneficiary=sent_to_beneficiary, sent_to_bank=sent_to_bank,
                   sent_to_routing=sent_to_routing, sent_to_account=sent_to_account,
                   check_number=check_number)
    project_name = p.get("project_name") or "proposal"
    ref = proposals.deposit_ref(p["proposal_id"])
    # A system line records it in the chat so both sides see the deposit is in flight.
    who = account_name or "The customer"
    action = (f"sent a bank transfer{f' on {sent_date}' if sent_date else ''}" if method == "ach"
              else f"is mailing a check{f' (#{check_number})' if check_number else ''}")
    db.add_message(p["proposal_id"], "staff", None,
                   f"Deposit initiated — {who} {action} (ref {ref}). We'll confirm once it clears.",
                   msg_type="system")
    if method == "ach":
        detail = (
            f"<p>From: {html.escape(account_name or '—')} · Bank: {html.escape(bank_name or '—')} · "
            f"Sent: {sent_date or '—'} · Trace: {html.escape(trace_ref or '—')}"
            f"{f' · Acct {masked_ref}' if masked_ref else ''}</p>"
            f"<p>Sent to (customer-recorded): {html.escape(sent_to_beneficiary or '—')} · "
            f"Bank: {html.escape(sent_to_bank or '—')} · Routing: {html.escape(sent_to_routing or '—')} · "
            f"Acct: {html.escape(sent_to_account or '—')}</p>"
        )
    else:   # check
        detail = (
            f"<p>Check #: {html.escape(check_number or '—')} · Name on check: {html.escape(account_name or '—')} · "
            f"Bank: {html.escape(bank_name or '—')} · Sent: {sent_date or '—'}</p>"
        )
    email_sender.notify_team(
        f"Deposit {'sent' if method == 'ach' else 'method'} — {project_name} ({ref})",
        f"<p>{'Bank transfer sent' if method == 'ach' else 'Paying by check'} for "
        f"<strong>{html.escape(project_name)}</strong> — match reference <strong>{html.escape(ref)}</strong> "
        f"on the statement.</p>"
        + detail
        + f"<p>Confirm it landed, then mark the deposit Received in the proposal tool.</p>",
        kind="deposit", reply_link=_staff_link(p["proposal_id"]), proposal_id=p["proposal_id"],
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
            r = httpx.get(
                config.PROPOSAL_TOOL_URL + "/api/admin/proposal-pdf",
                params={"draft_id": pid},
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
                                          reply_to=rt)
    elif kind == "reply":
        for e in recipients:
            email_sender.send_reply_notification(e, link, project, reply_to=rt)
    else:
        return _json({"ok": False, "error": "unknown_type"}, 400)
    return _json({"ok": True})


# ── inbound email (Resend receiving webhook) → CRM chat thread ─────────────────
@app.post("/api/inbound/resend")
async def api_inbound_resend(request: Request):
    """Resend `email.received` webhook. Auth = svix signature (no session/token).
    Flow: verify → match the proposal by the token in the recipient address →
    dedup → fetch the body from Resend → insert as a customer chat message →
    forward a copy + notify the team (best-effort). Non-2xx makes Svix retry, so
    only pre-insert failures return errors."""
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
    token = inbound.find_token(rcpts, config.RESEND_INBOUND_DOMAIN)
    p = (db.get_proposal_by_token(token) or db.get_proposal_by_token_ci(token)) if token else None
    if not p:
        log.info("inbound: no proposal match (token=%r)", token)
        return _json({"ok": True, "ignored": "no_match"})
    pid = p["proposal_id"]
    if db.has_email_message(pid, email_id):
        return _json({"ok": True, "ignored": "duplicate"})

    # Fetch the body (webhook is metadata-only). Failure → 500 so Svix retries;
    # nothing has been inserted yet, so the retry is safe.
    try:
        r = httpx.get(f"https://api.resend.com/emails/receiving/{email_id}",
                      headers={"Authorization": f"Bearer {config.RESEND_API_KEY}"}, timeout=10)
        r.raise_for_status()
        full = r.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("inbound: body fetch failed for %s: %s", email_id, exc)
        return _json({"ok": False, "error": "fetch_failed"}, 500)

    text = (full.get("text") or "")[:100_000]
    if not text.strip():
        html_body = (full.get("html") or "")[:300_000]
        text = html.unescape(re.sub(r"<[^>]{0,300}>", " ", html_body))
    body_txt = _cap(inbound.strip_quoted(text), 4000)
    names = [(_cap(a.get("filename"), 120) or "attachment")
             for a in (data.get("attachments") or [])[:10] if isinstance(a, dict)]
    if names:
        body_txt = (body_txt + "\n" + "\n".join(f"[Attachment: {n}]" for n in names)).strip()
    if not body_txt:
        body_txt = "(empty email)"

    from_email = (parseaddr(str(data.get("from") or ""))[1] or "").strip().lower()
    authorized = set(e.lower() for e in (db.get_recipients(pid) or []))
    authorized.add((p.get("customer_email") or "").strip().lower())
    verified = bool(from_email) and from_email in authorized
    project = p.get("project_name") or "proposal"
    subject = _cap(data.get("subject"), 200)

    if verified:
        # The insert is the idempotency anchor: after this line, retries dedup.
        db.add_message(pid, "customer", from_email, body_txt, msg_type="text",
                       meta={"source": "email", "email_id": email_id, "from": from_email})
    else:
        # Never let an unverified From speak as the customer in the thread —
        # staff still see it via the forward + team notification below.
        log.warning("inbound: unverified sender %r for proposal %s", from_email, pid)

    flag = "" if verified else "<p><strong>⚠ UNVERIFIED SENDER — not added to the portal thread.</strong></p>"
    esc_body = html.escape(body_txt).replace("\n", "<br>")
    fwd_html = (
        f"{flag}<p><strong>{html.escape(from_email or 'unknown')}</strong> replied by email on "
        f"<strong>{html.escape(project)}</strong>"
        f"{(' — ' + html.escape(subject)) if subject else ''}:</p>"
        f"<blockquote>{esc_body}</blockquote>"
    )
    link = _staff_link(pid)
    # ONE send, governed by the notification roster + this project's overrides — the
    # same switch as every other portal notification (no separate hardcoded list).
    # Sent via _send (not notify_team) so Reply-To stays the customer: a staff reply
    # from their own inbox reaches the customer. Empty/muted roster → nobody emailed.
    to = email_sender._resolve_notify("general", proposal_id=pid)
    try:
        if to:
            email_sender._send(
                to,
                f"Customer email reply — {project}",
                email_sender._wrap("Customer replied by email",
                                   fwd_html + f'<p><a href="{link}">Open in the proposal tool</a></p>'),
                reply_to=from_email or None)
        else:
            log.info("inbound: no notify recipients after roster/overrides for %s", pid)
    except Exception as exc:  # noqa: BLE001 — the CRM insert already happened
        log.error("inbound: forward failed: %s", exc)
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

    existing = db.get_proposal(draft_id)
    if existing:
        token = existing["token"]
        db.update_portal_proposal(draft_id, primary, name, project, pdf_path)
        _pdf_cache_drop(draft_id)   # a re-publish may have changed the document — don't serve a stale render
    else:
        token = ca.new_proposal_token()
        db.create_portal_proposal(draft_id, token, primary, name, project, pdf_path, by)
        # Seed the chat thread with the proposal card (first publish only).
        db.add_message(draft_id, "staff", None, "Your proposal is ready to review.",
                       msg_type="proposal_card")

    # Reconcile the recipient set.
    if recipients is None:                      # legacy call — preserve exact old semantics
        if existing:
            old = (existing.get("customer_email") or "").strip().lower()
            if old and old != primary:
                db.remove_recipient(draft_id, old)   # replaced primary loses access (as today)
        db.add_recipient(draft_id, primary, by)
        send_list = db.get_recipients(draft_id) or [primary]
    else:
        db.set_recipients(draft_id, recipients, by)  # revokes any extra dropped from the list
        send_list = recipients

    link = f"{config.PUBLIC_BASE_URL}/p/{token}"
    # One send per recipient (keeps _thread_headers per-recipient; recipients
    # never see each other's addresses). Only the primary gets the name greeting.
    rt = email_sender.proposal_reply_to(token)
    emailed = [e for e in send_list
               if email_sender.send_portal_link(e, name if e == primary else "", link, project, reply_to=rt)]
    return _json({"ok": True, "token": token, "url": link, "customer_email": primary,
                  "recipients": send_list, "emailed": emailed})


@app.get("/api/admin/pipeline")
def admin_pipeline(request: Request) -> JSONResponse:
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    unread = db.unread_counts()
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
            "unread": unread.get(r["proposal_id"], 0),   # customer messages awaiting a staff reply
        })
    return _json({"ok": True, "proposals": out})


@app.get("/api/admin/proposal/{proposal_id}")
def admin_proposal(proposal_id: str, request: Request) -> JSONResponse:
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    p = db.get_proposal(proposal_id)
    if not p:
        return _json({"ok": False, "error": "not_found"}, 404)
    appr = db.latest_approval(proposal_id)
    return _json({
        "ok": True,
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
            "recipients": db.get_recipients(proposal_id),
        },
        "contacts": [_contact(c) for c in db.list_contacts(proposal_id)],
        "approval": ({
            "name": appr["name"], "title": appr.get("title"),
            "date": appr["approved_date"].isoformat() if appr.get("approved_date") else None,
            "total": float(appr["total"]) if appr.get("total") is not None else None,
            "option": appr.get("option_label"), "options": appr.get("options"),
            "approver_email": appr.get("approver_email"),
        } if appr else None),
        "questions": [_q(q) for q in db.list_questions(proposal_id)],   # text-only (legacy drawer)
        "messages": [_msg(m) for m in db.list_messages(proposal_id)],   # full thread (revamped drawer)
        "deposit_ref": proposals.deposit_ref(proposal_id),
        "deposits": [{
            "method": d["method"], "account_name": d.get("account_name"), "bank_name": d.get("bank_name"),
            "masked_ref": d.get("masked_ref"), "note": d.get("note"),
            "sent_date": d["sent_date"].isoformat() if d.get("sent_date") else None,
            "trace_ref": d.get("trace_ref"),
            "sent_to_beneficiary": d.get("sent_to_beneficiary"), "sent_to_bank": d.get("sent_to_bank"),
            "sent_to_routing": d.get("sent_to_routing"), "sent_to_account": d.get("sent_to_account"),
            "check_number": d.get("check_number"),
            "submitted_at": d["submitted_at"].isoformat() if d.get("submitted_at") else None,
        } for d in db.list_deposits(proposal_id)],
    })


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
        email_sender.send_reply_notification(e, link, project, reply_to=rt)
    return _json({"ok": True})


@app.post("/api/admin/proposal/{proposal_id}/deposit-received")
def admin_deposit_received(proposal_id: str, request: Request) -> JSONResponse:
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    if not db.get_proposal(proposal_id):
        return _json({"ok": False, "error": "not_found"}, 404)
    db.set_deposit_status(proposal_id, "received")
    # Prompt the customer, in-thread, for the project contacts we now need.
    db.add_message(proposal_id, "staff", None,
                   "Deposit received — thank you! Please add your project contacts so we can schedule the work.",
                   msg_type="system")
    return _json({"ok": True})


@app.post("/api/admin/proposal/{proposal_id}/deposit-request")
async def admin_deposit_request(proposal_id: str, request: Request) -> JSONResponse:
    """Staff-triggered (NEVER auto-sent): after internal review, push a deposit
    request into the customer chat + email them. Requires an approved proposal."""
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

    msg = (f"Deposit requested: ${amount:,.2f}. Your deposit invoice will follow shortly."
           if amount is not None else "Deposit requested. Your deposit invoice will follow shortly.")
    db.add_message(proposal_id, "staff", None, msg, msg_type="deposit_request",
                   meta={"amount": amount} if amount is not None else None)
    db.set_deposit_requested(proposal_id)

    link = f"{config.PUBLIC_BASE_URL}/p/{p['token']}"
    project = p.get("project_name") or "your proposal"
    rt = email_sender.proposal_reply_to(p["token"])
    for e in (db.get_recipients(proposal_id) or [p["customer_email"]]):
        email_sender.send_deposit_request(e, link, project, amount, reply_to=rt)
    return _json({"ok": True, "amount": amount})


@app.post("/api/admin/proposal/{proposal_id}/scheduled")
def admin_scheduled(proposal_id: str, request: Request) -> JSONResponse:
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    if not db.get_proposal(proposal_id):
        return _json({"ok": False, "error": "not_found"}, 404)
    db.set_schedule_status(proposal_id, "scheduled")
    return _json({"ok": True})


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
    db.add_notify_recipient(email, kind, _cap(body.get("by"), 120) or None)
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
    """Serve top-level static assets."""
    f = FRONTEND_DIR / asset
    if f.is_file() and asset in {"styles.css", "app.js", "auth.js", "login.js", "favicon.ico"}:
        return FileResponse(f)
    return _json({"ok": False, "error": "not_found"}, 404)
