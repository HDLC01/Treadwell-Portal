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
import bank_crypto
import config
import customer_auth as ca
import db
import email_sender
import followup_draft
import followup_rules
import followup_settings
import followup_worker
import inbound
import invoice
import proposals
import ratelimit
import scope_groups
import signing
import uploads

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
    # One loud line at boot, never the key itself -- so "ACH stopped working" is a log grep,
    # not an SSH session. Bank details are still refused per-request even if this line is
    # somehow missed; this is a convenience, not the enforcement point (see api_deposit).
    if bank_crypto.key_configured():
        log.info("ACH bank-detail encryption ready (PORTAL_BANK_KEY configured)")
    else:
        log.warning("ACH is OFF: PORTAL_BANK_KEY is missing or invalid -- ACH deposits will be refused")
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
        # Always 'remind' in v1 (decisions 1-2): a due reminder tells the estimator, and only the
        # estimator sends. Travels so the drawer can say so instead of "automated".
        "mode": followup_rules.followup_mode(p),
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


# ── scope groups ──────────────────────────────────────────────────────────────
# RJ: a project with no single base bid. "Breakroom", "Lab 1" and "Lab 2" are each their own
# proposal (own document, own approval, own deposit), linked by a shared group id so the customer
# list, the board, one combined email and the follow-up worker treat them as one project.
#
# THE SHAPE the proposal tool writes into the draft blob, and the only one read here:
#     data["scope_group"] = {"id": "<group id>", "label": "<this scope's name>"}
# `id` is shared by every scope in the group (a uuid is the obvious choice); `label` is what the
# customer sees for THIS scope ("Lab 1"). Read from the draft at publish, never from the request.
_SCOPE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SCOPE_LABEL_MAX = 80


def _scope_of(data: dict) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """`(group_id, label, error)` from a draft blob. All None for an ungrouped draft.

    A PRESENT BUT MALFORMED value is an error, not "ungrouped". Treating it as ungrouped would
    publish this scope as a normal proposal and email it to the customer on its own, which is the
    exact thing the group exists to prevent."""
    raw = (data or {}).get("scope_group")
    if raw is None:
        return None, None, None
    if not isinstance(raw, dict):
        return None, None, "invalid_scope_group"
    gid, label = raw.get("id"), raw.get("label")
    if not isinstance(gid, str) or not _SCOPE_ID_RE.match(gid):
        return None, None, "invalid_scope_group"
    if label is not None and not isinstance(label, str):
        return None, None, "invalid_scope_group"
    return gid, (_cap(label, _SCOPE_LABEL_MAX) or None), None


def _scope_name(project: Optional[str], label: Optional[str]) -> str:
    """"Acme HQ — Lab 1" for a scope, the plain project name for anything else."""
    project = project or "Your Proposal"
    return f"{project} — {label}" if label else project


class _GroupUnreadable(Exception):
    """The group's membership could not be read. Callers FAIL CLOSED on this: send nothing,
    because the fallback they would otherwise take is the one-scope email the group forbids."""


def _active_scope_group(p: dict) -> Optional[str]:
    """The group `p` is a live scope of, or None when it is an ordinary proposal.

    THE DRAFTS DECIDE MEMBERSHIP, as in the combined send: a scope the tool has taken back out of
    its group keeps a stale scope_group on its portal row, and is an ordinary proposal again. With
    no live draft in the group at all there is nothing to compare against, and the row stands.
    A PARKED scope (archived, or a Test draft in a real project: scope_groups.split_members) is
    still in the group, so it is never emailed on its own either.
    Raises _GroupUnreadable when the drafts cannot be read."""
    group = p.get("scope_group")
    if not group:
        return None
    try:
        live, parked = scope_groups.split_members(db.list_scope_group_draft_states(group))
    except Exception as exc:  # noqa: BLE001
        raise _GroupUnreadable(str(exc)) from exc
    return group if (not (live or parked) or p.get("proposal_id") in live + parked) else None


def _send_scope_links_to_contact(p: dict, group: str, email: str) -> bool:
    """A contact added to one scope AFTER the combined send: the group's email, listing every
    scope of the group this address is on and that has already gone out. Never the one-scope
    "your proposal is ready", which is the email the group exists to replace.

    `p` itself is always listed -- staff just put this person on it. Closed-lost scopes, scopes
    taken out of the group and scopes not yet sent are left out, the same as the combined send.
    Replies land in `p`'s thread, the one they were added to."""
    expected = db.list_scope_group_drafts(group)
    if expected and p["proposal_id"] not in expected:
        # A PARKED scope (archived, or a Test draft in a real project): it is in the group, so it
        # never gets the one-scope email, and it is not a live member, so it gets no group email.
        log.info("%s is a parked scope of %s: no email to the new contact", p["proposal_id"], group)
        return False
    mine = []
    for r in db.list_scope_group(group):
        pid = r["proposal_id"]
        if pid != p["proposal_id"]:
            if expected and pid not in expected:
                continue
            if (r.get("proposal_status") or "") == "closed_lost" or not r.get("last_sent_at"):
                continue
            on = [(e or "").strip().lower()
                  for e in (db.get_recipients(pid) or [r.get("customer_email")])]
            if email not in on:
                continue
        mine.append(r)
    if not any(r["proposal_id"] == p["proposal_id"] for r in mine):
        mine.append(p)
    return email_sender.send_scope_group_links(
        email, "", p.get("project_name") or "your project",
        [{"label": r.get("scope_label") or r.get("project_name") or "Proposal",
          "url": f"{config.PUBLIC_BASE_URL}/p/{r['token']}",
          "approved": (r.get("proposal_status") or "") == "approved"} for r in mine],
        reply_to=email_sender.proposal_reply_to(p["token"]), token=p["token"])


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
                     peer_body_html: Optional[str] = None,
                     attachments: Optional[list[tuple[str, bytes]]] = None) -> None:
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
            # The attachment goes to BOTH versions. A signed contract belongs to every
            # contact on the project, not only to whoever happened to click the button --
            # the peer copy differs in its wording, never in what it encloses.
            if peer_body_html and not is_actor:
                email_sender.send_customer_update(e, link, project,
                                                  peer_heading or heading, peer_body_html,
                                                  reply_to=rt, token=p["token"],
                                                  attachments=attachments)
            else:
                email_sender.send_customer_update(e, link, project, heading, body_html,
                                                  reply_to=rt, token=p["token"],
                                                  attachments=attachments)
    except Exception as exc:  # noqa: BLE001
        log.warning("customer update email failed for %s: %s", p.get("proposal_id"), exc)


def _staff_link(proposal_id: str) -> str:
    """Deep-link a staff notification email into the proposal in the staff tool
    (so staff answer in-portal rather than replying to the email)."""
    return f"{config.PROPOSAL_TOOL_PUBLIC_URL}/portal.html?open={proposal_id}"


def _proposal_card(row: dict) -> dict:
    """One row in the customer's project list (login page + in-portal switcher)."""
    created = row.get("created_at")
    group = row.get("scope_group") or None
    label = (row.get("scope_label") or None) if group else None
    return {
        "token": row["token"],
        "project_name": row.get("project_name") or "Your Proposal",
        # Three scopes of one project would otherwise read as three identical "Acme HQ" rows.
        # `project_name` stays the raw name; `display_name` is what a list should print.
        "scope_group": group,
        "scope_label": label,
        "display_name": _scope_name(row.get("project_name"), label),
        "proposal_status": row.get("proposal_status"),
        "deposit_status": row.get("deposit_status"),
        "schedule_status": row.get("schedule_status"),
        "contacts_status": row.get("contacts_status") or "pending",
        "created_at": created.isoformat() if hasattr(created, "isoformat") else created,
        # So the card doesn't say "Deposit due" on a job that never needs one.
        "deposit_required": row.get("deposit_required") is not False,
        "deposit_requested": bool(row.get("deposit_requested_at")),
    }


def _group_cards(cards: list[dict]) -> list[dict]:
    """Keep a scope group's cards together, in the order its scopes were published.

    The list arrives newest first. A group takes the place of its newest scope and the rest of its
    scopes follow it, oldest first, so "Breakroom, Lab 1, Lab 2" reads in the order the estimator
    split them. Ungrouped cards do not move."""
    members: dict[str, list[dict]] = {}
    for c in cards:
        if c.get("scope_group"):
            members.setdefault(c["scope_group"], []).append(c)
    out, placed = [], set()
    for c in cards:
        g = c.get("scope_group")
        if not g:
            out.append(c)
        elif g not in placed:
            placed.add(g)
            out.extend(sorted(members[g], key=lambda m: (m.get("created_at") or "",
                                                         m.get("scope_label") or "")))
    return out


def _customer_cards(email: str) -> list[dict]:
    """The signed-in customer's project list. ONE builder for the three routes that return it,
    and always off list_proposals_by_email, which is what keeps another customer's scope of the
    same group out of it."""
    return _group_cards([_proposal_card(r) for r in db.list_proposals_by_email(email)])


def _scope_view(p: dict, session_email: str) -> Optional[dict]:
    """This proposal's scope and its sibling scopes, for the customer's proposal page.

    THE SIBLINGS COME FROM THE SESSION'S OWN LIST (list_proposals_by_email), filtered to this
    group -- never from db.list_scope_group, which is the whole group regardless of who each scope
    was sent to. A scope addressed to somebody else must not appear here even by name."""
    group = p.get("scope_group")
    if not group:
        return None
    sibs = [c for c in _customer_cards(session_email) if c.get("scope_group") == group]
    for c in sibs:
        c["current"] = c["token"] == p["token"]
    return {"group": group, "label": p.get("scope_label") or None, "siblings": sibs}


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
    resp = _json({"ok": True, "proposals": _customer_cards(email)})
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
    resp = _json({"ok": True, "proposals": _customer_cards(email)})
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


# NAMES, not glyphs. The bell row and the toast both escape whatever this returns
# (shell.js `esc()`), so markup could never have travelled this way even if we wanted it to —
# and Hanz asked for a real icon set rather than emoji. So the API names the icon and the client
# resolves the name against its own ICONS map, falling back to a dot for anything it does not
# know. Adding a kind here without adding the name there degrades to that dot; it cannot break
# the row.
_EVENT_ICONS = {"text": "message", "deposit_request": "receipt", "system": "bell"}


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
        "icon": _EVENT_ICONS.get(kind, "dot"),
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
            kind="feedback",
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
                  "proposals": _customer_cards(se)})


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
    # THIS ROUTE MARKS NOTHING. Signing in and landing in chat is not reading the proposal, and
    # marking here made "Viewed" mean "their browser fetched the portal". Two consequences, both
    # reported by Hanz on 2026-08-13:
    #   • "it should only move to viewed if they actually click view the proposal … They need
    #     first to open the Status page under the Proposal Step."
    #   • "I have resent the proposal again but it didnt move back to sent?" — publish DOES reset
    #     viewed→sent (db.reset_for_revision), but any open portal tab erased it within 12s: the
    #     poll sees revision_no change, refetches this endpoint, and the row was marked viewed
    #     again with nobody having read anything. That phantom also re-stamped cycle_viewed_at,
    #     putting the follow-up cadence on the "they opened it" track off a view that never was.
    # The signal now comes from POST /api/portal/{token}/viewed, which the page fires when the
    # proposal step is actually shown. See api_portal_viewed.
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
    # The LATEST send, so a fresh revision doesn't inherit the original send's age — this drives
    # the customer's "not moving forward?" card, which only offers itself after a few quiet days.
    vm["sent_at"] = _iso(p.get("last_sent_at") or p.get("created_at"))
    # A scope of a multi-scope project: its own name ("Lab 1"), which the page shows where an
    # ordinary proposal says "Base bid", and the other scopes THIS customer can see. None for
    # every ungrouped proposal, which the page reads as "render exactly as before".
    try:
        vm["scope"] = _scope_view(p, se)
    except Exception as exc:  # noqa: BLE001 — the sibling strip is not worth the whole page
        log.warning("scope siblings unavailable for %s: %s", p["proposal_id"], exc)
        vm["scope"] = ({"group": p["scope_group"], "label": p.get("scope_label") or None,
                        "siblings": []} if p.get("scope_group") else None)
    # What the customer writes on a cheque or a transfer for THIS proposal's deposit: the project
    # name, plus the scope for one scope of a multi-scope project ("Acme HQ - Lab 1", finding #21).
    # The same helper names the invoice, the contract and the approval emails.
    vm["payment_reference"] = scope_groups.display_project_name(
        p, vm.get("project_name") or p.get("project_name"))
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
        # `attachments` joins the whitelist because a customer has to be able to SEE the photo
        # they or the estimator attached. It is safe to pass through for the reason the whitelist
        # exists at all: unlike `meta.from`, every field in an attachment record was rebuilt by
        # uploads.sanitize when the message was written, never taken from the sender.
        "meta": {k: meta[k] for k in ("source", "revision_no", "superseded", "superseded_by",
                                     "amount", "invoice_no", "reference", "attachments")
                 if k in meta},
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
    }


async def _store_upload(request: Request, proposal_id: str) -> JSONResponse:
    """The body of both upload routes. Identical for customer and staff on purpose: the same caps,
    the same allow-list and the same refusal wording, so the two sides cannot drift apart."""
    blob = await request.body()
    if len(blob) > uploads.MAX_BYTES:
        return _json({"ok": False, "error": "that file is larger than 15 MB"}, 413)
    name = request.query_params.get("name")
    mime = (request.headers.get("content-type") or "").split(";")[0].strip()
    try:
        rec = uploads.store(proposal_id, name, mime, blob)
    except ValueError as e:
        return _json({"ok": False, "error": str(e)}, 400)
    except OSError:
        log.exception("attachment write failed for %s", proposal_id)
        return _json({"ok": False, "error": "could not save that file"}, 500)
    return _json({"ok": True, "file": {k: rec[k] for k in ("id", "name", "mime", "size", "image")}})


def _serve_upload(proposal_id: str, file_id: str, internal: bool = False):
    """Serve one attachment.

    THE THREAD IS THE ACCESS LIST. A file is served only when it appears in `meta.attachments` on a
    message of this proposal, and the name and content type come from THAT record rather than from
    the request or from the path on disk. So an id that was uploaded and never sent is not
    fetchable, and a caller cannot choose what a file claims to be on its way out.

    THE CALLER SAYS WHETHER STAFF-ONLY MESSAGES COUNT, and the two sides answer differently.
    This served both from one list that opted into `internal`, which would have let a customer
    fetch a file hanging off a staff-only card — "Kevin opened the proposal", the CRM's own closed
    -lost note — on nothing but a uuid. Guessing one is not realistic, and that is not the point:
    the rule about who may read an internal row should be the same rule everywhere, not relaxed in
    the one place that happens to be about bytes.

    (test_only_the_staff_reader_opts_in is what found it. It counts the opted-in readers in this
    file and insists there is exactly one — the staff drawer — which is precisely the guard a
    second reader should have to argue with.)
    """
    rec = None
    for m in db.list_messages(proposal_id, include_internal=internal):
        rec = uploads.pick(m.get("meta"), file_id)
        if rec:
            break
    if not rec:
        return _json({"ok": False, "error": "not_found"}, 404)
    path = uploads.path_of(proposal_id, file_id)
    if not path:
        return _json({"ok": False, "error": "not_found"}, 404)
    # `inline` for a picture so it opens in the tab; `attachment` for everything else so a .docx
    # downloads under its real name instead of being rendered as gibberish.
    disp = "inline" if uploads.is_image(rec.get("mime")) else "attachment"
    return FileResponse(path, media_type=rec.get("mime") or "application/octet-stream",
                        filename=rec.get("name") or "attachment",
                        content_disposition_type=disp,
                        # BELT AND BRACES, on a response whose body a customer supplied.
                        #
                        # nosniff: the media type here is the one uploads.verify proved, so no
                        # browser should be second-guessing it. Content sniffing is precisely how
                        # a file that is "only" a picture gets treated as something else.
                        #
                        # The CSP is what makes a mistake anywhere upstream survivable: even if
                        # something did render, `default-src 'none'` plus `sandbox` leaves it no
                        # script, no network, no form, and no same-origin privileges — and these
                        # files are served from the origin the customer's session cookie lives on.
                        headers={"X-Content-Type-Options": "nosniff",
                                 "Content-Security-Policy":
                                     "default-src 'none'; sandbox; frame-ancestors 'none'",
                                 "Referrer-Policy": "no-referrer"})


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
    body = await _body(request)
    text = _cap(body.get("body"), 4000)
    # `attachments` IS IGNORED HERE, deliberately and permanently. There is no customer upload
    # route (see the note above the fetch route), so any ids in this body name files this customer
    # did not put there — either an estimator's, or nothing at all. Reading them would let a
    # customer re-attach somebody else's file to their own message and, worse, would be the one
    # place the "staff only" rule could be talked out of later.
    if not text:
        return _json({"ok": False, "error": "empty"}, 400)
    who = _session_email(request)
    row = db.add_message(p["proposal_id"], "customer", who, text, msg_type="text")
    email_sender.notify_team(
        f"New proposal question — {p.get('project_name')}",
        f"<p><strong>{html.escape(who or '')}</strong> asked a question on "
        f"<strong>{html.escape(p.get('project_name') or '')}</strong>:</p>"
        f"<blockquote>{html.escape(text)}</blockquote>",
        kind="question",
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


# THERE IS NO CUSTOMER UPLOAD ROUTE, and its absence is the security answer rather than an
# omission. Hanz, 2026-08-26: "I think we apply the sending of the file attachments only to the
# treadwell side."
#
# Every defence in uploads.py reduces the risk of accepting a stranger's file; none of them
# removes it, because a genuinely valid PDF carrying a malicious payload is still a genuinely
# valid PDF and the only real answer to that is a scanner this VPS cannot host. Not letting an
# unknown party write to our disk at all closes the question instead of narrowing it.
#
# The customer keeps the half that matters to them: they can read and download whatever the
# estimator attached. Their own side of the thread is text.


@app.get("/api/portal/{token}/file/{file_id}")
async def api_portal_file(token: str, file_id: str, request: Request):
    p = _require(request, token)
    if not p:
        return _json({"ok": False, "error": "unauthorized"}, 401)
    # Customer side: staff-only messages are not theirs, and neither are the files on them.
    return _serve_upload(p["proposal_id"], file_id)


@app.post("/api/portal/{token}/viewed")
async def api_portal_viewed(token: str, request: Request) -> JSONResponse:
    """The customer opened the PROPOSAL STEP — the page with the status card and the document.

    This is the only thing that marks a proposal viewed. It used to happen on any authenticated
    GET of the portal, which meant signing in and reading the chat counted, and — worse — an open
    tab's 12-second poll re-marked the row seconds after a re-send had deliberately reset it to
    'sent'. Hanz, 2026-08-13: "it should only move to viewed if they actually click view the
    proposal in the chatbox inside the portal. Not by clicking the portal link only."

    THE REVISION GUARD IS LOAD-BEARING. `reset_for_revision` puts the row back to 'sent' on a
    re-send, and `mark_viewed` stamps `cycle_viewed_at` precisely when the status is 'sent' — so a
    tab still showing the PREVIOUS revision, posting this on a hash change, would recreate the same
    phantom view in a new shape and restart the follow-up cadence on the wrong track. A tab may
    only mark the revision it is actually displaying.

    A mismatch is a 200, not an error: the client latches on the response, and a stale tab must
    stop asking rather than retry forever against a revision it will never hold. It re-fires
    correctly once its poll delivers the new revision."""
    p = _require(request, token)
    if not p:
        return _json({"ok": False, "error": "unauthorized"}, 401)
    raw = (await _body(request)).get("revision_no")
    def _rev(v):
        # Legacy rows have no revision at all; None is a real value here, not "missing".
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None
    if _rev(raw) != _rev(p.get("current_revision_no")):
        return _json({"ok": True, "marked": False})
    try:
        # NOT self-guarded, unlike record_view — and it used to run inside the customer's main GET,
        # where a database blip took the whole proposal page down with it.
        #
        # The return value is the sent -> viewed TRANSITION, which is what makes the alerting below
        # an event instead of a stream: this endpoint fires on a hash change and a left-open tab
        # repeats it, and `last_viewed_at` moves every time.
        first_view = db.mark_viewed(p["proposal_id"])
    except Exception as exc:  # noqa: BLE001
        log.warning("mark_viewed failed for %s: %s", p["proposal_id"], exc)
        return _json({"ok": True, "marked": False})
    # WHICH recipient opened it — the staff-side question, separate from the shared status. The
    # session decides, never the body: the email is what the drawer prints beside "Viewed".
    who = _session_email(request)
    db.record_view(p["proposal_id"], who)
    if first_view:
        _alert_first_view(p, who)
    return _json({"ok": True, "marked": True})


def _alert_first_view(p: dict, who: Optional[str]) -> None:
    """Tell staff, once per send, that the customer has opened the proposal.

    Hanz wanted the opening to reach people rather than only sit on the board. The notification
    bell already reports it; this adds the two places staff actually live — the project's thread
    and their inbox.

    CALLED ONLY ON THE TRANSITION, which is the whole design. A customer reading a proposal
    properly opens it, scrolls, switches tabs and comes back, and the endpoint fires each time;
    without the guard the estimator's inbox gets one email per poll and learns to filter the
    lot. "They have opened it" is news exactly once per send — and a RE-send makes it news again,
    because reset_for_revision puts the row back to 'sent' and the transition is available a
    second time. That is correct rather than incidental: whether the customer looked at the
    revision is a different question from whether they looked at what it replaced.

    BOTH SIDES ARE BEST-EFFORT, and that is not defensiveness — it is the same rule the
    mark_viewed call above already follows. This runs inside the customer's own request; a
    Resend outage or a missing table must cost an estimator a notification, never cost the
    customer the page they are trying to read. Each is guarded separately so a broken thread
    write cannot swallow the email as well."""
    project = p.get("project_name") or "your proposal"
    # First name where the address gives us one, then the address, then a neutral noun. The card
    # goes in the SHARED thread, so the fallback has to be a sentence we would be happy for the
    # customer to read too — "opened the proposal" with nobody named beats "None opened".
    #
    # Stripped BEFORE _first_name_of, not after: that helper capitalises the first character of
    # whatever it is handed, so a whitespace-only session email came back as whitespace, which is
    # truthy, and the card was named after a space.
    who_clean = (who or "").strip()
    name = _first_name_of(who_clean) or who_clean or "The customer"
    try:
        # msg_type="system" renders as a card in both frontends already (app.js, portal.js) and
        # sits inside the existing CHECK constraint, so this needs no migration. `meta.view` is
        # what keeps it out of the customer's notification bell — see db.list_customer_events.
        # `internal` is what keeps this out of the CUSTOMER's copy of the shared thread — see
        # db.list_messages, which excludes it by default and is opted out of only by the two staff
        # readers. `view` stays alongside it so list_customer_events keeps its own bell rule and so
        # the row is still identifiable as a view rather than as any internal card.
        db.add_message(p["proposal_id"], "staff", None, f"{name} opened the proposal.",
                       msg_type="system", meta={"view": True, "internal": True})
    except Exception as exc:  # noqa: BLE001
        log.warning("could not post the view card for %s: %s", p["proposal_id"], exc)
    try:
        # The SAME kwargs the send confirmation passes, so this inherits the whole resolution
        # rather than growing a second opinion about who hears from a project: the enabled
        # roster, this project's adds and mutes, the assigned estimator folded in, mutes winning.
        # Naming no address in the body, for the reason recorded on the send confirmation.
        email_sender.notify_team(
            f"Proposal opened — {project}",
            f"<p><strong>{html.escape(name)}</strong> opened the proposal for "
            f"<strong>{html.escape(project)}</strong>.</p>",
            kind="viewed",
            reply_link=_staff_link(p["proposal_id"]), proposal_id=p["proposal_id"],
            reply_to=email_sender.proposal_reply_to(p.get("token")),
            token=p.get("token"), project=p.get("project_name"),
            assigned_estimator=p.get("assigned_estimator"),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("view notification failed for %s: %s", p["proposal_id"], exc)


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

    # ── the approval IS the signature ────────────────────────────────────────
    # CHECKED BEFORE ANYTHING IS WRITTEN, which is the whole placement. An approval is the
    # moment a customer reads "approved" and believes a contract now exists, so every reason
    # to refuse one has to be spent before the first insert. See signing.py.
    #
    # `is not True` rather than a truthiness test. ESIGN turns on an AFFIRMATIVE act of
    # assent, and consent:"false", consent:0 and consent:"maybe" are not one -- only the
    # boolean the checkbox emits is.
    #
    # A BUDGET PRICING PROPOSAL IS EXEMPT rather than blocked here. Its template carries no
    # Terms and Conditions, so there is nothing to be bound by and no honest consent sentence
    # to show; requiring one would lock those customers out of approving at all. The page
    # steers them to talk to us instead (signing_block.blocked_reason), and no certificate is
    # ever produced claiming T&Cs they were never shown.
    sign = signing.signing_block(p, data)
    if sign["required"] and body.get("consent") is not True:
        return _json({"ok": False, "error":
                      "Please tick the box to sign and approve this proposal."}, 400)

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
    signed_at = _now_utc()
    revision_no = p.get("current_revision_no")
    # WHAT WAS SIGNED, pinned by hash, and computed BEFORE the row is written so the row can
    # carry them. A fingerprint taken afterwards is a fingerprint of whatever the document had
    # become by then, which is the one thing a signature exists to rule out.
    #
    # Both are None for a Budget Pricing approval: hashes with no certificate attesting to
    # them would read as evidence of a signature that was never taken.
    revision_sha = signing.revision_sha256(data) if sign["required"] else None
    # 30 seconds, not the viewer's 90. This runs inside the customer's own click, and a cold
    # render that misses the deadline costs a NULL hash and a rebuild on first download --
    # never a stalled approval.
    proposal_pdf = _proposal_pdf_bytes(p, timeout=30.0) if sign["required"] else None
    if sign["required"] and proposal_pdf is None:
        log.warning("approval for %s has no proposal PDF to hash (upstream unavailable) -- "
                    "the signed contract will be built on first download", p["proposal_id"])
    approval_id = db.add_approval(
        p["proposal_id"], name, title, approved_date, total, option_summary,
        _client_ip(request), approver, options=label_list,
        consent_version=(signing.CONSENT_VERSION if sign["required"] else None),
        # Capped: a User-Agent is attacker-controlled free text on a legal record.
        user_agent=_cap(request.headers.get("user-agent"), 500) or None,
        revision_no=int(revision_no) if revision_no else None,
        revision_sha256=revision_sha,
        contract_sha256=signing.sha256_hex(proposal_pdf) if proposal_pdf else None,
    )
    db.set_approved(p["proposal_id"], total, option_summary, name, title, approved_date,
                    options=label_list, deposit_amount=deposit)

    project_name = p.get("project_name") or "proposal"
    # "Acme HQ - Lab 1" for one scope of a multi-scope project, in every approval email (finding
    # #21): three approvals of one project must say which scope each one is. project_name itself
    # stays the project's, for the staff thread key and the approval automations.
    shown_name = scope_groups.display_project_name(p) or project_name
    # A system line in the chat thread records the approval for both sides.
    sel_txt = "; ".join(f"{o['label']} (${o['total']:,.2f})" for o in chosen)
    db.add_message(p["proposal_id"], "staff", None,
                   f"Approved by {name} — {sel_txt}. Total ${total:,.2f}.", msg_type="system")
    # One scope of a multi-scope project: the other scopes stay open (RJ), and the estimator
    # hears that they do. "" for every ungrouped proposal, which leaves the email below
    # byte-for-byte what it was.
    scope_note_html = _notice_scope_siblings(p)
    # ...and their chase stops, with ONE task asking staff what to do about them (never closed).
    _raise_scope_decision(dict(p, proposal_status="approved", approved_at=signed_at))

    # Staff decided at send time whether this job collects a deposit. When it
    # doesn't, every mention of one has to go — promising an invoice that will
    # never arrive is worse than saying nothing.
    # ── the signed contract ──────────────────────────────────────────────────
    # BEST-EFFORT, AND EVERYTHING ABOVE IS ALREADY COMMITTED. This calls another container to
    # assemble a PDF; letting that decide whether an approval happened is how a customer ends
    # up pressing Approve three times because a LibreOffice pass was slow.
    #
    # A FAILURE IS NEVER SILENT. It costs the attachment and both emails SAY SO -- an
    # estimator must not read "APPROVED" and assume the paperwork exists. That is the exact
    # shape of the publish bug that sent a customer an email with nothing attached while the
    # estimator read "Sent".
    contract_pdf, contract_error = None, None
    if sign["required"]:
        certificate = signing.build_certificate(
            project_name=signing.signing_project_name(p, data),
            proposal_id=p["proposal_id"], revision_no=revision_no,
            signer_name=name, signer_title=title, signer_email=approver,
            signed_at=signed_at, ip_address=_client_ip(request),
            user_agent=_cap(request.headers.get("user-agent"), 500),
            options_summary=option_summary, total=total,
            deposit_amount=(deposit if p.get("deposit_required") is not False else None),
            proposal_pdf_sha256=signing.sha256_hex(proposal_pdf) if proposal_pdf else "",
            revision_sha=revision_sha or "",
            consent_version=signing.CONSENT_VERSION)
        contract_pdf, contract_error = _store_signed_contract(
            p["proposal_id"], approval_id, certificate, proposal_pdf)
    staff_contract_html, customer_contract_html, contract_atts = _contract_email_parts(
        signing.signing_project_name(p, data), contract_pdf, contract_error,
        signing_required=sign["required"])
    # THE SAME BYTES, UNDER THE SAME NAME, IN THE ONE PLACE BOTH SIDES ALREADY LOOK. Hanz,
    # 2026-09-21: "show the pdf as an attachment in chat that we can download". The emails
    # carry it and the portal has a download button, but an email gets deleted and a button
    # lives on one screen; the thread is where the estimator and the customer both go back to.
    #
    # GUARDED ON contract_atts RATHER THAN ON contract_pdf, so the filename can only ever have
    # ONE spelling: if the emails are carrying a document, the thread carries THAT document
    # named identically, and a customer never sees two names for one file. It also folds the
    # two cases that must post nothing into one condition -- a build that failed (both emails
    # say so instead, and the download rebuilds it) and a Budget Pricing approval, which has no
    # contract to attach because it has no Terms and Conditions to sign.
    if contract_atts:
        _post_contract_to_thread(p["proposal_id"], contract_atts[0][0], contract_atts[0][1])

    deposit_due = p.get("deposit_required") is not False
    email_sender.notify_team(
        f"Proposal APPROVED — {shown_name}",
        f"<p><strong>{html.escape(name)}</strong>{(', ' + html.escape(title)) if title else ''} approved "
        f"<strong>{html.escape(option_summary)}</strong> at <strong>${total:,.2f}</strong> on {approved_date}"
        f"{(' (signed in as ' + html.escape(approver) + ')') if approver else ''}.</p>"
        + (f"<p>Auto-calculated deposit (25%): <strong>${deposit:,.2f}</strong>.</p>" if deposit_due
           else "<p>No deposit required for this project — the customer has been asked for "
                "their project contacts.</p>")
        + f"<p>Project: {html.escape(shown_name)}.</p>"
        + scope_note_html
        + staff_contract_html,
        kind="approved",
        reply_link=_staff_link(p["proposal_id"]), proposal_id=p["proposal_id"],
        reply_to=email_sender.proposal_reply_to(p.get("token")),
        token=p.get("token"), project=p.get("project_name"),
        attachments=contract_atts,
    )
    # Confirm the approval to the customer in writing. They'd just committed to a
    # price and heard nothing back except (later) an invoice.
    _notify_customer(
        p, "Thank you — your proposal is approved",
        f"<p>We've recorded your approval of <strong>{html.escape(shown_name)}</strong>"
        f"{(' by ' + html.escape(name)) if name else ''} on {approved_date}.</p>"
        f"<p>Approved: <strong>{html.escape(option_summary)}</strong> — "
        f"<strong>${total:,.2f}</strong>.</p>"
        + (f"<p>A deposit of <strong>${deposit:,.2f}</strong> (25%) reserves your place on our "
           f"schedule; the invoice follows separately.</p>" if deposit_due
           else "<p>No deposit is needed. Next, please add your project contacts so we can "
                "schedule the work.</p>")
        # ESIGN 7001(d)/(e): the signer has to be ABLE TO RETAIN their own copy. Before this,
        # nobody could -- an approval left a database row and an email naming a total. This
        # line and its attachment are that requirement, and the portal download is the other
        # half (api_signed_contract_pdf).
        + customer_contract_html,
        # The other contacts hear WHO approved rather than "your approval". Named from the typed
        # signature, which every recipient already sees on the proposal itself, falling back to
        # the first name of whoever was signed in.
        actor_email=approver,
        peer_heading="This proposal has been approved",
        peer_body_html=(
            f"<p><strong>{html.escape(name or _first_name_of(approver) or 'Someone on your team')}"
            f"</strong> approved <strong>{html.escape(shown_name)}</strong> on {approved_date}.</p>"
            f"<p>Approved: <strong>{html.escape(option_summary)}</strong> — "
            f"<strong>${total:,.2f}</strong>.</p>"
            + (f"<p>A deposit of <strong>${deposit:,.2f}</strong> (25%) reserves the schedule; "
               f"the invoice follows separately.</p>" if deposit_due
               else "<p>No deposit is needed. Next, the project contacts.</p>")
            + customer_contract_html),
        attachments=contract_atts,
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


def _notice_scope_siblings(p: dict) -> str:
    """After one scope of a group is approved: a card on every OTHER open scope, and a line for
    the staff approval email. Returns that line, or "" when there is nothing to say.

    RJ's decision: approving one scope closes nothing. The others keep their own status and their
    own approve button -- nothing here writes to them except a thread card -- and staff are told
    which ones are still waiting. Their follow-up chase STOPS (Hanz, 2026-10-24, decision 2) until
    somebody answers the decision task _raise_scope_decision files; the wording says so.

    The card is `internal`: it is a note for the estimator opening that scope's drawer, not news
    for the customer, who has just approved it themselves.

    Best-effort and ADDITIVE. An ungrouped proposal returns before any read; a failed read costs
    the cards and the line, never the approval that has already been recorded."""
    group = p.get("scope_group")
    if not group:
        return ""
    label = p.get("scope_label") or p.get("project_name") or "Another scope"
    try:
        open_sibs = [s for s in db.list_scope_group(group)
                     if s.get("proposal_id") != p["proposal_id"]
                     and (s.get("proposal_status") or "") in ("sent", "viewed")]
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read scope group %s after approving %s: %s",
                    group, p["proposal_id"], exc)
        return ""
    for s in open_sibs:
        try:
            db.add_message(s["proposal_id"], "staff", None,
                           f"{label} was approved; follow-up reminders for this scope have "
                           f"stopped until somebody decides what to do with it.",
                           msg_type="system",
                           meta={"internal": True, "scope_group": group,
                                 "approved_scope": p["proposal_id"]})
        except Exception as exc:  # noqa: BLE001
            log.warning("could not post the scope notice on %s: %s", s.get("proposal_id"), exc)
    if not open_sibs:
        return (f"<p>This is the <strong>{html.escape(label)}</strong> scope. No other scope "
                f"in this project is still open.</p>")
    names = ", ".join(html.escape(s.get("scope_label") or s.get("project_name") or "a scope")
                      for s in open_sibs)
    return (f"<p>This is the <strong>{html.escape(label)}</strong> scope. These scopes have not "
            f"been approved, and their follow-up reminders have stopped: <strong>{names}</strong>. "
            f"Nothing has been closed; a task asks you to decide what to do with them.</p>")


def _raise_scope_decision(p: dict) -> None:
    """After one scope of a group is approved (Hanz, 2026-10-24, decision 2): stop chasing its
    siblings and raise ONE staff task, "decide what to do with the other scopes". NEVER closes a
    sibling.

      1. the group's open reminder task is expired (reason 'scope_approved');
      2. the chase stop itself is DERIVED FROM DATA: followup_rules.group_view returns None while a
         live member is approved and nobody has chosen 'keep' since -- no column;
      3. one `followup_task` with stage 'decision' on the APPROVED scope, rule key
         'sd:<group>:<approved scope>', listing each still-open sibling, and one email to the
         estimator (staff_scope_decision). It shows on the bell like any open task and is answered
         at /api/admin/followup-task/{id}/scope-decision.

    Best-effort and ADDITIVE, like _notice_scope_siblings: an ungrouped proposal returns before any
    read, and a failure costs the task, never the approval already recorded."""
    group = p.get("scope_group")
    if not group:
        return
    pid = p["proposal_id"]
    try:
        members = followup_worker.group_members(group, strict=False)
    except Exception as exc:  # noqa: BLE001
        log.warning("[followup] scope group %s unreadable after approving %s: %s", group, pid, exc)
        return
    if not any(m.get("proposal_id") == pid for m in members):
        return                  # taken out of the group in the tool: an ordinary proposal again
    try:
        for t in db.list_open_group_tasks(group) or []:
            d = t.get("detail") or {}
            if (d.get("stage") != followup_rules.DECISION_STAGE
                    and (d.get("status") or "pending") == "pending"):
                db.decide_followup_task(t["id"], "expired", None, {"reason": "scope_approved"})
    except Exception as exc:  # noqa: BLE001 — the worker's group pass expires it too
        log.warning("[followup] could not expire scope group %s's task: %s", group, exc)
    sibs = [m for m in members if m.get("proposal_id") != pid and followup_rules.is_open(m)]
    if not sibs:
        return
    label = p.get("scope_label") or p.get("project_name") or "One scope"
    scopes = [{"proposal_id": s["proposal_id"],
               "label": s.get("scope_label") or s.get("project_name") or "Proposal",
               "status": s.get("proposal_status")} for s in sibs]
    key = "sd:%s:%s" % (group, pid)
    try:
        rid = db.reserve_followup_task(pid, key, {
            "audience": "staff", "template": followup_rules.GROUP_TASK_TEMPLATE_DECISION,
            "stage": followup_rules.DECISION_STAGE, "status": "pending",
            "scope_group": group, "approved_scope": {"proposal_id": pid, "label": label},
            "scopes": scopes, "mode": followup_rules.followup_mode(p)})
    except Exception as exc:  # noqa: BLE001
        log.warning("[followup] could not raise the scope decision task on %s: %s", pid, exc)
        return
    if rid is None:
        return                  # already raised (a re-run, a double submit)
    due = followup_rules.Due(key, "staff", "staff_scope_decision")
    try:
        followup_worker._send_staff(
            dict(p, _decision={"approved_label": label, "scopes": scopes},
                 link_proposal_id=pid), due)
    except Exception as exc:  # noqa: BLE001 — the task is on the bell either way
        log.warning("[followup] scope decision email for %s failed: %s", pid, exc)
    followup_worker._echo_to_thread(pid, due, {"task_id": rid})


# ── why a bid died, and whether it died at all ────────────────────────────────
# THE CANONICAL MAP. Key to label for every reason a job can be closed LOST under, and the only
# spelling any staff screen renders.
#
# Kyle's own list, verbatim off his screenshot, adopted 2026-08-20 — it replaced a six-answer
# vocabulary that had been invented for the CUSTOMER's "not moving forward" form, which is a
# different question asked of a different person. A customer says "price"; a GC bid dies because we
# were not low. His answers are the ones the sales meeting says out loud.
#
# The last four are the ones ONLY the customer's own form can produce (frontend/index.html). They
# are not offered to staff, but a customer can still close a job under any of them, so they stay
# here or the Lost tab would file their bid under "Not recorded" and read as though nobody said.
#
# TWO OF KYLE'S EIGHT ARE NOT IN THIS MAP AT ALL: "Project on Hold" and "Small Bid <$25k - Pending"
# put a bid on hold rather than closing it (see _HOLD_REASON_LABELS). A hold is not a loss and has
# no business being a Lost column.
#
# "canceled" is the one key Kyle's list SHARES with the customer's form. His screenshot spells it
# "Cancelled" and the customer's radio says "canceled"; one reason must not become two columns over
# an l, so the key is the old one and this label is his spelling. The customer's radio copy is
# theirs and is left alone — it is prose written for them, not a vocabulary.
#
# THIS MAP AND crm-core.js's LOST_REASON ARE COMPARED BY A TEST, in both repos, since 2026-08-20.
# Nothing compared them before, which is how this side said "Selected another contractor" and the
# tool said "Another contractor" for weeks about the same key.
_LOST_REASON_LABELS = {
    "not_low_bid": "Not Low Bid",
    "no_response": "No Response",
    "to_rebid": "Project to Rebid",
    "different_gc": "Went to Different GC",
    "gc_schedule": "Unable to meet GC schedule",
    "canceled": "Project Cancelled",
    "other": "Other",
    "price": "Price",
    "another_contractor": "Selected another contractor",
    "scope_changed": "Scope changed",
    "timing": "Timing",
}

# The two answers that PAUSE a bid instead of killing it. Hanz, 2026-08-20: the card stays on the
# Active board and the reminder emails pause. They route through the `delayed` status this file
# already has rather than a mechanism of their own — see admin_set_status.
_HOLD_REASON_LABELS = {
    "on_hold": "Project on Hold",
    "small_bid_pending": "Small Bid <$25k - Pending",
}


def _delay_window(months: int) -> str:
    """How long a pause is, in words: "2 months", or "4+ months" at the top of the range.

    The picker offers 1-4 and its 4 means "four or more", so that one has to read open-ended or it
    promises a date nobody chose.

    Shared by the customer's own delay card (api_project_status) and the staff-side one
    (admin_set_status) rather than written out twice. Both land in the SAME thread, so two copies
    of this expression would eventually describe one pause two different ways in front of the same
    reader — and the "4+" special case is exactly the kind of detail a second copy loses."""
    return "4+ months" if months == 4 else "%d month%s" % (months, "s" if months > 1 else "")


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
        window = _delay_window(months)
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
        email_sender.notify_team(subject, body_html, kind="status_change",
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
    # client). We store them Fernet-encrypted (bank_crypto.py) so Treadwell can still
    # initiate the debit later via the staff reveal endpoint; the team email + chat +
    # CRM drawer only ever show the last-4 mask. Normalize to digits before storing.
    routing_number = account_number = masked_ref = routing_masked = account_type = None
    if method == "ach":
        # Fail CLOSED before touching anything the customer typed: a broken/missing key
        # must refuse the payment, never fall back to storing it in the clear. Checked
        # first (not just at startup) because the key can go bad mid-run — e.g. an env
        # var dropped by a bad deploy — and every request must re-verify, not trust boot.
        if not bank_crypto.key_configured():
            log.warning("ACH deposit refused for %s: PORTAL_BANK_KEY missing/invalid", p["proposal_id"])
            return _json({
                "ok": False,
                "error": "Bank transfer isn't available right now — please pay by check instead, "
                         "or contact us and we'll sort it out.",
            }, 503)
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
        masked_ref = bank_crypto.mask_last4(account_number)
        routing_masked = bank_crypto.mask_last4(routing_number)
        try:
            routing_number = bank_crypto.encrypt(routing_number)
            account_number = bank_crypto.encrypt(account_number)
        except bank_crypto.BankKeyError as exc:
            # Lost the race with the startup check above (key removed/broken between the two
            # reads). Never store what we have in plaintext -- refuse, exactly like the
            # startup-guarded branch above, and say why in the log without repeating the key.
            log.warning("ACH deposit refused for %s: encrypt failed, %s: %s",
                        p["proposal_id"], type(exc).__name__, exc)
            return _json({
                "ok": False,
                "error": "Bank transfer isn't available right now — please pay by check instead, "
                         "or contact us and we'll sort it out.",
            }, 503)

    db.add_deposit(p["proposal_id"], method, account_name, None, masked_ref, note,
                   routing_number=routing_number, account_number=account_number,
                   account_type=account_type, routing_masked=routing_masked,
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
            f"Routing: {html.escape(routing_masked or '—')} · "
            f"Account: {masked_ref or '—'} · Note: {html.escape(note or '—')}</p>"
            f"<p>Full numbers are encrypted at rest — reveal them for this deposit in the "
            f"proposal tool's CRM drawer.</p>"
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
        kind="deposit_submitted",
        reply_link=_staff_link(p["proposal_id"]), proposal_id=p["proposal_id"],
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


def _pdf_cache_key(pid: str, rev: object) -> str:
    """Cache under the REVISION, not just the proposal.

    Keyed on pid alone, a re-send inside the 10-minute TTL kept serving the previous revision's
    bytes — the page said one price and the PDF said another. `_pdf_cache_drop` covers the
    re-publishes that go through this process, but the key itself is what makes a stale hit
    impossible (two workers, a restart, a drop we forget to call)."""
    return f"{pid}@{int(rev) if rev else 0}"


def _pdf_headers(rev: object) -> dict[str, str]:
    """A pinned revision's bytes are immutable, so let the browser keep them — the viewer's URL
    carries ?rev= and a republish changes it. UNPINNED (legacy rows with no revision) renders the
    LIVE draft, whose bytes change under the same URL, so it must never be cached: that is the
    other half of "the PDF didn't update"."""
    h = {"Content-Disposition": 'inline; filename="proposal.pdf"'}
    h["Cache-Control"] = "private, max-age=600" if rev else "no-store, must-revalidate"
    return h


def _pdf_cache_put(pid: str, rev: object, content: bytes) -> None:
    """Store rendered bytes, sweeping expired entries and enforcing a hard cap so
    the cache can't grow unbounded (a bare dict would retain every viewed PDF for
    the life of the process)."""
    now = time.monotonic()
    for k in [k for k, (exp, _) in _PDF_CACHE.items() if exp <= now]:
        _PDF_CACHE.pop(k, None)
    while len(_PDF_CACHE) >= _PDF_CACHE_MAX:
        _PDF_CACHE.pop(next(iter(_PDF_CACHE)), None)   # evict oldest-inserted
    _PDF_CACHE[_pdf_cache_key(pid, rev)] = (now + _PDF_TTL, content)


def _pdf_cache_drop(pid: str) -> None:
    """Drop EVERY revision's bytes for this proposal — the keys are now pid-scoped, so popping
    the bare pid would leave every rev entry behind."""
    prefix = f"{pid}@"
    for k in [k for k in _PDF_CACHE if k == pid or k.startswith(prefix)]:
        _PDF_CACHE.pop(k, None)


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
        kind="contacts",
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


def _proposal_pdf_bytes(p: dict, *, timeout: float = 90.0) -> Optional[bytes]:
    """The official proposal PDF for this row's PINNED revision, from cache or the tool.

    ONE FETCHER, TWO READERS, and that is the point rather than tidiness: the customer's viewer
    (api_pdf) and the e-signature capture (api_approve) have to hash and sign THE SAME BYTES the
    customer read. Two call sites each running their own httpx.get would render the document
    twice -- two LibreOffice passes for one approval -- and could disagree, which would put a
    hash on a signature certificate for a document nobody ever saw.

    None on every failure, never an exception: both readers degrade. The viewer falls through to
    its stored-URL fallback; the signing path leaves the contract unbuilt for the lazy rebuild.

    `timeout` is a parameter because the two callers are not in the same hurry. A cold render is
    worth 90 seconds to somebody staring at a viewer waiting for it, and is not worth making a
    customer wait that long to have their approval recorded.
    """
    pid = p["proposal_id"]
    # The ROW's revision, never the client's. `?rev=` on the viewer's URL is a cache-buster only;
    # honouring it as a selector would let anyone read a superseded revision's document.
    rev = p.get("current_revision_no") or 0
    hit = _PDF_CACHE.get(_pdf_cache_key(pid, rev))
    if hit and hit[0] > time.monotonic():
        return hit[1]
    # Preferred: render the real Treadwell PDF on demand from the proposal tool.
    if config.PROPOSAL_TOOL_URL and config.SERVICE_TOKEN:
        try:
            # Render the pinned revision, so the downloaded document and the prices
            # on the page can never disagree. Omitted for legacy rows → live draft.
            params = {"draft_id": pid}
            if rev:
                params["revision_no"] = int(rev)
            r = httpx.get(
                config.PROPOSAL_TOOL_URL + "/api/admin/proposal-pdf",
                params=params,
                headers={"X-Service-Token": config.SERVICE_TOKEN},
                timeout=timeout,
            )
            if r.status_code == 200:
                _pdf_cache_put(pid, rev, r.content)
                return r.content
            log.info("proposal-pdf upstream %s for %s", r.status_code, pid)
        except Exception as exc:  # noqa: BLE001
            # The TYPE as well as the message. "fetch failed: " with an empty ConnectError
            # behind it is the log line that costs an SSH session to interpret.
            log.warning("proposal-pdf fetch failed: %s: %s", type(exc).__name__, exc)
    return None


@app.get("/api/portal/{token}/pdf")
def api_pdf(token: str, request: Request):
    p = _require(request, token)
    if not p:
        return _json({"ok": False, "error": "unauthorized"}, 401)
    rev = p.get("current_revision_no") or 0
    content = _proposal_pdf_bytes(p)
    if content is not None:
        return Response(content=content, media_type="application/pdf", headers=_pdf_headers(rev))
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


def _store_signed_contract(proposal_id: str, approval_id, certificate: dict,
                           proposal_pdf: Optional[bytes]
                           ) -> tuple[Optional[bytes], Optional[str]]:
    """Build the signed contract and persist it. Returns (pdf, why-not).

    NEVER RAISES. Both callers are in the middle of something a customer is watching -- an
    approval and a download -- and neither may fail over a document assembly.

    THE TWO HALVES ARE GUARDED SEPARATELY, deliberately: a database that refuses the blob must
    not throw away a PDF we already hold and are about to email. The customer gets their copy
    either way; only the stored copy is lost, and the next download rebuilds it.

    EVERY FAILURE NAMES ITSELF, exception type included. A log line that says "refused"
    without saying why has cost an SSH session and a container probe before now."""
    pdf: Optional[bytes] = None
    err: Optional[str] = None
    try:
        pdf = signing.render_signed_contract(proposal_pdf or b"", certificate)
    except signing.ContractUnavailable as exc:
        err = str(exc)
        log.warning("signed contract not built for %s: %s", proposal_id, err)
    except Exception as exc:  # noqa: BLE001 -- an unexpected shape must not cost the approval
        err = "%s: %s" % (type(exc).__name__, exc)
        log.error("signed contract build raised for %s: %s", proposal_id, err)
    if not approval_id:
        # Nothing to hang it off. On a real database this means the approval insert did not
        # happen, which is worth an error line of its own rather than a silent skip.
        log.error("no approval id for %s -- the signed contract cannot be stored", proposal_id)
        return pdf, err
    try:
        db.upsert_signed_contract(approval_id, proposal_id, pdf=pdf,
                                  pdf_sha256=signing.sha256_hex(pdf) if pdf else None,
                                  built_at=_now_utc() if pdf else None)
    except Exception as exc:  # noqa: BLE001
        log.error("could not store the signed contract for %s: %s: %s",
                  proposal_id, type(exc).__name__, exc)
    return pdf, err


def _contract_email_parts(project_name: Optional[str], pdf: Optional[bytes],
                          error: Optional[str], *, signing_required: bool
                          ) -> tuple[str, str, Optional[list[tuple[str, bytes]]]]:
    """(staff html, customer html, attachments) for the two approval emails.

    THE FAILURE WORDING IS THE POINT OF THIS FUNCTION. A publish once refused an attachment
    AFTER the proposal row was written, logged it, and returned 200 -- the estimator read
    "Sent" and the customer got an email with nothing attached. So when the build failed both
    emails say the document is not here yet, rather than quietly arriving thinner than usual.

    Nothing at all for a Budget Pricing approval: there is no contract to mention, and a line
    apologising for a missing one would invent an expectation nobody had."""
    if not signing_required:
        return "", "", None
    if not pdf:
        if error:
            log.info("approval email will say the contract is pending: %s", error)
        return ("<p><strong>The signed contract could not be built yet.</strong> It will be "
                "available in the staff tool shortly.</p>",
                "<p>Your signed copy is being prepared. It will be available to download "
                "from your project page shortly.</p>",
                None)
    short = html.escape(signing.short_hash(signing.sha256_hex(pdf)))
    staff = ("<p>The signed contract is attached (SHA-256 <code>%s</code>).</p>" % short)
    customer = ("<p>Your signed contract is attached, and can always be downloaded from your "
                "project page.</p>"
                '<p style="color:#64748b;font-size:13px">Document fingerprint (SHA-256): '
                "<code>%s</code>.</p>" % short)
    return staff, customer, [(signing.contract_filename(project_name), pdf)]


def _post_contract_to_thread(proposal_id: str, filename: str, pdf: bytes) -> None:
    """Put the signed contract in the project's chat thread, carried in `meta.attachments`.

    NO NEW STORE AND NO DDL. `portal_questions.meta` is jsonb and uploads.store already writes
    the bytes to the upload volume and hands back exactly the record a message's attachment
    list is made of -- the same two steps the publish path takes for an estimator's files.

    msg_type IS "text", NOT "system", AND THAT IS NOT A STYLE CHOICE. Neither renderer draws
    attachments on a system row: the customer portal's renderMsg returns a title/body card for
    msg_type "system", and the staff drawer's msgHtml renders one as a single `p.note.sys`
    line. Only the message bubble and the proposal card call attHtml. A system row here would
    store the attachment perfectly and show NOBODY a download -- the feature would be invisible
    on both sides with every test green. A bubble from Treadwell is also the honest reading of
    what this is: us handing over a document, not the thread reporting an event. The event is
    already recorded, by the "Approved by ..." system line written further up.

    IT NEVER RAISES. Everything that matters is committed by the time this runs, the customer
    has already been told their proposal is approved, and both emails are carrying the same
    document. A full disk or a refused insert costs the chat copy and nothing else.

    THE TWO HALVES ARE GUARDED SEPARATELY, like _store_signed_contract's are, so the log says
    WHICH one failed -- a stored file with no message is an orphan on the volume, a failed
    store is nothing at all, and they are diagnosed differently. Every line names the exception
    type and its message: "refused" without a reason has cost an SSH session before now.

    IT CARRIES `system_doc`, AND WITHOUT IT THIS FEATURE HIDES CUSTOMER QUESTIONS. This is the
    first staff `text` row the SERVER writes by itself, and `db.unread_counts` counts customer
    text newer than the last staff TEXT message -- its own docstring leans on the invariant that
    staff-authored rows are never `text`. So a customer who asks "can you start in October?" and
    then approves would have their question fall behind this row: the board badge clears, the
    drawer's Chat count clears, and the drawer stops opening on Chat. Kyle would never see the
    question, and nothing anywhere would say so. The marker keeps the row out of that one
    subquery while leaving it a normal bubble everywhere a human looks."""
    try:
        rec = uploads.store(proposal_id, filename, "application/pdf", pdf)
    except Exception as exc:  # noqa: BLE001 -- an approval is never lost over a file write
        log.error("signed contract not attached to the thread for %s: %s: %s",
                  proposal_id, type(exc).__name__, exc)
        return
    try:
        db.add_message(
            proposal_id, "staff", None,
            "Your signed contract is attached. This is your copy to keep, and you can "
            "download it here any time.",
            msg_type="text",
            # sanitize rather than the raw record, matching the publish path: `meta` is read
            # back into two web pages and an email, so the list is rebuilt field by field.
            #
            # `system_doc` is read by db.unread_counts and by nothing else. A dedicated
            # msg_type would be tidier but portal_questions.msg_type carries a CHECK
            # constraint, so it would cost DDL on both databases for a flag one query reads.
            meta={"attachments": uploads.sanitize([rec]), "system_doc": True})
    except Exception as exc:  # noqa: BLE001
        log.error("signed contract stored as %s but not posted to the thread for %s: %s: %s",
                  rec.get("id"), proposal_id, type(exc).__name__, exc)


def _approval_signed_at(approval: dict) -> Optional[datetime]:
    """When the signature was given, as an aware datetime. None when the row cannot say.

    latest_approval_record reads through to_jsonb, so this arrives as an ISO string rather
    than a datetime. A naive value is read as UTC, which is what the column stores.

    None rather than "now" on a value we cannot parse: the moment of signing IS the evidence,
    and a certificate stamped with the time somebody happened to press Download would be a
    confident statement of something false."""
    raw = approval.get("signed_at")
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _signed_contract_pdf(p: dict) -> tuple[Optional[bytes], Optional[str], int]:
    """(pdf, refusal sentence, http status) for one proposal's signed contract.

    LAZY-BUILDS ON A NULL BLOB, mirroring what the proposal tool's Done page does with a
    download token a restart expired: the thing being asked for is reconstructible, so
    reconstruct it rather than dead-ending somebody on a failure they can do nothing about.
    api_approve deliberately leaves the row unbuilt when the renderer is down, so this is a
    normal path and not an error path.

    EVERY REFUSAL IS A SENTENCE THE UI CAN SHOW AS-IS, because both callers render it
    unchanged and the customer-facing one is read by a customer."""
    pid = p["proposal_id"]
    data = db.get_pinned_draft_data(p) or {}
    # THE AUDIENCE TRAVELS TOO, since 2026-09-19. The signature is written onto the proposal's
    # own ACCEPTANCE row and only the Direct artwork has one, so "can this be signed" is a
    # question about the FORM, which is (work_type, audience) -- not work type alone. Reading
    # only the first half here would offer a GC customer a signature the tool then refuses.
    blocked = signing.signing_blocked_reason(data.get("work_type"),
                                             signing.draft_audience(data))
    if blocked:
        # Nowhere on this form to sign, so no contract was ever signed and none can be produced.
        return None, blocked, 400
    try:
        row = db.get_signed_contract(pid)
    except Exception as exc:  # noqa: BLE001 -- a missing migration reads as "not built"
        log.error("could not read the signed contract for %s: %s: %s",
                  pid, type(exc).__name__, exc)
        row = None
    if row and row.get("pdf"):
        return bytes(row["pdf"]), None, 200
    try:
        approval = db.latest_approval_record(pid)
    except Exception as exc:  # noqa: BLE001
        log.error("could not read the approval for %s: %s: %s", pid, type(exc).__name__, exc)
        return None, "We could not look up your signed contract. Please try again shortly.", 502
    if not approval:
        return None, "This proposal has not been signed yet.", 404
    signed_at = _approval_signed_at(approval)
    if signed_at is None:
        log.error("approval %s for %s has an unreadable signed_at %r -- refusing to certify",
                  approval.get("id"), pid, approval.get("signed_at"))
        return None, ("We could not confirm when this proposal was signed. Please contact "
                      "Treadwell and we will send you your copy."), 502
    proposal_pdf = _proposal_pdf_bytes(p)
    if not proposal_pdf:
        return None, ("The proposal document could not be retrieved, so your signed contract "
                      "cannot be rebuilt just now. Please try again shortly."), 502
    fetched_sha = signing.sha256_hex(proposal_pdf)
    signed_sha = approval.get("contract_sha256")
    if signed_sha and signed_sha != fetched_sha:
        # THE DOCUMENT MOVED UNDER THE SIGNATURE. Binding a certificate that attests to one
        # hash onto bytes carrying a different one would produce a contract that misstates
        # what was agreed, which is the worst thing this feature could emit. Refuse, loudly.
        log.error("signed contract for %s cannot be rebuilt: the proposal PDF now hashes %s, "
                  "the signature attests to %s", pid, signing.short_hash(fetched_sha),
                  signing.short_hash(signed_sha))
        return None, ("Your signed contract cannot be rebuilt because the proposal document "
                      "has changed since it was signed. Please contact Treadwell and we will "
                      "send you your copy."), 409
    try:
        certificate = signing.build_certificate(
            project_name=signing.signing_project_name(p, data), proposal_id=pid,
            revision_no=approval.get("revision_no") or p.get("current_revision_no"),
            signer_name=approval.get("name") or "", signer_title=approval.get("title"),
            signer_email=approval.get("approver_email"), signed_at=signed_at,
            ip_address=approval.get("ip"), user_agent=approval.get("user_agent"),
            options_summary=approval.get("option_label"),
            total=_float_or_none(approval.get("total")),
            deposit_amount=_float_or_none(p.get("deposit_amount")),
            proposal_pdf_sha256=signed_sha or fetched_sha,
            revision_sha=approval.get("revision_sha256") or "",
            # The version THIS customer ticked. An approval taken before e-signature existed
            # has none, and falls back to the current wording -- which is honest only because
            # such a row also has no consent to certify; the log line is how that surfaces.
            consent_version=approval.get("consent_version"))
    except signing.UnknownConsentVersion as exc:
        log.error("cannot rebuild the contract for %s: %s", pid, exc)
        return None, ("Your signed contract cannot be rebuilt by this version of the portal. "
                      "Please contact Treadwell and we will send you your copy."), 502
    if not approval.get("consent_version"):
        log.warning("rebuilding a contract for %s from an approval with no consent version -- "
                    "it predates e-signature capture", pid)
    approval_id = (row or {}).get("approval_id") or approval.get("id")
    pdf, err = _store_signed_contract(pid, approval_id, certificate, proposal_pdf)
    if not pdf:
        return None, ("Your signed contract could not be built just now. Please try again "
                      "shortly."), 502
    return pdf, None, 200


def _float_or_none(v):
    """Postgres numerics arrive as Decimal (or as a JSON number through to_jsonb); the
    certificate is JSON-encoded, which neither Decimal nor a stray string survives."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _signed_contract_response(pdf: bytes, project_name: Optional[str]) -> Response:
    """PRIVATE, NEVER STORED. Same posture as the deposit invoice: this is one customer's
    executed contract, and a shared cache holding it would be a disclosure, not a saving."""
    return Response(
        content=pdf, media_type="application/pdf",
        headers={"Content-Disposition":
                 'attachment; filename="%s"' % signing.contract_filename(project_name),
                 "Cache-Control": "private, max-age=0, no-store"},
    )


@app.get("/api/portal/{token}/signed-contract.pdf")
def api_signed_contract_pdf(token: str, request: Request):
    """The customer's own copy of what they signed.

    THE REASON THIS ENDPOINT EXISTS. ESIGN 7001(d)/(e) requires that the signer be able to
    RETAIN their copy of an electronically signed agreement. Until now nobody could: an
    approval left a row in portal_approvals and an email naming a total. The confirmation
    email attaches it too; this is the copy that is still here when the email is not.

    Session-gated exactly like api_deposit_invoice_pdf -- _require checks the customer's own
    session against this proposal, so the /p/<token> link is a deep link and not the gate."""
    p = _require(request, token)
    if not p:
        return _json({"ok": False, "error": "unauthorized"}, 401)
    pdf, err, status = _signed_contract_pdf(p)
    if pdf is None:
        return _json({"ok": False, "error": err or "No signed contract is available."}, status)
    return _signed_contract_response(pdf, p.get("project_name"))



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
        # A scope of a group is announced only by the combined send, same as admin_publish.
        # Unreadable membership counts as grouped: sending nothing is the recoverable mistake.
        try:
            grouped = _active_scope_group(p)
        except _GroupUnreadable as exc:
            log.error("notify %s: scope group unreadable, sent nothing: %s", p["proposal_id"], exc)
            grouped = p.get("scope_group")
        if grouped:
            return _json({"ok": True, "emailed": [], "awaiting_group_send": True})
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
    if auto:
        # An out-of-office is recorded in the thread (it is still the customer's mailbox talking)
        # but it is NOT the customer coming back to us: db._CUSTOMER_TOUCH_SQL skips rows marked
        # this way, so an autoresponder cannot reset their follow-up reminder count.
        meta["auto_reply"] = True

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
    #
    # Security audit, 2026-09-28: the verdict must be a DMARC pass for the From domain in the header
    # our receiving server wrote (inbound.sender_authenticated), and INBOUND_STAFF_EMAIL_POSTS can
    # switch the path off altogether (config.py says when to).
    if is_staff and not config.INBOUND_STAFF_EMAIL_POSTS:
        log.info("inbound: staff email from %r for proposal %s — forwarded to the roster "
                 "(staff email posting is off)", from_email, pid)
        is_staff = False
    if is_staff and not inbound.sender_authenticated(full.get("headers"), from_email):
        log.warning("inbound: %r is on the roster but the receiving server did not verify its "
                    "domain — refusing the staff path for proposal %s", from_email, pid)
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
    #
    # Except that a SENDER match is chosen by the From address alone, which anyone can write. So it
    # posts as the customer only when the receiving server verified that the mail really comes from
    # that address's domain (security audit, 2026-09-28). An unverified one is still forwarded to
    # staff below, marked as unverified; it just never appears in the thread as the customer.
    sender_ok = matched_by != "sender" or inbound.sender_authenticated(full.get("headers"), from_email)
    verified = bool(from_email) and sender_ok and (from_email in authorized or matched_by == "sender")

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


# What this portal build can do, for the proposal tool to ask BEFORE it does something an older
# portal would get wrong (findings #0/#23: an old portal emails each scope of a grouped publish on
# its own while the tool says nothing was sent). Bump `version` when the contract changes.
CAPABILITIES_VERSION = 1


@app.get("/api/admin/capabilities")
def admin_capabilities(request: Request) -> JSONResponse:
    """`{"scope_groups", "group_followups", "version"}`. Service-token gated like every admin route.

    `scope_groups` is true only when this database has the scope-group columns as well as this
    code: prod applies its DDL by hand, and a grouped publish against a database without them is
    refused (admin_publish answers 503 scope_groups_not_ready), so saying "yes" there would only
    move the refusal later.

    `group_followups` is true only when the remind-only DDL is in place too (fix round #29/#30):
    without the widened kind CHECK no follow-up task can be written, so approving a scope would stop
    its siblings' chase and file no decision task, with nothing but a log line to show for it."""
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    scope_groups = db.scope_group_columns_ready()
    return _json({"ok": True, "scope_groups": scope_groups,
                  "group_followups": bool(scope_groups and db.followup_tasks_ready()),
                  "version": CAPABILITIES_VERSION})


# Resend accepts 40 MB per message, but a proposal email that big is a bounce waiting to happen
# on the customer's own mail server. A tighter total, said out loud on the page before they press
# Send, beats a delivery failure they find out about from the customer.
_PUBLISH_ATT_TOTAL = 10 * 1024 * 1024


def _proposal_card_files(proposal_id: str) -> list[tuple[str, bytes]]:
    """Whatever went out attached to the newest proposal card, for a recipient added afterwards.

    Read off the card rather than remembered, because the card IS the record of what was sent --
    `attach_to_latest_card` put them there at publish time for exactly this reason, and a revision
    posts a new card, so this follows the current version without needing to know about revisions
    at all.
    """
    # NOT include_internal. A proposal card is never an internal row -- it is the customer's own
    # "your proposal is ready" -- so opting in would widen the staff-only rule for nothing, and
    # test_only_the_staff_reader_opts_in is right to refuse a third opted-in reader.
    for m in reversed(db.list_messages(proposal_id)):
        if (m.get("msg_type") or "") == "proposal_card":
            return _mail_files(proposal_id, (m.get("meta") or {}).get("attachments"))
    return []


def _mail_files(proposal_id: str, atts) -> list[tuple[str, bytes]]:
    """The stored files for a message's attachments, as `(name, bytes)` ready for an email.

    The publish path has the bytes in hand already -- they arrive base64 in the request. A REPLY
    only has ids, because the file was uploaded minutes earlier and lives on the volume, so the
    bytes have to be read back. That asymmetry is exactly why the reply path was missed: it looked
    like it had the same information available and it did not.

    Capped at the same total as a publish, and a drop is LOGGED rather than silent: an estimator
    who attached four photographs and sees three arrive should be findable in the log, and the
    thread still holds all four either way.
    """
    out: list[tuple[str, bytes]] = []
    total = 0
    for a in atts or []:
        path = uploads.path_of(proposal_id, str(a.get("id") or ""))
        if not path:
            log.warning("attachment %r has no file on disk for %s", a.get("id"), proposal_id)
            continue
        try:
            blob = path.read_bytes()
        except OSError:
            log.exception("could not read attachment %r for %s", a.get("id"), proposal_id)
            continue
        if total + len(blob) > _PUBLISH_ATT_TOTAL:
            log.warning("attachment %r left out of the email for %s: over the %d byte total",
                        a.get("name"), proposal_id, _PUBLISH_ATT_TOTAL)
            continue
        total += len(blob)
        out.append((uploads.clean_name(a.get("name")), blob))
    return out


def _decode_publish_attachments(items) -> list[tuple[str, str, bytes]]:
    """`[{name, mime, b64}]` -> `[(name, mime, bytes)]`, or ValueError with a sentence to show.

    Base64 rather than a separate upload for the reason at the top of this change: on a first send
    there is no proposal row for an upload to belong to. The cost is a bigger request body, which
    is why the total is capped here as well as per file.
    """
    import base64

    out: list[tuple[str, str, bytes]] = []
    total = 0
    for a in (items or [])[:uploads.MAX_PER_MESSAGE]:
        if not isinstance(a, dict):
            continue
        try:
            blob = base64.b64decode(str(a.get("b64") or ""), validate=True)
        except (ValueError, TypeError):
            raise ValueError("%s could not be read" % uploads.clean_name(a.get("name")))
        if not blob:
            continue
        # THE CONTENT, checked here rather than left to the store. This used to test the CLAIMED
        # type; a file whose bytes disagreed then got as far as the storage loop, was refused
        # there, logged, and DROPPED -- while the publish carried on and returned 200. The
        # estimator saw "Sent to customer portal" and the customer got an email with no
        # attachment. A file we will not carry has to stop the send, by name, before the proposal
        # row is touched.
        mime = uploads.detect(blob)
        if mime not in uploads.ALLOWED:
            raise ValueError("%s cannot be attached — that file is not a kind we can send"
                             % uploads.clean_name(a.get("name")))
        total += len(blob)
        if len(blob) > uploads.MAX_BYTES or total > _PUBLISH_ATT_TOTAL:
            raise ValueError("those files come to more than 10 MB together — "
                             "send the large ones in the chat instead")
        out.append((uploads.clean_name(a.get("name")), mime, blob))
    return out


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
    # One scope of a multi-scope project? Read off the draft itself (the same blob everything
    # else here comes from), so the tool needs no new request field. Checked before anything is
    # written: a malformed group must not fall through to an ordinary, individually emailed send.
    scope_group, scope_label, err = _scope_of(data)
    if err:
        return _json({"ok": False, "error": err}, 400)

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

    # THE FILES, decoded and checked BEFORE the proposal row is touched. A publish that has
    # already created a row and posted a card, and only then discovers it cannot read one of the
    # attachments, would leave the customer a proposal whose email is missing half of what the
    # estimator meant to send -- with nothing on screen to say so.
    try:
        pub_atts = _decode_publish_attachments(body.get("attachments"))
    except ValueError as e:
        return _json({"ok": False, "error": str(e)}, 400)

    existing = db.get_proposal(draft_id)
    revised = False
    # A SCOPE JOINS ITS GROUP BEFORE ANYTHING CUSTOMER-VISIBLE IS WRITTEN (findings #24/#40). The
    # scope columns are named directly, so on a database without the scope-group DDL this refuses
    # the publish while nothing exists yet, instead of leaving a visible, ungrouped, un-emailed
    # row. An existing row joins here (adopt also records an earlier ordinary send as announced);
    # a new row carries its group in its own insert below.
    if scope_group and existing:
        try:
            db.adopt_into_scope_group(draft_id, scope_group, scope_label)
        except Exception as exc:  # noqa: BLE001 — nothing has been written; say so plainly
            log.error("publish %s: could not put it in scope group %s: %s",
                      draft_id, scope_group, exc)
            return _json({"ok": False, "error": "scope_groups_not_ready"}, 503)
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
        dep = True if require_deposit is None else require_deposit
        if scope_group:
            try:
                db.create_portal_proposal(draft_id, token, primary, name, project, pdf_path, by,
                                          deposit_required=dep, revision_no=rev_no,
                                          scope_group=scope_group, scope_label=scope_label)
            except Exception as exc:  # noqa: BLE001 — the insert failed whole: nothing exists
                log.error("publish %s: could not create it in scope group %s: %s",
                          draft_id, scope_group, exc)
                return _json({"ok": False, "error": "scope_groups_not_ready"}, 503)
        else:
            db.create_portal_proposal(draft_id, token, primary, name, project, pdf_path, by,
                                      deposit_required=dep, revision_no=rev_no)
        # Seed the chat thread with the proposal card (first publish only).
        db.add_message(draft_id, "staff", None, "Your proposal is ready to review.",
                       msg_type="proposal_card",
                       meta={"revision_no": rev_no} if rev_no else None)

    # On disk now that there is a proposal to scope them to, and recorded on the newest proposal
    # card so the customer can find them in the thread a week later rather than hunting an inbox.
    mail_atts: list[tuple[str, bytes]] = []
    if pub_atts:
        stored = []
        for name_, mime_, blob_ in pub_atts:
            try:
                stored.append(uploads.store(draft_id, name_, mime_, blob_))
                mail_atts.append((uploads.clean_name(name_), blob_))
            except (ValueError, OSError) as exc:
                # Only a genuine WRITE failure can reach here now -- the content was already
                # checked before the row was touched, so this is a disk problem, not a bad file.
                # The publish carries on rather than costing the customer their proposal, and the
                # reason is logged: "refused" with no reason is what made the last one take an
                # SSH session to diagnose.
                log.warning("publish attachment %r not stored for %s: %s: %s",
                            name_, draft_id, type(exc).__name__, exc)
        if stored:
            db.attach_to_latest_card(draft_id, uploads.sanitize(stored))

    # A SCOPE OF A GROUP emails nobody here, so the note and the files typed on the Files page are
    # KEPT for the combined email that will name this scope (finding #17: they used to be dropped
    # with nothing to say so). The response says what was deferred, and whether it was kept.
    deferred: dict[str, Any] = {}
    if scope_group:
        kept_files = uploads.sanitize(stored) if pub_atts else []
        deferred = {"note_deferred": bool(note),
                    "attachments_deferred": [f["name"] for f in kept_files]}
        if note or kept_files:
            try:
                # Kept against the revision the row now points at, which is what the combined
                # send compares (a call with no revision_no leaves the row's own).
                at_rev = rev_no or (db.get_proposal(draft_id) or {}).get("current_revision_no")
                db.stash_scope_group_note(draft_id, scope_group, at_rev, note, kept_files, by)
                deferred["deferred_saved"] = True
            except Exception as exc:  # noqa: BLE001 — the publish stands; say the extras did not
                log.error("publish %s: could not keep its note/files for the group email: %s",
                          draft_id, exc)
                deferred["deferred_saved"] = False

    # Whoever BUILT this estimate hears about the project, roster or no roster.
    #
    # Will, via Hanz on 2026-08-13: "There are set members for the global notification. And this
    # estimator or treadwell employee created an estimate, by default this estimator should be
    # included." The assigned estimator was already folded in at notify time, but that is a
    # different person: RJ can build a bid and hand it to Kyle, and today RJ hears nothing back
    # from a job he priced.
    #
    # Written as a per-project ADD rather than resolved invisibly at send time, for two reasons.
    # It appears on the Notification Sending page beside the hand-added people, so nobody needs to
    # know a hidden rule to explain why RJ is on this thread. And it can be muted — with the mute
    # surviving every later publish, which is why this inserts only when absent.
    #
    # Best-effort: a roster write must never be able to stop a proposal from being delivered.
    # `parseaddr` alone is not validation: it turns "not an email" into "not", which would be
    # written into the roster and later handed to the mailer as a recipient. Same regex every
    # other address on this route goes through.
    creator = (parseaddr(str(body.get("created_by") or ""))[1] or "").strip().lower()
    if creator and (len(creator) > 254 or not _EMAIL_RE.match(creator)):
        log.warning("ignoring an unusable created_by on %s: %r", draft_id, body.get("created_by"))
        creator = ""
    if creator:
        try:
            db.add_notify_override_if_absent(draft_id, creator)
        except Exception as exc:  # noqa: BLE001 — the proposal matters more than the roster
            log.warning("could not add the creator %s to %s's notifications: %s",
                        creator, draft_id, exc)

    # WHO HEARS ABOUT *THIS* SEND, chosen on the Files screen before pressing Send.
    #
    # Hanz, 2026-08-19: "we need that notifcation sending selection in the Files. so we can select
    # who receives it first."
    #
    # It has to arrive in the publish body rather than as a separate call the browser makes first,
    # and that is not a style choice. `portal_notify_overrides.proposal_id` is a foreign key onto
    # `portal_proposals`, and on a FIRST send that row does not exist until create_portal_proposal
    # runs a few lines above — so a pre-publish write is refused by the 404 guard on
    # /api/admin/proposal/{id}/notify-overrides and by the FK underneath it. The send Hanz cares
    # most about is exactly the one a client-side sequence could not have configured.
    #
    # So it lands HERE: after the row exists, before notify_team resolves the recipients a few
    # lines below. Anything written after that resolution would only affect the NEXT notification.
    #
    # `adds` and `mutes` are the complete set of deviations from the global roster, so an address in
    # neither list is returned to whatever the roster says — that is what makes the screen's chips
    # mean what they show. Upsert rather than insert-if-absent, because unlike the creator rule this
    # is somebody deliberately clicking a person. A mute still beats an add in
    # resolve_notify_recipients, so muting the assigned estimator here does stick despite the
    # automatic add they get for owning the job.
    #
    # Best-effort as a whole: the customer's proposal has more claim on this request than the
    # roster does.
    def _addrs(key: str) -> list[str]:
        out = []
        for raw in (body.get(key) or [])[:_MAX_NOTIFY_RECIPIENTS]:
            addr = (parseaddr(str(raw or ""))[1] or "").strip().lower()
            if addr and len(addr) <= 254 and _EMAIL_RE.match(addr):
                out.append(addr)
        return out

    notify_add, notify_mute = _addrs("notify_add"), _addrs("notify_mute")
    if notify_add or notify_mute:
        try:
            muted = set(notify_mute)
            for addr in notify_add:
                if addr not in muted:            # an address in both is a client bug; mute wins
                    db.set_notify_override(draft_id, addr, "add")
            for addr in muted:
                db.set_notify_override(draft_id, addr, "mute")
            # Everyone the screen showed as simply following the roster: drop any stale override so
            # the chip and the behaviour agree. Scoped to rows that exist, so this cannot invent
            # anything, and it is what lets un-ticking on Files undo a mute set in the CRM drawer.
            chosen = set(notify_add) | muted
            for row in db.list_notify_overrides(draft_id):
                addr = (row.get("email") or "").strip().lower()
                if addr and addr not in chosen and addr != creator:
                    db.clear_notify_override(draft_id, addr)
        except Exception as exc:  # noqa: BLE001 — the proposal matters more than the roster
            log.warning("could not apply the chosen notifications on %s: %s", draft_id, exc)

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
    # A SCOPE OF A GROUP SENDS NOTHING HERE. The customer hears about every scope at once, in the
    # one combined email (POST /api/admin/scope-group/{id}/send), and that send is also what
    # stamps last_sent_at, starts the follow-up cadence and confirms to staff -- so all three are
    # skipped below as well. Everything else about the publish (the row, the recipients, the
    # proposal card, a revision reopening approval) is exactly the ordinary path.
    if scope_group:
        emailed = []
    else:
        emailed = [e for e in send_list
                   if email_sender.send_portal_link(e, name if e == primary else "", link, project,
                                                    attachments=mail_atts or None,
                                                    reply_to=rt, note=note, token=token,
                                                    revised=revised)]

    # When this publish happened, so a re-sent card shows the re-send date rather than the
    # original. Stamped even when nothing was delivered: somebody pressed send, and the card
    # showing that attempt is how they find out it needs doing again.
    if not scope_group:
        db.mark_last_sent(draft_id)

    # Enrol (or re-enrol) in follow-up automation. Stamped AFTER the emails go out so
    # the cadence clock starts from the send the customer actually received, and last
    # so a failure here can never stop a proposal from being delivered.
    #
    # ONLY IF SOMETHING ACTUALLY REACHED SOMEBODY. Hanz, 2026-08-19: "auto follow ups shoulnd
    # trigger if a project was not sent". This used to enrol on every publish, so a send where
    # every address bounced still started the cadence — and three days later the worker mailed
    # that same dead address "just following up on the proposal we sent you", about a proposal it
    # had never received. The confirmation email one block down already knows the difference and
    # says "Proposal did NOT send"; the automation has to know it too.
    #
    # Skipping is also the right thing for a FAILED RE-SEND, and for the same reason enrolment is
    # a re-anchor: revision 1 is out with the customer and being chased on its own clock. Moving
    # that anchor to a revision-2 send that never landed would restart the cadence from a date
    # nothing happened on, and (via cycle_key) discard the cycle's send history with it. Leaving
    # the row untouched keeps chasing what the customer actually has.
    try:
        if assigned:
            db.set_assigned_estimator(draft_id, assigned)   # assignment is not a send
        if emailed:
            db.enroll_followup(draft_id)
        elif scope_group:
            log.info("%s is a scope of group %s: enrolled by the combined send, not here",
                     draft_id, scope_group)
        else:
            log.warning("not enrolling %s in follow-ups: the send reached nobody (%d attempted)",
                        draft_id, len(send_list))
    except Exception as exc:  # noqa: BLE001 — the proposal is sent; automation is secondary
        log.warning("could not enrol %s in follow-ups: %s", draft_id, exc)

    # Staff hear that the send actually happened — and, louder, when it did not.
    #
    # Hanz, 2026-08-19: "When a proposal has been sent it does not email the estimator and the
    # people in notification sending. To confirm that the email has been sent." Until now the only
    # record that a proposal went out was the response the sender's own browser showed for a few
    # seconds. The estimator who owns the job and the roster on the Notification Sending page
    # found out when the customer replied — or never.
    #
    # Routed through notify_team so every existing rule applies unchanged: the enabled roster,
    # this project's per-project adds (which now include the creator, written above), the assigned
    # estimator folded in as an add, and mutes winning over all of it. Threaded into the project's
    # staff conversation like every other update about the job.
    #
    # A delivery failure is the version of this email that matters MOST — the sender's browser is
    # the only other place that knows, and it has navigated away — so it always sends and names
    # the addresses that failed rather than going quiet on the bad day.
    #
    # THE SUCCESS LINE NAMES NOBODY, and the asymmetry with the failure branch is the point.
    # Hanz, 2026-08-19, on receiving one: "Remove this whats this?? Jsut say Treadwell has sent you
    # a proposal" / "DONT pout the email address in the email sent just the generic one we had".
    # Who it went to is already on the project, one tap away behind the Reply-in-Portal button, so
    # reciting it back to the roster copies customer contact details into a dozen staff inboxes to
    # support no decision anybody makes from the email. The alternative — keeping the list and only
    # shortening it ("to 2 recipients") — was rejected as still answering a question nobody asked.
    # A scope of a group is confirmed once, by the combined send, for every scope together.
    if not scope_group:
        try:
            failed = [e for e in send_list if e not in emailed]
            doc = ("Revision %d" % rev_no) if (rev_no and rev_no > 1) else "The proposal"
            if not emailed:
                heading = f"Proposal did NOT send — {project}"
            elif failed:
                heading = f"Proposal sent, with failures — {project}"
            else:
                heading = f"Proposal sent — {project}"
            opener = (f"<p><strong>{html.escape(by)}</strong> sent {html.escape(doc.lower())} for "
                      if by else f"<p>{html.escape(doc)} for ")
            parts = [opener + f"<strong>{html.escape(project)}</strong>"]
            if emailed:
                parts.append(".</p>")
            else:
                parts.append(", and <strong>no email was delivered</strong>.</p>")
            # THIS branch keeps the addresses, and a tidy-up that strips them for consistency with the
            # line above breaks the email. Its whole purpose is "this one did not arrive, go fix it",
            # and an address nobody can see is not an address anybody can correct and re-send to. The
            # instruction above was scoped to the email that says it SENT.
            if failed:
                parts.append("<p><strong>Delivery failed for "
                             + ", ".join(html.escape(e) for e in failed) + ".</strong> "
                             + ("They have" if len(failed) > 1 else "That customer has")
                             + " not received the proposal — open the project and send it again.</p>")
            email_sender.notify_team(
                heading, "".join(parts), kind="sent",
                reply_link=_staff_link(draft_id), proposal_id=draft_id,
                reply_to=rt, token=token, project=project,
                assigned_estimator=assigned or (existing or {}).get("assigned_estimator"),
            )
        except Exception as exc:  # noqa: BLE001 — the proposal is sent; the confirmation is secondary
            log.warning("send-confirmation email failed for %s: %s", draft_id, exc)

    return _json({"ok": True, "token": token, "url": link, "customer_email": primary,
                  "recipients": send_list, "emailed": emailed,
                  "revision_no": rev_no, "revised": revised,
                  "assigned_estimator": assigned or None,
                  # For a scope of a group: nothing has been emailed yet, and the tool's next step
                  # is the combined send once every scope is published.
                  "scope_group": scope_group, "scope_label": scope_label,
                  "awaiting_group_send": bool(scope_group), **deferred})


@app.post("/api/admin/scope-group/{group_id}/send")
async def admin_scope_group_send(group_id: str, request: Request) -> JSONResponse:
    """Email the customer ONE message about the scopes of a multi-scope project they have not been
    told about yet.

    RJ: three scopes are three proposals, but one email. Publishing a scope sends nothing (see
    admin_publish); this is the send. Body, all optional:
        {"by": "<who pressed send>", "message": "<personal note>",
         "attachments": [{name, mime, b64}], "retry_failed": true}

    PENDING = the group's published LIVE scopes (scope_groups.send_rows) that are not ANNOUNCED: no
    email has reached anybody about them at their current revision (scope_groups.scope_states). The
    first email of a project lists every scope; a later one names only the NEW and the REVISED ones
    (Hanz's decision 3). Nothing pending is 409 `nothing_to_send`, and that is also what makes a
    double click, a retried request or a second tab safe: the claim is taken under a row lock
    before anything is sent (db.claim_scope_group_send).

    UNPUBLISHED SCOPES BLOCK ONLY THE FIRST EMAIL (409 scopes_not_published): a customer who gets
    two scopes now and the third separately has been sent the thing RJ asked us not to. Once the
    project has gone out, a scope still being priced never holds back a new one; it is reported
    as `not_included`.

    DELIVERY IS THE TRUTH. One email per address, listing only the scopes that address is on.
    Every recipient's result is in `recipients`. A scope is announced only if at least one address
    got it; when nobody got anything the claim is released, nothing is marked, and the answer is
    HTTP 502 `delivery_failed`. `retry_failed: true` mails ONLY the addresses that failed last time
    (read from the marks), never everybody, and never restarts the follow-up cadence.

    Recorded like a publish for each newly announced scope: last_sent_at, the follow-up cadence
    (a customer's pause is kept), a staff-only card on its thread, and ONE staff confirmation."""
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    if not _SCOPE_ID_RE.match(group_id or ""):
        return _json({"ok": False, "error": "invalid_scope_group"}, 400)
    body = await _body(request)
    by = _cap(body.get("by"), 120) or None
    note = _cap(body.get("message"), 2000) or None
    retry = body.get("retry_failed") is True
    # A person's answer to "the last send's result was lost -- send those scopes again?"
    # (scope_groups.mark_status 'unknown'). Never assumed: guessing yes mails the customer twice.
    resend_unknown = body.get("resend_unknown") is True
    # The estimator's files, accepted exactly as a single-scope publish accepts them (finding #17:
    # they used to be dropped on the floor for a scope), and checked before anything is claimed.
    try:
        group_atts = [(n, b) for n, _m, b in _decode_publish_attachments(body.get("attachments"))]
    except ValueError as e:
        return _json({"ok": False, "error": str(e)}, 400)

    rows = db.list_scope_group(group_id)
    states = db.list_scope_group_draft_states(group_id)
    expected, parked = scope_groups.split_members(states)
    if not rows and not states:
        return _json({"ok": False, "error": "group_not_found"}, 404)
    # A LIVE scope whose portal row exists but does not carry the group: a project already sent as
    # an ordinary proposal and then given "New scope" in the tool (findings #1/#2). It joins here,
    # with its earlier send counted as announced, rather than blocking the group until somebody
    # republishes it (which would reopen its approval).
    published = {r["proposal_id"] for r in rows}
    adopted = []
    for pid in expected:
        if pid in published or not db.get_proposal(pid):
            continue
        gid, label, err = _scope_of(db.get_draft_data(pid) or {})
        if err or gid != group_id:
            continue
        try:
            db.adopt_into_scope_group(pid, group_id, label)
            adopted.append(pid)
        except Exception as exc:  # noqa: BLE001 — it simply stays unpublished for this send
            log.warning("scope group %s: could not adopt %s: %s", group_id, pid, exc)
    if adopted:
        rows = db.list_scope_group(group_id)
    published_ids = [r["proposal_id"] for r in rows]
    known = set(expected) | set(parked)
    # THE DRAFTS DECIDE MEMBERSHIP. A portal row keeps its scope_group when a later publish carries
    # none, so a scope split back out of the group in the tool can still be tagged here. Reported,
    # never emailed. Parked scopes (archived, or Test in a real project) are neither.
    detached = [pid for pid in published_ids if known and pid not in known]
    live = scope_groups.send_rows(rows, states)
    not_included = [d for d in expected if d not in published_ids]
    plan = scope_groups.plan_send(live, db.list_scope_group_marks([group_id]), _now_utc(),
                                  include_unknown=resend_unknown)
    if not retry and plan["first"] and (not_included or not rows):
        return _json({"ok": False, "error": "scopes_not_published",
                      "missing": not_included, "published": published_ids}, 409)
    if not live:
        return _json({"ok": False, "error": "nothing_to_send", "not_included": not_included}, 409)

    lead = live[0]                                   # list_scope_group is in publish order
    project = lead.get("project_name") or "your project"
    by_pid = {r["proposal_id"]: r for r in live}

    def _label(r: dict) -> str:
        return r.get("scope_label") or r.get("project_name") or "Proposal"

    def _url(r: dict) -> str:
        return f"{config.PUBLIC_BASE_URL}/p/{r['token']}"

    def _on(r: dict) -> list[str]:
        # The same list the portal's access check reads, with the primary as a publish's fallback.
        return [e for e in ((x or "").strip().lower()
                            for x in (db.get_recipients(r["proposal_id"]) or [r.get("customer_email")]))
                if e]

    if retry:
        return _retry_scope_group_send(group_id, live, by_pid, project, lead, by, note, group_atts,
                                       _label, _url, _on, not_included, detached)

    claim = db.claim_scope_group_send(group_id, [r["proposal_id"] for r in live], by=by,
                                      include_unknown=resend_unknown)
    if not claim:
        return _json({"ok": False, "error": "nothing_to_send", "not_included": not_included,
                      "detached": detached, "unknown": list(plan.get("unknown") or [])}, 409)
    send_no, kinds = claim["send_no"], claim["kinds"]
    pending = [by_pid[pid] for pid in claim["pending"] if pid in by_pid]

    # address -> the pending scopes that address is on, in publish order.
    per: dict[str, list[dict]] = {}
    for r in pending:
        for e in _on(r):
            per.setdefault(e, []).append(r)
    if not per:
        db.settle_scope_group_send(group_id, send_no, {r["proposal_id"]: {} for r in pending})
        return _json({"ok": False, "error": "no_contact_email"}, 400)

    # What each scope's email carries: this request's note and files, plus the note and files each
    # scope's own (grouped) publish was given on the Files page (finding #17).
    extras = _scope_send_extras(group_id, pending, note, group_atts)
    # STAMPED BEFORE THE FIRST EMAIL. From here on a lost result means "maybe sent", and the scope
    # is never re-sent on its own (scope_groups.mark_status 'unknown'). A stamp that cannot be
    # written means nothing has been mailed yet, so the claim is undone and nobody is.
    try:
        db.mark_scope_group_attempt(group_id, send_no, {
            pid: {"note": x["note"], "files": x["refs"]} for pid, x in extras.items()})
    except Exception as exc:  # noqa: BLE001
        log.error("scope group %s: could not stamp send %s: %s", group_id, send_no, exc)
        try:
            db.release_scope_group_send(group_id, send_no)
        except Exception as exc2:  # noqa: BLE001 — unstamped, the claim expires on its own
            log.error("scope group %s: could not release send %s: %s", group_id, send_no, exc2)
        return _json({"ok": False, "error": "send_not_recorded", "emailed": []}, 503)

    primary = (lead.get("customer_email") or "").strip().lower()
    emailed: list[str] = []
    for e, mine in per.items():
        try:
            ok = email_sender.send_scope_group_links(
                e, (lead.get("customer_name") or "") if e == primary else "", project,
                [{"label": _label(r), "url": _url(r),
                  "approved": (r.get("proposal_status") or "") == "approved",
                  "change": None if kinds.get(r["proposal_id"]) == "first"
                  else kinds.get(r["proposal_id"])} for r in mine],
                # The first of THEIR scopes, so a reply from a contact on Lab 2 alone lands in
                # Lab 2's thread rather than in one they cannot open.
                reply_to=email_sender.proposal_reply_to(mine[0]["token"]),
                note=_scope_note_for(note, mine, extras, _label), token=mine[0]["token"],
                attachments=_scope_files_for(group_atts, mine, extras) or None)
        except Exception as exc:  # noqa: BLE001 — one bad address must not stop the others
            log.warning("scope group %s: send to %s failed: %s", group_id, e, exc)
            ok = False
        if ok:
            emailed.append(e)

    def _has(e: str, pid: str) -> bool:
        return any(x["proposal_id"] == pid for x in per[e])

    delivery = {r["proposal_id"]: {
        "delivered": [e for e in emailed if _has(e, r["proposal_id"])],
        "failed": [e for e in per if e not in emailed and _has(e, r["proposal_id"])]}
        for r in pending}
    # Tried twice: a mark left unsettled after the emails went out is 'unknown' -- safe (never
    # re-sent on its own) but it needs a person, so one database blip is not allowed to cause it.
    recorded = False
    for attempt in (1, 2):
        try:
            db.settle_scope_group_send(group_id, send_no, delivery)
            recorded = True
            break
        except Exception as exc:  # noqa: BLE001 — the emails are out; never undo that
            log.error("scope group %s: could not record send %s (try %d): %s",
                      group_id, send_no, attempt, exc)
    reached = [r for r in pending if delivery[r["proposal_id"]]["delivered"]]
    for r in reached:
        pid = r["proposal_id"]
        # Each step guarded on its own: the email is out, and bookkeeping must not undo that.
        try:
            db.mark_last_sent(pid)
        except Exception as exc:  # noqa: BLE001
            log.warning("scope group %s: last_sent on %s failed: %s", group_id, pid, exc)
        if (r.get("proposal_status") or "") in ("sent", "viewed"):
            try:
                # Only a NEWLY announced scope is enrolled, and a customer's pause is kept.
                db.enroll_followup(pid, keep_pause=True)
            except Exception as exc:  # noqa: BLE001
                log.warning("scope group %s: could not enrol %s: %s", group_id, pid, exc)
        others = [_label(o) for o in reached if o["proposal_id"] != pid]
        what = {"new": "Sent to the customer as a new scope",
                "revised": "Sent to the customer as a revised proposal"}.get(
                    kinds.get(pid), "Sent to the customer")
        try:
            db.add_message(pid, "staff", None,
                           (what + (" together with %s." % ", ".join(others) if others else ".")),
                           msg_type="system",
                           meta={"internal": True, "scope_group": group_id, "send_no": send_no})
        except Exception as exc:  # noqa: BLE001
            log.warning("scope group %s: could not post the sent card on %s: %s",
                        group_id, pid, exc)

    names = ", ".join(html.escape(_label(r)) + ("" if kinds.get(r["proposal_id"]) == "first"
                                                 else " (%s)" % kinds.get(r["proposal_id"]))
                      for r in pending)
    _confirm_scope_group_send(lead, project, by, per, emailed,
                              f"the {len(pending)} scope{'s' if len(pending) != 1 else ''} of "
                              f"<strong>{html.escape(project)}</strong> in one email: {names}",
                              group_id)

    out = {"ok": bool(emailed), "group_id": group_id, "send_no": send_no if emailed else None,
           "first": bool(claim.get("first")),
           "scopes": [{"proposal_id": r["proposal_id"], "label": _label(r), "token": r["token"],
                       "url": _url(r), "status": r.get("proposal_status"),
                       "kind": kinds.get(r["proposal_id"]),
                       "announced": bool(delivery[r["proposal_id"]]["delivered"])}
                      for r in pending],
           "not_included": not_included, "detached": detached,
           "recipients": [{"email": e, "delivered": e in emailed} for e in per],
           "emailed": emailed,
           # False: the emails above went out but could not be recorded. The scopes show as
           # 'unknown' (scope_send_state.unknown_scope_ids) instead of quietly becoming pending.
           "recorded": recorded}
    if not emailed:
        out["error"] = "delivery_failed"
        return _json(out, 502)
    return _json(out)


def _scope_send_extras(group_id: str, pending: list, note: Optional[str],
                       group_atts: list) -> dict:
    """{proposal_id: {"note", "stash_files", "refs"}} for a combined send's pending scopes.

    `note`: this request's message, else the note the scope's grouped publish was given on the
    Files page (scope_groups.stashed_extras). `stash_files`: that publish's files, already on disk
    under the scope. `refs`: every file the scope's email carries as `{pid, id, name}`, recorded on
    the send mark so a `retry_failed` sends the same files: this request's own files are stored
    once, under the first pending scope, to make that possible (best-effort -- a file that cannot
    be stored is still emailed now, it just cannot be retried)."""
    pids = [r["proposal_id"] for r in pending]
    try:
        stashed = scope_groups.stashed_extras(pending, db.list_scope_group_notes(group_id, pids))
    except Exception as exc:  # noqa: BLE001 — the email matters more than a note
        log.warning("scope group %s: stashed publish notes unreadable: %s", group_id, exc)
        stashed = {}
    own_refs: list[dict] = []
    if group_atts and pids:
        for name_, blob_ in group_atts:
            try:
                rec = uploads.store(pids[0], name_, None, blob_)
                own_refs.append({"pid": pids[0], "id": rec["id"], "name": rec["name"]})
            except (ValueError, OSError) as exc:
                log.warning("scope group %s: %r not kept for a retry: %s", group_id, name_, exc)
    out = {}
    for pid in pids:
        s = stashed.get(pid) or {}
        files = list(s.get("files") or [])
        out[pid] = {"note": note or s.get("note"), "stash_files": files,
                    "refs": own_refs + [{"pid": pid, "id": f["id"], "name": f.get("name")}
                                        for f in files]}
    return out


def _scope_note_for(note: Optional[str], mine: list, extras: dict, label) -> Optional[str]:
    """The note one recipient's combined email carries. The request's own message wins; otherwise
    the Files-page notes of THEIR scopes, each named when there is more than one."""
    if note:
        return note
    notes: list[tuple[str, str]] = []
    for r in mine:
        n = (extras.get(r["proposal_id"]) or {}).get("note")
        if n and n not in [x[1] for x in notes]:
            notes.append((label(r), n))
    if not notes:
        return None
    if len(notes) == 1:
        return notes[0][1]
    return _cap("\n\n".join("%s: %s" % (lbl, n) for lbl, n in notes), 4000)


def _read_stored_file(pid: str, f: dict) -> Optional[tuple[str, bytes]]:
    """(name, bytes) of one file already on disk under proposal `pid`, or None (logged)."""
    try:
        p = uploads.path_of(pid, str(f.get("id") or ""))
        if p is None:
            log.warning("stored file %s of %s is gone", f.get("id"), pid)
            return None
        return uploads.clean_name(f.get("name")), p.read_bytes()
    except OSError as exc:
        log.warning("stored file %s of %s unreadable: %s", f.get("id"), pid, exc)
        return None


def _scope_files_for(group_atts: list, mine: list, extras: dict) -> list:
    """The files one recipient's combined email carries: this request's, then the Files-page
    files of THEIR scopes (a contact on Lab 2 alone never gets Lab 1's drawings)."""
    out = list(group_atts or [])
    for r in mine:
        for f in (extras.get(r["proposal_id"]) or {}).get("stash_files") or []:
            got = _read_stored_file(r["proposal_id"], f)
            if got:
                out.append(got)
    return out


def _retry_files_for(group_atts: list, mine: list, targets: dict) -> list:
    """A retry's files for one recipient: this request's own if it brought any, otherwise the
    files the ORIGINAL email carried for their scopes (the `{pid, id}` refs on the send mark),
    each once."""
    if group_atts:
        return list(group_atts)
    seen: set = set()
    out = []
    for r in mine:
        for f in (targets.get(r["proposal_id"]) or {}).get("files") or []:
            key = (f.get("pid"), f.get("id"))
            if not f.get("pid") or key in seen:
                continue
            seen.add(key)
            got = _read_stored_file(f["pid"], f)
            if got:
                out.append(got)
    return out


def _retry_scope_group_send(group_id, live, by_pid, project, lead, by, note, group_atts,
                            _label, _url, _on, not_included, detached) -> JSONResponse:
    """`retry_failed`: mail again ONLY the addresses a scope's latest email did not reach (finding
    #16), each listing only the scopes it failed for. Nothing else moves: no new announcement, no
    last_sent_at, no follow-up re-enrolment, no pause cleared."""
    targets = db.claim_scope_group_retry(group_id, list(by_pid))
    if not targets:
        return _json({"ok": False, "error": "nothing_to_send", "not_included": not_included,
                      "detached": detached}, 409)
    per: dict[str, list[dict]] = {}
    for pid, t in targets.items():
        r = by_pid.get(pid)
        if not r:
            continue
        still_on = set(_on(r))
        for e in t["failed"]:
            if e in still_on:                  # an address taken off the scope is not chased
                per.setdefault(e, []).append(r)
    primary = (lead.get("customer_email") or "").strip().lower()
    emailed: list[str] = []
    # THE ORIGINAL EMAIL'S note and files (recorded on the mark by mark_scope_group_attempt),
    # unless this request brings its own: the person it failed for gets what everyone else got.
    orig = {pid: {"note": t.get("note")} for pid, t in targets.items()}
    for e, mine in per.items():
        try:
            ok = email_sender.send_scope_group_links(
                e, (lead.get("customer_name") or "") if e == primary else "", project,
                [{"label": _label(r), "url": _url(r),
                  "approved": (r.get("proposal_status") or "") == "approved",
                  "change": None if targets[r["proposal_id"]]["kind"] == "first"
                  else targets[r["proposal_id"]]["kind"]} for r in mine],
                reply_to=email_sender.proposal_reply_to(mine[0]["token"]),
                note=_scope_note_for(note, mine, orig, _label), token=mine[0]["token"],
                attachments=_retry_files_for(group_atts, mine, targets) or None)
        except Exception as exc:  # noqa: BLE001
            log.warning("scope group %s: retry to %s failed: %s", group_id, e, exc)
            ok = False
        if ok:
            emailed.append(e)
    results = {}
    for pid, t in targets.items():
        ok_here = [e for e in emailed if any(x["proposal_id"] == pid for x in per.get(e, []))]
        results[t["mark_id"]] = {"delivered": list(t.get("delivered") or []) + ok_here,
                                 "failed": [e for e in t["failed"] if e not in ok_here]}
    try:
        db.settle_scope_group_retry(results)
    except Exception as exc:  # noqa: BLE001
        log.error("scope group %s: could not record the retry: %s", group_id, exc)
    labels = ", ".join(html.escape(_label(by_pid[p])) for p in targets if p in by_pid)
    _confirm_scope_group_send(lead, project, by, per, emailed,
                              f"<strong>{html.escape(project)}</strong> again to the contacts "
                              f"it had not reached: {labels}", group_id)
    out = {"ok": bool(emailed), "group_id": group_id, "retry": True,
           "scopes": [{"proposal_id": p, "label": _label(by_pid[p])} for p in targets
                      if p in by_pid],
           "not_included": not_included, "detached": detached,
           "recipients": [{"email": e, "delivered": e in emailed} for e in per],
           "emailed": emailed}
    if not emailed:
        out["error"] = "delivery_failed"
        return _json(out, 502)
    return _json(out)


def _confirm_scope_group_send(lead: dict, project: str, by: Optional[str], per: dict,
                              emailed: list, what_html: str, group_id: str) -> None:
    """ONE staff confirmation for a combined send. Same rules as a publish's: names nobody when it
    worked, names every address that bounced when it did not."""
    try:
        failed = [e for e in per if e not in emailed]
        if not emailed:
            heading = f"Proposals did NOT send — {project}"
        elif failed:
            heading = f"Proposals sent, with failures — {project}"
        else:
            heading = f"Proposals sent — {project}"
        opener = (f"<p><strong>{html.escape(by)}</strong> sent " if by else "<p>We sent ")
        parts = [opener + what_html]
        parts.append(".</p>" if emailed else ", and <strong>no email was delivered</strong>.</p>")
        if failed:
            parts.append("<p><strong>Delivery failed for "
                         + ", ".join(html.escape(e) for e in failed) + ".</strong> "
                         + ("They have" if len(failed) > 1 else "That customer has")
                         + " not received these proposals — send them again.</p>")
        email_sender.notify_team(
            heading, "".join(parts), kind="sent",
            reply_link=_staff_link(lead["proposal_id"]), proposal_id=lead["proposal_id"],
            reply_to=email_sender.proposal_reply_to(lead["token"]), token=lead["token"],
            project=project, assigned_estimator=lead.get("assigned_estimator"),
        )
    except Exception as exc:  # noqa: BLE001 — the emails are out; the confirmation is secondary
        log.warning("scope group %s: send confirmation failed: %s", group_id, exc)


def _pipeline_groups(rows: list, now: datetime) -> dict:
    """{group id: {"ids", "view", "home"}} for every scope group on the board, built from the
    board's own rows. Two guarded reads per GROUP (not per row): which drafts are live members, and
    the group's last 'keep chasing' decision. An unreadable read degrades to the stored rows, which
    only affects the dates shown -- the worker fails closed on its own read."""
    by: dict = {}
    for r in rows or []:
        if r.get("scope_group"):
            by.setdefault(r["scope_group"], []).append(r)
    out: dict = {}
    for gid, grows in by.items():
        try:
            drafts = db.list_scope_group_draft_states(gid)
        except Exception as exc:  # noqa: BLE001
            log.warning("[pipeline] scope group %s drafts unreadable: %s", gid, exc)
            drafts = []
        try:
            resumed = db.scope_group_resumed_at(gid)
        except Exception as exc:  # noqa: BLE001
            log.warning("[pipeline] scope group %s resume mark unreadable: %s", gid, exc)
            resumed = None
        members = [dict(m, scope_group_resumed_at=resumed)
                   for m in followup_rules.live_members(grows, drafts)]
        ids = {m["proposal_id"] for m in members}
        # The board's own non-live rows of the group (a closed former home) keep the group's last
        # send and counted reminders, as the worker's view does (followup_worker._group_history).
        out[gid] = {"ids": ids,
                    "view": followup_rules.group_view(
                        members, now, followup_rules.history_rows(grows, drafts, ids)),
                    "home": followup_rules.group_home(members),
                    "rows": grows, "drafts": drafts}
    return out


def _pipeline_send_states(groups: dict, now: datetime) -> dict:
    """{group id: send state} for the board: which scopes the combined email has NOT told the
    customer about yet (finding #3). A grouped publish of a revision emails nobody -- the group
    email owns it -- so without this the tool cannot say "Email not sent" honestly about a scope
    that already went out once. ONE guarded read of the send marks for the whole board."""
    if not groups:
        return {}
    try:
        marks = db.list_scope_group_marks(list(groups))
    except Exception as exc:  # noqa: BLE001 — the board must not die on a bookkeeping read
        log.warning("[pipeline] scope send marks unreadable: %s", exc)
        return {}
    by: dict = {}
    for m in marks or []:
        by.setdefault((m.get("detail") or {}).get("scope_group"), []).append(m)
    out = {}
    for gid, g in groups.items():
        rows, drafts = g.get("rows") or [], g.get("drafts") or []
        plan = scope_groups.plan_send(scope_groups.send_rows(rows, drafts), by.get(gid, []), now)
        published = {r.get("proposal_id") for r in rows}
        out[gid] = {"plan": plan,
                    "not_included": [d for d in scope_groups.split_members(drafts)[0]
                                     if d not in published]}
    return out


def _apply_group_schedules(rows: list, out: list, followup_cfg: dict) -> None:
    """The board's scope-group pass, after its per-row loop (rows[i] is out[i]).

    A scope group is chased as ONE project: an OPEN live member's `next_followup_at` and
    `followup_plan` are the GROUP's (followup_rules.group_view over the board's own rows, which
    carry the same inputs the worker reads), and `followup_home_id` names the scope whose row holds
    the group's schedule and task. An approved scope keeps its own deposit schedule. Every
    ungrouped row is left exactly as the loop built it, and costs nothing here."""
    if not any(r.get("scope_group") for r in rows):
        return
    now = _now_utc()
    groups = _pipeline_groups(rows, now)
    sends = _pipeline_send_states(groups, now)
    for r, o in zip(rows, out):
        s = sends.get(r.get("scope_group") or "")
        if s:
            # {announced, pending_scope_ids, not_included}: whether THIS scope's current revision
            # has reached the customer, and what an "Email again" would name for the project.
            o["scope_send_state"] = {
                "announced": bool((s["plan"]["states"].get(r["proposal_id"]) or {})
                                  .get("announced")),
                "pending_scope_ids": list(s["plan"]["pending"]),
                "not_included": list(s["not_included"]),
                # A send whose result was lost after the emails went out: maybe delivered. Not
                # pending (a second press would mail them twice); the send's `resend_unknown`
                # is a person's call to send them again.
                "unknown_scope_ids": list(s["plan"].get("unknown") or []),
            }
        g = groups.get(r.get("scope_group") or "")
        if not g or r["proposal_id"] not in g["ids"] or not followup_rules.is_open(r):
            continue
        o["next_followup_at"] = _iso(followup_rules.next_due_at(g["view"], now, followup_cfg)
                                     if g["view"] else None)
        o["followup_plan"] = _followup_plan(g["home"] or r)
        o["followup_home_id"] = (g["home"] or {}).get("proposal_id")


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
    rows = []                       # the raw rows, for the scope-group pass below
    for r in db.list_all_portal_proposals():
        rows.append(r)
        out.append({
            "proposal_id": r["proposal_id"], "token": r["token"],
            "customer_email": r["customer_email"], "customer_name": r.get("customer_name"),
            "project_name": r.get("project_name"), "proposal_status": r["proposal_status"],
            # One scope of a multi-scope project, and its name. The board groups on the id; both
            # are null for every ungrouped proposal.
            "scope_group": r.get("scope_group") or None,
            "scope_label": (r.get("scope_label") or None) if r.get("scope_group") else None,
            # Published but not yet in a combined email: the row reads 'sent' and sent_at falls
            # back to created_at, so without this the board would show a scope the customer has
            # never been told about as sent. Only the combined send stamps last_sent_at on a scope.
            "scope_send_pending": bool(r.get("scope_group")) and not r.get("last_sent_at"),
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
            # Remind-only (2026-10-23). The open follow-up task, if any — the staff bell can read
            # it here off the poll it already makes ({id, status, template, label, stage, due_at}
            # or null; full detail at GET /api/admin/followup-task/{id}) — and this proposal's own
            # schedule ({mode, interval_days, stop_after, next_at, anchor_at}; nulls = global).
            "followup_task": _task_summary(r.get("pending_task")),
            "followup_plan": _followup_plan(r),
            "unread": unread.get(r["proposal_id"], 0),   # customer messages awaiting a staff reply
            # Who owns it, and the milestones the board dates a card by. The
            # staff side picks the latest of these — it also owns turning the
            # email into a name, because portal_app is denied `profiles`.
            "estimator_email": r.get("estimator_email"),
            # The LATEST send. created_at is the first one and never moves, so a re-sent card
            # would otherwise re-enter Sent showing a date from weeks earlier. Coalesced in
            # Python so rows predating the column (and every dict fixture) keep working.
            "sent_at": _iso(r.get("last_sent_at") or r.get("created_at")),
            "viewed_at": _iso(r.get("viewed_at")),       # FIRST view only
            "approved_at": _iso(r.get("approved_at")),
            "deposit_requested_at": _iso(r.get("deposit_requested_at")),
        })
    _apply_group_schedules(rows, out, followup_cfg)
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
            "scope_group": p.get("scope_group") or None,
            "scope_label": (p.get("scope_label") or None) if p.get("scope_group") else None,
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
        # include_internal: this is the STAFF drawer, and the internal cards (a customer opening
        # the proposal, and the CRM's own actions) are written for whoever is reading here.
        "messages": [_msg(m) for m in db.list_messages(proposal_id, include_internal=True)],
        "deposit_ref": proposals.deposit_ref(proposal_id),
        # Masked only (2026-09-29) -- routing_number/account_number are encrypted at rest and
        # list_deposits() does not even select them. A staff member who needs the full numbers
        # calls POST /api/admin/deposit/{id}/reveal for that one deposit; see bank_crypto.py.
        "deposits": [{
            "id": d.get("id"),
            "method": d["method"], "account_name": d.get("account_name"), "bank_name": d.get("bank_name"),
            "masked_ref": d.get("masked_ref"), "routing_masked": d.get("routing_masked"), "note": d.get("note"),
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
# The same four as an English noun phrase, for the thread card. Keyed off the stored kind rather
# than the short form the drawer sends, because that one is normalised away before we get here.
_FOLLOWUP_NOUNS = {"staff_call": "a call", "staff_email": "an email",
                   "staff_text": "a text", "staff_note": "a note"}
# DERIVED, not typed out again. This was a second literal tuple 1300 lines below the label map it
# is supposed to agree with, and the two had already drifted once. A reason that is accepted but has
# no label renders as a bare "Marked Closed-Lost." with the reason silently dropped.
_LOST_REASONS = tuple(_LOST_REASON_LABELS)
_HOLD_REASONS = tuple(_HOLD_REASON_LABELS)
_PAUSE_MONTHS = (1, 2, 3, 4)
# Why the comment is required. Hanz, 2026-08-20. A reason alone does not tell the next person
# anything — "Not Low Bid" is eight identical cards by the end of a quarter, and "we were 12% over
# Wilson on the pour" is the sentence the sales meeting is actually held to read. Refused empty on
# the STAFF paths only: the customer's own form asks "if you don't mind saying" and must keep
# taking no for an answer.
_NOTE_MAX = 2000


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


# ── staff actions in the project's thread (STAFF-ONLY cards) ──────────────────
# Hanz: "every actiion of customer and staff should appear in the chatbox whether it be email,
# messages etc". Nearly all of it already did — the send, each revision, questions and replies in
# both directions and by email, the approval, the invoice, the deposit, the follow-up emails, the
# customer's own delayed/not-going-ahead/ready-again, the customer opening it. What the thread
# could not tell you was what STAFF had done to the project: who it was handed to, that somebody
# rang the customer, that it had been filed as lost, that the chasing had been switched off. A
# reader opening a quiet project could not tell "nobody has touched this" from "Kyle called them
# on Tuesday and they asked for a fortnight".
#
# These four are the CRM's own bookkeeping and several of them are things a customer must never
# read — "Closed–Lost. Reason: Selected another contractor", "reassigned from Kyle to RJ". So every
# card carries meta.internal, which db.list_messages excludes by default and only the staff drawer
# opts out of, and which db.list_customer_events now excludes from the customer's notification bell
# as well. Same mechanism as the view card, no schema change: msg_type="system" is already in the
# CHECK constraint and already renders as a card in both frontends.
def _actor_name(by: Optional[str]) -> str:
    """A person's name for a card, from whatever the staff tool stamped into `by`.

    It sends an address today ("kyle@wetreadwell.com"), which reads badly mid-sentence, so an
    address is shortened to a name. A value with no "@" is passed straight through rather than run
    through _estimator_name as well: that helper capitalises each word, so a typed "Kyle Smith"
    came back "Kyle smith" — mangling a real name to normalise an address we were not given.

    "" when there is nothing, so a caller can choose a sentence with no subject instead of naming
    the card after None."""
    v = (by or "").strip()
    if not v:
        return ""
    if "@" not in v:
        return v
    return _estimator_name(v) or v


def _crm_card(proposal_id: str, body: str, what: str) -> None:
    """Record a staff action in the project's thread, for staff eyes only.

    GUARDED, ALWAYS, and the guard is the point rather than defensiveness. Every caller has
    already made the state change the estimator pressed the button for — the estimator is
    assigned, the deposit chase is off, the job is filed as lost. An unguarded write means a
    database blip on a courtesy row returns a 500 from an endpoint that ALREADY did the work: the
    drawer says "couldn't save", the rep believes it, and they either give up or do it twice. Same
    posture and the same reason as the contacts prompts at admin_deposit_received and api_approve.
    A missing card costs a reader one line of history; undoing the action costs them the action.

    author_kind is "staff" — NOT "customer", which would be a quiet corruption rather than a
    cosmetic slip: db.list_all_portal_proposals reads the newest customer-authored row as
    `customer_replied_at`, so a mislabelled card would tell the board and the digest that the
    customer had answered when nobody had.

    `crm` names WHICH action alongside `internal`, the way the view card carries `view`: internal
    is what enforces the visibility, and a bare flag would leave four different rows
    indistinguishable to anything that later wants to render or count one kind of them."""
    try:
        db.add_message(proposal_id, "staff", None, body,
                       msg_type="system", meta={"crm": what, "internal": True})
    except Exception as exc:  # noqa: BLE001 — the action is done; do not undo it over a card
        log.warning("could not post the %s card for %s: %s", what, proposal_id, exc)


@app.post("/api/admin/proposal/{proposal_id}/assign")
async def admin_assign(proposal_id: str, request: Request) -> JSONResponse:
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    # The row is KEPT now (this was `if not db.get_proposal(...)`, which threw it away) because a
    # handover is the thing a reader needs to see and the outgoing estimator exists nowhere else:
    # one line later set_assigned_estimator has overwritten them.
    p = db.get_proposal(proposal_id)
    if not p:
        return _json({"ok": False, "error": "not_found"}, 404)
    body = await _body(request)
    email = (parseaddr(str(body.get("estimator_email") or ""))[1] or "").strip().lower()
    if not email:
        return _json({"ok": False, "error": "invalid_estimator"}, 400)
    by = _cap(body.get("by"), 120) or None
    previous = (p.get("assigned_estimator") or "").strip().lower()
    db.set_assigned_estimator(proposal_id, email)
    db.add_followup(proposal_id, "staff_note", {"action": "reassigned", "to": email}, by)
    # NO CARD WHEN NOBODY CHANGED. The drawer's picker posts the whole form, so re-saving a
    # project — or picking the estimator it already has — arrives here as a real request. A thread
    # carrying "reassigned from Kyle to Kyle" is worse than a thread carrying nothing: it is one
    # more line to read past, and it pushes the entries that do mean something off the screen.
    if previous != email:
        who = _actor_name(by)
        new_name = _estimator_name(email) or email
        if previous:
            old_name = _estimator_name(previous) or previous
            text = ("%s reassigned this from %s to %s." % (who, old_name, new_name) if who
                    else "Reassigned from %s to %s." % (old_name, new_name))
        else:
            text = ("%s assigned %s as the estimator." % (who, new_name) if who
                    else "%s assigned as the estimator." % new_name)
        _crm_card(proposal_id, text, "assign")
    return _json({"ok": True, "assigned_estimator": email})


@app.post("/api/admin/proposal/{proposal_id}/followup-automation")
async def admin_followup_automation(proposal_id: str, request: Request) -> JSONResponse:
    """Take a proposal off automation, or put it back on.

    This is the spec's "estimator manually removes the proposal from automation" —
    the deliberate human override, so it is sticky across re-publishes.

    TURNING IT ON ALSO LIFTS A PAUSE, since 2026-08-21, and until then it did not: this route
    called set_followup_enabled and nothing else, so on a paused project the switch flipped a flag
    and the cadence stayed stopped. Two things were wrong with that. The button's own title in the
    drawer is "Start the follow-up cadence from today", which it then did not do — followup_disabled_at
    and followup_paused_until both suppress a send, and clearing one of two stops is not a start.
    And it is the control an estimator reaches for when they cannot find a way out of a hold, which
    is the state every held SENT bid was in until today, so the one obvious workaround failed
    silently too.

    Only on the way ON, and only when there is a pause to lift. Turning automation OFF must not
    touch the date: a paused project that somebody also takes off automation has two separate
    decisions on it, and resuming while switching off would be this route inventing a third."""
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    p = db.get_proposal(proposal_id)
    if not p:
        return _json({"ok": False, "error": "not_found"}, 404)
    body = await _body(request)
    enabled = bool(body.get("enabled"))
    by = _cap(body.get("by"), 120) or None
    # Read through _followup_state, which is what the drawer's switch is DRAWN from, so the card
    # and the switch can never disagree about what "on" meant a moment ago.
    was = _followup_state(p)["enabled"]
    # Read BEFORE the writes, for the reason admin_set_status gives: after them the comparison is
    # against what this request just stored.
    was_paused = bool(p.get("followup_paused_until"))
    db.set_followup_enabled(proposal_id, enabled)
    if enabled and was_paused:
        db.resume_followups(proposal_id)
    db.add_followup(proposal_id, "staff_note",
                    {"action": "automation_on" if enabled else "automation_off"}, by)
    # NO CARD WHEN THE SWITCH DID NOT MOVE — a second tab, a double click, or a drawer re-save all
    # post the state the project already has.
    #
    # One deliberate silence: switching a never-enrolled legacy proposal OFF does write a
    # followup_disabled_at, but nothing is chasing a proposal with no followup_enrolled_at, so the
    # switch already read off and there is no news in a line confirming it.
    if enabled != was:
        who = _actor_name(by)
        if enabled:
            text = ("%s turned follow-up automation back on." % who if who
                    else "Follow-up automation was turned back on.")
        else:
            text = ("%s turned follow-up automation off." % who if who
                    else "Follow-up automation was turned off.")
        _crm_card(proposal_id, text, "automation")
    # THE PAUSE GETS ITS OWN LINE, and it is outside the switch-moved guard on purpose. Lifting a
    # pause is a change to what the CUSTOMER receives, not to a flag: a project paused until
    # December starts being emailed again today. That has to be readable even in the case where the
    # switch itself did not move (a re-save, or a project enrolled-and-enabled but held), because
    # "why did they get a reminder in September?" is answered by this line and by nothing else.
    if enabled and was_paused:
        who = _actor_name(by)
        _crm_card(proposal_id,
                  ("%s lifted the follow-up pause, so the reminders start again." % who if who
                   else "The follow-up pause was lifted, so the reminders start again."),
                  "automation_resumed")
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
    by = _cap(body.get("by"), 120) or None
    row = _log_staff_followup(proposal_id, kind, note, by)
    # A call, email or text logged from the drawer IS the follow-up an open task is asking for, so
    # it resolves that task as "called" — otherwise the task would sit pending and, being the one
    # open task, block every later reminder on this proposal. A NOTE is not contact with the
    # customer and leaves the task alone. Guarded: the log is recorded either way.
    if kind in ("staff_call", "staff_email", "staff_text"):
        try:
            tid = db.pending_followup_task_id(proposal_id)
            if tid is not None:
                db.decide_followup_task(tid, "called", by,
                                        {"kind": kind, "note": note, "via": "drawer_log"})
                _stamp_touch(proposal_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("[followup] could not resolve the open task on %s: %s", proposal_id, exc)
    return _json({"ok": True, "followup": {
        "kind": row["kind"], "detail": row.get("detail") or {},
        "by": row.get("created_by"), "created_at": _iso(row.get("created_at"))}})


def _log_staff_followup(proposal_id: str, kind: str, note: Optional[str], by: Optional[str],
                        extra: Optional[dict] = None) -> dict:
    """Record an estimator's own outreach and put it in the thread. Shared by the drawer's "log a
    call" and a follow-up task's "I called them instead", so the two cannot record it differently.

    The row is a staff touch (db._STAFF_FOLLOWUP_SQL), which is what now re-anchors the follow-up
    schedule: before 2026-10-23 logging a call removed the project from the morning digest and
    changed nothing else, so the estimator was told to follow up the next day anyway."""
    detail = {"note": note}
    detail.update(extra or {})
    row = db.add_followup(proposal_id, kind, detail, by)
    # THE MOST VALUABLE OF THESE CARDS, because until now a phone call left no mark anywhere a
    # person reads. portal_followups recorded it — the digest queries it and stops recommending a
    # chase — but nothing rendered it, so the estimator who rang on Tuesday and the estimator who
    # forgot looked identical in the thread, and the next person rang the customer again.
    #
    # No no-op case to check, and there is nothing to compare against if there were: every call
    # logs a NEW event, and logging a second call after the first is the normal way to use this.
    who = _actor_name(by)
    # .get, not [kind] — the sentence is built OUTSIDE _crm_card's guard, so a fifth kind added to
    # _STAFF_FOLLOWUP_KINDS without a noun here would KeyError after add_followup had already
    # succeeded, which is the exact failure the guard exists to prevent. A vaguer card is the
    # cheaper wrong answer.
    noun = _FOLLOWUP_NOUNS.get(kind) or "a follow-up"
    text = ("%s logged %s" % (who, noun)) if who else ("%s was logged" % noun.capitalize())
    # The note is what makes the card worth reading — "they're waiting on the GC's schedule" is
    # the whole content of the call. Quoted so it stays distinguishable from our own sentence.
    _crm_card(proposal_id, text + (' — "%s"' % note if note else "."), "followup")
    return row


@app.get("/api/admin/proposal/{proposal_id}/followups")
def admin_list_followups(proposal_id: str, request: Request) -> JSONResponse:
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    return _json({"ok": True, "followups": [{
        "kind": f["kind"], "detail": f.get("detail") or {},
        "by": f.get("created_by"), "created_at": _iso(f.get("created_at")),
    } for f in db.list_followups(proposal_id)]})


# ── remind-only follow-up tasks (2026-10-23) ──────────────────────────────────
# Hanz's decisions: every proposal is REMIND-ONLY (1, 2). When a customer reminder comes due the
# worker raises a follow-up task and tells the estimator; nothing reaches the customer until the
# estimator sends it from here. The staff tool proxies these with its service token, exactly like
# every other /api/admin route.
#
# EVERY decision is a touch that re-anchors the schedule (followup_rules._customer_plan): the next
# reminder lands one interval after the decision, not one interval after an anchor nobody moved.
# Each also clears a snooze date, except the snooze itself.
_TASK_LOG_KINDS = {"call": "staff_call", "text": "staff_text", "email": "staff_email",
                   "note": "staff_note"}
_SNOOZE_MAX_DAYS = 366


def _task_draft(detail: dict) -> dict:
    """The draft a task opens with: what was stored on it, or the stage's template as a floor."""
    d = detail.get("draft") if isinstance(detail.get("draft"), dict) else {}
    template = detail.get("template") or "checkin"
    if d.get("title") and d.get("body"):
        # The normal case: every task stores its draft when it is raised, so the bell's feed does
        # not read the settings row once per task.
        cta = d.get("cta") or (followup_settings.DEFAULT_TEMPLATES.get(template)
                               or followup_settings.DEFAULT_TEMPLATES["checkin"])["cta"]
        return {"title": d["title"], "body": d["body"], "cta": cta,
                "source": d.get("source") or "default"}
    base = followup_draft.default_draft(template, _followup_cfg())
    return {"title": d.get("title") or base["title"], "body": d.get("body") or base["body"],
            "cta": d.get("cta") or base["cta"], "source": d.get("source") or "default"}


_DECISION_LABEL = "Decide the other scopes"


def _task_group_fields(row: dict) -> dict:
    """Scope groups: which group a task belongs to, the scopes it covers, and the proposal a staff
    link should OPEN. A group reminder lives on the group's home scope, which may since have been
    approved, so the link goes to the first scope it covers that was open when it was raised; a
    decision task opens the approved scope it is filed on. {} -> nulls for every other task."""
    d = row.get("detail") or {}
    scopes = [s for s in (d.get("scopes") or []) if isinstance(s, dict)]
    link = row.get("proposal_id")
    if d.get("scope_group") and d.get("stage") != followup_rules.DECISION_STAGE:
        ids = [s.get("proposal_id") for s in scopes
               if (s.get("status") or "") in followup_rules.OPEN_STATUSES]
        if ids and link not in ids:
            link = ids[0]
    return {"scope_group": d.get("scope_group") or None, "scopes": scopes,
            "approved_scope": d.get("approved_scope") or None,
            "link_proposal_id": link}


def _task_view(row: dict) -> dict:
    """One task, as every task endpoint returns it (the shape the staff tool renders)."""
    d = row.get("detail") or {}
    template = d.get("template") or "checkin"
    stage = d.get("stage") or followup_rules.task_stage(template)
    out = {
        "id": row.get("id"),
        "proposal_id": row.get("proposal_id"),
        "status": d.get("status") or "pending",
        "template": template,
        "label": (_DECISION_LABEL if stage == followup_rules.DECISION_STAGE
                  else followup_settings.label(template)),
        "stage": stage,
        "rule_key": d.get("rule_key"),
        "include_status_ask": bool(d.get("include_status_ask")),
        "due_at": _iso(row.get("created_at")),
        "decided_by": d.get("decided_by"),
        "decided_at": d.get("decided_at"),
        # Set when the worker reopened a send it could not confirm (_recover_stuck_sends): the
        # email MAY already have gone, so the compose box should say "check the thread first".
        "send_unconfirmed": bool(d.get("send_unconfirmed")),
        "draft": _task_draft(d),
    }
    out.update(_task_group_fields(row))
    return out


def _task_summary(raw) -> Optional[dict]:
    """The pipeline's compact form of the open task (the jsonb pending_task column)."""
    if not isinstance(raw, dict) or not raw.get("id"):
        return None
    d = raw.get("detail") or {}
    template = d.get("template") or "checkin"
    stage = d.get("stage") or followup_rules.task_stage(template)
    out = {"id": raw.get("id"), "status": d.get("status") or "pending", "template": template,
           "label": (_DECISION_LABEL if stage == followup_rules.DECISION_STAGE
                     else followup_settings.label(template)),
           "stage": stage,
           "due_at": _iso(raw.get("created_at"))}
    if d.get("scope_group"):
        # Only for a group's task, so an ordinary proposal's summary keeps its exact shape.
        out.update(_task_group_fields(raw))
        out["proposal_id"] = raw.get("proposal_id")
    return out


def _followup_plan(p: dict) -> dict:
    """This proposal's own schedule, for the drawer. None everywhere = the global cadence."""
    return {"mode": followup_rules.followup_mode(p),
            "interval_days": p.get("followup_interval_days"),
            "stop_after": p.get("followup_stop_after"),
            "next_at": _iso(p.get("followup_next_at")),
            "anchor_at": _iso(p.get("followup_anchor_at"))}


def _followup_cfg() -> dict:
    try:
        return followup_settings.merge(db.get_settings(followup_settings.ROW_ID))
    except Exception as exc:  # noqa: BLE001
        log.warning("[followup] could not read the cadence, using the shipped one: %s", exc)
        return followup_settings.defaults()


def _load_task(task_id: str):
    """(task, proposal, error_response). Exactly one of the first two pairs is usable."""
    try:
        tid = int(task_id)
    except (TypeError, ValueError):
        return None, None, _json({"ok": False, "error": "not_found"}, 404)
    task = db.get_followup_task(tid)
    if not task:
        return None, None, _json({"ok": False, "error": "not_found"}, 404)
    return task, db.get_proposal(task["proposal_id"]), None


def _not_pending(task: dict) -> Optional[JSONResponse]:
    status = (task.get("detail") or {}).get("status") or "pending"
    if status != "pending":
        return _json({"ok": False, "error": "already_decided", "status": status}, 409)
    return None


def _is_decision(task: dict) -> bool:
    return (task.get("detail") or {}).get("stage") == followup_rules.DECISION_STAGE


def _refuse_decision(task: dict) -> Optional[JSONResponse]:
    """A "decide the other scopes" task is not a reminder: it has no email to send, nothing to
    skip to, and a call or a snooze does not answer it. It is answered at .../scope-decision."""
    if _is_decision(task):
        return _json({"ok": False, "error": "decision_task",
                      "message": "Answer this task with keep, close or leave."}, 409)
    return None


def _judge_task(task: dict, p: Optional[dict]) -> tuple:
    """(live, why, subject) -- followup_worker.judge_task for a scope group's task (judged against
    the GROUP, not the home row), the plain rule for every other task. A group that cannot be read
    is not live: a send must fail closed."""
    if followup_worker.is_group_task(task.get("detail")):
        try:
            return followup_worker.judge_task(task, _now_utc())
        except Exception as exc:  # noqa: BLE001
            log.warning("[followup] could not judge group task %s: %s", task.get("id"), exc)
            return False, "group_unreadable", None
    live, why = followup_rules.task_is_live(task.get("detail") or {}, p, _now_utc())
    return live, why, p


def _stamp_touch(proposal_id: str, clear_next: bool = True) -> None:
    """Re-anchor on a decision. Guarded: the decision is already recorded, and for send and
    called the staff_* row it wrote re-anchors the schedule on its own (db._STAFF_FOLLOWUP_SQL)."""
    try:
        db.stamp_followup_anchor(proposal_id, clear_next=clear_next)
    except Exception as exc:  # noqa: BLE001
        log.warning("[followup] could not stamp the follow-up anchor on %s: %s", proposal_id, exc)


def _parse_when(raw) -> Optional[datetime]:
    """A snooze date: 'YYYY-MM-DD' (8am Central that day) or a full ISO timestamp."""
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        if len(s) == 10:
            d = date.fromisoformat(s)
            return datetime(d.year, d.month, d.day, followup_rules.SEND_START_HOUR,
                            tzinfo=followup_rules.BUSINESS_TZ)
        when = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=followup_rules.BUSINESS_TZ)


def _future_when(raw) -> tuple:
    """(datetime, None) for a usable snooze date, or (None, error_code)."""
    when = _parse_when(raw)
    if when is None:
        return None, "invalid_date"
    now = _now_utc()
    if when <= now:
        return None, "date_in_past"
    if (when - now).days > _SNOOZE_MAX_DAYS:
        return None, "date_too_far"
    return when, None


@app.get("/api/admin/followup-tasks")
def admin_followup_tasks(request: Request) -> JSONResponse:
    """Every OPEN follow-up task, oldest first — the feed the staff tool's bell polls.

    `?estimator=<email>` narrows it to one estimator's queue (matched on the same coalesce the
    board uses: assigned estimator, then the draft's owner, then whoever published)."""
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    who = str(request.query_params.get("estimator") or "").strip().lower()
    out = []
    for r in db.list_pending_followup_tasks():
        est = (r.get("estimator_email") or "").strip().lower()
        if who and est != who:
            continue
        v = _task_view(r)
        v.update({"project_name": r.get("project_name"), "customer_name": r.get("customer_name"),
                  "customer_email": r.get("customer_email"), "estimator_email": est or None})
        out.append(v)
    return _json({"ok": True, "tasks": out})


@app.get("/api/admin/followup-task/{task_id}")
def admin_followup_task(task_id: str, request: Request) -> JSONResponse:
    """One task with what the compose box needs: the draft, who it would go to, and whether it can
    still be sent at all (`live`; `why` names the reason it cannot)."""
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    task, p, err = _load_task(task_id)
    if err:
        return err
    live, why, subject = _judge_task(task, p)
    try:
        if _is_decision(task):
            to = []                         # a staff task: nobody outside is emailed
        elif followup_worker.is_group_task(task.get("detail")):
            # Everybody the group email would reach, across the scopes it covers.
            to = []
            for m in ((subject or {}).get("_open_members") or []):
                for e in followup_worker._customer_recipients(m):
                    if e and e not in to:
                        to.append(e)
        else:
            to = followup_worker._customer_recipients(p) if p else []
    except Exception:  # noqa: BLE001 — labels only
        to = []
    return _json({"ok": True, "task": _task_view(task), "live": live, "why": why or None,
                  "recipients": to,
                  "proposal": ({"proposal_id": p["proposal_id"],
                                "project_name": p.get("project_name"),
                                "customer_name": p.get("customer_name"),
                                "customer_email": p.get("customer_email"),
                                "followup_plan": _followup_plan(p)} if p else None)})


@app.get("/api/admin/followup-task/{task_id}/draft")
def admin_followup_task_draft(task_id: str, request: Request) -> JSONResponse:
    """The compose box's starting point, and the request for an AI draft.

    `draft` is always usable on its own: the AI draft if one has been accepted, otherwise the
    stage's template — the words the automatic email used to send. `ai_request` is what the staff
    tool runs through its Claude CLI (`_autofill_via_cli(input, system)`; this container has no
    CLI) before posting the model's answer back to the POST below. The thread inside `input` is
    the CUSTOMER-VISIBLE thread only and is JSON-quoted data in the user slot."""
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    task, p, err = _load_task(task_id)
    if err:
        return err
    if not p:
        return _json({"ok": False, "error": "not_found"}, 404)
    d = task.get("detail") or {}
    template = d.get("template") or "checkin"
    draft = _task_draft(d)
    try:
        messages = db.list_messages(p["proposal_id"])          # default: no internal rows
    except Exception as exc:  # noqa: BLE001 — without the thread the default still stands
        log.warning("[followup] could not read the thread for task %s: %s", task_id, exc)
        messages = []
    if draft["source"] != "ai" and (d.get("status") or "pending") == "pending":
        # An in-process hook, if one is ever wired (followup_draft.set_run_claude). Unset in v1.
        got = followup_draft.draft_for(p, template, messages, _followup_cfg())
        if got.get("source") == "ai":
            draft = got
            try:
                db.set_followup_task_draft(task["id"], got)
            except Exception as exc:  # noqa: BLE001
                log.warning("[followup] could not store the draft on task %s: %s", task_id, exc)
    return _json({"ok": True, "draft": draft,
                  "ai_request": {"system": followup_draft.DRAFT_SYSTEM,
                                 "input": followup_draft.build_input(p, template, messages)}})


@app.post("/api/admin/followup-task/{task_id}/draft")
async def admin_followup_task_draft_submit(task_id: str, request: Request) -> JSONResponse:
    """Hand back what the model wrote. Kept only if it passes every check in
    followup_draft.clean_model_output; anything else returns the default with `fallback: true`,
    and a 200 either way — the compose box must always open with something sendable."""
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    task, p, err = _load_task(task_id)
    if err:
        return err
    blocked = _not_pending(task)
    if blocked:
        return blocked
    body = await _body(request)
    d = task.get("detail") or {}
    template = d.get("template") or "checkin"
    got = followup_draft.clean_model_output(body.get("model_output"), template)
    if not got:
        return _json({"ok": True, "fallback": True, "draft": _task_draft(d)})
    draft = dict(got, source="ai")
    try:
        db.set_followup_task_draft(task["id"], draft)
    except Exception as exc:  # noqa: BLE001 — the estimator still gets it on screen
        log.warning("[followup] could not store the AI draft on task %s: %s", task_id, exc)
    return _json({"ok": True, "fallback": False, "draft": draft})


@app.post("/api/admin/followup-task/{task_id}/send")
async def admin_followup_task_send(task_id: str, request: Request) -> JSONResponse:
    """Send the estimator's reviewed follow-up to the customer. Body: {title, body, by, cta?}.

    Order is the whole safety story:
      1. validate the text with the SAME rules a saved customer template gets ({link} required,
         no HTML, no unknown placeholders, length-limited) — 400 otherwise;
      2. re-check the proposal is still in scope — signed and paid, closed lost, automation off,
         paused, re-sent or moved stage all return 409 and expire the task. A task raised on
         Tuesday must never email a customer who signed on Wednesday;
      3. CLAIM the task (pending -> sending) before emailing, so a double click or a second tab
         finds nothing to claim and sends nothing;
      4. send; if nothing went out, put the task back to pending and return 502;
      5. finish it (sending -> sent), record it as a staff email (a touch: the schedule re-anchors
         on it), stamp the anchor, echo it into the customer's thread like every reminder, and
         card it for staff. Each of these is guarded: the email has gone, so the answer is 200,
         with `warnings` naming any write that failed."""
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    task, p, err = _load_task(task_id)
    if err:
        return err
    blocked = _not_pending(task) or _refuse_decision(task)
    if blocked:
        return blocked
    body = await _body(request)
    by = _cap(body.get("by"), 120) or None
    d = task.get("detail") or {}
    template = d.get("template") or "checkin"
    if template not in followup_settings.TEMPLATE_KEYS:
        template = "checkin"
    raw_body = body.get("body")
    if not isinstance(raw_body, str) or not raw_body.strip():
        return _json({"ok": False, "error": "empty"}, 400)
    try:
        tpl = followup_settings.validate_template(
            template, {"title": body.get("title"), "body": raw_body, "cta": body.get("cta")})
    except followup_settings.ValidationError as exc:
        return _json({"ok": False, "error": "invalid_draft", "message": str(exc)}, 400)

    live, why, subject = _judge_task(task, p)
    if not live:
        if why != "group_unreadable":           # unreadable is not "moved on": leave it open
            try:
                db.decide_followup_task(task["id"], "expired", None, {"reason": why})
            except Exception as exc:  # noqa: BLE001 — the refusal is the important half
                log.warning("[followup] could not expire task %s: %s", task_id, exc)
        return _json({"ok": False, "error": "out_of_scope", "reason": why}, 409)
    if followup_worker.is_group_task(d):
        return _send_group_task(task, subject, tpl, by)

    # Claimed as `sending`, NOT `sent` (audit, 2026-10-23). `sent` before the email meant that a
    # failed send whose reopen ALSO failed left a task that read as sent, refused every retry, and
    # blocked its occurrence for ever. `sending` is still not pending — a double click finds
    # nothing to claim — but it is never final: success finishes it below, failure reopens it, and
    # if both writes are lost the worker settles it (followup_worker._recover_stuck_sends).
    sent_text = {"title": tpl["title"], "body": tpl["body"]}
    claimed = db.decide_followup_task(task["id"], "sending", by,
                                      {"sent": sent_text, "sending_at": _now_utc().isoformat()})
    if not claimed:
        return _json({"ok": False, "error": "already_decided"}, 409)
    pid = p["proposal_id"]
    ask = bool(d.get("include_status_ask")) and template != "deposit_nudge"
    try:
        sent = followup_worker.send_task_email(p, template, ask, tpl, d.get("rule_key") or "task")
    except Exception as exc:  # noqa: BLE001
        log.warning("[followup] task %s send raised: %s", task_id, exc)
        sent = False
    if not sent:
        _reopen_after_failed_send(task["id"])
        return _json({"ok": False, "error": "send_failed"}, 502)

    # THE EMAIL HAS GONE. Everything below is bookkeeping and every step is guarded: an unguarded
    # write here turned a delivered email into a 500 (the estimator believes it failed and sends it
    # again) AND skipped the anchor stamp, the thread echo and the card after it. A step that fails
    # is named in `warnings` instead.
    warnings = []
    done = None
    try:
        done = db.decide_followup_task(task["id"], "sent", by, {"sent": sent_text},
                                       from_status="sending")
    except Exception as exc:  # noqa: BLE001 — the staff_email row below lets the worker finish it
        log.error("[followup] task %s sent but could not be marked sent: %s", task_id, exc)
    if not done:
        warnings.append("task_not_finalised")
    try:
        # Written right after the send on purpose: it is a touch (re-anchors the schedule via
        # db._STAFF_FOLLOWUP_SQL) AND the evidence the worker's recovery reads to finish a task
        # whose `sent` write was lost.
        db.add_followup(pid, "staff_email",
                        {"note": tpl["title"], "task_id": task["id"], "followup_task": True}, by)
    except Exception as exc:  # noqa: BLE001
        log.error("[followup] task %s sent but the staff_email row failed: %s", task_id, exc)
        warnings.append("touch_not_recorded")
    _stamp_touch(pid)
    followup_worker._echo_to_thread(
        pid, followup_rules.Due(d.get("rule_key") or "task", "customer", template),
        {"task_id": task["id"]})
    who = _actor_name(by)
    _crm_card(pid, ('%s sent a follow-up email — "%s"' % (who, tpl["title"])) if who
              else ('A follow-up email was sent — "%s"' % tpl["title"]), "followup_sent")
    view = _task_view(done or claimed)
    view["status"] = "sent"                 # the email went, whatever the row managed to record
    out = {"ok": True, "task": view}
    if warnings:
        out["warnings"] = warnings
    return _json(out)


def _send_group_task(task: dict, view: dict, tpl: dict, by: Optional[str]) -> JSONResponse:
    """The send endpoint for a scope group's reminder: ONE email PER RECIPIENT, listing only the
    open scopes that recipient is on, with the estimator's edited text (send_group_task_email).

    The same order and the same guards as the single send: claim (pending -> sending), send,
    reopen on total failure (502), then finish and record -- every bookkeeping step guarded,
    because the email has gone. The touch (staff_email row + anchor stamp) is written on the HOME
    scope, the group's task row, which group_view re-anchors the whole group from; the customer
    echo goes on every scope that reached somebody, since each thread is one they read."""
    d = task.get("detail") or {}
    template = d.get("template") or "checkin"
    sent_text = {"title": tpl["title"], "body": tpl["body"]}
    claimed = db.decide_followup_task(task["id"], "sending", by,
                                      {"sent": sent_text, "sending_at": _now_utc().isoformat()})
    if not claimed:
        return _json({"ok": False, "error": "already_decided"}, 409)
    try:
        delivered = followup_worker.send_group_task_email(view, tpl)
    except Exception as exc:  # noqa: BLE001
        log.warning("[followup] group task %s send raised: %s", task["id"], exc)
        delivered = {}
    if not delivered:
        _reopen_after_failed_send(task["id"])
        return _json({"ok": False, "error": "send_failed"}, 502)

    home = task["proposal_id"]
    warnings = []
    done = None
    try:
        done = db.decide_followup_task(task["id"], "sent", by,
                                       {"sent": sent_text, "delivered": sorted(delivered)},
                                       from_status="sending")
    except Exception as exc:  # noqa: BLE001
        log.error("[followup] group task %s sent but could not be marked sent: %s", task["id"], exc)
    if not done:
        warnings.append("task_not_finalised")
    try:
        db.add_followup(home, "staff_email",
                        {"note": tpl["title"], "task_id": task["id"], "followup_task": True,
                         "scope_group": d.get("scope_group")}, by)
    except Exception as exc:  # noqa: BLE001
        log.error("[followup] group task %s sent but the staff_email row failed: %s",
                  task["id"], exc)
        warnings.append("touch_not_recorded")
    _stamp_touch(home)
    reached = []
    for pids in delivered.values():
        for pid in pids:
            if pid not in reached:
                reached.append(pid)
    for pid in reached:
        followup_worker._echo_to_thread(
            pid, followup_rules.Due(d.get("rule_key") or "task", "customer", template),
            {"task_id": task["id"]})
    who = _actor_name(by)
    _crm_card(home, ('%s sent a follow-up email for the whole project — "%s"' % (who, tpl["title"]))
              if who else ('A follow-up email was sent for the whole project — "%s"'
                           % tpl["title"]), "followup_sent")
    view_out = _task_view(done or claimed)
    view_out["status"] = "sent"
    out = {"ok": True, "task": view_out, "emailed": sorted(delivered)}
    if warnings:
        out["warnings"] = warnings
    return _json(out)


def _reopen_after_failed_send(task_id: int) -> None:
    """Put a `sending` task back after its email did not go. Tried twice: the likeliest failure is
    a dead pooled connection after a database restart (portal-pool-has-no-liveness-check), which
    the pool replaces on the next checkout. If both fail the task stays `sending` and the worker
    reopens it within SENDING_STALE_MINUTES — never `sent`, never lost."""
    for attempt in (1, 2):
        try:
            db.reopen_followup_task(task_id)
            return
        except Exception as exc:  # noqa: BLE001
            level = log.warning if attempt == 1 else log.error
            level("[followup] task %s did not send; reopen attempt %d failed: %s",
                  task_id, attempt, exc)


@app.post("/api/admin/followup-task/{task_id}/skip")
async def admin_followup_task_skip(task_id: str, request: Request) -> JSONResponse:
    """Not now. Body: {by, note?}. Still a touch: an explicit human "not yet" restarts the
    interval, so the estimator is not asked again tomorrow about the same customer."""
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    task, p, err = _load_task(task_id)
    if err:
        return err
    blocked = _not_pending(task) or _refuse_decision(task)
    if blocked:
        return blocked
    body = await _body(request)
    by = _cap(body.get("by"), 120) or None
    note = _cap(body.get("note"), _NOTE_MAX) or None
    claimed = db.decide_followup_task(task["id"], "skipped", by, {"note": note})
    if not claimed:
        return _json({"ok": False, "error": "already_decided"}, 409)
    pid = task["proposal_id"]
    _stamp_touch(pid)
    warnings = []
    # Bookkeeping (`action`), not outreach: nobody contacted the customer, so the digest must not
    # read this as "chased" — the anchor stamp above is what moves the schedule. Guarded: the task
    # is already decided, so a 500 here would report a failure the estimator cannot retry (409).
    try:
        db.add_followup(pid, "staff_note",
                        {"action": "followup_skipped", "task_id": task["id"], "note": note}, by)
    except Exception as exc:  # noqa: BLE001
        log.warning("[followup] task %s skipped but the note row failed: %s", task_id, exc)
        warnings.append("touch_not_recorded")
    who = _actor_name(by)
    _crm_card(pid, ("%s skipped this follow-up for now" % who if who
                    else "Follow-up skipped for now")
              + (' — "%s"' % note if note else "."), "followup_skipped")
    out = {"ok": True, "task": _task_view(claimed)}
    if warnings:
        out["warnings"] = warnings
    return _json(out)


@app.post("/api/admin/followup-task/{task_id}/called")
async def admin_followup_task_called(task_id: str, request: Request) -> JSONResponse:
    """Followed up some other way. Body: {by, note?, kind?: call|text|email|note (default call)}.

    Goes through the drawer's own "log a follow-up" path (_log_staff_followup), so it records,
    cards and re-anchors exactly as logging a call from the drawer does."""
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    task, p, err = _load_task(task_id)
    if err:
        return err
    blocked = _not_pending(task) or _refuse_decision(task)
    if blocked:
        return blocked
    body = await _body(request)
    by = _cap(body.get("by"), 120) or None
    note = _cap(body.get("note"), 2000) or None
    kind = _TASK_LOG_KINDS.get(str(body.get("kind") or "call").strip().lower())
    if not kind:
        return _json({"ok": False, "error": "invalid_kind"}, 400)
    claimed = db.decide_followup_task(task["id"], "called", by, {"kind": kind, "note": note})
    if not claimed:
        return _json({"ok": False, "error": "already_decided"}, 409)
    pid = task["proposal_id"]
    # Stamp FIRST: it is what moves the schedule, and it must not depend on the history row.
    _stamp_touch(pid)
    out = {"ok": True, "task": _task_view(claimed)}
    try:
        # Guarded HERE, not inside _log_staff_followup: from the drawer, a failed insert is a real
        # failure the estimator should see and retry. From a task it is not — the task is already
        # decided (a retry is 409), and the call itself happened outside this system.
        _log_staff_followup(pid, kind, note, by, {"task_id": task["id"]})
    except Exception as exc:  # noqa: BLE001
        log.warning("[followup] task %s marked called but the log row failed: %s", task_id, exc)
        out["warnings"] = ["touch_not_recorded"]
    return _json(out)


@app.post("/api/admin/followup-task/{task_id}/snooze")
async def admin_followup_task_snooze(task_id: str, request: Request) -> JSONResponse:
    """Remind me on a specific day. Body: {until: 'YYYY-MM-DD' | ISO datetime, by}.

    The next task is raised ON that day (8am Central for a bare date), and the interval runs on
    from there. Written to the proposal BEFORE the task is resolved, so a failure leaves the task
    pending rather than resolved with no date behind it."""
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    task, p, err = _load_task(task_id)
    if err:
        return err
    blocked = _not_pending(task) or _refuse_decision(task)
    if blocked:
        return blocked
    body = await _body(request)
    by = _cap(body.get("by"), 120) or None
    when, bad = _future_when(body.get("until"))
    if bad:
        return _json({"ok": False, "error": bad}, 400)
    pid = task["proposal_id"]
    # Stamp first, then the date: the snooze is honoured only while it is later than the latest
    # touch, and this stamp IS that touch.
    db.stamp_followup_anchor(pid, clear_next=False)
    db.set_followup_plan(pid, next_at=when)
    claimed = db.decide_followup_task(task["id"], "snoozed", by, {"until": when.isoformat()})
    if not claimed:
        return _json({"ok": False, "error": "already_decided"}, 409)
    out = {"ok": True, "task": _task_view(claimed), "next_at": when.isoformat()}
    try:
        # Guarded for the same reason as skip: the task is decided and the date is written.
        db.add_followup(pid, "staff_note",
                        {"action": "followup_snoozed", "task_id": task["id"],
                         "until": when.isoformat()}, by)
    except Exception as exc:  # noqa: BLE001
        log.warning("[followup] task %s snoozed but the note row failed: %s", task_id, exc)
        out["warnings"] = ["touch_not_recorded"]
    day = when.astimezone(followup_rules.BUSINESS_TZ).date().isoformat()
    who = _actor_name(by)
    _crm_card(pid, ("%s will follow up on %s." % (who, day)) if who
              else ("Follow-up snoozed until %s." % day), "followup_snoozed")
    return _json(out)


_SCOPE_DECISIONS = ("keep", "close", "leave")


@app.post("/api/admin/followup-task/{task_id}/scope-decision")
async def admin_followup_task_scope_decision(task_id: str, request: Request) -> JSONResponse:
    """Answer a "decide what to do with the other scopes" task (Hanz, 2026-10-24, decision 9).
    Body: {action: 'keep'|'close'|'leave', scope_ids?: [...], reason?, note?, by}.

      close  the chosen scopes (default: every scope the task lists) are closed LOST through the
             SAME staff path the drawer's status route uses; reason and note are REQUIRED, exactly
             as they are there. Validated before anything is written.
      keep   the rare one: chase the remaining scopes again. Writes a `scope_group_resume` note,
             which followup_rules.group_view reads to restart the group chase, and re-anchors the
             group's home so the next reminder is one interval from now, not overdue at once.
      leave  only resolves the task. The siblings stay open and un-chased.

    Never closes anything the estimator did not pick. The task is claimed first (pending ->
    decided), so a double click answers it once."""
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    task, p, err = _load_task(task_id)
    if err:
        return err
    if not _is_decision(task):
        return _json({"ok": False, "error": "not_a_decision_task"}, 409)
    blocked = _not_pending(task)
    if blocked:
        return blocked
    body = await _body(request)
    by = _cap(body.get("by"), 120) or None
    action = str(body.get("action") or "").strip().lower()
    if action not in _SCOPE_DECISIONS:
        return _json({"ok": False, "error": "invalid_action"}, 400)
    d = task.get("detail") or {}
    gid = d.get("scope_group")
    listed = [s.get("proposal_id") for s in (d.get("scopes") or []) if isinstance(s, dict)]
    raw_ids = body.get("scope_ids")
    if raw_ids is None:
        chosen = list(listed)
    elif isinstance(raw_ids, list):
        chosen = [x for x in raw_ids if isinstance(x, str)]
    else:
        return _json({"ok": False, "error": "invalid_scope_ids"}, 400)
    if any(x not in listed for x in chosen):
        return _json({"ok": False, "error": "unknown_scope"}, 400)
    reason = note = None
    skipped: list = []
    if action == "close":
        reason = str(body.get("reason") or "").strip().lower() or None
        note = _cap(str(body.get("note") or "").strip(), _NOTE_MAX) or None
        if not chosen:
            return _json({"ok": False, "error": "no_scopes"}, 400)
        if reason not in _LOST_REASONS:
            return _json({"ok": False, "error": "invalid_reason"}, 400)
        if not note:
            return _json({"ok": False, "error": "note_required"}, 400)
        # The task's scope list was frozen when it was raised. A listed scope may have been
        # APPROVED (signed) or closed since, and closing a signed job as lost from here would stop
        # its deposit chase. So 'close' only ever touches scopes that are STILL OPEN right now: the
        # default drops the others (and reports them as skipped), naming one explicitly is refused.
        not_open = []
        for sid in chosen:
            row = db.get_proposal(sid)
            if not row or row.get("deleted_at") or not followup_rules.is_open(row):
                not_open.append(sid)
        if not_open and (raw_ids is not None or len(not_open) == len(chosen)):
            return _json({"ok": False, "error": "scope_not_open", "scope_ids": not_open}, 409)
        skipped = not_open
        chosen = [x for x in chosen if x not in not_open]

    claimed = db.decide_followup_task(task["id"], "decided", by,
                                      {"outcome": action, "scope_ids": chosen,
                                       "reason": reason, "note": note})
    if not claimed:
        return _json({"ok": False, "error": "already_decided"}, 409)
    pid = task["proposal_id"]
    who = _actor_name(by)
    out: dict = {"ok": True, "task": _task_view(claimed), "outcome": action}
    if action == "close":
        closed, failed = [], []
        for sid in chosen:
            try:
                row = db.get_proposal(sid)
                if not row:
                    failed.append(sid)
                    continue
                if row.get("deleted_at") or not followup_rules.is_open(row):
                    skipped.append(sid)         # approved or closed since the check above
                    continue
                if _staff_close_lost(sid, row, reason, note, by) is None:
                    closed.append(sid)
                else:
                    failed.append(sid)
            except Exception as exc:  # noqa: BLE001 — one scope must not stop the others
                log.warning("[followup] scope decision %s: closing %s failed: %s",
                            task["id"], sid, exc)
                failed.append(sid)
        out.update({"closed": closed, "failed": failed, "skipped": skipped})
        _crm_card(pid, ("%s closed the other scopes as lost." % who) if who
                  else "The other scopes were closed as lost.", "scope_decision")
    elif action == "keep":
        try:
            db.add_followup(pid, "staff_note",
                            {"action": "scope_group_resume", "scope_group": gid,
                             "task_id": task["id"], "scope_ids": chosen}, by)
        except Exception as exc:  # noqa: BLE001
            log.error("[followup] scope decision %s: the resume note failed: %s", task["id"], exc)
            out["warnings"] = ["resume_not_recorded"]
        try:
            home = followup_rules.group_home(followup_worker.group_members(gid, strict=False))
            if home:
                _stamp_touch(home["proposal_id"])
        except Exception as exc:  # noqa: BLE001
            log.warning("[followup] scope decision %s: could not re-anchor: %s", task["id"], exc)
        _crm_card(pid, ("%s chose to keep following up on the other scopes." % who) if who
                  else "Follow-ups on the other scopes will continue.", "scope_decision")
    else:
        _crm_card(pid, ("%s left the other scopes as they are; their reminders stay off." % who)
                  if who else "The other scopes were left as they are; their reminders stay off.",
                  "scope_decision")
    return _json(out)


@app.post("/api/admin/proposal/{proposal_id}/followup-plan")
async def admin_followup_plan(proposal_id: str, request: Request) -> JSONResponse:
    """This proposal's own schedule. Body (every key optional; null clears it):
    {interval_days: 1-90, stop_after: 1-60, next_at: 'YYYY-MM-DD' | ISO, by}.

    stop_after is the PRE-DECISION cap only; the deposit chase keeps its own (decision 5)."""
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    p = db.get_proposal(proposal_id)
    if not p:
        return _json({"ok": False, "error": "not_found"}, 404)
    body = await _body(request)
    by = _cap(body.get("by"), 120) or None
    kw: dict = {}
    for key, lo, hi in (("interval_days", 1, 90), ("stop_after", 1, 60)):
        if key in body:
            v = body.get(key)
            if v is None or v == "":
                kw[key] = None
                continue
            try:
                n = int(v)
            except (TypeError, ValueError):
                return _json({"ok": False, "error": "invalid_" + key}, 400)
            if isinstance(v, bool) or not lo <= n <= hi:
                return _json({"ok": False, "error": "invalid_" + key}, 400)
            kw[key] = n
    if "next_at" in body:
        if body.get("next_at") in (None, ""):
            kw["next_at"] = None
        else:
            when, bad = _future_when(body.get("next_at"))
            if bad:
                return _json({"ok": False, "error": bad}, 400)
            kw["next_at"] = when
    if not kw:
        return _json({"ok": False, "error": "nothing_to_change"}, 400)
    # A scope group is chased as ONE project, on its HOME scope's schedule (group_view reads the
    # interval, stop-after and snooze from there), so a change made from any open scope's drawer is
    # written to the home row. Everything else is unchanged.
    target = _followup_home_of(p) or proposal_id
    if kw.get("next_at") is not None:
        db.stamp_followup_anchor(target, clear_next=False)
    db.set_followup_plan(target, **kw)
    who = _actor_name(by)
    parts = []
    if "interval_days" in kw:
        parts.append("every %d days" % kw["interval_days"] if kw["interval_days"]
                     else "the standard interval")
    if "stop_after" in kw:
        parts.append("stop after %d reminders" % kw["stop_after"] if kw["stop_after"]
                     else "the standard reminder limit")
    if "next_at" in kw:
        parts.append(("next on %s" % kw["next_at"].astimezone(
            followup_rules.BUSINESS_TZ).date().isoformat()) if kw["next_at"]
            else "no fixed next date")
    _crm_card(proposal_id, ("%s changed this project's follow-ups: %s." % (who, ", ".join(parts)))
              if who else ("Follow-ups changed: %s." % ", ".join(parts)), "followup_plan")
    fresh = db.get_proposal(target) or p
    out = {"ok": True, "followup_plan": _followup_plan(fresh)}
    if target != proposal_id:
        out["home_proposal_id"] = target
    return _json(out)


def _followup_home_of(p: dict) -> Optional[str]:
    """The HOME scope's proposal_id when `p` is an OPEN live member of a scope group, else None.
    Guarded: an unreadable group leaves the write on `p` itself, which is what it always did."""
    gid = p.get("scope_group")
    if not gid or not followup_rules.is_open(p):
        return None
    try:
        members = followup_worker.group_members(gid)
    except Exception as exc:  # noqa: BLE001
        log.warning("[followup] scope group %s unreadable; plan written to %s: %s",
                    gid, p.get("proposal_id"), exc)
        return None
    if not any(m.get("proposal_id") == p.get("proposal_id") for m in members):
        return None
    home = followup_rules.group_home(members)
    return home.get("proposal_id") if home else None


@app.post("/api/admin/proposal/{proposal_id}/status")
async def admin_set_status(proposal_id: str, request: Request) -> JSONResponse:
    """Staff-side equivalent of the customer's project-status card: pause the chase,
    close the opportunity, or put it back in play. Same db helpers, so the two paths
    can never diverge.

    THE STAFF PATH IS STRICTER THAN THE CUSTOMER PATH, deliberately, since 2026-08-20. It used to
    be looser: `reason` was validated only `if reason`, so a sent project could be closed lost with
    no reason at all, or with a made-up one, while the tool's own draft route 422s both for a bid
    nobody has sent. The Lost tab has no column for a reason nobody recognises, so the same act
    from two drawers filed one bid under a reason and the other under "Not recorded". Both ends now
    refuse, and a comment saying WHY is required too.

    The customer's own form (api_project_status) stays lenient on both counts and must: it asks "if
    you don't mind saying", and a customer who does mind still has to be able to tell us they are
    out.

    A `delayed` WITH A REASON is one of Kyle's two hold answers — "Project on Hold" and "Small Bid
    <$25k - Pending" — arriving from the close-out dialog. Hanz's instruction was to route those
    through this status rather than build a mechanism: the card stays on the Active board and the
    cadence pauses, which is what this branch already does. A `delayed` with NO reason is the older
    "Mark delayed" control, which asks for a number of months and nothing else, and keeps working
    exactly as it did."""
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    p = db.get_proposal(proposal_id)
    if not p:
        return _json({"ok": False, "error": "not_found"}, 404)
    body = await _body(request)
    status = str(body.get("status") or "").strip().lower()
    by = _cap(body.get("by"), 120) or None
    # Read once for both branches. Capped and stripped here so "   " is the same as "" — an
    # estimator hitting space to get past a required field has said nothing, and a required field
    # that accepts a space is decoration.
    note = _cap(str(body.get("note") or "").strip(), _NOTE_MAX) or None

    who = _actor_name(by)

    if status == "delayed":
        try:
            months = int(body.get("months") or 0)
        except (TypeError, ValueError):
            months = 0
        if months not in _PAUSE_MONTHS:
            return _json({"ok": False, "error": "invalid_months"}, 400)
        hold = str(body.get("reason") or "").strip().lower() or None
        if hold is not None:
            if hold not in _HOLD_REASONS:
                return _json({"ok": False, "error": "invalid_reason"}, 400)
            if not note:
                return _json({"ok": False, "error": "note_required"}, 400)
        until = followup_rules.add_months(followup_rules.business_today(_now_utc()), months)
        # Read BEFORE the write, or the comparison below is against what we just stored.
        already = followup_rules.as_date(p.get("followup_paused_until")) == until
        db.pause_followups(proposal_id, until)
        # The reason and the comment ride in the SAME jsonb detail the note has always used
        # (portal_followups.detail), which is why this needed no DDL. Only when there is one:
        # nulls on every older pause would make "was a reason given" a truthiness test on a key
        # that is always present.
        detail = {"action": "paused", "months": months, "until": until.isoformat()}
        if hold:
            detail["reason"] = hold
            detail["note"] = note
        db.add_followup(proposal_id, "staff_note", detail, by)
        # Same no-op the CUSTOMER path guards, and for the same reason: pausing to the date it is
        # already paused to has changed nothing, so it is not worth a line. Only the card is
        # skipped — that path returns early, and matching it here would change what this endpoint
        # does to its caller.
        #
        # A HOLD ALWAYS GETS ITS CARD, even when the date is unchanged. The old rule was about a
        # pause carrying nothing but a date, where a second press said nothing new. A hold carries
        # a reason and somebody's sentence about why, and that sentence is the whole content of the
        # act — swallowing it because a colleague happened to pause to the same day last week would
        # lose the only part anyone reads.
        if not already or hold:
            window = _delay_window(months)
            label = _HOLD_REASON_LABELS.get(hold or "", "")
            _crm_card(proposal_id,
                      ("%s marked this delayed by about %s — follow-ups paused until %s."
                       % (who, window, until.isoformat())) if who
                      else ("Marked delayed by about %s — follow-ups paused until %s."
                            % (window, until.isoformat())),
                      "status_delayed")
            if label:
                _crm_card(proposal_id, 'On hold: %s — "%s"' % (label, note),
                          "status_on_hold")
    elif status == "closed_lost":
        reason = str(body.get("reason") or "").strip().lower() or None
        if reason not in _LOST_REASONS:
            return _json({"ok": False, "error": "invalid_reason"}, 400)
        if not note:
            return _json({"ok": False, "error": "note_required"}, 400)
        # An approved proposal is closeable from here as of 2026-08-10, per Hanz: a customer
        # can sign and the job still die, so staff need a way to file it as lost. The approval
        # is kept rather than erased (close_lost leaves the approved_* columns and the
        # portal_approvals rows alone) and moving the card back to Active restores 'approved',
        # so the old "a stray click must not clobber a win" objection is covered without
        # blocking the move. The only failure left is the row disappearing between the
        # get_proposal above and this write.
        was_closed = (p.get("proposal_status") or "") == "closed_lost"
        if not db.close_lost(proposal_id, reason):
            return _json({"ok": False, "error": "not_found"}, 404)
        # THE COMMENT HAS NO COLUMN AND DOES NOT NEED ONE. portal_proposals carries the reason and
        # the timestamp; this is the jsonb precedent the CUSTOMER's own note has used since the
        # follow-up system shipped — portal_followups.detail, plus the message meta below. No DDL,
        # and the thread is where a sentence about a job belongs anyway.
        db.add_followup(proposal_id, "staff_note",
                        {"action": "closed_lost", "reason": reason, "note": note}, by)
        # A closed job closed again with the SAME reason is a re-click, not news. A different
        # reason is: "we lost on price" and "they went with somebody else" are different stories
        # about the same job and the correction belongs in the thread.
        #
        # THE COMMENT IS ALWAYS NEWS, though, which is why it is ORed in. Two people can close the
        # same bid under "Not Low Bid" and only one of them wrote down that we were 12% over
        # Wilson; dropping the second card because the reason matched would drop the only part
        # worth reading. The reason-changed rule stays for the bare re-click it was written for.
        if not (was_closed and (p.get("closed_lost_reason") or None) == reason):
            label = _LOST_REASON_LABELS.get(reason or "", "")
            _crm_card(proposal_id,
                      ("%s marked this Closed–Lost." % who if who else "Marked Closed–Lost.")
                      + (" Reason: %s." % label if label else "")
                      + ' "%s"' % note,
                      "status_closed_lost")
        else:
            _crm_card(proposal_id,
                      ("%s added a note on the close: " % who if who else "Note on the close: ")
                      + '"%s"' % note,
                      "status_closed_lost")
    elif status == "active":
        # Both facts read before either write, for the reason the delayed branch gives.
        was_closed = p.get("proposal_status") == "closed_lost"
        was_paused = bool(p.get("followup_paused_until"))
        db.resume_followups(proposal_id)
        if was_closed:
            db.reopen_if_closed(proposal_id)
        db.add_followup(proposal_id, "staff_note", {"action": "reactivated"}, by)
        # Nothing to announce when the project was already active and unpaused: this is the
        # button's resting state, so the board re-saving a live project must not narrate it.
        if was_closed or was_paused:
            _crm_card(proposal_id,
                      "%s moved this back to Active." % who if who else "Moved back to Active.",
                      "status_active")
    else:
        return _json({"ok": False, "error": "invalid_status"}, 400)

    fresh = db.get_proposal(proposal_id) or p
    return _json({"ok": True, "proposal_status": fresh.get("proposal_status"),
                  "followup_state": _followup_state(fresh)})


def _staff_close_lost(proposal_id: str, p: dict, reason: str, note: str,
                      by: Optional[str]) -> Optional[JSONResponse]:
    """The staff closed-lost write for the scope-group decision task's 'close': the SAME three
    writes admin_set_status's closed_lost branch makes (db.close_lost, the `closed_lost` staff_note
    with reason + note, and the CRM card), in the same order and the same wording, so a scope
    closed from the decision task reads exactly like one closed from the drawer.

    A mirror rather than a refactor of admin_set_status on purpose: that route is working code
    whose closed_lost branch is pinned by test_close_lost_after_approval.py, and this change was not
    asked to touch it. If you change one, change both. The caller has validated `reason` and `note`.
    Returns an error response, or None when it was written."""
    who = _actor_name(by)
    # An approved proposal is closeable from here as of 2026-08-10, per Hanz: a customer
    # can sign and the job still die, so staff need a way to file it as lost. The approval
    # is kept rather than erased (close_lost leaves the approved_* columns and the
    # portal_approvals rows alone) and moving the card back to Active restores 'approved',
    # so the old "a stray click must not clobber a win" objection is covered without
    # blocking the move. The only failure left is the row disappearing between the
    # get_proposal above and this write.
    was_closed = (p.get("proposal_status") or "") == "closed_lost"
    if not db.close_lost(proposal_id, reason):
        return _json({"ok": False, "error": "not_found"}, 404)
    # THE COMMENT HAS NO COLUMN AND DOES NOT NEED ONE. portal_proposals carries the reason and
    # the timestamp; this is the jsonb precedent the CUSTOMER's own note has used since the
    # follow-up system shipped — portal_followups.detail, plus the message meta below. No DDL,
    # and the thread is where a sentence about a job belongs anyway.
    db.add_followup(proposal_id, "staff_note",
                    {"action": "closed_lost", "reason": reason, "note": note}, by)
    # A closed job closed again with the SAME reason is a re-click, not news. A different
    # reason is: "we lost on price" and "they went with somebody else" are different stories
    # about the same job and the correction belongs in the thread.
    #
    # THE COMMENT IS ALWAYS NEWS, though, which is why it is ORed in. Two people can close the
    # same bid under "Not Low Bid" and only one of them wrote down that we were 12% over
    # Wilson; dropping the second card because the reason matched would drop the only part
    # worth reading. The reason-changed rule stays for the bare re-click it was written for.
    if not (was_closed and (p.get("closed_lost_reason") or None) == reason):
        label = _LOST_REASON_LABELS.get(reason or "", "")
        _crm_card(proposal_id,
                  ("%s marked this Closed–Lost." % who if who else "Marked Closed–Lost.")
                  + (" Reason: %s." % label if label else "")
                  + ' "%s"' % note,
                  "status_closed_lost")
    else:
        _crm_card(proposal_id,
                  ("%s added a note on the close: " % who if who else "Note on the close: ")
                  + '"%s"' % note,
                  "status_closed_lost")
    return None


# ── deleting a project off the board, and putting it back ─────────────────────
# Hanz, 2026-08-24: "In the proposals tab under the Active Projects create a 'delete project'
# button", and "make sure there is a confirmation dialog". He had two sent test bids that no
# control in either app could take off the Active Projects board.
#
# WHY THIS END HAD TO GROW A ROUTE. The staff tool's own Trash button calls DELETE /api/draft/{id},
# which is one table — the draft. The board reads the PORTAL's rows
# (db.list_all_portal_proposals), whose join deliberately does not filter the draft's deleted_at
# because "trashing the draft does not retract the proposal the customer already has". So trashing
# a SENT project removed it from the Proposals Database and left its card on the board for good,
# and worse: the board's enrichment reads drafts.list_drafts(), which skips trashed rows, so the
# card silently lost its is_test flag, its bid total and its won mark and could move itself from
# the Test tab onto Active with nobody touching it. Hiding the portal row is what makes the card
# leave; the two halves have to happen together, which is what the tool's route now guarantees.
#
# A SOFT HIDE, not a delete, and the reversibility is the requirement rather than caution. The
# customer's link keeps working either way — it reads portal_proposals by TOKEN and renders the
# pinned draft_revisions snapshot, neither of which is filtered — because deleting a project off
# our board is not the same act as revoking what somebody was already sent.
@app.post("/api/admin/proposal/{proposal_id}/delete")
async def admin_delete_proposal(proposal_id: str, request: Request) -> JSONResponse:
    """Take this proposal off the staff board and stop its follow-ups. Reversible."""
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    p = db.get_proposal(proposal_id)
    if not p:
        # The tool reads this as "there is no portal row", which is the normal answer for a bid
        # nobody ever sent — so it must stay a 404 and must not be dressed up as an error.
        return _json({"ok": False, "error": "not_found"}, 404)
    body = await _body(request)
    by = _cap(body.get("by"), 120) or None
    if not db.delete_proposal(proposal_id):
        return _json({"ok": False, "error": "not_found"}, 404)
    # Bookkeeping, so `action` is set: last_staff_followup_at counts only rows WITHOUT one, and
    # deleting a project is not somebody chasing a customer.
    db.add_followup(proposal_id, "staff_note", {"action": "deleted"}, by)
    who = _actor_name(by)
    # An internal card, so the record survives into the restore. It is `internal`, which is what
    # keeps it out of the customer's copy of the thread (list_messages) and out of their
    # notification bell (list_customer_events) — the customer is told nothing about this.
    _crm_card(proposal_id,
              ("%s deleted this project. It is off the board and the follow-ups have stopped; "
               "the link the customer already has still works." % who) if who
              else ("Project deleted. It is off the board and the follow-ups have stopped; "
                    "the link the customer already has still works."),
              "status_deleted")
    return _json({"ok": True, "deleted": True})


@app.post("/api/admin/proposal/{proposal_id}/restore")
async def admin_restore_proposal(proposal_id: str, request: Request) -> JSONResponse:
    """Put a deleted proposal back on the board. THE CADENCE STAYS OFF — see db.restore_proposal."""
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    p = db.get_proposal(proposal_id)
    if not p:
        return _json({"ok": False, "error": "not_found"}, 404)
    body = await _body(request)
    by = _cap(body.get("by"), 120) or None
    # Read BEFORE the write, or "was this actually deleted" is a question about what we just did.
    # A restore of a live project is a no-op worth reporting rather than narrating: nothing moved,
    # so nothing goes in the thread.
    was_deleted = p.get("deleted_at") is not None
    if not db.restore_proposal(proposal_id):
        return _json({"ok": False, "error": "not_found"}, 404)
    if was_deleted:
        db.add_followup(proposal_id, "staff_note", {"action": "restored"}, by)
        who = _actor_name(by)
        _crm_card(proposal_id,
                  ("%s restored this project. The follow-up reminders stay off until somebody "
                   "switches them back on." % who) if who
                  else ("Project restored. The follow-up reminders stay off until somebody "
                        "switches them back on."),
                  "status_restored")
    return _json({"ok": True, "restored": True, "was_deleted": was_deleted})


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
    atts = uploads.sanitize(body.get("attachments"))
    # Same rule as the customer side: a photo on its own is a message.
    if not text and not atts:
        return _json({"ok": False, "error": "empty"}, 400)
    db.add_message(proposal_id, "staff", _cap(body.get("by"), 120) or "Treadwell", text,
                   msg_type="text", meta={"attachments": atts} if atts else None)
    link = f"{config.PUBLIC_BASE_URL}/p/{p['token']}"
    project = p.get("project_name") or "your proposal"
    rt = email_sender.proposal_reply_to(p["token"])
    # THE FILES GO WITH IT. Storing them on the message put them in the customer's thread, which
    # is what made this look finished -- but the notification email is a separate step on the same
    # request, and it was left as it was. Hanz: "When I attach an image and resend in the customer
    # portal, the image or files does not appear in the email".
    files = _mail_files(proposal_id, atts)
    for e in (db.get_recipients(proposal_id) or [p["customer_email"]]):
        email_sender.send_reply_notification(e, link, project, reply_to=rt, message=text,
                                             token=p["token"], attachments=files or None)
    return _json({"ok": True})


@app.post("/api/admin/proposal/{proposal_id}/upload")
async def admin_upload(proposal_id: str, request: Request) -> JSONResponse:
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    if not db.get_proposal(proposal_id):
        return _json({"ok": False, "error": "not_found"}, 404)
    return await _store_upload(request, proposal_id)


@app.get("/api/admin/proposal/{proposal_id}/file/{file_id}")
async def admin_file(proposal_id: str, file_id: str, request: Request):
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    return _serve_upload(proposal_id, file_id, internal=True)


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
    awaiting_group_send = False
    if add and email not in existing:
        if len(existing) >= MAX_RECIPIENTS:
            return _json({"ok": False, "error": "too_many_recipients"}, 400)
        db.add_recipient(proposal_id, email, by)
        # A SCOPE OF A GROUP NEVER SENDS THE ONE-SCOPE EMAIL. Before the combined send, nothing
        # goes: that send reads this recipients list, so the new contact is in it. After it, the
        # contact gets the group's email for the scopes they are on. Unreadable membership sends
        # nothing at all rather than falling through to the single-scope link below.
        try:
            group = _active_scope_group(p)
        except _GroupUnreadable as exc:
            log.error("followup-recipient %s: scope group unreadable, sent nothing: %s",
                      proposal_id, exc)
            group, awaiting_group_send = p.get("scope_group"), True
        if group and not awaiting_group_send:
            if not p.get("last_sent_at"):
                awaiting_group_send = True
            else:
                try:
                    _send_scope_links_to_contact(p, group, email)
                except Exception as exc:  # noqa: BLE001 — they are on the list; resend covers it
                    log.error("could not send the scope links to a newly added contact: %s", exc)
        elif not group:
            # They cannot reach the portal without the link, so adding somebody and not sending it
            # would put a contact on the list who can never open the thing they are a contact for.
            try:
                # WITH THE FILES THE OTHERS GOT. The same omission as the reply path, wearing a
                # different hat: without this, a contact added after the send is the only recipient
                # who has to ask what the photo was.
                email_sender.send_portal_link(
                    email, "", f"{config.PUBLIC_BASE_URL}/p/{p['token']}",
                    p.get("project_name") or "your project",
                    reply_to=email_sender.proposal_reply_to(p["token"]), token=p["token"],
                    attachments=_proposal_card_files(proposal_id) or None)
            except Exception as exc:  # noqa: BLE001 — they are on the list; the link can be re-sent
                log.error("could not send the portal link to a newly added contact: %s", exc)
    elif email not in existing:
        return _json({"ok": False, "error": "not_a_recipient"}, 404)

    if not db.set_followup_recipient(proposal_id, email, enabled):
        return _json({"ok": False, "error": "not_a_recipient"}, 404)
    out = {"ok": True, "email": email, "followups": enabled}
    if awaiting_group_send:
        out["awaiting_group_send"] = True
    return _json(out)


@app.get("/api/admin/signed-contract.pdf")
def admin_signed_contract_pdf(proposal_id: str, request: Request):
    """The executed contract, for staff and for the proposal tool.

    SERVICE_TOKEN-gated like every other /api/admin/* route here, because it serves a
    customer's signed agreement to anything that asks. The staff UI for a proposal lives in
    the OTHER repository (treadwell-proposal-tool frontend/js/portal.js) and is not touched
    from here; this endpoint is what a later link there will call.

    Lazy-builds on the same path the customer download uses, so an approval taken while the
    renderer was down produces a document the first time anybody asks for one, from either
    side."""
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    p = db.get_proposal(proposal_id) if proposal_id else None
    if not p:
        return _json({"ok": False, "error": "not_found"}, 404)
    pdf, err, status = _signed_contract_pdf(p)
    if pdf is None:
        return _json({"ok": False, "error": err or "No signed contract is available."}, status)
    return _signed_contract_response(pdf, p.get("project_name"))



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
        kind="deposit_received",
        reply_link=_staff_link(proposal_id), proposal_id=proposal_id,
        reply_to=email_sender.proposal_reply_to(p.get("token")),
        token=p.get("token"), project=p.get("project_name"),
    )
    _notify_customer(
        p, "Deposit received — thank you",
        f"<p>We've received your deposit for <strong>{html.escape(project)}</strong>.</p>"
        f"<p>Next: add your project contacts so we can schedule the work.</p>",
    )
    return _json({"ok": True})


@app.post("/api/admin/deposit/{deposit_id}/reveal")
async def admin_deposit_reveal(deposit_id: str, request: Request) -> JSONResponse:
    """Decrypt ONE deposit's routing/account numbers for a staff member who is looking at it in
    the CRM drawer. SERVICE_TOKEN-gated like every other /api/admin/* route (the tool re-checks
    its own nav-access rules before ever calling this), and additionally requires the acting
    staff member's email in the body so the audit line below can say who looked.

    Every branch that can fail returns before touching the numbers, and the response never
    appears in a log line -- only this JSON body carries the plaintext, and the caller sets
    Cache-Control: no-store."""
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    body = await _body(request)
    staff_email = (body.get("staff_email") or "").strip()
    if not staff_email:
        return _json({"ok": False, "error": "staff_email is required"}, 400)
    dep = db.get_deposit_bank_details(deposit_id)
    if not dep or dep.get("method") != "ach":
        return _json({"ok": False, "error": "not_found"}, 404)
    try:
        routing_number = bank_crypto.decrypt(dep.get("routing_number"))
        account_number = bank_crypto.decrypt(dep.get("account_number"))
    except (bank_crypto.BankKeyError, bank_crypto.BankDecryptError) as exc:
        log.warning("deposit reveal failed for %s: %s: %s", deposit_id, type(exc).__name__, exc)
        return _json({
            "ok": False,
            "error": "These numbers can't be decrypted right now — check PORTAL_BANK_KEY.",
        }, 503)
    # WHO / WHEN / WHICH deposit — never the numbers. Timestamp is embedded explicitly rather
    # than relied on from ambient log formatting (this app's logging.basicConfig has no
    # timestamp in its default format).
    log.info("bank-detail reveal: deposit=%s by=%s at=%s",
             deposit_id, staff_email, datetime.now(timezone.utc).isoformat())
    return _json({
        "ok": True,
        "routing_number": routing_number,
        "account_number": account_number,
        "account_type": dep.get("account_type"),
    })


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
# DISTINCT PEOPLE, not rows. See db.count_notify_people.
_MAX_NOTIFY_RECIPIENTS = 40

# Everything the `kind` column may hold: the floor, the nine CRM steps, and the legacy value the
# two money steps shared before 2026-08-21. Sourced from email_sender so the API, the resolver and
# the CHECK constraint cannot disagree about the vocabulary.
# Kinds the ADD FIELD may create. The floor plus the legacy deposit kind - deliberately NOT
# the steps; see admin_notify_add for why a disabled step row is not the same thing as a new
# colleague. `_NOTIFY_KINDS` below stays the full set, because reads and the resolver must
# still recognise every value the column can legally hold.
_NOTIFY_ADDABLE_KINDS = frozenset((email_sender.GENERAL_KIND, email_sender.LEGACY_DEPOSIT_KIND))
_NOTIFY_KINDS = frozenset((email_sender.GENERAL_KIND, email_sender.LEGACY_DEPOSIT_KIND)
                          + email_sender.NOTIFY_STEP_IDS)
_NOTIFY_STEP_STATES = ("on", "off", "inherit")


def _notify_people(rows: list[dict] | None = None) -> set[str]:
    """The DISTINCT addresses on the roster, which is what the cap is about.

    The cap counted ROWS, and that was fine while a person had one. With a row per (person, step)
    a 13-person roster holds up to 130 of them, so a row cap would refuse the fourteenth
    colleague because the first thirteen had used their toggles — reported as "too many
    recipients", which would be true of nothing anybody could see.

    Derived from the rows we already fetch rather than a second count(distinct) query: one round
    trip, and no chance of the cap and the list disagreeing about who is on it.

    `rows` lets a caller that has already loaded the roster reuse it, so the step endpoint can
    check the cap AND the unsilenceable-step guard off ONE read. Two reads could straddle another
    admin's write and answer the two questions about two different rosters."""
    return {(r.get("email") or "").strip().lower()
            for r in (db.list_notify_recipients() if rows is None else rows) if r.get("email")}


def _silences_step(rows: list[dict], email: str, step: str, state: str) -> bool:
    """Would this one write take `step` from reaching somebody to reaching nobody?

    ONE HELPER BECAUSE THERE ARE THREE WRITE PATHS, and the first version of this guard lived
    inside only one of them. Its own comment claimed a server-side refusal "holds for a stale page,
    a second tab and a curl alike" - and that was not true, because POST and PATCH on the same
    resource create suppression rows without passing through it. Review executed both: two POSTs
    of {kind: "sent"} returned 200 and took the reach from ['hanz','will'] to [], and a PATCH of
    {enabled: false} on two `sent` rows did the same. A guard that one of three doors respects is
    a guard nobody can rely on, so the check moved here and every door calls it.

    Only steps in UNSILENCEABLE_STEPS are guarded, and only in the somebody -> nobody direction.
    """
    if step not in email_sender.UNSILENCEABLE_STEPS:
        return False
    before = email_sender.step_reach(rows, step)
    if not before:
        return False
    return not email_sender.step_reach(_notify_rows_after(rows, email, step, state), step)


def _notify_rows_after(rows: list[dict], email: str, step: str, state: str) -> list[dict]:
    """The roster as it would stand AFTER this one cell write, without writing it.

    Mirrors db.set_notify_step (an upsert on (kind, lower(email))) and db.clear_notify_step (a
    delete on the same key), so a guard computed from this is answering the question the write
    actually poses. A legacy 'deposit' row is deliberately untouched: neither DB call can reach
    one, because both key on the step id."""
    out = [r for r in rows
           if not ((r.get("kind") or "") == step
                   and (r.get("email") or "").strip().lower() == email)]
    if state != "inherit":
        out.append({"email": email, "kind": step, "enabled": state == "on"})
    return out


@app.get("/api/admin/notify-recipients")
def admin_notify_list(request: Request) -> JSONResponse:
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    # `steps` rides along with the rows so the Notification Sending page renders its columns
    # from the vocabulary the RESOLVER uses, and keeps no list of its own. The page lives in the
    # other repo; a hardcoded copy over there is a copy that drifts, and a column the resolver
    # does not recognise is a toggle that silently does nothing.
    return _json({"ok": True,
                  "steps": email_sender.steps_payload(),
                  "recipients": [
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
    # THE ADD FIELD ADDS PEOPLE TO THE TEAM, and it creates the row switched OFF. Under the old
    # two-value vocabulary that meant only "on the roster, not emailed yet". Under the step
    # vocabulary a disabled STEP row means "suppress", so accepting a step kind here mints exactly
    # what the unsilenceable-step guard exists to prevent - review executed it: two POSTs of
    # {kind: "sent"} returned 200 and took that alert's reach to nobody.
    #
    # So this route accepts the floor and the legacy deposit kind only, which is what it accepted
    # before the matrix existed. Nothing sends anything else: the tool's own proxy allows the same
    # two, and the page's add field has a single kind. Step rows are written through
    # PUT .../step, which is guarded. This removes capability rather than adding a check.
    if kind not in _NOTIFY_ADDABLE_KINDS:
        return _json({"ok": False, "error": "invalid_kind"}, 400)
    if len(email) > 254 or not _EMAIL_RE.match(email):
        return _json({"ok": False, "error": "invalid_email"}, 400)
    # PEOPLE, not rows: a person now holds up to one row per step, so counting rows would refuse
    # the fourteenth colleague because the first thirteen had used their toggles.
    if len(_notify_people()) >= _MAX_NOTIFY_RECIPIENTS:
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
    enabled = bool(body.get("enabled"))
    # PATCH TAKES AN id, NOT AN (email, step) PAIR, which is why it slipped the guard: nothing in
    # the request says which step is being silenced. Look the row up and ask the same question the
    # PUT asks. Review reached this from the tool's proxy, which forwards any rid on an admin
    # session alone, with the roster GET handing step-row ids to the browser.
    if not enabled:
        rows = db.list_notify_recipients()
        row = next((r for r in rows if int(r.get("id") or 0) == int(rid)), None)
        if row is not None:
            kind = (row.get("kind") or "").strip().lower()
            if _silences_step(rows, (row.get("email") or "").strip().lower(), kind, "off"):
                return _json({"ok": False, "error": "would_silence_step", "step": kind}, 400)
    db.set_notify_recipient_enabled(rid, enabled)
    return _json({"ok": True})


@app.delete("/api/admin/notify-recipients/{rid}")
def admin_notify_delete(rid: int, request: Request) -> JSONResponse:
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    db.delete_notify_recipient(rid)
    return _json({"ok": True})


@app.put("/api/admin/notify-recipients/step")
async def admin_notify_step_set(request: Request) -> JSONResponse:
    """Set ONE cell of the person x step matrix.

    `state` is the three things a cell can actually be, and they are three because two would be a
    lie:
      * "on"      — an explicit row, enabled. This person hears about this step.
      * "off"     — an explicit row, disabled. This person does NOT, even though the general floor
                    would otherwise have reached them. A suppression, and the only way to take one
                    moment off somebody without taking them off the team entirely.
      * "inherit" — no row. The floor decides, which is what an untouched cell has always meant.

    ONE request per click, rather than the POST-then-PATCH pair the chips use. That pair would
    create the row disabled and then enable it, so a half-failed click would leave a cell reading
    off that nobody chose — and it would have to fight the roster cap on the way in.

    Deliberately a step-only route: `general` is the floor and is set by the person's own on/off
    chip, not by a matrix cell. Accepting it here would give one state two controls."""
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    body = await _body(request)
    email = (body.get("email") or "").strip().lower()
    step = (body.get("step") or "").strip().lower()
    state = (body.get("state") or "").strip().lower()
    if len(email) > 254 or not _EMAIL_RE.match(email):
        return _json({"ok": False, "error": "invalid_email"}, 400)
    if step not in email_sender.NOTIFY_STEP_IDS:
        return _json({"ok": False, "error": "invalid_step"}, 400)
    if state not in _NOTIFY_STEP_STATES:
        return _json({"ok": False, "error": "invalid_state"}, 400)
    rows = db.list_notify_recipients()
    # ONE STEP MAY NOT BE EMPTIED, AND THE WRITE IS REFUSED RATHER THAN FLAGGED.
    #
    # `sent` is the only step whose email is also a WARNING: admin_publish sends it on a delivery
    # FAILURE too, saying "That customer has not received the proposal". Every other step reports
    # something that happened; this one also reports something that did not, and it fires exactly
    # when nobody is watching the project. Suppressing the last person on it would be a silent
    # failure hidden behind a click that looked like it worked, which is the worst thing the new
    # suppression power can do.
    #
    # REFUSING beats warning harder. A column badge is a report, and it only appears AFTER the
    # last person is gone; a refusal is enforced here, so it holds for a stale page, a second tab
    # and a curl alike, which is where UI-only copy fails. And it is narrow: one step, one
    # direction (somebody -> nobody). Undo, every other step, and moving the alert from one person
    # to another (turn the new one on first) all keep working.
    #
    # OUT OF SCOPE, deliberately: switching the whole team off from the roster card also empties
    # this step. That path is not new, it silences all nine steps rather than hiding one, and the
    # card it happens on shows the whole roster going grey. This guard is a brake on the power
    # this change ADDS, not a new invariant over the roster as a whole.
    if _silences_step(rows, email, step, state):
        return _json({"ok": False, "error": "would_silence_step", "step": step}, 400)
    if state == "inherit":
        db.clear_notify_step(email, step)
        return _json({"ok": True})
    # A cell for somebody who is not on the roster at all is how a deposit-only person is added
    # (kylene@ is exactly that), so it must be allowed — but it is also the one path that can grow
    # the roster, so it answers to the same cap as the add field.
    known = _notify_people(rows)
    if email not in known and len(known) >= _MAX_NOTIFY_RECIPIENTS:
        return _json({"ok": False, "error": "too_many"}, 400)
    db.set_notify_step(email, step, state == "on", _cap(body.get("by"), 120) or None)
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
