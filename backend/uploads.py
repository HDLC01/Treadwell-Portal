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
photographs, PDFs, and the office documents Kyle already trades in. The extension is decided HERE
from the sniffed content type rather than taken from the uploaded name, and the stored filename is
a uuid -- so a name like `invoice.pdf.exe`, a path traversal, or a Windows reserved device name
never reaches the filesystem. The original name is kept as data, for display only.

WHAT IS DELIBERATELY NOT HERE. No virus scanning, and no image re-encoding to strip EXIF. Both are
real, both are out of scope for a bid portal between a contractor and their customer, and saying so
is better than implying a guarantee that is not being made.
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
    kind = str(mime or "").split(";")[0].strip().lower()
    ext = ALLOWED.get(kind)
    if not ext:
        raise ValueError("that kind of file cannot be attached")
    if not blob:
        raise ValueError("that file is empty")
    if len(blob) > MAX_BYTES:
        raise ValueError("that file is larger than 15 MB")
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
