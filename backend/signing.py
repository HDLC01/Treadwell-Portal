"""E-signature capture for a customer's approval — the portal's half.

WHAT THIS IS FOR. Kyle wants an approval to BE a signed contract: the customer types
their legal name, ticks a consent box, and staff — and the customer — end up holding a
PDF of the proposal with a signature certificate appended. The legal basis is the
federal ESIGN Act (15 U.S.C. 7001) and the Kansas UETA (K.S.A. 16-1601 et seq.), which
between them make a typed name with recorded intent a signature. Neither requires a
drawn squiggle, so there is none.

THE GAP THAT ACTUALLY MATTERED. ESIGN 7001(d)/(e) requires that the signer be ABLE TO
RETAIN their own copy. Before this, nobody could: the approval left a row in
portal_approvals and an email that named a total. That is why the customer download
endpoint exists and why the confirmation email carries the document, not only the
staff notification.

WHAT LIVES HERE. Everything about a signature that is pure — the consent wording and
its version, the hashes that pin what was signed, the Central-time stamp — plus the
one SERVICE_TOKEN-gated call that asks the proposal tool to build the document. Kept
out of main.py so the parts a lawyer will read are in one file and testable without a
request.

THE SPLIT IS THE SAME ONE invoice.py ALREADY MAKES. This container has no python-docx,
no templates and no LibreOffice; the proposal tool has all three. So the portal owns
WHAT was signed and by whom, and the proposal tool owns turning that into paper.

BUDGET PRICING CANNOT BE SIGNED. The Budget Pricing template (proposal_writer's
TEMPLATE_PICKER key ("budget", "Direct") in the proposal-tool repo) carries no Terms
and Conditions section at all. A certificate saying "I have reviewed the proposal,
including its Terms and Conditions" against a document that has none is a false
statement in a legal record, so signing_blocked_reason() refuses those outright rather
than producing one.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

import config
from followup_rules import BUSINESS_TZ

log = logging.getLogger("portal.signing")

_ENDPOINT = "/api/admin/signed-contract"

# ── the consent wording ───────────────────────────────────────────────────────
# VERSIONED, and the version is stored on the approval row next to the text itself.
# Wording is what a court reads, so "which sentence did this customer agree to" has to
# be answerable years later without archaeology through git — a proposal signed under
# v1 must keep reporting v1 even after the wording changes.
#
# ATTORNEY REVIEW IS STILL PENDING as of 2026-09-18. Bump the version when it comes
# back; never edit the string of a version that has been signed under.
CONSENT_VERSION = "2026-09-v1"

_CONSENT_TEMPLATE = (
    "I have reviewed the proposal for {project}, including its Terms and Conditions, and "
    "agree to be bound by them. I intend my typed name to be my electronic signature. I "
    "agree to conduct this transaction electronically and to receive my copy of the signed "
    "agreement by email and in this portal. I confirm I am authorized to sign on behalf of "
    "the customer named in the proposal."
)


# EVERY VERSION EVER SIGNED UNDER STAYS IN THIS MAP. A contract can be rebuilt long after the
# fact (see main.py _signed_contract_pdf), and it has to quote the wording that customer
# actually ticked -- not whatever the wording has since become. Landing the attorney's
# revision means a NEW key and a new CONSENT_VERSION, never an edit to an existing entry:
# editing one silently rewrites the record of every signature already taken under it.
_CONSENT_TEMPLATES: dict[str, str] = {CONSENT_VERSION: _CONSENT_TEMPLATE}


class UnknownConsentVersion(KeyError):
    """Asked for consent wording this build does not carry.

    Raised rather than quietly falling back to the current template. The fallback would put
    today's sentence on a certificate for a signature given under a different one, and
    nothing downstream could tell the difference."""


def consent_text(project_name: Optional[str], version: Optional[str] = None) -> str:
    """The exact sentence the customer ticks, naming their project.

    ONE SOURCE, SERVED TO THE PAGE. The browser renders whatever this returns (via the view
    model) rather than carrying its own copy, because the certificate records this string
    verbatim: two copies of a legal sentence become two sentences the moment either is edited,
    and the certificate would then quote wording the customer never saw."""
    template = _CONSENT_TEMPLATES.get(version or CONSENT_VERSION)
    if template is None:
        raise UnknownConsentVersion("no consent wording on file for version %r" % (version,))
    project = (project_name or "this project").strip() or "this project"
    return template.format(project=project)


# ── who cannot sign ───────────────────────────────────────────────────────────
# Keyed on the work type carried in the pinned draft blob (`data["work_type"]`), the
# same value the proposal tool keys its TEMPLATE_PICKER on.
#
# TWO DIFFERENT REASONS LIVE HERE, and they are not the same shape.
#
# `budget` has no Terms and Conditions section in its template, so a "signed contract" built
# from it would be a signature attached to no terms -- worse than no signature at all.
#
# `gyp` is here for a different reason, added 2026-09-19: its form has nowhere to sign. Kyle's
# proposal artwork paints the ACCEPTANCE row -- SIGNATURE / DATE / PRINTED NAME / TOTAL -- and
# only the DIRECT artwork has it. Gyp has its own form and GC has a third, and neither carries
# that row. The signature is written onto that row now (the tool's acceptance_signature.py), so
# a template without one has no place to put it. Hanz, 2026-09-19, told which templates have the
# block: "Direct only for now."
UNSIGNABLE_WORK_TYPES = frozenset({"budget", "gyp"})

# GC IS AN AUDIENCE, NOT A WORK TYPE, which is why it cannot join the set above. The same
# `polish` job renders on the Direct form for a direct customer and on the GC form for a general
# contractor; only the first has an acceptance row. So the audience has to be read as well, and
# anything that is not Direct cannot be signed.
SIGNABLE_AUDIENCE = "direct"

# Written as a sentence the UI can show as-is. The portal HAS a way to reach the
# estimator — the project thread every customer already uses — so it points there
# rather than at a phone number this repo does not know.
BLOCKED_MESSAGE = (
    "This proposal type doesn't carry Terms and Conditions for e-signature, so it can't be "
    "signed here. Send us a message in this project's thread (or call us) and we'll get you "
    "a signable contract."
)


def draft_audience(data: Optional[dict[str, Any]]) -> str:
    """Which proposal form this job renders on: "Direct", "GC", or whatever was saved.

    `proposal_payload` FIRST, top level second. The payload is what `_generate` actually picked
    the template from, so reading it here means this gate and the tool's own guard cannot
    disagree about which form a job is on -- and disagreeing would mean the portal offering a
    signature the tool then refuses, with the customer watching.

    DEFAULTS TO Direct, which is not a guess: the tool's own `GenerateIn.audience` defaults to
    "Direct", so a blob that never stated one did render the Direct form.
    """
    d = data if isinstance(data, dict) else {}
    payload = d.get("proposal_payload")
    if isinstance(payload, dict):
        a = str(payload.get("audience") or "").strip()
        if a:
            return a
    return str(d.get("audience") or "").strip() or "Direct"


def signing_blocked_reason(work_type: Any, audience: Any = "Direct") -> Optional[str]:
    """Why this proposal cannot be e-signed, or None when it can.

    A SENTENCE, not a code, because both the page and an API error render it unchanged.

    `audience` DEFAULTS TO Direct so an older caller that passes only a work type keeps its
    current answer rather than silently blocking every proposal -- but every live call site
    passes it, and the estimate path reads it through `draft_audience` above.
    """
    if str(work_type or "").strip().lower() in UNSIGNABLE_WORK_TYPES:
        return BLOCKED_MESSAGE
    if str(audience or "Direct").strip().lower() != SIGNABLE_AUDIENCE:
        return BLOCKED_MESSAGE
    return None


# ── what was signed ───────────────────────────────────────────────────────────
def sha256_hex(blob: bytes) -> str:
    return hashlib.sha256(blob or b"").hexdigest()


def short_hash(digest: Optional[str]) -> str:
    """The first 12 hex characters — enough for a human to compare two documents at a
    glance in an email. The full digest lives on the certificate page and in the
    database; this is only ever a convenience."""
    return (digest or "")[:12]


def revision_sha256(data: Optional[dict[str, Any]]) -> str:
    """A fingerprint of the project state the customer was SENT.

    Deterministic by construction: sort_keys puts the blob in one order regardless of
    how psycopg handed it over, and explicit separators stop a Python release changing
    the bytes under us. `default=str` is the catch for anything json cannot encode on
    its own (a Decimal that reached the blob, a date) — str() of a given value is
    stable, so it does not cost determinism, and without it a single odd value would
    raise inside the customer's approval."""
    canonical = json.dumps(data or {}, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False, default=str)
    return sha256_hex(canonical.encode("utf-8"))


def central_stamp(when: datetime) -> str:
    """A signing time in the BUSINESS timezone, pre-formatted for the certificate.

    Reuses followup_rules.BUSINESS_TZ — this repo's one America/Chicago definition,
    already responsible for when a customer's follow-up email may go out. A second
    definition is how two parts of one document come to disagree about what day it was,
    and the container clock is UTC, so `datetime.now()` would put a 7pm Kansas
    signature on the following morning.

    Built with %-codes and string formatting rather than a locale call so it reads the
    same on the container as it does here."""
    local = when.astimezone(BUSINESS_TZ)
    hour = local.strftime("%I").lstrip("0") or "12"
    day = local.strftime("%d").lstrip("0") or "1"
    return "%s %s, %d at %s:%s %s %s" % (
        local.strftime("%B"), day, local.year, hour,
        local.strftime("%M"), local.strftime("%p"), local.strftime("%Z"),
    )


def build_certificate(*, project_name: Optional[str], proposal_id: str,
                      revision_no: Optional[int], signer_name: str,
                      signer_title: Optional[str], signer_email: Optional[str],
                      signed_at: datetime, ip_address: Optional[str],
                      user_agent: Optional[str], options_summary: Optional[str],
                      total: Optional[float], deposit_amount: Optional[float],
                      proposal_pdf_sha256: str, revision_sha: str,
                      consent_version: Optional[str] = None) -> dict[str, Any]:
    """The evidence page's field set, exactly as the proposal tool's
    /api/admin/signed-contract expects it.

    Both timestamps travel. The UTC one is the machine record; the Central one is what a
    reader sees, and it is computed HERE rather than at the far end so the document and
    this database can never disagree about the hour."""
    # A REBUILD PASSES THE VERSION OFF THE APPROVAL ROW. Defaulting to the current one would
    # be right only until the wording changes, after which every rebuilt certificate would
    # start quoting a sentence its signer was never shown.
    version = consent_version or CONSENT_VERSION
    utc = signed_at.astimezone(timezone.utc)
    return {
        "project_name": (project_name or "").strip(),
        "proposal_id": proposal_id,
        "revision_no": int(revision_no) if revision_no else None,
        "signer_name": (signer_name or "").strip(),
        "signer_title": (signer_title or "").strip(),
        "signer_email": (signer_email or "").strip(),
        "signed_at_utc": utc.isoformat(),
        "signed_at_central": central_stamp(utc),
        "ip_address": (ip_address or "").strip(),
        "user_agent": (user_agent or "").strip(),
        "options_summary": (options_summary or "").strip(),
        "total": None if total is None else float(total),
        "deposit_amount": None if deposit_amount is None else float(deposit_amount),
        "consent_text": consent_text(project_name, version),
        "consent_version": version,
        "proposal_pdf_sha256": proposal_pdf_sha256,
        "revision_sha256": revision_sha,
    }


def signing_project_name(proposal_row: Optional[dict[str, Any]],
                         data: Optional[dict[str, Any]]) -> str:
    """The project name that goes INSIDE the consent sentence.

    ONE resolution, deliberately, because two places need it and they must not disagree: the
    view model that shows the customer the sentence, and the certificate that records what they
    agreed to. The draft blob wins over the portal row, matching build_view_model's own order --
    otherwise a project renamed in the tool after publication would put one name on screen and a
    different one in the legal record of the same click."""
    for v in ((data or {}).get("project_name"), (proposal_row or {}).get("project_name")):
        s = str(v or "").strip()
        if s:
            return s
    return "this project"


def signing_block(proposal_row: Optional[dict[str, Any]],
                  data: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Everything the approve card needs, decided server-side.

    The page renders `consent_text` rather than carrying its own copy of the wording. That is the
    whole point of serving it: the certificate quotes this string verbatim, so a second copy in
    the browser becomes a second sentence the moment either is edited, and the legal record would
    then quote wording the customer never read.

    `blocked_reason` is prose, not a code -- the UI shows it unchanged and so does the refusal
    from the download endpoints, so one refusal has exactly one sentence."""
    reason = signing_blocked_reason((data or {}).get("work_type"), draft_audience(data))
    return {
        "work_type": str((data or {}).get("work_type") or "").strip().lower(),
        # Signing is REQUIRED for every work type that has Terms and Conditions to be bound by.
        "required": reason is None,
        "blocked_reason": reason,
        "consent_text": None if reason else consent_text(signing_project_name(proposal_row, data)),
        "consent_version": None if reason else CONSENT_VERSION,
    }


# ── building the document ─────────────────────────────────────────────────────
class ContractUnavailable(RuntimeError):
    """The proposal tool could not build the signed contract (unconfigured, down, or
    it refused the request).

    NEVER FATAL TO AN APPROVAL. The customer pressed a button that means "yes, at this
    price"; that fact is theirs and is recorded whether or not a PDF renderer answered.
    Callers catch this, leave the contract unbuilt for the lazy rebuild to pick up, and
    SAY SO in the emails rather than letting an estimator read "signed" and find no
    attachment.

    The message always names what went wrong, including the upstream body — a refusal
    that says only "refused" costs an SSH session and a container probe to diagnose."""


def render_signed_contract(proposal_pdf: bytes, certificate: dict[str, Any], *,
                           timeout: float = 90.0) -> bytes:
    """PDF bytes: the proposal with the signature certificate appended.

    multipart/form-data, matching the endpoint the proposal tool exposes — the proposal
    PDF is a file part and the certificate is a JSON string part. Same SERVICE_TOKEN
    header as /api/admin/deposit-invoice and /api/admin/proposal-pdf; see invoice.py,
    which this is modelled on."""
    if not (config.PROPOSAL_TOOL_URL and config.SERVICE_TOKEN):
        raise ContractUnavailable("PROPOSAL_TOOL_URL / SERVICE_TOKEN not configured")
    if not proposal_pdf:
        raise ContractUnavailable("no proposal PDF bytes to sign")
    try:
        payload = json.dumps(certificate)
    except Exception as exc:  # noqa: BLE001 — an unencodable value in the certificate
        # Not the network's fault. Blaming the far end for a local encoding problem is
        # what misdirected the first diagnosis of a Decimal reaching the invoice payload.
        raise ContractUnavailable(
            "could not encode the certificate: %s: %s" % (type(exc).__name__, exc)) from exc
    try:
        r = httpx.post(
            config.PROPOSAL_TOOL_URL + _ENDPOINT,
            files={"proposal_pdf": ("proposal.pdf", proposal_pdf, "application/pdf")},
            data={"certificate": payload},
            headers={"X-Service-Token": config.SERVICE_TOKEN},
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        raise ContractUnavailable(
            "proposal tool unreachable: %s: %s" % (type(exc).__name__, exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise ContractUnavailable(
            "could not build the signed-contract request: %s: %s"
            % (type(exc).__name__, exc)) from exc
    if r.status_code != 200:
        # The endpoint answers a failure as {"ok": false, "error": ...}. Carry that
        # error through verbatim: "502" alone names the symptom and nothing else.
        detail = ""
        try:
            detail = str((r.json() or {}).get("error") or "")
        except Exception:  # noqa: BLE001 — a non-JSON body is normal for a proxy error
            detail = (r.text or "")[:200]
        raise ContractUnavailable(
            "proposal tool returned %s%s" % (r.status_code, (": " + detail) if detail else ""))
    if not r.content.startswith(b"%PDF"):
        raise ContractUnavailable(
            "proposal tool returned %d bytes that are not a PDF" % len(r.content))
    return r.content


def contract_filename(project_name: Optional[str]) -> str:
    """`<project> - Signed Contract.pdf`, with anything a mail client or filesystem
    would choke on taken out. A project name reaches here straight from an estimator's
    typing, so it can carry slashes, quotes and newlines."""
    safe = "".join(c for c in (project_name or "") if c.isalnum() or c in " -_.&,()")
    safe = " ".join(safe.split())[:80].strip(" .-") or "Treadwell Proposal"
    return "%s - Signed Contract.pdf" % safe
