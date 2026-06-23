"""Customer auth: unguessable proposal token + email one-time code + session.

The proposal link (/p/<token>) is unguessable. To act on a proposal the
customer proves control of the on-file email by entering a 6-digit code mailed
to that address. A successful code mints an opaque, DB-backed session stored in
an HttpOnly cookie scoped to that one proposal.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

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
    """Unguessable token for a published proposal's URL (used by the admin side)."""
    return secrets.token_urlsafe(24)


def issue_code(proposal_id: str, email: str) -> str:
    code = generate_code()
    db.upsert_login_code(proposal_id, email, hash_code(code), now() + timedelta(minutes=config.OTP_TTL_MINUTES))
    return code


def verify_code(proposal_id: str, code: str) -> tuple[bool, str]:
    """Returns (ok, reason)."""
    row = db.get_login_code(proposal_id)
    if not row:
        return False, "No code requested. Request a new code."
    if row["expires_at"] <= now():
        return False, "That code has expired. Request a new one."
    if row["attempts"] >= config.OTP_MAX_ATTEMPTS:
        return False, "Too many attempts. Request a new code."
    if hash_code(code.strip()) != row["code_hash"]:
        db.bump_login_attempts(proposal_id)
        return False, "Incorrect code. Please try again."
    return True, ""


def start_session(proposal_id: str, email: str) -> str:
    token = new_session_token()
    db.create_session(token, proposal_id, email, now() + timedelta(hours=config.SESSION_TTL_HOURS))
    db.clear_login_code(proposal_id)
    return token


def session_for(cookie_value: str | None, proposal_id: str) -> dict | None:
    """Return the session row iff it's valid AND scoped to this proposal."""
    if not cookie_value:
        return None
    sess = db.get_session(cookie_value)
    if not sess or sess["proposal_id"] != proposal_id:
        return None
    return sess
