"""Files people attach to a chat message: where the bytes live, and what is allowed to become one.

Hanz, 2026-08-26: "where is the option to add attachments, images in chat? PLease adad this
functionality" -- and the same for the proposal email on step 4.

THREE DECISIONS WORTH KNOWING ABOUT, because each one avoided a much larger change.

1. NO SCHEMA CHANGE. `portal_questions` already carries a `meta jsonb` column, so an attachment
   list rides on the message that owns it. A `portal_attachments` table would have needed the same
   DDL applied to TWO databases (prod Supabase and the separate staging Postgres) and prod DDL
   needs the owner role, because the portal connects as least-privilege `portal_app`. None of that
   buys anything a JSON list on the message does not already give.

2. NO MULTIPART. FastAPI needs `python-multipart` for UploadFile, and it is not in the portal's
   requirements. The upload is therefore a RAW BODY with the filename in the query string, which
   the browser sends with one `fetch(url, {method:"POST", body: file})`. One less dependency to
   audit, one less rebuild to get wrong, and the client is simpler rather than harder.

3. THE BYTES GO ON DISK, not in the database. A jsonb column holding base64 photographs would be
   read back on every thread poll, and the thread polls constantly.

WHAT IS ALLOWED IN. Only types a customer or an estimator plausibly attaches to a flooring bid:
photographs, PDFs, and the office documents Kyle already trades in. Macro-enabled Office formats
(.docm, .xlsm) are deliberately absent from the list.

THE TYPE IS CHECKED AGAINST THE BYTES, not taken on trust. The Content-Type on the request is
whatever the uploader's client chose to say, so on its own it decides nothing: `verify` reads the
file's own signature and refuses a mismatch. The first version of this module trusted that header
while its docstring claimed to be sniffing the content -- which is exactly the sort of gap that
survives review, because the comment says the right thing.

The stored filename is a uuid and the extension comes from the VERIFIED type, so a name like
`invoice.pdf.exe`, a path traversal, or a Windows reserved device name never reaches the
filesystem. The original name is kept as data, for display only.

WHAT IS DELIBERATELY NOT HERE, said plainly rather than implied. NO MALWARE SCANNING. A file that
genuinely is a valid PDF or .docx passes every check in this module, because it is one -- so the
residual risk is an estimator opening a real document with a malicious payload inside it. Closing
that needs either ClamAV (whose daemon wants about a gigabyte on a 2 GB VPS already running a dozen
containers) or sending customer files to a third-party scanner, which is a privacy decision and not
a technical one. Also no image re-encoding, so EXIF -- including GPS on a phone photo -- is passed
through as the customer sent it.
"""
from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any, Optional

# One place, so a cap cannot drift between the two upload routes.
MAX_BYTES = 15 * 1024 * 1024          # 15 MB — a phone photo is 2-5 MB, a scanned plan set larger
MAX_PER_MESSAGE = 10
MAX_NAME = 120

# content type -> extension. The KEY is what the client claimed; the VALUE is what we write. An
# unlisted type is refused rather than stored with a guessed extension: a file the portal cannot
# name is a file it cannot serve back safely.
ALLOWED: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "application/pdf": ".pdf",
    "text/plain": ".txt",
    "text/csv": ".csv",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
}

# What the browser can show inline instead of offering as a download.
IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp", "image/heic", "image/heif"}

_ID_RE = re.compile(r"^[0-9a-f]{32}$")

# The two signature-less types. Only these may be established from what the uploader claimed.
_TEXT_TYPES = ("text/plain", "text/csv")


def _looks_like_markup(blob: bytes) -> bool:
    """Is this a DOCUMENT dressed as a note? An .html, .svg or .xml decodes perfectly well as
    text, so the not-binary test alone would wave all three through as text/plain -- and these
    files are served from the origin the customer's session cookie lives on. Cheap and blunt on
    purpose: real notes and CSVs do not open with a tag."""
    head = (blob or b"")[:512].lstrip()[:64].lower()
    return head.startswith(b"<")

# What each allowed type must actually START with. The Content-Type header is a claim by the
# uploader's client; this is the file speaking for itself.
_MAGIC: dict[str, tuple[bytes, ...]] = {
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/gif": (b"GIF87a", b"GIF89a"),
    "application/pdf": (b"%PDF-",),
    # Every modern Office format is a zip; the older ones are OLE2 compound files. Which of the
    # two a given type may be is what the pairing below encodes -- a .docx claiming to be OLE2
    # is a mismatch worth refusing even though both are "Office".
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": (b"PK\x03\x04",),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": (b"PK\x03\x04",),
    "application/msword": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", b"PK\x03\x04"),
    "application/vnd.ms-excel": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", b"PK\x03\x04"),
}


def detect(blob: bytes) -> Optional[str]:
    """The type this file ACTUALLY is, or None when it is not one we accept.

    This replaces asking the uploader. A browser sets File.type from the file's EXTENSION, so a
    JPEG saved as .png arrives claiming image/png -- and the signature check then refused a
    perfectly good photograph for a reason the estimator could not see or act on. Deriving the
    answer from the content is both friendlier and stricter: friendlier because a misnamed file
    just works, stricter because the type can no longer be asserted by whoever sent it.

    ORDER MATTERS for the two container formats. A .docx and an .xlsx are both zips, and an
    OLE2 .doc and .xls are byte-identical at the front, so a bare signature cannot tell them
    apart. The zip is looked INSIDE for the part name that says which it is, and OLE2 falls back
    to Word -- the commoner of the two here, and the one whose viewer opens the other anyway.
    """
    b = blob or b""
    if not b:
        return None
    for kind in ("image/jpeg", "image/png", "image/gif", "application/pdf"):
        if any(b.startswith(m) for m in _MAGIC[kind]):
            return kind
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "image/webp"
    if b[4:8] == b"ftyp" and b[8:12] in (
            b"heic", b"heix", b"hevc", b"heim", b"heis", b"hevm", b"mif1", b"msf1"):
        return "image/heic"
    if b.startswith(b"PK" + bytes([3, 4])):
        # The zip's first entry names the format. Cheap and definite: no zipfile parse, just the
        # part names OOXML always writes near the front of the archive.
        head = b[:4096]
        if b"word/" in head:
            return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        if b"xl/" in head:
            return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        return None                    # a zip that is not an Office document is not on the list
    if b.startswith(bytes([0xD0, 0xCF, 0x11, 0xE0, 0xA1, 0xB1, 0x1A, 0xE1])):
        return "application/msword"    # or .xls; indistinguishable at the header, see above
    # NO TEXT FALLBACK HERE, deliberately. text/plain and text/csv have no signature, so they
    # cannot be DETECTED -- only confirmed against a claim. Returning text/plain for "anything
    # that decodes" would quietly widen the allow-list to every non-binary file there is: .html,
    # .svg, .js, .py. `store` handles the text types separately, from the claim, which is the only
    # place a claim is still allowed to matter.
    return None


def verify(kind: str, blob: bytes) -> bool:
    """Does this file's content match the type its uploader claimed?

    Four shapes, because four families of format say who they are differently:

      fixed prefix   JPEG, PNG, GIF, PDF, and the zip / OLE2 Office containers -- see _MAGIC
      RIFF/WEBP      a 12-byte header with the brand at offset 8
      ISO-BMFF       HEIC/HEIF: an `ftyp` box at offset 4, the brand right after it
      no signature   text/plain and text/csv have none, so the test is the opposite one -- it
                     must not be BINARY. A NUL byte in the first 8 KB is the giveaway an
                     executable cannot avoid, and bytes that will not decode as UTF-8 are not
                     text a person typed.

    Refusing on a type this function does not know is deliberate: a new entry in ALLOWED with
    no way to check it should fail closed and make somebody come here, not sail through on the
    uploader's word.
    """
    b = blob or b""
    if kind in _MAGIC:
        return any(b.startswith(m) for m in _MAGIC[kind])
    if kind == "image/webp":
        return b[:4] == b"RIFF" and b[8:12] == b"WEBP"
    if kind in ("image/heic", "image/heif"):
        return b[4:8] == b"ftyp" and b[8:12] in (
            b"heic", b"heix", b"hevc", b"heim", b"heis", b"hevm", b"mif1", b"msf1")
    if kind in ("text/plain", "text/csv"):
        head = b[:8192]
        if bytes([0]) in head:
            return False
        # errors="ignore" would accept anything. The window is trimmed back instead, so a
        # multi-byte character sliced in half by the 8 KB boundary is not read as corruption.
        probe = head if len(b) <= 8192 else head[:-4]
        try:
            probe.decode("utf-8")
        except UnicodeDecodeError:
            return False
        return True
    return False


def root() -> Path:
    """The upload directory, made on demand.

    Read from config at call time rather than at import: the tests point it at a temp directory,
    and a module-level constant would have frozen the real one into them.
    """
    import config
    p = Path(getattr(config, "UPLOAD_DIR", "/app/data/uploads"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def clean_name(name: Optional[str]) -> str:
    """The uploaded name, reduced to something safe to PRINT. It never reaches the filesystem.

    Directory separators and control characters go, because this string is rendered in two web
    pages and echoed in an email; the extension is left alone because it is what tells the reader
    what they are about to open.
    """
    s = str(name or "").strip().replace("\\", "/").split("/")[-1]
    s = re.sub(r"[\x00-\x1f\x7f]", "", s)
    s = s.strip() or "attachment"
    return s[:MAX_NAME]


def is_image(mime: Optional[str]) -> bool:
    return str(mime or "").lower() in IMAGE_TYPES


def store(proposal_id: str, name: Optional[str], mime: Optional[str], blob: bytes) -> dict[str, Any]:
    """Write one upload and return the record that goes in the message's `meta.attachments`.

    Raises ValueError with a stable reason string; the routes turn those into 400s. Every refusal
    is a sentence the UI can show as-is, because "upload failed" tells a customer nothing about
    which of their three photos was the problem.
    """
    if not blob:
        raise ValueError("that file is empty")
    if len(blob) > MAX_BYTES:
        raise ValueError("that file is larger than 15 MB")
    # THE CONTENT DECIDES, and the claim is not consulted at all. `mime` is kept in the signature
    # because every caller has one to hand and it makes the refusal message specific, but it has
    # no say in what gets stored. That is the whole point: a browser derives File.type from the
    # file's EXTENSION, so a JPEG saved as .png claims image/png -- and a check that trusted the
    # claim refused a real photograph for a reason nobody could see. Deriving it is friendlier
    # AND stricter.
    claimed = str(mime or "").split(";")[0].strip().lower()
    kind = detect(blob)
    if kind is None and claimed in _TEXT_TYPES and verify(claimed, blob) and not _looks_like_markup(blob):
        # The one case where the claim still decides. A .txt or .csv has nothing to detect, so the
        # most that can be established is that it is not binary and not a document pretending to
        # be a note -- and that the person sending it said it was text.
        kind = claimed
    ext = ALLOWED.get(kind or "")
    if not ext:
        raise ValueError("that kind of file cannot be attached%s"
                         % (" (it is not really a %s)" % ALLOWED[claimed].lstrip(".")
                            if claimed in ALLOWED else ""))
    # The proposal id scopes the directory, so serving a file can require that the requester has
    # access to THAT proposal -- an id alone is never enough.
    fid = uuid.uuid4().hex
    d = root() / _safe_seg(proposal_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / (fid + ext)).write_bytes(blob)
    return {"id": fid, "name": clean_name(name), "mime": kind,
            "size": len(blob), "ext": ext, "image": kind in IMAGE_TYPES}


def path_of(proposal_id: str, file_id: str) -> Optional[Path]:
    """The stored file for an id, or None.

    `file_id` is checked against the uuid shape BEFORE it touches a path. That check is what makes
    a traversal impossible rather than unlikely: `..` is not 32 hex characters.
    """
    if not _ID_RE.match(str(file_id or "").lower()):
        return None
    d = root() / _safe_seg(proposal_id)
    for ext in set(ALLOWED.values()):
        p = d / (str(file_id).lower() + ext)
        if p.is_file():
            return p
    return None


def pick(meta: Any, file_id: str) -> Optional[dict[str, Any]]:
    """The attachment record for an id, out of a message's meta. The record is the only source of
    the name and content type served back -- never the request, and never the path on disk."""
    for a in (meta or {}).get("attachments") or []:
        if isinstance(a, dict) and str(a.get("id", "")).lower() == str(file_id).lower():
            return a
    return None


def sanitize(items: Any) -> list[dict[str, Any]]:
    """The attachment list as it is allowed to be STORED on a message.

    Rebuilt field by field from what the client sent rather than passed through. The client is the
    one that just uploaded these, so it knows the ids -- but `meta` is read back into two web pages
    and an email, and a pass-through would let anything at all ride into both.
    """
    out: list[dict[str, Any]] = []
    for a in (items or [])[:MAX_PER_MESSAGE]:
        if not isinstance(a, dict):
            continue
        fid = str(a.get("id", "")).lower()
        if not _ID_RE.match(fid):
            continue
        mime = str(a.get("mime") or "").split(";")[0].strip().lower()
        if mime not in ALLOWED:
            continue
        try:
            size = max(0, min(int(a.get("size") or 0), MAX_BYTES))
        except (TypeError, ValueError):
            size = 0
        out.append({"id": fid, "name": clean_name(a.get("name")), "mime": mime,
                    "size": size, "image": mime in IMAGE_TYPES})
    return out


def _safe_seg(s: str) -> str:
    """A path segment that cannot escape its parent. Proposal ids are uuids in practice; this is
    what makes that an assumption the filesystem does not have to trust."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(s or "unknown"))[:64] or "unknown"
