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

# ── Service token (admin proposal tool -> this portal /api/notify) ────────────
SERVICE_TOKEN = _env("SERVICE_TOKEN")

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
# Secure cookie when served over HTTPS (staging + prod), regardless of ENVIRONMENT.
COOKIE_SECURE = PUBLIC_BASE_URL.lower().startswith("https")
# Staging-only convenience: surface the OTP on-screen so testers can sign in
# without a live email provider. HARD-disabled in production.
SHOW_OTP = (_env("PORTAL_SHOW_OTP", "false").lower() == "true") and not IS_PROD
