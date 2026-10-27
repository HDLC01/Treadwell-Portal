"""The customer page draws a deleted proposal as one card with one sentence.

EXECUTED, NOT GREPPED: tests/js/gone-state-harness.js runs the real api() and renderGone() out of
frontend/app.js against a 410 proposal_gone and reports what the page drew. The server half is
test_deleted_proposal.py.
"""
import json
import pathlib
import shutil
import subprocess

import pytest

FRONTEND = pathlib.Path(__file__).resolve().parents[2] / "frontend"
HARNESS = pathlib.Path(__file__).resolve().parent / "js" / "gone-state-harness.js"
SENTENCE = ("This proposal is no longer available. If you have questions, reply to the email or "
            "contact Treadwell.")

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


@pytest.fixture(scope="module")
def ran():
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    proc = subprocess.run(["node", str(HARNESS), str(FRONTEND)],
                          capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert proc.returncode == 0, (
        "the harness itself failed -- read this before assuming a product bug:\n" + proc.stderr)
    return json.loads(proc.stdout.strip().splitlines()[-1])


@needs_node
def test_a_gone_answer_replaces_the_page_with_the_card(ran):
    r = ran["fresh"]
    assert r["gone"] is True
    assert r["portalHidden"] and r["loadingHidden"] and not r["gateHidden"]
    assert "Proposal no longer available" in r["gate"]
    # Escaped: the sentence comes from the server and is drawn as text, never as markup.
    assert "&lt;b&gt;x&lt;/b&gt;" in r["gate"] and "<b>" not in r["gate"]
    # Nothing but the card: no approve, deposit, contract or chat controls.
    for word in ("Approve", "Deposit", "contract", "chat", "$"):
        assert word not in r["gate"], word


@needs_node
def test_the_fallback_sentence_is_hanzs(ran):
    assert SENTENCE in ran["noMessage"]["gate"]


@needs_node
def test_deleted_while_open_stops_polling_and_reloads_once(ran):
    """Two 410s in a row (the poll and an action) reload once, not twice, and the poll stops."""
    r = ran["midSession"]
    assert r["reloads"] == 1 and r["cleared"] == [7] and r["timer"] is None
    assert r["gate"] == "", "the card is the reloaded boot's job, on a page without the shell"


@needs_node
def test_any_other_answer_leaves_the_page_alone(ran):
    for case in ("otherGone", "live"):
        r = ran[case]
        assert r["gone"] is False and r["reloads"] == 0 and r["gate"] == "", case
        assert r["portalHidden"] is False, case


# -- the login page: a signed-in customer with nothing left to open ----------
LOGIN_HARNESS = pathlib.Path(__file__).resolve().parent / "js" / "login-empty-harness.js"


@pytest.fixture(scope="module")
def login():
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    proc = subprocess.run(["node", str(LOGIN_HARNESS), str(FRONTEND)],
                          capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert proc.returncode == 0, (
        "the harness itself failed -- read this before assuming a product bug:\n" + proc.stderr)
    return json.loads(proc.stdout.strip().splitlines()[-1])


@needs_node
def test_signed_in_with_no_proposals_shows_an_empty_state_not_the_login_form(login):
    """Every proposal on this email was deleted. Before, the page drew the sign-in form again,
    which reads as "sign-in failed"."""
    r = login["empty"]
    assert r["loginCalls"] == 0 and r["listHidden"] is False
    assert "No proposals to show" in r["list"]
    assert "reply to the email or contact Treadwell" in r["list"]


@needs_node
def test_the_empty_state_offers_a_different_email(login):
    assert login["emptySwitch"] == {"logout": 1, "loginCalls": 1}


@needs_node
def test_signed_out_and_one_proposal_are_unchanged(login):
    assert login["anon"] == {"loginCalls": 1, "list": ""}
    assert login["one"]["redirect"] == "/p/tok-a"
