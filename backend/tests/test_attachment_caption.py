"""An attachment in the thread has to say its own name, on the red bubble as well as the grey.

Hanz, 2026-08-26, looking at the staff side of this same conversation: "when sending out files and
images, also fix how this looks I cant see the name of the file well."

This is the customer half. The two sides show one conversation and they have drifted before, so the
shape, the states, the wording and the geometry are deliberately identical to the drawer's; only the
tokens differ, because each app uses its own.

THREE FAULTS, and only one of them was the one anybody had noticed.

  1. The name lived on the anchor's `title`. On a phone, where this side is mostly read, a tooltip
     cannot be reached at all. So there is a visible caption now, on picture tiles as well as on
     the chips that always had one.

  2. `.att-img` set no colour and no text-decoration of its own. An anchor whose text has no
     colour is drawn in the user agent's link blue with the user agent's underline -- and on the
     customer's own #9E001F bubble that is illegible. It went unnoticed here because on this side
     the tile always had a picture in it and there was no text to see it happen to.

  3. THE FAILURE STATE COULD NEVER HAVE RUN. It was an `onerror="..."` attribute, and this app
     sends `script-src 'self' https://accounts.google.com https://www.gstatic.com` on every
     response with no 'unsafe-inline' -- so the browser refuses an event-handler attribute
     outright. A customer whose photo 404'd got a torn page and the code that was supposed to
     replace it was dead in the markup. It is bound as a listener now, after the paint, next to
     the two listeners renderChat already binds for the same reason.

TWO KINDS OF TEST. The renderer and its states are EXECUTED through att-render-harness.js, on a
small real tree -- the failure path mutates one tile out of several, so which element changed is
the whole question. The colours are resolved through the CASCADE, weight first and source order
only as the tie-break, because "the rule is in the file" is not the same claim as "the rule wins".
"""
import json
import pathlib
import re
import shutil
import subprocess

import pytest

FRONTEND = pathlib.Path(__file__).resolve().parents[2] / "frontend"
HARNESS = pathlib.Path(__file__).resolve().parent / "js" / "att-render-harness.js"

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


@pytest.fixture(scope="module")
def ran():
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    proc = subprocess.run(["node", str(HARNESS), str(FRONTEND)],
                          capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert proc.returncode == 0, (
        "the harness itself failed — read this before assuming a product bug:\n" + proc.stderr)
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert not out["errors"], out["errors"]
    return out


PHOTO = "Slab-north-bay-before-grinding-2026-08-26.jpg"
DOCX = "Ridgeline-Cold-Storage-schedule-rev-C.docx"


# ── 1. the caption ───────────────────────────────────────────────────────────

@needs_node
def test_a_picture_shows_its_filename_as_text_not_as_a_tooltip(ran):
    """THE ASK. Readable without hovering, which on a phone means readable at all."""
    tile = ran["rendered"]["tile"]
    assert tile["hasCaption"], "the image tile has no caption element: %r" % (tile,)
    assert tile["name"] == PHOTO, tile
    assert tile["size"] == "2.3 MB", "the size line is missing; the chips have always had one"


@needs_node
def test_a_document_keeps_the_same_name_and_size_pair(ran):
    """A photo and a .docx in one message must read as two of the same thing.

    They are drawn differently on purpose — a picture is shown and a document cannot be — but both
    use the same `.att-name` / `.att-size` pair, so the second one is not a fifth kind of card."""
    chip = ran["rendered"]["chip"]
    assert chip["name"] == DOCX and chip["size"] == "40 KB", chip


@needs_node
def test_a_long_name_is_handed_over_whole_for_css_to_truncate(ran):
    """The renderer must NOT cut the string.

    Truncation is the stylesheet's job. A name cut here could not be recovered from the title
    either, so the customer would have no way at all to read a long filename."""
    got = ran["longName"]
    assert got["tile"]["name"] == got["given"], (
        "the renderer truncated the filename itself: %r" % (got["tile"]["name"],))
    assert got["tile"]["title"] == got["given"], (
        "the title no longer carries the untruncated name the ellipsis is hiding")


@needs_node
def test_the_tile_has_no_aria_label_over_its_own_visible_caption(ran):
    """An aria-label on the anchor OVERRIDES the text inside it.

    It would announce the filename and swallow the size, to say the thing the caption now says in
    writing. And `alt` stays empty for the same reason: a filename is not a description of a
    photograph, and the browser lays alt text out while the image is still fetching."""
    tile = ran["rendered"]["tile"]
    assert tile["ariaLabel"] is None, tile
    assert tile["imgAlt"] == "", "the filename is back in alt, where it is announced twice"


# ── 2. the failure state, which used to be unreachable ───────────────────────

@needs_node
def test_no_attachment_carries_an_inline_event_handler(ran):
    """THE DEAD CODE. `script-src 'self'` with no 'unsafe-inline' refuses handler attributes, so an
    `onerror=` in this markup is a failure state that reads as implemented and never fires.

    Asserted as a blanket ban rather than on `onerror` alone: the next person reaching for an
    inline handler here will reach for whichever one their problem needs."""
    assert ran["rendered"]["inlineHandlers"] == 0, (
        "an inline event handler is back in the attachment markup, and this app's CSP will refuse "
        "to run it: %s" % ran["rendered"]["html"])


@needs_node
def test_the_error_listener_is_bound_once_however_often_the_thread_repaints(ran):
    """Bound in JavaScript, and latched.

    The thread repaints on every poll, so an unlatched bind would stack a listener per poll on
    every image in the conversation."""
    assert ran["armed"]["tile"]["errorListeners"] == 1, (
        "two calls to attHydrate left %r listeners on one image"
        % ran["armed"]["tile"]["errorListeners"])
    assert ran["armed"]["latched"] == "1", "nothing marks a tile as already armed"


@needs_node
def test_a_failed_photo_keeps_its_tile_and_says_so_in_the_caption(ran):
    """A FAILURE MUST NOT RESIZE ANYTHING.

    Swapping the tile for a small chip shrinks the bubble, and renderChat scrolls the thread to the
    newest message — so a reflow pulls the view out from under whoever is reading, which is the
    exact problem the fixed box exists to prevent. The tile keeps its box, the image-off glyph that
    shipped with the markup is revealed by a class, and the second caption line stops being a size
    and starts being the reason."""
    got = ran["oneFailed"]["failed"]
    assert got["classes"] == ["att-img", "is-failed"], (
        "a failed tile changed shape (%r) — that is a reflow on the error path" % (got["classes"],))
    assert got["size"] == "did not load", got
    assert got["name"] == "first-photo.jpg", "the failure took the filename with it"
    assert got["brokeGlyphs"] == 1, "no image-off glyph; a torn page is what we were replacing"
    assert not got["hasHref"], "a photo that did not load still navigates somewhere"


@needs_node
def test_the_other_photo_in_the_same_message_is_untouched(ran):
    """One tile failing must not mark its neighbour.

    This is the case a globally-scoped DOM stub cannot see: it would hand `attFailed` the first
    `.att-size` in the document whichever tile was passed in, and the test would pass on a version
    that wrote "did not load" into the wrong caption."""
    ok = ran["oneFailed"]["survivor"]
    assert ok["classes"] == ["att-img"], ok
    assert ok["size"] == "2.3 MB" and ok["hasHref"], ok


@needs_node
def test_a_photo_that_failed_before_the_listener_existed_is_still_caught(ran):
    """The cached 404, which is the case the attribute handler used to cover.

    An image can finish before the paint's own script runs, and a listener bound afterwards never
    hears about it. A complete image with no intrinsic width has failed, whether or not anybody was
    listening at the time."""
    got = ran["alreadyBroken"]["tile"]
    assert "is-failed" in got["classes"] and got["size"] == "did not load", got


@needs_node
def test_a_photo_that_simply_loaded_early_is_not_called_broken(ran):
    """The other half of the same check, and the reason it tests naturalWidth rather than complete.

    Every cached image that loads fine is also `complete` by the time attHydrate runs. Treating
    complete as failure would mark every fast photo in the thread as missing."""
    got = ran["completeAndFine"]["tile"]
    assert "is-failed" not in got["classes"], got
    assert got["size"] == "2.3 MB" and got["hasHref"], got


@needs_node
def test_a_failed_document_stays_a_chip(ran):
    """`attFailed` is one function for both shapes now. A version that special-cased the tile and
    rebuilt the chip would pass every test above and lose this one."""
    got = ran["failedChip"]["chip"]
    assert got["classes"] == ["att-file", "is-failed"], got
    assert got["size"] == "did not load" and got["name"] == DOCX, got
    assert not got["hasHref"], "a file that did not load still offers a download"


# ── 3. the cascade ───────────────────────────────────────────────────────────
#
# Weight first, source order only as the tie-break, because that is how the cascade resolves. A
# regex for "is the declaration in the file" passes on a rule that can never apply.

INHERITED = ("color",)


def _rules(css):
    """Every `selector { body }` in the stylesheet, in source order.

    Comments are stripped FIRST, and that is not tidiness: without it the selector capture begins
    at the newline after the previous rule and swallows the comment block in between, so every
    class name written in English inside a comment reads as part of the next rule's selector."""
    css = re.sub(r"/\*[\s\S]*?\*/", "", css)
    return [(m.start(), m.group(1).strip(), m.group(2))
            for m in re.finditer(r"([^{}@][^{}]*?)\{([^{}]*)\}", css)]


def _weigh(sel):
    return (len(re.findall(r"#[\w-]+", sel)),
            len(re.findall(r"\.[\w-]+|\[[^\]]+\]|:[\w-]+", sel)))


def _tokens(compound):
    """One compound selector as a set: its classes, plus `tag:x` when it names an element type.

    Tags matter here for exactly one rule — `.att-img.is-failed img`, which takes the broken
    `<img>` out of the flow — and a resolver that silently ignored the tag reported that rule as
    absent while it was sitting in the file working correctly."""
    got = set("." + c for c in re.findall(r"\.([\w-]+)", compound))
    tag = re.match(r"^([a-zA-Z][\w-]*)", compound)
    if tag:
        got.add("tag:" + tag.group(1).lower())
    return got


def _matches(sel, chain):
    """Does `sel` match the last element of `chain` (the ancestry, outermost first)?

    THE LAST COMPOUND MUST TARGET THE ELEMENT; everything before it walks the ancestors in order.
    Without that distinction `.msg.customer { color:#fff }` looks like it out-weighs
    `.att-img { color:inherit }`, when in fact one paints the bubble and the other paints the
    anchor inside it and they are not competing at all."""
    compounds = [_tokens(c) for c in sel.split() if c.strip()]
    if not compounds or not compounds[-1] or not compounds[-1] <= chain[-1]:
        return False
    i = 0
    for want in compounds[:-1]:
        while i < len(chain) - 1 and not want <= chain[i]:
            i += 1
        if i >= len(chain) - 1:
            return False
        i += 1
    return True


def _resolve(css, chain, prop):
    """The declaration of `prop` that wins for the last element of `chain`.

    For an inherited property a rule on an ancestor is a FALLBACK: it decides the value only when
    nothing targets the element itself, which is exactly what `inherit` means."""
    best = own = None
    for at, sel, body in _rules(css):
        for one in sel.split(","):
            one = one.strip()
            if not one:
                continue
            m = re.search(r"(?:^|;)\s*" + prop + r"\s*:\s*([^;]+)", body)
            if not m:
                continue
            value = m.group(1).strip()
            if _matches(one, chain):
                key = _weigh(one) + (at,)
                if own is None or key > own[0]:
                    own = (key, one, value)
            elif prop in INHERITED and _matches(one, chain[:-1]):
                key = _weigh(one) + (at,)
                if best is None or key > best[0]:
                    best = (key, one, value)
    return own or best


@pytest.fixture(scope="module")
def css():
    return (FRONTEND / "styles.css").read_text(encoding="utf-8")


# In THIS app the red bubble is `.msg.customer` (the customer's own words) and the neutral ones are
# `.msg.staff` and `.msg.peer`. The staff drawer has the same two colours with the speakers
# swapped, which is why every rule below is written per-bubble rather than per-speaker.
def _chain(bubble, *rest):
    """An ancestry as a list of token sets, outermost first. Classes carry their dot so a class
    called `img` could never be mistaken for the element type."""
    return [{".msg", "." + bubble}] + [set("." + c for c in r) for r in rest]


RED_TILE = _chain("customer", ["att-img"])
RED_CHIP = _chain("customer", ["att-file"])
RED_TILE_NAME = _chain("customer", ["att-img"], ["att-cap"], ["att-name"])
RED_TILE_SIZE = _chain("customer", ["att-img"], ["att-cap"], ["att-size"])
RED_CHIP_SIZE = _chain("customer", ["att-file"], ["att-size"])
RED_FAIL_TILE = _chain("customer", ["att-img", "is-failed"], ["att-cap"], ["att-size"])
RED_FAIL_CHIP = _chain("customer", ["att-file", "is-failed"], ["att-size"])
NEUTRAL = ("staff", "peer")


def test_no_attachment_anchor_is_left_with_the_browsers_link_styling(css):
    """FAULT 2: blue and underlined, on dark red.

    `.att-img` had no colour and no text-decoration, and got away with it while it contained only
    a picture. It contains writing now, so it has to answer for it — and so does the chip, on every
    bubble this app draws."""
    chains = [RED_TILE, RED_CHIP]
    for b in NEUTRAL:
        chains += [_chain(b, ["att-img"]), _chain(b, ["att-file"])]
    for chain in chains:
        who = "/".join(sorted(chain[0] | chain[-1]))
        colour = _resolve(css, chain, "color")
        assert colour, "nothing sets a colour on %s" % who
        assert colour[2] in ("inherit", "currentColor"), (
            "%s pins its own colour (%r via %s) instead of taking the bubble's — which is how an "
            "anchor ends up blue on red" % (who, colour[2], colour[1]))
        deco = _resolve(css, chain, "text-decoration")
        assert deco and deco[2] == "none", "%s keeps the user agent underline (%r)" % (who, deco)


def test_a_long_filename_truncates_instead_of_wrapping(css):
    """Three lines of filename inside a chat bubble is worse than an ellipsis.

    All three declarations are load-bearing and each one missing breaks it differently: without
    nowrap it wraps, without overflow:hidden the ellipsis never appears, without text-overflow it
    clips mid-glyph. The tile needs a fixed width too, or the name simply widens it."""
    for prop, want in (("white-space", "nowrap"),
                       ("overflow", "hidden"),
                       ("text-overflow", "ellipsis")):
        got = _resolve(css, RED_TILE_NAME, prop)
        assert got and got[2] == want, (
            "the caption's %s resolves to %r, so a long name does not truncate" % (prop, got))
    width = _resolve(css, RED_TILE, "width")
    assert width and width[2].endswith("px"), (
        "the tile has no fixed width, so a long filename widens it: %r" % (width,))


def test_the_size_line_is_never_left_thinned_by_an_opacity_multiplier(css):
    """THE MEASURED DEFECT, and why it survived: it was only ever wrong on one bubble.

    `opacity: .65` over #E9E8E7 is 4.6:1 and fine. The same rule inside the customer's own red
    bubble is white at 65% over #9E001F: 3.4:1, under the 4.5:1 floor for small text. So the line
    takes a real colour per bubble, and the multiplier goes back to 1 — on the same rule, because
    a separate declaration could tie with it on weight and be decided by file order."""
    chains = [("red tile", RED_TILE_SIZE), ("red chip", RED_CHIP_SIZE)]
    for b in NEUTRAL:
        chains.append((b + " tile", _chain(b, ["att-img"], ["att-cap"], ["att-size"])))
        chains.append((b + " chip", _chain(b, ["att-file"], ["att-size"])))
    for name, chain in chains:
        op = _resolve(css, chain, "opacity")
        assert op and op[2] == "1", (
            "the %s size line is still thinned by opacity %r" % (name, op))
        colour = _resolve(css, chain, "color")
        assert colour and colour[2] not in ("inherit", "currentColor"), (
            "the %s size line has no colour of its own (%r)" % (name, colour))


def test_did_not_load_is_legible_on_the_red_bubble_and_not_only_on_the_grey(css):
    """The old failure note was `#9e001f` — the bubble's own colour, on the bubble. 1.4:1.

    The palette already had the answer, so nothing new had to be invented: --primary-tint (#FFDAD8)
    reads 4.7:1 on the lifted chip and 7.1:1 on the tile's caption plate. The neutral bubbles keep
    --primary, where a dark red is the legible one at 6.1:1.

    Four classes on the winning rules deliberately, so they out-WEIGH the three-class per-bubble
    rules rather than merely following them in the file."""
    for chain in (RED_FAIL_TILE, RED_FAIL_CHIP):
        got = _resolve(css, chain, "color")
        assert got and got[2] == "var(--primary-tint)", (
            "the failure note on the customer's own bubble resolves to %r — check it has not gone "
            "back to being the bubble's own colour" % (got,))
    for b in NEUTRAL:
        for chain in (_chain(b, ["att-img", "is-failed"], ["att-cap"], ["att-size"]),
                      _chain(b, ["att-file", "is-failed"], ["att-size"])):
            got = _resolve(css, chain, "color")
            assert got and got[2] == "var(--primary)", (
                "the failure note on the %s bubble resolves to %r" % (b, got))


def test_the_tile_is_lifted_off_the_red_bubble_and_recessed_into_the_grey_ones(css):
    """FOUND IN A BROWSER, AND ONLY THERE.

    The first version gave the caption one neutral `rgba(0,0,0,.06)` plate for every bubble, on the
    argument that a black wash reads as inset whatever it sits on. It does not. Over #9E001F it
    measures 1.08:1 AGAINST THE BUBBLE — the same colour, to the eye — because darkening something
    already dark barely moves its luminance. On the customer's own bubble the caption floated with
    nothing holding it to its picture, while the identical rule read perfectly on the grey ones.

    The chip had already learned this and carried a white wash for the red bubble; the tile now does
    too, from the same pair of rules, so "what colour is an attachment on this bubble" has one
    answer rather than two that can drift."""
    cases = [(RED_TILE, "rgba(255,255,255", "the customer's own bubble needs it LIFTED"),
             (RED_CHIP, "rgba(255,255,255", "the customer's own bubble needs it LIFTED")]
    for b in NEUTRAL:
        cases.append((_chain(b, ["att-img"]), "rgba(0,0,0", "a light bubble recesses it"))
        cases.append((_chain(b, ["att-file"]), "rgba(0,0,0", "a light bubble recesses it"))
    for chain, want, why in cases:
        got = _resolve(css, chain, "background")
        assert got and got[2].replace(" ", "").startswith(want), (
            "%s — resolved to %r" % (why, got))
    assert (_resolve(css, RED_TILE, "background")[2]
            == _resolve(css, RED_CHIP, "background")[2]), (
        "the tile and the chip are different colours on the same bubble")


def test_a_failed_image_element_is_taken_out_of_the_flow(css):
    """FOUND IN A BROWSER AND BY NOTHING ELSE.

    A failed `<img>` is still in the flow, and Chromium draws its own torn-page mark in the corner
    of the well — the exact glyph this state exists to replace, sitting right beside the
    replacement. Every DOM assertion in this file passed while that was on screen, because the
    element is present and correct and the browser is the one drawing the extra thing.

    The drawer needs no equivalent rule: over there the `<img>` is only ever created once the bytes
    are in hand, so a failed tile has never had one."""
    got = _resolve(css, [{".att-img", ".is-failed"}, {"tag:img"}], "display")
    assert got and got[2] == "none", (
        "a failed photo's <img> is still laid out, so the browser draws a broken-image icon next "
        "to the one we drew: %r" % (got,))


def test_the_composer_chips_are_left_alone(css):
    """`.att-chip` is the queue above the message box, not a bubble, and it is shared with the
    upload-error state. It sits on the page's own cream background where the .65 multiplier is
    fine, and it must not be dragged into the per-bubble rules above — the pair of them is the
    reason those rules name `.att-cap` and `.att-file` instead of just `.att-size`."""
    chain = [{".composer"}, {".att-strip"}, {".att-chip"}, {".att-size"}]
    op = _resolve(css, chain, "opacity")
    assert op and op[1].startswith(".att-chip"), (
        "a bubble rule reached the composer chip and set its opacity: %r" % (op,))
