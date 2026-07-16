"""Inbound email (Resend receiving) — pure helpers for the webhook endpoint.

Resend delivers an `email.received` webhook (metadata only) signed with Svix
headers. We verify the signature manually (no extra dependency): the signed
content is `{svix-id}.{svix-timestamp}.{raw_body}`, HMAC-SHA256 keyed with the
base64-decoded portion of the `whsec_` secret, compared against the
space-separated `v1,<base64sig>` entries of the `svix-signature` header.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import time
from email.utils import parseaddr


def verify_svix(secret: str, svix_id: str, svix_timestamp: str, signature_header: str,
                raw_body: bytes, tolerance: int = 300, now: float | None = None) -> bool:
    """True iff the webhook signature is valid and the timestamp is within
    `tolerance` seconds. Any missing/malformed input → False (never raises)."""
    if not (secret and svix_id and svix_timestamp and signature_header and raw_body is not None):
        return False
    try:
        ts = int(svix_timestamp)
    except (TypeError, ValueError):
        return False
    if abs((now if now is not None else time.time()) - ts) > tolerance:
        return False
    try:
        key = base64.b64decode(secret.split("whsec_", 1)[-1])
    except Exception:  # noqa: BLE001 — malformed secret
        return False
    signed = f"{svix_id}.{svix_timestamp}.".encode() + raw_body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    for entry in signature_header.split(" "):
        if not entry.startswith("v1,"):
            continue
        if hmac.compare_digest(expected, entry[3:]):
            return True
    return False


def find_token(recipients, domain: str) -> str | None:
    """Extract the proposal token from the recipient list: the local part of the
    first address on our receiving `domain`. Handles "Name <addr>" forms; the
    domain comparison is case-insensitive; the local part is returned verbatim
    (tokens are case-sensitive — the caller may retry case-insensitively)."""
    if not domain:
        return None
    want = domain.strip().lower()
    for r in recipients or []:
        addr = parseaddr(str(r or ""))[1] or str(r or "").strip()
        if "@" not in addr:
            continue
        local, _, dom = addr.rpartition("@")
        if dom.strip().lower() == want and local:
            return local.strip()
    return None


_QUOTE_STARTS = (
    re.compile(r"^On .{1,200} wrote:\s*$"),
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}", re.IGNORECASE),
    re.compile(r"^_{5,}\s*$"),                      # Outlook divider
    re.compile(r"^From:\s.+", re.IGNORECASE),        # Outlook header block
    re.compile(r"^Sent:\s.+", re.IGNORECASE),
)


def strip_quoted(text: str) -> str:
    """Cut the quoted history off an email reply: stop at the first quote marker
    ("On … wrote:", "> …", Original-Message / Outlook header dividers). If that
    leaves nothing (someone wrote inside the quote), fall back to the original."""
    lines = (text or "").splitlines()
    kept: list[str] = []
    for ln in lines:
        s = ln.strip()
        if s.startswith(">"):
            break
        if any(p.match(s) for p in _QUOTE_STARTS):
            break
        kept.append(ln)
    out = "\n".join(kept).strip()
    return out if out else (text or "").strip()
