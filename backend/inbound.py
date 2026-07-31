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


def _domain_set(domain) -> set[str]:
    """Normalize one domain or a list of them to a lowercase set."""
    if not domain:
        return set()
    items = [domain] if isinstance(domain, str) else list(domain)
    return {str(d).strip().lower() for d in items if d and str(d).strip()}


def find_token(recipients, domain) -> str | None:
    """Extract the proposal token from the recipient list: the local part of the
    first address on one of our receiving domains. `domain` is a single domain or
    a list of them (primary + legacy, so replies to a retired receiving domain
    still resolve). Handles "Name <addr>" forms; the domain comparison is
    case-insensitive; the local part is returned verbatim (tokens are
    case-sensitive — the caller may retry case-insensitively)."""
    want = _domain_set(domain)
    if not want:
        return None
    for r in recipients or []:
        addr = parseaddr(str(r or ""))[1] or str(r or "").strip()
        if "@" not in addr:
            continue
        local, _, dom = addr.rpartition("@")
        if dom.strip().lower() in want and local:
            return local.strip()
    return None


def addressed_to_domain(recipients, domain) -> bool:
    """True iff any recipient is on `domain` (single or list), token or not. Lets
    the caller restrict sender-matching to the primary receiving domain."""
    want = _domain_set(domain)
    if not want:
        return False
    for r in recipients or []:
        addr = parseaddr(str(r or ""))[1] or str(r or "").strip()
        if "@" in addr and addr.rpartition("@")[2].strip().lower() in want:
            return True
    return False


def _header_values(headers, *names) -> list[str]:
    """Pull header values by name from Resend's payload, which returns `headers` as
    a dict whose values are sometimes a string and sometimes a list (References
    comes back as a JSON array). Tolerates a list-of-{name,value} shape too, and
    never raises on anything unexpected."""
    want = {n.strip().lower() for n in names}
    out: list[str] = []

    def add(v):
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, (list, tuple)):
            out.extend(str(x) for x in v if isinstance(x, (str, int, float)))
        elif v is not None:
            out.append(str(v))

    try:
        if isinstance(headers, dict):
            for k, v in headers.items():
                if str(k).strip().lower() in want:
                    add(v)
        elif isinstance(headers, (list, tuple)):
            for h in headers:
                if isinstance(h, dict) and str(h.get("name", "")).strip().lower() in want:
                    add(h.get("value"))
    except Exception:  # noqa: BLE001 — malformed header payload is never fatal
        return []
    return out


_ANCHOR_RE = re.compile(r"tw-proposal\.([A-Za-z0-9_\-]{8,80})@", re.IGNORECASE)


def find_thread_token(headers) -> str | None:
    """Recover the proposal token from the threading headers of an inbound reply.

    This is what lets the visible Reply-To be one clean address: we stamp a
    per-proposal Message-ID on outbound mail (email_sender.proposal_anchor) and the
    customer's client quotes it back in In-Reply-To / References. In-Reply-To is
    checked first — it is the message actually being answered — then References,
    newest last, so the most specific match wins.

    Returns the token verbatim (they are case-sensitive; the caller may retry
    case-insensitively, as with address tokens)."""
    for value in _header_values(headers, "in-reply-to"):
        m = _ANCHOR_RE.search(value)
        if m:
            return m.group(1)
    refs = _header_values(headers, "references")
    for value in reversed(refs):
        for m in reversed(list(_ANCHOR_RE.finditer(value))):
            return m.group(1)
    return None


_SPF_PASS = re.compile(r"\bspf\s*=\s*pass\b", re.IGNORECASE)
_DKIM_PASS = re.compile(r"\bdkim\s*=\s*pass\b", re.IGNORECASE)


def sender_authenticated(headers) -> bool:
    """True when the receiving MTA verified BOTH SPF and DKIM for this message.

    Resend's inbound payload carries an `authentication-results` header from SES.
    A From address is trivially forgeable and the svix signature only proves the
    webhook came from Resend — this is the one signal that says the sending domain
    actually authorised the message. Used to gate the privileged path where an
    inbound email may speak AS Treadwell to a customer.

    Absent or unparseable header → False. Failing closed only costs a staff reply
    a trip through the roster forward; failing open would let a forged From post
    to a customer's thread."""
    for value in _header_values(headers, "authentication-results",
                               "arc-authentication-results"):
        if _SPF_PASS.search(value) and _DKIM_PASS.search(value):
            return True
    return False


def is_own_address(from_email: str, own_from: str, domains) -> bool:
    """True when an inbound email appears to come from US — our own From address,
    or any address at a receiving domain. Guards the loop where a message we
    forward or relay is delivered straight back into the webhook."""
    addr = (parseaddr(str(from_email or ""))[1] or str(from_email or "")).strip().lower()
    if not addr:
        return True                     # no usable From — nothing safe to do with it
    if addr == (parseaddr(str(own_from or ""))[1] or str(own_from or "")).strip().lower():
        return True
    return addr.rpartition("@")[2] in _domain_set(domains)


_AUTO_SUBJECTS = re.compile(
    r"^\s*(auto(matic)?[ -]?(reply|response|generated)|autoreply|out of (the )?office|"
    r"abwesenheitsnotiz|réponse automatique)\b[: ]?", re.IGNORECASE)
_AUTO_HEADERS = {
    "auto-submitted": lambda v: v.strip().lower() not in ("", "no"),
    "precedence": lambda v: v.strip().lower() in ("bulk", "auto_reply", "junk", "list"),
    "x-autoreply": lambda v: True,
    "x-autorespond": lambda v: True,
    "x-auto-response-suppress": lambda v: True,
}


def is_auto_reply(subject: str | None, headers=None) -> bool:
    """Detect vacation/out-of-office/bulk auto-responders, so we neither relay
    them to a customer nor bounce them back and forth with another autoresponder.
    `headers` may be a dict or Resend's list of {name, value} — both tolerated,
    and anything unexpected is simply ignored (never raises)."""
    if subject and _AUTO_SUBJECTS.match(str(subject)):
        return True
    pairs: list[tuple[str, str]] = []
    try:
        if isinstance(headers, dict):
            pairs = [(str(k), str(v)) for k, v in headers.items()]
        elif isinstance(headers, (list, tuple)):
            for h in headers:
                if isinstance(h, dict):
                    pairs.append((str(h.get("name") or ""), str(h.get("value") or "")))
    except Exception:  # noqa: BLE001 — malformed header payload is not fatal
        return False
    for name, value in pairs:
        test = _AUTO_HEADERS.get(name.strip().lower())
        if test and test(value):
            return True
    return False


_QUOTE_STARTS = (
    re.compile(r"^On .{1,250} wrote:\s*$"),           # Gmail attribution (single line)
    re.compile(r"^On .{1,250}<[^>]+>\s*$"),           # Gmail attribution wrapped — first line ends with <email>
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}", re.IGNORECASE),
    re.compile(r"^_{5,}\s*$"),                        # Outlook divider
    re.compile(r"^From:\s.+", re.IGNORECASE),          # Outlook header block
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
