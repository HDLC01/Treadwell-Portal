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


# A sender's own contact block, which every desktop and phone client bolts onto the end.
_SIG_DELIM = re.compile(r"^--\s*$|^—\s*$|^Sent from my \w+", re.IGNORECASE)
_EMAIL_IN = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_PHONE_IN = re.compile(r"(?:\+?\d[\d\s().-]{7,}\d)")
_URL_IN = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
# "*WILL* *BUCHANAN*" — the HTML-to-text pass turns bold into asterisks, and a name in
# bold on its own line is the single most reliable signature tell in practice.
_BOLD_NAME = re.compile(r"^\**[A-Z][A-Za-z.'-]*\**(\s+\**[A-Z][A-Za-z.'-]*\**){0,3}[\s*|]*$")
_LABELLED = re.compile(r"^\**\s*(E|C|T|M|P|F|W|O|Cell|Mobile|Direct|Office|Tel|Fax|Phone|Web|"
                       r"Email|Main)\s*[:.]", re.IGNORECASE)
_ADDRESS = re.compile(r"\d+\s+[\w.\s]{2,40}\b(St|Street|Ave|Avenue|Rd|Road|Blvd|Ter|Terrace|"
                      r"Dr|Drive|Ln|Lane|Ct|Court|Way|Pkwy|Suite|Ste|Hwy)\b", re.IGNORECASE)
_CITY_ST_ZIP = re.compile(r"[A-Za-z.\s]+,\s*[A-Z]{2}\s*\d{5}")
_COMPANY = re.compile(r"\b(LLC|L\.L\.C\.|Inc\.?|Ltd\.?|Corp\.?|Co\.|Capital|Partners|Group)\b")
_MAX_SIG_LINES = 14


def _hard_signature_signal(s: str) -> bool:
    """A line that can only be contact details: an address, a phone, a labelled field.

    A block has to contain at least one of these before any of it is dropped. Without that
    requirement the soft tests below are enough on their own to eat a trailing "Thanks" —
    or a one-word answer like "Yes" at the end of a real message.
    """
    return bool(_LABELLED.match(s) or _ADDRESS.search(s) or _CITY_ST_ZIP.search(s)
                or _EMAIL_IN.search(s) or _URL_IN.search(s) or _PHONE_IN.search(s))


_PROSE = re.compile(r"[a-z]{2,}\s+[a-z]{2,}\s+[a-z]{2,}")


def _looks_like_signature_line(s: str) -> bool:
    """One line of a trailing contact block, rather than something a person wrote to us."""
    if not s:
        return True
    # Three lowercase words in a row is a sentence, and a sentence is content even when it
    # holds a phone number. Without this, "Call me on 913-555-1234 before you order the
    # material" reads as a signature line and the instruction is thrown away.
    if _PROSE.search(s):
        return False
    if _hard_signature_signal(s):
        return True
    # Soft tells, only ever trusted alongside a hard one: a bolded name, a company, a
    # "*|*" separator, any short line with no sentence in it.
    if len(s) <= 70 and (_BOLD_NAME.match(s) or _COMPANY.search(s)
                         or not re.search(r"[a-z]{2,}\s+[a-z]{2,}", s)):
        return True
    return False


def strip_signature(text: str) -> str:
    """Cut the sender's own contact block off the end of an inbound email.

    Hanz, 2026-08-11, on a real reply of Will's landing in the thread as a wall of text:
    "he is telling it is very clutter". strip_quoted removes the QUOTED HISTORY below a
    reply; it never touched signatures, so every inbound message carried the sender's
    name, mobile, office, website and street address into the chat bubble — on both the
    staff CRM and the customer's portal.

    Two passes, both conservative:

      1. an explicit delimiter ("-- ", "Sent from my iPhone") cuts everything after it;
      2. otherwise, walk BACKWARDS from the end dropping lines that read as contact
         details, and stop at the first line with a sentence in it.

    Never returns empty: if the whole message reads as a signature (someone replying
    with just a phone number) the original is kept. Capped at 14 lines so a long message
    that happens to end in contact details loses its footer, not its content.

    Line-based, so a client that flattens the signature onto the same line as the message
    keeps it. That is why the bubble also renders newlines now — the structure has to be
    visible for this to have anything to work with.
    """
    lines = (text or "").splitlines()
    cut = next((i for i, ln in enumerate(lines) if _SIG_DELIM.match(ln.strip())), None)
    if cut is not None and cut > 0:
        out = "\n".join(lines[:cut]).strip()
        if out:
            return out

    first = next((i for i, ln in enumerate(lines) if ln.strip()), None)
    if first is None:
        return (text or "").strip()

    # Never past the first line somebody wrote. A one-word reply ("Approved.", "Yes") trips
    # every soft test there is, so without this floor a short answer above a full Outlook
    # block strips to nothing.
    end = len(lines)
    while end > first + 1 and (len(lines) - end) < _MAX_SIG_LINES:
        if not _looks_like_signature_line(lines[end - 1].strip()):
            break
        end -= 1

    # Only a block that proves itself is dropped. A trailing "Thanks", or a blank line, is
    # every bit as strippable by the soft tests as a real signature is — and losing a
    # customer's last word is a worse outcome than leaving their phone number on screen.
    if not any(_hard_signature_signal(ln.strip()) for ln in lines[end:]):
        return "\n".join(lines).strip()

    # Accepted limit: a message that is ONLY a signature keeps its first line and loses the
    # rest. Rare, and staff still have the original email in their own inbox.
    out = "\n".join(lines[:end]).strip()
    return out if out else (text or "").strip()
