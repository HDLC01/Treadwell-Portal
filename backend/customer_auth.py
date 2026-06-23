"""Customer auth (account model).

A customer proves control of their email — via a 6-digit code mailed to it, or
via Google Sign-In — and gets an EMAIL-scoped session (HttpOnly cookie) that
grants access to every proposal on that email. The /p/<token> link is a
convenient deep-link, not the access gate.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import httpx

import config
import db


def now() -> datetime:
    return datetime.now(timezone.utc)


def generate_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def hash_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def new_proposal_token() -> str:
    """Unguessable token for a published proposal's deep-link (used by admin)."""
    return secrets.token_urlsafe(24)


# ── email one-time code ─────────────────────────────────────────────────────────
def issue_code(email: str) -> str:
    code = generate_code()
    db.upsert_login_code(email, hash_code(code), now() + timedelta(minutes=config.OTP_TTL_MINUTES))
    return code


def verify_code(email: str, code: str) -> tuple[bool, str]:
    row = db.get_login_code(email)
    if not row:
        return False, "No code requested. Request a new code."
    if row["expires_at"] <= now():
        return False, "That code has expired. Request a new one."
    if row["attempts"] >= config.OTP_MAX_ATTEMPTS:
        return False, "Too many attempts. Request a new code."
    if hash_code(code.strip()) != row["code_hash"]:
        db.bump_login_attempts(email)
        return False, "Incorrect code. Please try again."
    return True, ""


# ── Google Sign-In ───────────────────────────────────────────────────────────────
def verify_google_idtoken(id_token: str) -> str | None:
    """Verify a Google ID token via Google's tokeninfo endpoint. Returns the
    verified, lower-cased email if the token is valid, the audience matches our
    client id, and the email is verified — else None."""
    if not (config.GOOGLE_CLIENT_ID and id_token):
        return None
    try:
        r = httpx.get("https://oauth2.googleapis.com/tokeninfo", params={"id_token": id_token}, timeout=10)
        if r.status_code != 200:
            return None
        claims = r.json()
    except Exception:  # noqa: BLE001
        return None
    if claims.get("aud") != config.GOOGLE_CLIENT_ID:
        return None
    if str(claims.get("email_verified")).lower() != "true":
        return None
    email = (claims.get("email") or "").strip().lower()
    return email or None


# ── session ──────────────────────────────────────────────────────────────────────
def start_session(email: str) -> str:
    token = new_session_token()
    db.create_session(token, email, now() + timedelta(hours=config.SESSION_TTL_HOURS))
    db.clear_login_code(email)
    return token


def session_email(cookie_value: str | None) -> str | None:
    """Return the verified email of a valid session, else None."""
    if not cookie_value:
        return None
    sess = db.get_session(cookie_value)
    return sess["email"] if sess else None
