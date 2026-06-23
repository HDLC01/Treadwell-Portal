"""Treadwell Customer Proposal Portal — FastAPI app (customer side only).

Serves the static portal frontend and the token/OTP-gated customer API. The
admin side is the proposal tool; both share one Postgres database.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime
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
    """Apply the portal schema (idempotent) and, locally, the dev seed."""
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


def _mask_email(email: str) -> str:
    try:
        local, domain = email.split("@", 1)
    except ValueError:
        return "your email"
    head = local[0] if local else ""
    return f"{head}{'•' * max(len(local) - 1, 3)}@{domain}"


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return (fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "")) or ""


def _set_session_cookie(resp: Response, token: str) -> None:
    resp.set_cookie(
        config.SESSION_COOKIE, token, max_age=config.SESSION_TTL_HOURS * 3600,
        httponly=True, samesite="lax", secure=config.IS_PROD, path="/",
    )


def _require_session(request: Request, proposal_id: str) -> Optional[dict]:
    return ca.session_for(request.cookies.get(config.SESSION_COOKIE), proposal_id)


# ── health + static ─────────────────────────────────────────────────────────────
@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.get("/p/{token}")
def portal_page(token: str) -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/")
def root() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "landing.html")


# ── customer API ──────────────────────────────────────────────────────────────
@app.get("/api/portal/{token}")
def api_get_portal(token: str, request: Request) -> JSONResponse:
    p = db.get_proposal_by_token(token)
    if not p:
        return _json({"ok": False, "error": "not_found"}, 404)
    sess = _require_session(request, p["proposal_id"])
    base = {"ok": True, "authed": bool(sess), "project_name": p.get("project_name") or "Your Proposal",
            "email_hint": _mask_email(p["customer_email"])}
    if not sess:
        return _json(base)
    data = db.get_draft_data(p["proposal_id"]) or {}
    db.mark_viewed(p["proposal_id"])
    p = db.get_proposal(p["proposal_id"])  # refresh status after view
    vm = proposals.build_view_model(p, data)
    vm["questions"] = [_q(q) for q in db.list_questions(p["proposal_id"])]
    base["view"] = vm
    return _json(base)


def _q(row: dict) -> dict:
    return {"author_kind": row["author_kind"], "body": row["body"],
            "created_at": row["created_at"].isoformat() if row.get("created_at") else None}


@app.post("/api/portal/{token}/request-code")
async def api_request_code(token: str, request: Request) -> JSONResponse:
    p = db.get_proposal_by_token(token)
    if not p:
        return _json({"ok": False, "error": "not_found"}, 404)
    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    # Generic success regardless of match (don't leak whether the email is on file).
    if email and email == (p["customer_email"] or "").strip().lower():
        code = ca.issue_code(p["proposal_id"], email)
        email_sender.send_otp(p["customer_email"], code, p.get("project_name") or "your proposal")
    return _json({"ok": True})


@app.post("/api/portal/{token}/verify-code")
async def api_verify_code(token: str, request: Request) -> JSONResponse:
    p = db.get_proposal_by_token(token)
    if not p:
        return _json({"ok": False, "error": "not_found"}, 404)
    body = await request.json()
    code = (body.get("code") or "").strip()
    ok, reason = ca.verify_code(p["proposal_id"], code)
    if not ok:
        return _json({"ok": False, "error": reason}, 400)
    session_token = ca.start_session(p["proposal_id"], p["customer_email"])
    resp = _json({"ok": True})
    _set_session_cookie(resp, session_token)
    return resp


@app.post("/api/portal/{token}/questions")
async def api_post_question(token: str, request: Request) -> JSONResponse:
    p = db.get_proposal_by_token(token)
    if not p:
        return _json({"ok": False, "error": "not_found"}, 404)
    sess = _require_session(request, p["proposal_id"])
    if not sess:
        return _json({"ok": False, "error": "unauthorized"}, 401)
    body = await request.json()
    text = (body.get("body") or "").strip()
    if not text:
        return _json({"ok": False, "error": "empty"}, 400)
    row = db.add_question(p["proposal_id"], "customer", sess["email"], text[:4000])
    link = f"{config.PUBLIC_BASE_URL}/p/{token}"
    email_sender.notify_team(
        f"New proposal question — {p.get('project_name')}",
        f"<p><strong>{sess['email']}</strong> asked a question on "
        f"<strong>{p.get('project_name')}</strong>:</p><blockquote>{text}</blockquote>"
        f'<p>Answer it in the proposal tool. <a href="{link}">Portal link</a></p>',
    )
    return _json({"ok": True, "question": _q(row)})


@app.post("/api/portal/{token}/approve")
async def api_approve(token: str, request: Request) -> JSONResponse:
    p = db.get_proposal_by_token(token)
    if not p:
        return _json({"ok": False, "error": "not_found"}, 404)
    sess = _require_session(request, p["proposal_id"])
    if not sess:
        return _json({"ok": False, "error": "unauthorized"}, 401)
    body = await request.json()
    name = (body.get("name") or "").strip()
    title = (body.get("title") or "").strip()
    option_label = (body.get("option_label") or "").strip()
    if not name:
        return _json({"ok": False, "error": "Name is required."}, 400)

    data = db.get_draft_data(p["proposal_id"]) or {}
    options = proposals.pricing_options(data)
    # Authoritative total comes from the server-side option (prevents tampering).
    chosen = next((o for o in options if o["label"] == option_label), None)
    if chosen is None and len(options) == 1:
        chosen = options[0]
        option_label = chosen["label"]
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
    except Exception as exc:  # noqa: BLE001 — automations must never block approval
        log.error("approval automations failed: %s", exc)
    return _json({"ok": True})


@app.post("/api/portal/{token}/deposit")
async def api_deposit(token: str, request: Request) -> JSONResponse:
    p = db.get_proposal_by_token(token)
    if not p:
        return _json({"ok": False, "error": "not_found"}, 404)
    sess = _require_session(request, p["proposal_id"])
    if not sess:
        return _json({"ok": False, "error": "unauthorized"}, 401)
    body = await request.json()
    method = (body.get("method") or "").strip().lower()
    if method not in ("ach", "check"):
        return _json({"ok": False, "error": "Choose ACH or check."}, 400)
    account_name = (body.get("account_name") or "").strip() or None
    bank_name = (body.get("bank_name") or "").strip() or None
    note = (body.get("note") or "").strip() or None
    # SECURITY: never store the full account number — only the last 4.
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
    p = db.get_proposal_by_token(token)
    if not p:
        return _json({"ok": False, "error": "not_found"}, 404)
    if not _require_session(request, p["proposal_id"]):
        return _json({"ok": False, "error": "unauthorized"}, 401)
    if not p.get("pdf_path"):
        return _json({"ok": False, "error": "no_pdf"}, 404)
    # pdf_path is a Supabase Storage URL (signed/public) captured at publish time.
    return RedirectResponse(p["pdf_path"])


# ── service endpoint (admin proposal tool -> portal) ──────────────────────────
@app.post("/api/notify")
async def api_notify(request: Request) -> JSONResponse:
    if not config.SERVICE_TOKEN or request.headers.get("x-service-token") != config.SERVICE_TOKEN:
        return _json({"ok": False, "error": "unauthorized"}, 401)
    body = await request.json()
    proposal_id = body.get("proposal_id")
    p = db.get_proposal(proposal_id) if proposal_id else None
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


# Static assets (styles.css, js/, landing) — mounted last so /api and /p win.
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="static")
