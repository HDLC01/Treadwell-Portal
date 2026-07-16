"""Central settings for the Treadwell Customer Proposal Portal.

This app is the CUSTOMER side of the proposal flow. The admin side is the
existing proposal tool (proposals.wetreadwell.com). Both read/write the SAME
Postgres database (one source of truth): proposal content lives in `drafts`
(owned by the proposal tool); this app owns the `portal_*` tables.
"""
from __future__ import annotations

import os


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


# ── Database (same Postgres the proposal tool uses) ───────────────────────────
# Local dev: a docker Postgres. Prod: the Supabase Postgres connection string.
DATABASE_URL = _env("DATABASE_URL", "postgresql://portal:portal@localhost:5436/portal")

# ── Public URL of this portal (used in emailed links) ─────────────────────────
PUBLIC_BASE_URL = _env("PUBLIC_BASE_URL", "http://localhost:8898").rstrip("/")

# ── Customer session / OTP ────────────────────────────────────────────────────
SESSION_TTL_HOURS = int(_env("PORTAL_SESSION_TTL_HOURS", "24"))
OTP_TTL_MINUTES = int(_env("PORTAL_OTP_TTL_MINUTES", "10"))
OTP_MAX_ATTEMPTS = int(_env("PORTAL_OTP_MAX_ATTEMPTS", "5"))
SESSION_COOKIE = "portal_session"

# ── Rate limiting (in-process sliding window; single uvicorn worker) ──────────
OTP_REQUEST_COOLDOWN_SEC = int(_env("PORTAL_OTP_COOLDOWN_SEC", "45"))   # min gap between codes for one email
OTP_REQUESTS_PER_EMAIL = int(_env("PORTAL_OTP_PER_EMAIL", "5"))         # per window
RATE_WINDOW_SEC = int(_env("PORTAL_RATE_WINDOW_SEC", "900"))            # 15 min
RATE_REQUESTS_PER_IP = int(_env("PORTAL_RATE_PER_IP", "40"))            # auth POSTs per IP per window

# ── Deposit (shown to the customer; exact instructions provided by Treadwell) ──
CHECK_ADDRESS = _env("PORTAL_CHECK_ADDRESS", "Treadwell — Attn: Accounts Receivable (mailing address provided by your representative)")

# ── Service token (admin proposal tool -> this portal /api/notify) ────────────
SERVICE_TOKEN = _env("SERVICE_TOKEN")

# ── Proposal tool (renders the real Treadwell proposal PDF on demand) ─────────
# When set, the portal fetches the official PDF from the proposal tool's
# SERVICE_TOKEN-gated /api/admin/proposal-pdf and serves it to the customer.
PROPOSAL_TOOL_URL = _env("PROPOSAL_TOOL_URL").rstrip("/")
# PUBLIC-facing URL of the staff proposal tool — used in staff notification
# emails' "Reply in Portal" link. PROPOSAL_TOOL_URL above is the internal Docker
# hostname (unreachable from a browser), so this is a separate, browsable URL.
PROPOSAL_TOOL_PUBLIC_URL = _env("PROPOSAL_TOOL_PUBLIC_URL", "https://proposals.wetreadwell.com").rstrip("/")

# ── Email (Resend) ────────────────────────────────────────────────────────────
RESEND_API_KEY = _env("RESEND_API_KEY")
EMAIL_FROM = _env("EMAIL_FROM", "Treadwell <noreply@wetreadwell.com>")
# Comma-separated recipient lists.
NOTIFY_EMAILS = [e.strip() for e in _env("NOTIFY_EMAILS", "bids@wetreadwell.com").split(",") if e.strip()]
DEPOSIT_NOTIFY_EMAILS = [
    e.strip()
    for e in _env("DEPOSIT_NOTIFY_EMAILS", ",".join(NOTIFY_EMAILS)).split(",")
    if e.strip()
]
# Optional Reply-To for notification emails (empty by default → no Reply-To, so
# replies go to the noreply From address). Set EMAIL_REPLY_TO in the env to a
# MONITORED inbox only if you want stray customer replies to reach a human until
# inbound→CRM capture exists. NOT auto-defaulted to bids@ or any team address.
EMAIL_REPLY_TO = _env("EMAIL_REPLY_TO", "")

# ── Google Sign-In for customers (optional alt to email OTP) ──────────────────
# A Google OAuth *Web Client ID* (public). The button only renders when set; the
# backend verifies the returned Google ID token and matches email to the proposal.
GOOGLE_CLIENT_ID = _env("GOOGLE_CLIENT_ID")
GOOGLE_AUTH_ENABLED = bool(GOOGLE_CLIENT_ID)

# ── Dropbox (folder creation on approval — optional/graceful) ─────────────────
DROPBOX_ENABLED = bool(_env("DROPBOX_REFRESH_TOKEN") or _env("DROPBOX_ACCESS_TOKEN"))

# ── Misc ──────────────────────────────────────────────────────────────────────
ENVIRONMENT = _env("ENVIRONMENT", "development")
IS_PROD = ENVIRONMENT == "production"
# Apply the dev seed (stand-in `drafts` + sample proposal) on startup. NOT prod.
DEV_SEED = _env("DEV_SEED", "false").lower() == "true"
# Apply schema.sql (DDL) on startup. Convenient for local/staging, where the app
# connects as the table owner. In prod the portal connects as the least-privilege
# `portal_app` role (no DDL rights) and schema is applied out-of-band via
# migration (security_prod.sql + schema.sql) — so default OFF in prod. Overridable.
APPLY_SCHEMA_ON_BOOT = _env("PORTAL_APPLY_SCHEMA_ON_BOOT", "false" if IS_PROD else "true").lower() == "true"
# Secure cookie when served over HTTPS (staging + prod), regardless of ENVIRONMENT.
COOKIE_SECURE = PUBLIC_BASE_URL.lower().startswith("https")
# Staging-only convenience: surface the OTP on-screen so testers can sign in
# without a live email provider. HARD-disabled in production.
SHOW_OTP = (_env("PORTAL_SHOW_OTP", "false").lower() == "true") and not IS_PROD
