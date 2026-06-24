"""Treadwell Customer Proposal Portal — FastAPI app (customer side only).

Account model: a customer signs in (email code or Google), proving control of
their email, and gets an EMAIL-scoped session that grants access to every
proposal on that email. The /p/<token> link is a convenient deep-link, not the
access gate. The admin side is the proposal tool; both share one Postgres DB.
"""
from __future__ import annotations

import hmac
import html
import logging
from datetime import date
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
    "frame-src https://accounts.google.com; "
    "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)


@app.on_event("startup")
def _startup() -> None:
    try:
        db.run_script((BACKEND_DIR / "schema.sql").read_text(encoding="utf-8"))
        if config.DEV_SEED:
            db.run_script((BACKEND_DIR / "staging" / "dev_seed.sql").read_text(encoding="utf-8"))
        db.cleanup_expired()
        log.info("schema applied%s", " + dev seed" if config.DEV_SEED else "")
    except Exception as exc:  # noqa: BLE001
        log.error("startup schema apply failed: %s", exc)


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
    return bool(se and se == (proposal.get("customer_email") or "").strip().lower())


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
    # CSRF backstop: state-changing API POSTs (except the service endpoint, which
    # has its own token) must originate from our own site.
    if request.method == "POST" and request.url.path.startswith("/api/") and request.url.path != "/api/notify":
        ref = request.headers.get("origin") or request.headers.get("referer") or ""
        if ref:
            host = urlparse(ref).netloc
            req_host = request.headers.get("host", "")
            if host not in (req_host, ALLOWED_HOST):
                return _json({"ok": False, "error": "bad_origin"}, 403)
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
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
    authed = bool(se and se == (p.get("customer_email") or "").strip().lower())
    base = {"ok": True, "authed": authed, "project_name": p.get("project_name") or "Your Proposal",
            "wrong_account": bool(se and not authed)}
    if not authed:
        return _json(base)
    data = db.get_draft_data(p["proposal_id"]) or {}
    db.mark_viewed(p["proposal_id"])
    p = db.get_proposal(p["proposal_id"])
    vm = proposals.build_view_model(p, data)
    vm["questions"] = [_q(q) for q in db.list_questions(p["proposal_id"])]
    vm["check_address"] = config.CHECK_ADDRESS
    if config.PROPOSAL_TOOL_URL:   # official PDF available via on-demand render
        vm["has_pdf"] = True
    base["view"] = vm
    return _json(base)


def _q(row: dict) -> dict:
    return {"author_kind": row["author_kind"], "body": row["body"],
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
    row = db.add_question(p["proposal_id"], "customer", who, text)
    link = f"{config.PUBLIC_BASE_URL}/p/{token}"
    email_sender.notify_team(
        f"New proposal question — {p.get('project_name')}",
        f"<p><strong>{html.escape(who or '')}</strong> asked a question on "
        f"<strong>{html.escape(p.get('project_name') or '')}</strong>:</p>"
        f"<blockquote>{html.escape(text)}</blockquote>"
        f'<p>Answer it in the proposal tool. <a href="{link}">Portal link</a></p>',
    )
    return _json({"ok": True, "question": _q(row)})


@app.post("/api/portal/{token}/approve")
async def api_approve(token: str, request: Request) -> JSONResponse:
    p = _require(request, token)
    if not p:
        return _json({"ok": False, "error": "unauthorized"}, 401)
    body = await _body(request)
    name = _cap(body.get("name"), 120)
    title = _cap(body.get("title"), 120)
    option_label = _cap(body.get("option_label"), 200)
    if not name:
        return _json({"ok": False, "error": "Name is required."}, 400)

    data = db.get_draft_data(p["proposal_id"]) or {}
    options = proposals.pricing_options(data)
    chosen = next((o for o in options if o["label"] == option_label), None)
    if chosen is None and len(options) == 1:
        chosen, option_label = options[0], options[0]["label"]
    if chosen is None:
        return _json({"ok": False, "error": "Please choose which option you're approving."}, 400)
    total = chosen["total"]
    try:
        approved_date = date.fromisoformat(body["date"]) if body.get("date") else date.today()
    except (ValueError, TypeError):
        approved_date = date.today()

    db.add_approval(p["proposal_id"], name, title, approved_date, total, option_label, _client_ip(request))
    db.set_approved(p["proposal_id"], total, option_label, name, title, approved_date)

    project_name = p.get("project_name") or "proposal"
    link = f"{config.PUBLIC_BASE_URL}/p/{token}"
    email_sender.notify_team(
        f"Proposal APPROVED — {project_name}",
        f"<p><strong>{html.escape(name)}</strong>{(', ' + html.escape(title)) if title else ''} approved "
        f"<strong>{html.escape(option_label)}</strong> at <strong>${total:,.2f}</strong> on {approved_date}.</p>"
        f'<p>Project: {html.escape(project_name)}. <a href="{link}">Portal link</a></p>',
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
    last4 = "".join(ch for ch in (body.get("account_last4") or "") if ch.isdigit())[-4:]
    masked_ref = f"••••{last4}" if last4 else None

    db.add_deposit(p["proposal_id"], method, account_name, bank_name, masked_ref, note)
    project_name = p.get("project_name") or "proposal"
    label = "ACH details submitted" if method == "ach" else "Paying by check"
    email_sender.notify_team(
        f"Deposit {('info' if method == 'ach' else 'method')} submitted — {project_name}",
        f"<p>{label} for <strong>{html.escape(project_name)}</strong>.</p>"
        f"<p>Account name: {html.escape(account_name or '—')} · Bank: {html.escape(bank_name or '—')} · "
        f"Ref: {masked_ref or '—'}</p>"
        f"<p>Confirm receipt internally, then mark the deposit Received in the proposal tool.</p>",
        recipients=config.DEPOSIT_NOTIFY_EMAILS,
    )
    return _json({"ok": True})


@app.get("/api/portal/{token}/pdf")
def api_pdf(token: str, request: Request):
    p = _require(request, token)
    if not p:
        return _json({"ok": False, "error": "unauthorized"}, 401)
    # Preferred: render the real Treadwell PDF on demand from the proposal tool.
    if config.PROPOSAL_TOOL_URL and config.SERVICE_TOKEN:
        try:
            r = httpx.get(
                config.PROPOSAL_TOOL_URL + "/api/admin/proposal-pdf",
                params={"draft_id": p["proposal_id"]},
                headers={"X-Service-Token": config.SERVICE_TOKEN},
                timeout=90,
            )
            if r.status_code == 200:
                return Response(content=r.content, media_type="application/pdf",
                                headers={"Content-Disposition": 'inline; filename="proposal.pdf"'})
            log.info("proposal-pdf upstream %s for %s", r.status_code, p["proposal_id"])
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
    if kind == "published":
        email_sender.send_portal_link(p["customer_email"], p.get("customer_name") or "", link,
                                      p.get("project_name") or "your proposal")
    elif kind == "reply":
        email_sender.send_reply_notification(p["customer_email"], link, p.get("project_name") or "your proposal")
    else:
        return _json({"ok": False, "error": "unknown_type"}, 400)
    return _json({"ok": True})


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
    email = (data.get("contact_email") or "").strip().lower()
    if not email:
        return _json({"ok": False, "error": "no_contact_email"}, 400)  # can't publish without a customer email
    name = _cap(data.get("contact_name"), 120)
    project = _cap(data.get("project_name"), 200) or "Your Proposal"
    pdf_path = (body.get("pdf_path") or "").strip() or None
    by = _cap(body.get("by"), 120) or None

    existing = db.get_proposal(draft_id)
    if existing:
        token = existing["token"]
        db.update_portal_proposal(draft_id, email, name, project, pdf_path)
    else:
        token = ca.new_proposal_token()
        db.create_portal_proposal(draft_id, token, email, name, project, pdf_path, by)
    link = f"{config.PUBLIC_BASE_URL}/p/{token}"
    email_sender.send_portal_link(email, name, link, project)
    return _json({"ok": True, "token": token, "url": link, "customer_email": email})


@app.get("/api/admin/pipeline")
def admin_pipeline(request: Request) -> JSONResponse:
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    out = []
    for r in db.list_all_portal_proposals():
        out.append({
            "proposal_id": r["proposal_id"], "token": r["token"],
            "customer_email": r["customer_email"], "customer_name": r.get("customer_name"),
            "project_name": r.get("project_name"), "proposal_status": r["proposal_status"],
            "deposit_status": r["deposit_status"], "schedule_status": r["schedule_status"],
            "approved_total": float(r["approved_total"]) if r.get("approved_total") is not None else None,
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
        },
        "approval": ({
            "name": appr["name"], "title": appr.get("title"),
            "date": appr["approved_date"].isoformat() if appr.get("approved_date") else None,
            "total": float(appr["total"]) if appr.get("total") is not None else None,
            "option": appr.get("option_label"),
        } if appr else None),
        "questions": [_q(q) for q in db.list_questions(proposal_id)],
        "deposits": [{
            "method": d["method"], "account_name": d.get("account_name"), "bank_name": d.get("bank_name"),
            "masked_ref": d.get("masked_ref"), "note": d.get("note"),
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
    email_sender.send_reply_notification(p["customer_email"], f"{config.PUBLIC_BASE_URL}/p/{p['token']}",
                                         p.get("project_name") or "your proposal")
    return _json({"ok": True})


@app.post("/api/admin/proposal/{proposal_id}/deposit-received")
def admin_deposit_received(proposal_id: str, request: Request) -> JSONResponse:
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    if not db.get_proposal(proposal_id):
        return _json({"ok": False, "error": "not_found"}, 404)
    db.set_deposit_status(proposal_id, "received")
    return _json({"ok": True})


@app.post("/api/admin/proposal/{proposal_id}/scheduled")
def admin_scheduled(proposal_id: str, request: Request) -> JSONResponse:
    if not _admin_ok(request):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    if not db.get_proposal(proposal_id):
        return _json({"ok": False, "error": "not_found"}, 404)
    db.set_schedule_status(proposal_id, "scheduled")
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
