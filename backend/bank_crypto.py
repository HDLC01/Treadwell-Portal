"""Encrypt-at-rest for the customer's ACH routing + account numbers.

Hanz, 2026-09-29: staff were reading customers' full bank numbers straight off the CRM drawer
and the team-notify email. The customer still types the full, double-entry-verified numbers --
that intake behaviour is untouched. What changes is everything AFTER the INSERT: the numbers are
Fernet-encrypted before they reach the database, and decryptable ONLY through this module -- no
other file imports `cryptography`, so there is exactly one place a mistake can leak one.

STORAGE SHAPE: an encrypted value is `ENC_PREFIX + <fernet token>` (`enc:v1:...`), so a reader can
tell ciphertext from a pre-encryption plaintext row on sight, and so a rotated encryption scheme
in the future can add `enc:v2:` beside this one without a backfill deadline.

FAIL CLOSED, NOT SILENT PLAINTEXT. A missing or malformed PORTAL_BANK_KEY must never make
`encrypt()` fall back to returning its input unchanged -- that would be worse than refusing the
payment, because nothing downstream would know it happened. `encrypt()` always raises
`BankKeyError` when the key is unusable; main.py's request handler is the one place that catches
it, and only to turn it into a refusal message, never a plaintext write.

`decrypt()` is deliberately looser: given a value that does NOT carry the `enc:v1:` prefix, it
returns it unchanged rather than raising. That is not a security hole -- it is what lets the
reveal endpoint keep working on a legacy row that predates this file, in the window between
shipping this code and running `scripts/encrypt_bank_numbers.py --apply`. Once every row has been
migrated the branch is simply never taken.
"""
from __future__ import annotations

import logging
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

import config

log = logging.getLogger("portal.bank_crypto")

ENC_PREFIX = "enc:v1:"


class BankKeyError(Exception):
    """PORTAL_BANK_KEY is missing or is not a valid Fernet key.

    The message names the problem, never the key -- it is safe to log or to fold into a
    customer-facing refusal without a second thought about what it might repeat back."""


class BankDecryptError(Exception):
    """A value that IS enc:v1:-prefixed did not decrypt under the configured key -- the key was
    rotated, or the ciphertext is corrupt. Distinct from BankKeyError so a caller can tell "we
    have no key" from "we have a key, and it is the wrong one" -- both are fail-closed, but only
    the second is worth paging someone about."""


def _fernet() -> Fernet:
    raw = (config.PORTAL_BANK_KEY or "").strip()
    if not raw:
        raise BankKeyError("PORTAL_BANK_KEY is not set")
    try:
        return Fernet(raw.encode("ascii"))
    except Exception as exc:  # noqa: BLE001 -- bad base64/length; never repeat `raw` in the message
        raise BankKeyError(
            "PORTAL_BANK_KEY is not a valid Fernet key (%s)" % type(exc).__name__
        ) from exc


def key_configured() -> bool:
    """True when PORTAL_BANK_KEY parses as a usable Fernet key. Never raises -- this is the
    non-raising check the startup log line and the ACH submit handler both call before doing
    anything else, so a bad key is discovered once, loudly, instead of on the customer's request."""
    try:
        _fernet()
        return True
    except BankKeyError:
        return False


def is_encrypted(value: Optional[str]) -> bool:
    return bool(value) and str(value).startswith(ENC_PREFIX)


def encrypt(plaintext: str) -> str:
    """`plaintext` -> `enc:v1:<fernet token>`.

    Raises BankKeyError if the key is unusable. Callers MUST NOT catch this and store
    `plaintext` verbatim as a fallback -- that is the one failure mode this file exists to rule
    out. The caller's job is to refuse the write and tell the customer, not to keep going."""
    token = _fernet().encrypt(plaintext.encode("utf-8"))
    return ENC_PREFIX + token.decode("ascii")


def decrypt(value: Optional[str]) -> str:
    """The inverse of `encrypt`, tolerant of legacy plaintext (see module docstring).

    Raises BankKeyError (no/invalid key) or BankDecryptError (this value does not decrypt under
    the configured key) only when the value actually needs decrypting. A value without the
    `enc:v1:` prefix is returned as-is -- there is nothing to decrypt."""
    if not value:
        return ""
    if not is_encrypted(value):
        return value
    body = value[len(ENC_PREFIX):]
    try:
        return _fernet().decrypt(body.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise BankDecryptError("stored value does not decrypt under PORTAL_BANK_KEY") from exc


def mask_last4(digits: Optional[str]) -> str:
    """"••••" + the last 4 digits, matching the frontend's own mask4() so a value
    masked here and a value masked in the browser (a pre-migration row with no stored mask) read
    identically. Non-digit characters (spaces, dashes) are stripped first -- callers may pass
    either the raw typed value or the already-normalized digit string.

    ONLY ever call this on plaintext -- never on an enc:v1: token, which has no meaningful
    "last 4"."""
    s = "".join(ch for ch in str(digits or "") if ch.isdigit())
    if len(s) < 4:
        return "••••"
    return "••••" + s[-4:]
