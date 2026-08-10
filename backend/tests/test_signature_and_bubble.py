"""An emailed reply lands in the thread as a message, not as a business card.

Hanz, 2026-08-11, on a real reply of Will's: "he is telling it is very clutter", then "can we
simplify it to just the reply contents and the date? just specify if its from email".

WHAT THE BUBBLE ACTUALLY CONTAINED. Will replied from his Wabash address and the thread showed:

    Testing if this goes back to will's Treadwell email *WILL* *BUCHANAN* E:
    will@wabashcapitalpartners.com *|* C: 913.777.1849 * * *WABASH CAPITAL*
    www.wabashcapitalpartners.com ( http://www.wabashcapitalpartners.com/ ) *|* ** 1707 E.
    123rd Ter, Olathe, KS 66061

Eleven words of message and a full contact block, run together into one paragraph. Three
separate causes, and fixing one without the others leaves it looking broken:

  1. `strip_quoted` removes the QUOTED HISTORY under a reply. Nothing removed the sender's own
     signature, so every inbound message carried it — on the staff CRM and in the customer's
     own portal, since both render the one stored body.
  2. the bubble had no `white-space: pre-wrap`, so the signature's line breaks collapsed and
     what was six tidy lines became one run. This is why it read as a WALL rather than as a
     footer, and it is why stripping alone would not have been enough.
  3. every bubble carried a "TREADWELL" / "CUSTOMER" label above the text, restating what the
     side and colour of the bubble already said.

THE STRIPPER'S SAFETY RULE, WHICH IS THE POINT OF MOST OF THIS FILE. A signature heuristic that
is too eager eats what the customer wrote, and on this system that text is the record of what was
agreed. So nothing is dropped unless the trailing block contains a HARD signal — an address, a
phone, a labelled field, a URL — and the first line somebody wrote is never dropped at all.
"""
import pathlib
import re

import pytest

import inbound

BACKEND = pathlib.Path(__file__).resolve().parents[1]
PORTAL_FRONTEND = BACKEND.parent / "frontend"

# Verbatim, as Resend delivered it. The one input this whole change exists for.
WILL = ("Testing if this goes back to will's Treadwell email\n"
        "\n"
        "*WILL* *BUCHANAN*\n"
        "E: will@wabashcapitalpartners.com *|* C: 913.777.1849\n"
        "\n"
        "*WABASH CAPITAL*\n"
        "www.wabashcapitalpartners.com ( http://www.wabashcapitalpartners.com/ )\n"
        "*|* ** 1707 E. 123rd Ter, Olathe, KS 66061")


# ── the reported case ────────────────────────────────────────────────────────
def test_wills_signature_is_gone_and_his_sentence_is_not():
    assert inbound.strip_signature(WILL) == "Testing if this goes back to will's Treadwell email"


def test_none_of_his_contact_details_survive():
    """Asserted field by field rather than by equality above, so a partial strip that leaves
    the phone number or the street address behind is a distinct failure with a distinct name."""
    out = inbound.strip_signature(WILL)
    for leaked in ("wabashcapitalpartners", "913.777.1849", "123rd Ter", "Olathe", "BUCHANAN"):
        assert leaked not in out, "%r is still in the chat bubble" % leaked


def test_the_quoted_history_and_the_signature_come_off_together():
    """The two run in sequence in _inbound_body, and a real reply has both."""
    raw = (WILL + "\n\nOn Mon, Aug 10, 2026 at 1:00 PM Treadwell <p@notify.x> wrote:\n"
           "> Your proposal is ready to review.")
    assert inbound.strip_signature(inbound.strip_quoted(raw)) == (
        "Testing if this goes back to will's Treadwell email")


# ── what must NEVER be stripped ──────────────────────────────────────────────
@pytest.mark.parametrize("text,why", [
    ("Can you start on the 14th?", "no signature at all"),
    ("We looked at it.\nCall me on 913-555-1234 before you order the material.",
     "a SENTENCE holding a phone number is content; this is the one that would quietly lose "
     "an instruction, because the phone number makes the line look like a footer"),
    ("We walked the site this morning.\n\nLet me know.\n\nThanks",
     "a bare sign-off trips every soft test, and the block has no hard signal, so nothing goes"),
    ("Did you get my last note?\nYes", "a one-word answer at the end is an answer"),
    ("Approved. Please invoice us at accounts@acme.com for the deposit.",
     "an email address the customer is ASKING us to use"),
    ("Meet at 1707 E. 123rd Ter at 7am and bring the moisture meter.",
     "an address that is the instruction, not a letterhead"),
])
def test_content_is_never_mistaken_for_a_signature(text, why):
    assert inbound.strip_signature(text) == text, why


def test_a_message_that_is_only_a_signature_keeps_its_first_line():
    """The accepted limit, pinned so it is a decision rather than a surprise. Somebody replying
    with nothing but a contact block is pathological, and staff still hold the original email."""
    assert inbound.strip_signature("*WILL*\nC: 913.777.1849") == "*WILL*"


def test_a_short_answer_above_a_full_outlook_block_survives():
    """"Approved." is one word, ends in a period and has no sentence in it, so every soft test
    calls it signature material. The floor at the first written line is what saves it."""
    out = inbound.strip_signature(
        "Approved.\n\nDana Reed\nProject Manager\nAcme Construction LLC\n"
        "O: 816.555.0100 | dana@acme.com\n1200 Main St, Kansas City, MO 64105")
    assert out == "Approved."


@pytest.mark.parametrize("text", ["", "   \n  \n", None])
def test_nothing_in_nothing_out(text):
    assert inbound.strip_signature(text) == ""


def test_an_explicit_delimiter_is_taken_at_its_word():
    assert inbound.strip_signature("Yes please.\n\n--\nDana Reed\nAcme") == "Yes please."
    assert inbound.strip_signature("Sounds good.\n\nSent from my iPhone") == "Sounds good."


def test_a_delimiter_on_the_first_line_is_not_a_signature():
    """Cutting at index 0 would leave an empty message. `cut > 0` is what stops that; without
    it this returns the original by the empty-fallback, which is right but by accident."""
    assert inbound.strip_signature("--\nJust this") == "--\nJust this"


def test_a_long_message_cannot_lose_its_body_to_the_cap():
    """_MAX_SIG_LINES bounds how much a bad match can take. A twenty-line contact block is not
    a thing; a twenty-line message that happens to end in one is."""
    body = "\n".join("Line %d of the walkthrough notes we took on site." % i for i in range(20))
    out = inbound.strip_signature(body + "\n\nDana\nO: 816.555.0100\n1200 Main St, KC, MO 64105")
    assert out.startswith("Line 0 of the walkthrough")
    assert out.count("\n") >= 19, "the cap ate part of the message"
    assert "816.555.0100" not in out


# ── it is wired into the webhook, not just available ─────────────────────────
def test_the_webhook_actually_calls_it():
    """A stripper nothing calls is the shape this bug already had once: strip_quoted was wired
    and worked, and the signature simply had no equivalent."""
    src = (BACKEND / "main.py").read_text(encoding="utf-8")
    m = re.search(r"body_txt = _cap\((.+?), 4000\)", src)
    assert m, "the inbound body pipeline moved; this test needs rewriting"
    assert "strip_signature" in m.group(1), "the inbound body is stored without stripping"
    assert "strip_quoted" in m.group(1), "the quoted history is no longer stripped"


# ── the bubble ───────────────────────────────────────────────────────────────
def _js(path: pathlib.Path, fn: str) -> str:
    """The body of `function fn(` in a JS file, brace-counted, with `//` lines stripped.

    The comment stripping is not optional. These files explain a removal by quoting what was
    removed, so the comment above this very bubble contains the words "You" / "Treadwell" and
    the first version of this test failed on its own explanation. Fourth time in a day.
    """
    src = "\n".join(l for l in path.read_text(encoding="utf-8").splitlines()
                    if not l.strip().startswith("//"))
    m = re.search(r"function " + re.escape(fn) + r"\s*\(", src)
    assert m, "%s() is gone from %s" % (fn, path.name)
    i = src.index("{", m.end())
    depth, j = 0, i
    while j < len(src):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
        j += 1
    pytest.fail("unbalanced braces in %s" % fn)


def test_the_customer_bubble_is_contents_date_and_whether_it_came_by_email():
    body = _js(PORTAL_FRONTEND / "app.js", "renderMsg")
    assert '"You"' not in body and '"Treadwell"' not in body, (
        "the author label is back; the side of the thread already says who wrote it")
    assert "via-email" in body, "an emailed reply is no longer marked as one"
    assert 'class="mbody"' in body, "the text is not in the element pre-wrap is written for"
    assert "${when}" in body, "the date is gone"


def test_the_line_breaks_in_an_emailed_reply_are_rendered():
    """Stripping the signature is only half of it. Without pre-wrap a multi-paragraph customer
    email still arrives as one run of text, which is the complaint restated."""
    css = (PORTAL_FRONTEND / "styles.css").read_text(encoding="utf-8")
    assert re.search(r"\.msg \.mbody\s*\{[^}]*white-space:\s*pre-wrap", css), (
        "the message body does not preserve the sender's line breaks")
    assert re.search(r"\.msg \.mbody\s*\{[^}]*overflow-wrap:\s*anywhere", css), (
        "a long URL can push the bubble past its max-width")


def test_the_dead_who_rule_went_with_the_markup():
    """.msg .who styled an element nothing emits any more. Harmless, but it is the leftover that
    makes the next reader think the label is still there."""
    css = (PORTAL_FRONTEND / "styles.css").read_text(encoding="utf-8")
    assert ".msg .who" not in css and ".msg.customer .who" not in css
