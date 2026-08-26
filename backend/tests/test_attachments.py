"""Attachments: what is allowed to become a file, and who is allowed to read one back.

Hanz, 2026-08-26: "where is the option to add attachments, images in chat? PLease adad this
functionality".

The interesting half of this feature is not the upload, it is the two places it could go wrong
quietly: a filename that reaches the filesystem, and a file that reaches the wrong reader. Both
have their own section below.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
import uploads  # noqa: E402

# A REAL PNG, and the tests below now have to claim image/png for it. Until the signature check
# landed, several of them stored these bytes as image/jpeg and passed -- which is exactly the
# mismatch the check exists to refuse, so those tests had been asserting against the hole.
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point the store at a temp directory. `uploads.root()` reads config at CALL time exactly so
    this is possible; a module-level constant would have frozen the real path into every test."""
    monkeypatch.setattr(config, "UPLOAD_DIR", str(tmp_path), raising=False)
    return tmp_path


# ── 1. what is allowed in ────────────────────────────────────────────────────

def test_only_the_listed_types_can_be_stored(store):
    """The allow-list is the whole gate. An executable, a script, an unknown type: refused, and
    refused with a sentence the UI can show as-is rather than a generic failure."""
    for bad in ("application/x-msdownload", "text/html", "image/svg+xml", "", "application/zip"):
        with pytest.raises(ValueError) as e:
            uploads.store("pid", "x", bad, PNG)
        assert "cannot be attached" in str(e.value), bad


def test_svg_is_refused_even_though_it_is_an_image(store):
    """Named on its own because it is the one that looks like it belongs. An SVG is a document
    that can carry script, and these are served back from the portal's own origin — where the
    customer's session cookie lives."""
    with pytest.raises(ValueError):
        uploads.store("pid", "logo.svg", "image/svg+xml", b"<svg/>")


def test_an_oversize_or_empty_file_is_refused(store):
    with pytest.raises(ValueError) as big:
        uploads.store("pid", "p.png", "image/png", b"0" * (uploads.MAX_BYTES + 1))
    assert "15 MB" in str(big.value)
    with pytest.raises(ValueError):
        uploads.store("pid", "p.png", "image/png", b"")


def test_the_content_type_may_carry_a_charset(store):
    """Browsers send `text/plain;charset=utf-8`. Splitting on `;` is what stops that being read
    as an unknown type and refused."""
    rec = uploads.store("pid", "notes.txt", "text/plain;charset=utf-8", b"hello")
    assert rec["mime"] == "text/plain"


# ── 2. the uploaded name NEVER reaches the filesystem ────────────────────────

@pytest.mark.parametrize("name", [
    "../../../../etc/passwd",
    "..\\..\\windows\\system32\\config\\sam",
    "invoice.pdf.exe",
    "CON",                       # a Windows reserved device name
    "photo\x00.jpg",
    "a" * 500,
])
def test_a_hostile_filename_is_data_not_a_path(store, name):
    """The stored file is named from a uuid and an extension chosen from the VERIFIED type. The
    uploaded name is kept only to print, so none of these can decide where a byte lands or what
    it is called on disk."""
    rec = uploads.store("pid", name, "image/png", PNG)
    written = list((store / "pid").iterdir())
    assert len(written) == 1
    assert written[0].name == rec["id"] + ".png"
    assert written[0].parent == store / "pid", "a file escaped its proposal's directory"
    # And what is kept for display carries no separators or control characters.
    assert "/" not in rec["name"] and "\\" not in rec["name"] and "\x00" not in rec["name"]
    assert len(rec["name"]) <= uploads.MAX_NAME


def test_the_proposal_id_cannot_escape_its_parent_either(store):
    uploads.store("../../evil", "p.png", "image/png", PNG)
    assert not (store.parent.parent / "evil").exists()
    assert any(d.is_dir() for d in store.iterdir())


def test_an_id_that_is_not_a_uuid_resolves_to_nothing(store):
    uploads.store("pid", "p.png", "image/png", PNG)
    for bad in ("..", "../../etc/passwd", "", "x" * 32, "ABC", "*"):
        assert uploads.path_of("pid", bad) is None, bad


# ── 3. what is allowed to be STORED on a message ─────────────────────────────

def test_sanitize_rebuilds_every_field_and_drops_the_rest(store):
    rec = uploads.store("pid", "slab.png", "image/png", PNG)
    out = uploads.sanitize([{
        "id": rec["id"], "name": "slab.png", "mime": "image/png", "size": 72,
        "path": "/etc/passwd", "internal": True, "onerror": "alert(1)",
    }])
    assert out == [{"id": rec["id"], "name": "slab.png", "mime": "image/png",
                    "size": 72, "image": True}]


@pytest.mark.parametrize("junk", [
    {"id": "nope", "mime": "image/jpeg"},                 # not a uuid
    {"id": "0" * 32, "mime": "application/x-msdownload"},  # not an allowed type
    "a string",
    None,
    42,
])
def test_sanitize_drops_what_it_cannot_vouch_for(junk):
    assert uploads.sanitize([junk]) == []


def test_sanitize_caps_the_count_and_the_claimed_size():
    many = [{"id": "%032x" % i, "mime": "image/png", "name": "x", "size": 10 ** 12}
            for i in range(50)]
    out = uploads.sanitize(many)
    assert len(out) == uploads.MAX_PER_MESSAGE
    assert all(a["size"] <= uploads.MAX_BYTES for a in out), (
        "a claimed size nobody checked would be printed to the customer as fact")


def test_a_size_that_is_not_a_number_does_not_raise():
    """The client supplies this and the client can be wrong. A 500 on a chat message because a
    size arrived as a string would be a strange way to find out."""
    assert uploads.sanitize([{"id": "0" * 32, "mime": "image/png", "size": "big"}])[0]["size"] == 0


# ── 4. only what is on a message can be read back ────────────────────────────

def test_an_uploaded_file_nobody_sent_is_not_fetchable(store):
    """The thread is the access list. Until an id appears in a message's `meta.attachments`, the
    bytes exist and are unreachable — which is what makes an abandoned upload harmless."""
    rec = uploads.store("pid", "p.png", "image/png", PNG)
    assert uploads.pick({"attachments": []}, rec["id"]) is None
    assert uploads.pick({}, rec["id"]) is None
    assert uploads.pick(None, rec["id"]) is None
    assert uploads.pick({"attachments": [{"id": rec["id"], "name": "p.png"}]}, rec["id"])


def test_the_record_not_the_request_decides_what_a_file_claims_to_be(store):
    """`pick` returns the stored record, and the route serves the name and content type off THAT.
    A caller cannot ask for a .jpg to come back as text/html."""
    rec = uploads.store("pid", "p.png", "image/png", PNG)
    got = uploads.pick({"attachments": [uploads.sanitize([rec])[0]]}, rec["id"])
    assert got["mime"] == "image/png" and got["name"] == "p.png"


def test_the_customer_route_does_not_read_staff_only_messages():
    """The leak test_only_the_staff_reader_opts_in caught, pinned where it happened.

    One `_serve_upload` serves both sides. It opted into `internal` for both, so a customer could
    have fetched a file hanging off a staff-only card — "Kevin opened the proposal", the CRM's own
    closed-lost note. Guessing a uuid is not realistic; that is not the point. The rule about who
    may read an internal row should be one rule, not one that relaxes in the place that happens to
    be about bytes."""
    import inspect

    import main
    src = inspect.getsource(main)
    assert "def _serve_upload(proposal_id: str, file_id: str, internal: bool = False)" in src
    # The customer route takes the default; the staff route asks for internal explicitly.
    assert "return _serve_upload(p[\"proposal_id\"], file_id)" in src
    assert "return _serve_upload(proposal_id, file_id, internal=True)" in src


# ── 5. a photo on its own is a message ───────────────────────────────────────

def test_only_the_staff_reply_can_carry_an_attachment():
    """ATTACHMENTS TRAVEL ONE WAY. Hanz, 2026-08-26, after asking how a customer-supplied file
    could be shown to be safe: "I think we apply the sending of the file attachments only to the
    treadwell side."

    That is the strongest answer available to the question, and it is why there is no customer
    upload route. Every defence in uploads.py reduces the risk of taking a stranger's file; none
    removes it, because a genuinely valid PDF carrying a malicious payload is still a genuinely
    valid PDF. Not letting an unknown party write to our disk closes the question.

    Asserted as an absence, because an absence is what somebody re-adds by accident. The staff
    reply keeps the "a photo on its own is a message" rule; the customer's route requires text,
    since a message with neither text nor attachments is nothing at all."""
    import inspect

    import main
    src = inspect.getsource(main)
    assert src.count("if not text and not atts:") == 1, (
        "there should be exactly one send path that accepts a bare attachment — the staff reply")
    assert "async def admin_upload(" in src, "the staff upload route is gone"
    assert "async def api_portal_upload(" not in src, (
        "a customer upload route is back — attachments are staff-send-only")


def test_a_customer_cannot_attach_by_putting_ids_in_the_body():
    """The obvious way round the missing route: post `attachments` to /questions anyway, naming
    ids the estimator uploaded. The customer send path does not read the field at all, so there is
    nothing to sanitize and nothing to get wrong — and a customer cannot pin somebody else's file
    to their own message."""
    import inspect

    import main
    src = inspect.getsource(main.api_post_question)
    assert "uploads.sanitize" not in src, (
        "the customer send path reads attachments again — with no upload route, every id in that "
        "body belongs to somebody else")
    assert "attachments" in src, (
        "the reason the field is ignored is no longer written where somebody would look for it")


# ── 6. the files that ride along with a publish ──────────────────────────────

def _b64(blob: bytes) -> str:
    import base64
    return base64.b64encode(blob).decode()


def test_the_publish_decoder_returns_bytes_for_what_it_accepts():
    import main
    out = main._decode_publish_attachments(
        [{"name": "slab.jpg", "mime": "image/jpeg", "b64": _b64(PNG)}])
    assert out == [("slab.jpg", "image/jpeg", PNG)]


def test_a_type_the_email_may_not_carry_names_the_file_it_refused():
    """Named, because "attachment failed" on a send with four files tells the estimator nothing
    about which one to take out."""
    import main
    with pytest.raises(ValueError) as e:
        main._decode_publish_attachments(
            [{"name": "macro.xlsm", "mime": "application/vnd.ms-excel.sheet.macroEnabled.12",
              "b64": _b64(b"x")}])
    assert "macro.xlsm" in str(e.value)


def test_base64_that_is_not_base64_is_a_400_not_a_500():
    """The browser encodes these, so a corrupt one is a bug on our own side — and the estimator
    still needs a sentence rather than a stack trace, mid-send, on the one action that must not
    fail silently."""
    import main
    with pytest.raises(ValueError) as e:
        main._decode_publish_attachments([{"name": "p.jpg", "mime": "image/jpeg", "b64": "!!!!"}])
    assert "could not be read" in str(e.value)


def test_the_total_is_capped_below_what_resend_would_take():
    """Resend accepts 40 MB. The customer's own mail server is the one that bounces, and they
    would find out from the customer — so the cap is ours, and it is a sentence that says what to
    do instead."""
    import main
    half = b"0" * (6 * 1024 * 1024)
    with pytest.raises(ValueError) as e:
        main._decode_publish_attachments([
            {"name": "a.jpg", "mime": "image/jpeg", "b64": _b64(half)},
            {"name": "b.jpg", "mime": "image/jpeg", "b64": _b64(half)},
        ])
    assert "10 MB" in str(e.value) and "chat" in str(e.value)


def test_the_files_are_decoded_before_the_proposal_row_is_touched():
    """Ordering, pinned. A publish that has already created the row and posted the card, and only
    then finds it cannot read an attachment, leaves the customer a proposal whose email is missing
    what the estimator meant to send — with nothing on screen saying so."""
    import inspect

    import main
    src = inspect.getsource(main.admin_publish)
    assert src.index("_decode_publish_attachments") < src.index("existing = db.get_proposal"), (
        "the attachments are decoded after the proposal row is read — a refusal now happens "
        "half-way through a publish")


def test_one_unreadable_file_does_not_cost_the_customer_their_proposal():
    """`uploads.store` is inside a try in the publish loop: a disk error on one photo drops that
    photo, not the send. The alternative is a 500 on the single action in this product that must
    not fail after the estimator has pressed the button."""
    import inspect

    import main
    src = inspect.getsource(main.admin_publish)
    i = src.index("uploads.store(draft_id")
    assert "except (ValueError, OSError):" in src[i:i + 400], (
        "a failed attachment write is no longer contained — it will take the publish with it")


def test_the_card_the_files_land_on_is_the_newest_one():
    """A revision posts a new card and supersedes the old one. An estimator attaching a photo to
    revision 3 is saying nothing about revision 1, so the update is scoped to the latest card."""
    import inspect

    import db
    sql = " ".join(inspect.getsource(db.attach_to_latest_card).split())
    assert "order by id desc limit 1" in sql
    assert "coalesce(meta, '{}'::jsonb) ||" in sql, (
        "meta is replaced rather than merged — that drops revision_no and superseded, and the "
        "wrong card would be retired on screen")


# ── 7. the declared type has to be true ──────────────────────────────────────
#
# Hanz, before promoting this to prod: "how could we verify if its actually safe or not especially
# if a customer sends it". The answer was weaker than the module's own docstring claimed: the
# Content-Type is a string the uploader's client writes, so the allow-list was checking the claim
# rather than the file. These are the tests for the fix.

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
PNG_REAL = b"\x89PNG\r\n\x1a\n" + b"0" * 32
PDF = b"%PDF-1.7\n" + b"0" * 32
DOCX = b"PK\x03\x04" + b"0" * 32
OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"0" * 32
EXE = b"MZ\x90\x00" + b"\x00" * 32


@pytest.mark.parametrize("kind,blob", [
    ("image/jpeg", JPEG),
    ("image/png", PNG_REAL),
    ("image/gif", b"GIF89a" + b"0" * 20),
    ("image/webp", b"RIFF\x00\x00\x00\x00WEBPVP8 "),
    ("image/heic", b"\x00\x00\x00\x18ftypheic" + b"0" * 16),
    ("application/pdf", PDF),
    ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", DOCX),
    ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", DOCX),
    ("application/msword", OLE2),
    ("application/vnd.ms-excel", OLE2),
    ("text/plain", "cove base — 240 LF".encode()),
    ("text/csv", b"room,sf\nlobby,1200\n"),
])
def test_a_real_file_of_every_allowed_type_passes(kind, blob):
    """All thirteen allowed types have a check. A type in ALLOWED with no check would fail closed,
    which is the right direction — but it would also silently stop working, so each one is here."""
    assert uploads.verify(kind, blob), kind


@pytest.mark.parametrize("kind", sorted(uploads.ALLOWED))
def test_an_executable_cannot_wear_any_of_the_allowed_types(kind):
    """The heart of it. Before this check, an .exe posted with `Content-Type: image/jpeg` was
    stored as <uuid>.jpg — it could not execute from there, because it is served back as
    image/jpeg and a browser simply fails to draw it, but "cannot be exploited today" depends on
    browser sniffing behaviour that is not ours to control."""
    assert not uploads.verify(kind, EXE), kind
    with pytest.raises(ValueError) as e:
        uploads.store("pid", "photo.jpg", kind, EXE)
    assert "not really" in str(e.value)


def test_html_cannot_wear_a_pdfs_clothes():
    """The case that would matter most if it got through: these files are served from the portal's
    own origin, where the customer's session cookie lives."""
    assert not uploads.verify("application/pdf", b"<html><script>alert(1)</script></html>")
    assert not uploads.verify("image/png", b"<svg onload=alert(1)>")


def test_a_type_with_no_check_fails_closed():
    """A new entry in ALLOWED that nobody wrote a signature for must be refused, not waved
    through on the uploader's word. This is what makes forgetting to update verify() a visible
    bug rather than a silent hole."""
    assert not uploads.verify("application/zip", b"PK\x03\x04")
    assert not uploads.verify("", JPEG)


def test_a_binary_file_is_not_text(store):
    """text/plain has no signature, so the test is inverted: it must not be binary. A NUL byte in
    the first 8 KB is the thing an executable cannot hide."""
    assert not uploads.verify("text/plain", EXE)
    assert not uploads.verify("text/csv", b"a,b\n" + bytes([0]) + b"payload")


def test_a_utf8_character_split_by_the_probe_window_is_not_corruption():
    """The probe reads 8 KB. A multi-byte character straddling that boundary is not a malformed
    file, and refusing it would reject a perfectly ordinary long note with an accent in it."""
    text = ("a" * 8190).encode() + "é".encode()      # the 2-byte char lands across the edge
    assert uploads.verify("text/plain", text)


def test_the_stored_extension_comes_from_the_verified_type(store):
    """Not from the name, and not from the header alone — from the type the bytes proved. So the
    file on disk cannot be named something it is not."""
    rec = uploads.store("pid", "whatever.exe", "application/pdf", PDF)
    assert rec["ext"] == ".pdf"
    assert (store / "pid" / (rec["id"] + ".pdf")).is_file()


def test_the_file_response_cannot_be_re_interpreted_by_a_browser():
    """nosniff, because the media type served is the one verify() proved and no browser should be
    second-guessing it. And a CSP, because that is what makes a mistake anywhere upstream
    survivable: no script, no network, no same-origin privileges, on a response whose body a
    customer supplied to an origin their own session lives on."""
    import inspect

    import main
    src = inspect.getsource(main._serve_upload)
    assert '"X-Content-Type-Options": "nosniff"' in src
    assert "default-src 'none'" in src and "sandbox" in src


def test_what_is_not_covered_is_written_down():
    """A docstring that overstated the protection is what hid the original gap — the comment said
    "sniffed content type" while the code read a header. So the limits are now stated in the same
    place, and this test is here to make deleting them a failing change rather than a tidy-up."""
    assert "NO MALWARE SCANNING" in uploads.__doc__
    assert "EXIF" in uploads.__doc__
