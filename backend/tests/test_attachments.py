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
        uploads.store("pid", "p.jpg", "image/jpeg", b"0" * (uploads.MAX_BYTES + 1))
    assert "15 MB" in str(big.value)
    with pytest.raises(ValueError):
        uploads.store("pid", "p.jpg", "image/jpeg", b"")


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
    """The stored file is named from a uuid and an extension chosen from the CONTENT TYPE. The
    uploaded name is kept only to print, so none of these can decide where a byte lands or what
    it is called on disk."""
    rec = uploads.store("pid", name, "image/jpeg", PNG)
    written = list((store / "pid").iterdir())
    assert len(written) == 1
    assert written[0].name == rec["id"] + ".jpg"
    assert written[0].parent == store / "pid", "a file escaped its proposal's directory"
    # And what is kept for display carries no separators or control characters.
    assert "/" not in rec["name"] and "\\" not in rec["name"] and "\x00" not in rec["name"]
    assert len(rec["name"]) <= uploads.MAX_NAME


def test_the_proposal_id_cannot_escape_its_parent_either(store):
    uploads.store("../../evil", "p.jpg", "image/jpeg", PNG)
    assert not (store.parent.parent / "evil").exists()
    assert any(d.is_dir() for d in store.iterdir())


def test_an_id_that_is_not_a_uuid_resolves_to_nothing(store):
    uploads.store("pid", "p.jpg", "image/jpeg", PNG)
    for bad in ("..", "../../etc/passwd", "", "x" * 32, "ABC", "*"):
        assert uploads.path_of("pid", bad) is None, bad


# ── 3. what is allowed to be STORED on a message ─────────────────────────────

def test_sanitize_rebuilds_every_field_and_drops_the_rest(store):
    rec = uploads.store("pid", "slab.jpg", "image/jpeg", PNG)
    out = uploads.sanitize([{
        "id": rec["id"], "name": "slab.jpg", "mime": "image/jpeg", "size": 72,
        "path": "/etc/passwd", "internal": True, "onerror": "alert(1)",
    }])
    assert out == [{"id": rec["id"], "name": "slab.jpg", "mime": "image/jpeg",
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
    rec = uploads.store("pid", "p.jpg", "image/jpeg", PNG)
    assert uploads.pick({"attachments": []}, rec["id"]) is None
    assert uploads.pick({}, rec["id"]) is None
    assert uploads.pick(None, rec["id"]) is None
    assert uploads.pick({"attachments": [{"id": rec["id"], "name": "p.jpg"}]}, rec["id"])


def test_the_record_not_the_request_decides_what_a_file_claims_to_be(store):
    """`pick` returns the stored record, and the route serves the name and content type off THAT.
    A caller cannot ask for a .jpg to come back as text/html."""
    rec = uploads.store("pid", "p.jpg", "image/jpeg", PNG)
    got = uploads.pick({"attachments": [uploads.sanitize([rec])[0]]}, rec["id"])
    assert got["mime"] == "image/jpeg" and got["name"] == "p.jpg"


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

def test_both_send_paths_accept_attachments_without_text():
    """Requiring text as well would make somebody invent a sentence to go with a picture of their
    floor. Asserted on both routes together, because the two sides of one conversation disagreeing
    about what counts as a message is exactly the kind of drift nobody notices until a customer
    reports it."""
    import inspect

    import main
    src = inspect.getsource(main)
    assert src.count("if not text and not atts:") == 2, (
        "one of the two send paths still refuses a message that is only an attachment")


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
