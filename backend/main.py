"""Treadwell Customer Proposal Portal — FastAPI app (customer side only).

Account model: a customer signs in (email code or Google), proving control of
their email, and gets an EMAIL-scoped session that grants access to every
proposal on that email. The /p/<token> link is a convenient deep-link, not the
access gate. The admin side is the proposal tool; both share one Postgres DB.
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

import automations
import config
import customer_auth as ca
import db
import email_sender
import proposals

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("portal")

app = FastAPI(title="Treadwell Customer Proposal Portal", docs_url=None, redoc_url=None)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
BACKEND_DIR = Path(__file__).resolve().parent


@app.on_event("startup")
def _startup() -> None:
    try:
        db.run_script((BACKEND_DIR / "schema.sql").read_text(encoding="utf-8"))
        if config.DEV_SEED:
            db.run_script((BACKEND_DIR / "staging" / "dev_seed.sql").read_text(encoding="utf-8"))
        log.info("schema applied%s", " + dev seed" if config.DEV_SEED else "")
    except Exception as exc:  # noqa: BLE001
        log.error("startup schema apply failed: %s", exc)


# ── helpers ───────────────────────────────────────────────────────────────────
def _json(data: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(status_code=status, content=data)


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return (fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "")) or ""


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
    email = ((await request.json()).get("email") or "").strip().lower()
    if not email:
        return _json({"ok": False, "error": "Enter your email."}, 400)
    if not db.email_has_proposal(email):
        return _json({"ok": False, "error": "no_project"})  # 200: a normal outcome, not an error
    code = ca.issue_code(email)
    email_sender.send_otp(email, code, "your Treadwell proposal")
    return _json({"ok": True, "dev_code": code if config.SHOW_OTP else None})


@app.post("/api/auth/verify-code")
async def auth_verify_code(request: Request) -> JSONResponse:
    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    ok, reason = ca.verify_code(email, (body.get("code") or "").strip())
    if not ok:
        return _json({"ok": False, "error": reason}, 400)
    resp = _json({"ok": True, "proposals": [_proposal_card(r) for r in db.list_proposals_by_email(email)]})
    _set_session_cookie(resp, ca.start_session(email))
    return resp


@app.post("/api/auth/google")
async def auth_google(request: Request) -> JSONResponse:
    if not config.GOOGLE_AUTH_ENABLED:
        return _json({"ok": False, "error": "Google sign-in isn't enabled."}, 400)
    email = ca.verify_google_idtoken((await request.json()).get("credential") or "")
    if not email:
        return _json({"ok": False, "error": "Could not verify your Google sign-in."}, 401)
    if not db.email_has_proposal(email):
        return _json({"ok": False, "error": "no_project", "email": email})  # 200: normal outcome
    resp = _json({"ok": True, "proposals": [_proposal_card(r) for r in db.list_proposals_by_email(email)]})
    _set_session_cookie(resp, ca.start_session(email))
    return resp


@app.post("/api/auth/logout")
def auth_logout() -> JSONResponse:
    resp = _json({"ok": True})
    resp.delete_cookie(config.SESSION_COOKIE, path="/")
    return resp


@app.get("/api/me/proposals")
def me_proposals(request: Request) -> JSONResponse:
    se = _session_email(request)
    if not se:
        # 200 (not 401) so the logged-out probe doesn't log a console error.
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
    text = ((await request.json()).get("body") or "").strip()
    if not text:
        return _json({"ok": False, "error": "empty"}, 400)
    row = db.add_question(p["proposal_id"], "customer", _session_email(request), text[:4000])
    link = f"{config.PUBLIC_BASE_URL}/p/{token}"
    email_sender.notify_team(
        f"New proposal question — {p.get('project_name')}",
        f"<p><strong>{_session_email(request)}</strong> asked a question on "
        f"<strong>{p.get('project_name')}</strong>:</p><blockquote>{text}</blockquote>"
        f'<p>Answer it in the proposal tool. <a href="{link}">Portal link</a></p>',
    )
    return _json({"ok": True, "question": _q(row)})


@app.post("/api/portal/{token}/approve")
async def api_approve(token: str, request: Request) -> JSONResponse:
    p = _require(request, token)
    if not p:
        return _json({"ok": False, "error": "unauthorized"}, 401)
    body = await request.json()
    name = (body.get("name") or "").strip()
    title = (body.get("title") or "").strip()
    option_label = (body.get("option_label") or "").strip()
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
        f"<p><strong>{name}</strong>{(', ' + title) if title else ''} approved "
        f"<strong>{option_label}</strong> at <strong>${total:,.2f}</strong> on {approved_date}.</p>"
        f'<p>Project: {project_name}. <a href="{link}">Portal link</a></p>',
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
    body = await request.json()
    method = (body.get("method") or "").strip().lower()
    if method not in ("ach", "check"):
        return _json({"ok": False, "error": "Choose ACH or check."}, 400)
    account_name = (body.get("account_name") or "").strip() or None
    bank_name = (body.get("bank_name") or "").strip() or None
    note = (body.get("note") or "").strip() or None
    last4 = "".join(ch for ch in (body.get("account_last4") or "") if ch.isdigit())[-4:]
    masked_ref = f"••••{last4}" if last4 else None

    db.add_deposit(p["proposal_id"], method, account_name, bank_name, masked_ref, note)
    project_name = p.get("project_name") or "proposal"
    label = "ACH details submitted" if method == "ach" else "Paying by check"
    email_sender.notify_team(
        f"Deposit {('info' if method == 'ach' else 'method')} submitted — {project_name}",
        f"<p>{label} for <strong>{project_name}</strong>.</p>"
        f"<p>Account name: {account_name or '—'} · Bank: {bank_name or '—'} · Ref: {masked_ref or '—'}</p>"
        f"<p>Confirm receipt internally, then mark the deposit Received in the proposal tool.</p>",
        recipients=config.DEPOSIT_NOTIFY_EMAILS,
    )
    return _json({"ok": True})


@app.get("/api/portal/{token}/pdf")
def api_pdf(token: str, request: Request):
    p = _require(request, token)
    if not p:
        return _json({"ok": False, "error": "unauthorized"}, 401)
    if not p.get("pdf_path"):
        return _json({"ok": False, "error": "no_pdf"}, 404)
    return RedirectResponse(p["pdf_path"])


# ── service endpoint (admin proposal tool -> portal) ──────────────────────────
@app.post("/api/notify")
async def api_notify(request: Request) -> JSONResponse:
    if not config.SERVICE_TOKEN or request.headers.get("x-service-token") != config.SERVICE_TOKEN:
        return _json({"ok": False, "error": "unauthorized"}, 401)
    body = await request.json()
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


# Static assets — mounted last so /api, /, /p win.
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/{asset}")
def asset(asset: str):
    """Serve top-level static assets (styles.css, app.js, auth.js)."""
    f = FRONTEND_DIR / asset
    if f.is_file() and asset in {"styles.css", "app.js", "auth.js", "favicon.ico"}:
        return FileResponse(f)
    return _json({"ok": False, "error": "not_found"}, 404)
