"""A customer can tell which price is the job and which is an extra — and which tab they are on.

Hanz, 2026-08-13, two requests against the live customer portal:

    "Label whether the priicing sent to the customer is the base bid or the options"
    "highlight the color in which tab you are in. This is for the deposit of the customer portal"

WHY THE LABELS MATTER MORE THAN THEY SOUND. His screenshot showed "EPOXY $29,942.00" and
"ROOM 1 $15,801.00", both ticked, "Selected total $45,743.00". Nothing on the row said which was
the main scope and which was an add-on. `is_base` had been in the payload from the beginning
(`proposals.pricing_options`) and was rendered nowhere — so the one field that answers the
question was already travelling and simply never shown. A customer who misreads a row here signs
for the wrong number, which is the most expensive kind of unclear UI this app has.

THREE KINDS, NOT TWO. "Option" would be a lie about a value-engineering row: a VE alternative
REPLACES the base bid rather than adding to it, and `updateSelectedTotal` already contributes its
delta rather than its total. Calling it an option would invite a customer to read $24,000 as
$24,000 ON TOP of the base.

THE DEPOSIT TABS WERE ALREADY CORRECT AND INVISIBLE. `showAch`/`showCheck` set `aria-pressed`
properly, so a screen reader always knew which method was active — but no CSS rule rendered it,
and a sighted customer saw two identical buttons above a form. The fix is one rule; the test is
here so nobody deletes it as unused.

EXECUTED, NOT GREPPED: the claim is what the rendered row SAYS, so the harness runs the real
`renderOptions` and reads the HTML it produced. `is_base` being present in the source is exactly
the state that shipped the bug.
"""
import json
import pathlib
import shutil
import subprocess

import pytest

FRONTEND = pathlib.Path(__file__).resolve().parents[2] / "frontend"
HARNESS = pathlib.Path(__file__).resolve().parent / "js" / "portal-pricing-harness.js"

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


@pytest.fixture(scope="module")
def ran():
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    proc = subprocess.run(["node", str(HARNESS), str(FRONTEND)],
                          capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert proc.returncode == 0, (
        "the harness itself failed — read this before assuming a product bug:\n" + proc.stderr)
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ── the labels ───────────────────────────────────────────────────────────────
@needs_node
def test_the_base_bid_is_labelled_as_the_base_bid(ran):
    """The screenshot's exact case: EPOXY $29,942 base, ROOM 1 $15,801 option."""
    rows = ran["baseAndOption"]
    assert [r["name"] for r in rows] == ["EPOXY", "ROOM 1"], rows
    assert rows[0]["kind"] == "Base bid", rows[0]
    assert rows[1]["kind"] == "Option", rows[1]


@needs_node
def test_a_value_engineering_row_is_an_ALTERNATIVE_not_an_option(ran):
    """It replaces the base bid instead of adding to it — `updateSelectedTotal` contributes its
    delta, not its total. Calling it an "Option" would invite a customer to read it as an extra
    charge on top."""
    rows = ran["withAlternative"]
    assert rows[0]["kind"] == "Base bid"
    assert rows[1]["kind"] == "Alternative", (
        "a deduct/VE row is labelled %r, which reads as an addition" % rows[1]["kind"])


@needs_node
def test_every_row_carries_a_label_even_when_there_is_only_one(ran):
    """A single base-only proposal is the common case. "Base bid" on its own still answers "is
    this the whole job?", which is not obvious from a lone number."""
    rows = ran["baseOnly"]
    assert len(rows) == 1 and rows[0]["kind"] == "Base bid", rows


@needs_node
def test_the_labels_survive_approval(ran):
    """After approval the checkboxes are disabled and the page becomes a record of what was
    agreed. That is precisely when "which of these was the base bid" gets asked."""
    rows = ran["approved"]
    assert [r["kind"] for r in rows] == ["Base bid", "Option"], rows


@needs_node
def test_the_base_label_is_visually_distinguished(ran):
    """Three identical grey pills answer the question only if you read all of them. The base one
    is tinted because "which of these is the actual job" is the first question."""
    base = ran["baseAndOption"][0]
    opt = ran["baseAndOption"][1]
    assert base["kindClass"] == "is-base" and opt["kindClass"] == "is-opt"
    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")
    assert ".option .opt-kind.is-base" in css, "the base label has no distinguishing rule"


@needs_node
def test_each_label_explains_itself_on_hover(ran):
    """"Option" is only obvious to whoever wrote the estimate. The title says what selecting it
    does to the total, which is the thing a customer is actually deciding."""
    for key in ("baseAndOption", "withAlternative", "baseOnly"):
        for r in ran[key]:
            assert r["hasTitle"], "%s row %r has no explanatory title" % (key, r["name"])


@needs_node
def test_no_pricing_means_no_stray_labels(ran):
    """"Your pricing is being finalized" is a real state — it must not sprout a badge."""
    assert ran["empty"] == []


# ── the deposit tabs ─────────────────────────────────────────────────────────
@needs_node
def test_the_selected_deposit_method_is_visible_not_only_announced(ran):
    """The state was always tracked (aria-pressed) and never rendered: a screen reader knew which
    method was active while a sighted customer saw two identical buttons above a form they were
    already filling in."""
    t = ran["depositTabs"]
    assert t["jsSetsPressed"], "showAch/showCheck no longer set aria-pressed"
    assert t["markupHasPressed"], "the tab markup lost its aria-pressed attribute"
    assert t["cssStylesPressed"], (
        "nothing styles [aria-pressed=true], so the active deposit tab is invisible again")


@needs_node
def test_the_active_tab_is_FILLED_not_merely_outlined(ran):
    """This page is read in dark mode, where a border tint alone is close to invisible — which is
    how two identical-looking buttons happened in the first place."""
    body = ran["depositTabs"]["pressedBody"]
    assert "background" in body, body
    assert "color" in body, "the filled tab has no contrasting text colour: %r" % body
