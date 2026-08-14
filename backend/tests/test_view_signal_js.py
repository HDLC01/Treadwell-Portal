"""The browser only reports a view when the customer opens the PROPOSAL STEP.

Hanz, 2026-08-13: "it should only move to viewed if they actually click view the proposal in the
chatbox inside the portal. Not by clicking the portal link only. They need first to open the Status
page under the Proposal Step."

EXECUTED, NOT GREPPED. The requirement is a negative — signing in and reading the chat must send
nothing — and no source assertion can prove what code DOESN'T do. The harness runs the real
`applyHashView` and `signalProposalViewed` out of app.js and reports every request they made.

The backend guard is tested in test_viewed_signal.py; this is the half that decides whether the
request is made at all.
"""
import json
import pathlib
import shutil
import subprocess

import pytest

FRONTEND = pathlib.Path(__file__).resolve().parents[2] / "frontend"
HARNESS = pathlib.Path(__file__).resolve().parent / "js" / "view-signal-harness.js"

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


# ── the requirement ──────────────────────────────────────────────────────────
@needs_node
def test_landing_in_the_chat_never_marks_the_proposal_viewed(ran):
    """The whole point. Signing in used to be enough, because the marking sat on the portal's own
    GET — which runs at boot, before the customer has navigated anywhere."""
    assert ran["chatAndStatusNeverMark"] == [], ran["chatAndStatusNeverMark"]


@needs_node
def test_opening_the_proposal_step_reports_the_revision_on_screen(ran):
    posts = ran["proposalMarks"]
    assert len(posts) == 1, posts
    assert posts[0]["method"] == "POST" and posts[0]["path"] == "/viewed"
    assert posts[0]["body"] == {"revision_no": 2}, "the server can't guard without the revision"


@needs_node
def test_the_deposit_tab_counts_as_opening_the_proposal(ran):
    """Deposit and contacts are tabs INSIDE the proposal step, and every follow-up email's
    #proposal/deposit deep link lands on one. Reading the document there is still reading it."""
    assert len(ran["depositTabMarks"]) == 1, ran["depositTabMarks"]


@needs_node
def test_navigating_back_and_forth_reports_once(ran):
    """This runs on every hash change AND every poll-driven re-render. Without the latch a
    customer flicking between chat and proposal would hammer the endpoint."""
    assert len(ran["repeatIsIdempotent"]) == 1, ran["repeatIsIdempotent"]


# ── the case the whole batch exists for ──────────────────────────────────────
@needs_node
def test_a_revision_landing_under_the_customer_is_reported_again(ran):
    """Staff re-send while the customer is sitting on the proposal step: the poll re-renders and
    the document swaps in front of them. That IS a view of the new revision, and the latch has to
    notice the revision moved — otherwise the board never leaves Sent for a customer who is
    literally looking at the new proposal."""
    posts = ran["newRevisionReSignals"]
    assert [p["body"]["revision_no"] for p in posts] == [2, 3], posts


# ── failure and edge shapes ──────────────────────────────────────────────────
@needs_node
def test_a_failed_report_is_retried_on_the_next_navigation(ran):
    """One dropped packet must not cost the proposal its viewed status for that revision. The
    latch is set before the request and reverted on failure — back to the never-signalled
    sentinel, not to null, which is a real revision."""
    f = ran["failureRetries"]
    assert len(f["posts"]) == 2, f["posts"]
    assert f["latchTypeAfterFailure"] == "undefined", f["latchTypeAfterFailure"]


@needs_node
def test_a_row_that_predates_revisions_is_still_reported(ran):
    """Legacy rows carry `revision_no: null` for real. If null were the never-signalled sentinel
    these customers would never be recorded as having read anything — and the latch would also
    re-fire on every single navigation."""
    posts = ran["legacyNullRevision"]
    assert len(posts) == 1, posts
    assert posts[0]["body"] == {"revision_no": None}


@needs_node
def test_navigating_before_the_first_load_is_harmless(ran):
    """A deep link resolves the hash before the view model arrives. No request, and above all no
    exception — this runs inside the router that paints the page."""
    assert ran["noStateIsSafe"] == {"posts": [], "threw": False}
