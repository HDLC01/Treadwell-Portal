"""The approve card, as the customer's browser actually runs it.

WHAT THE SCREEN HAS TO GUARANTEE, now that pressing this button is signing a contract:

1. Nobody signs a document they never opened. The card stays locked until the proposal PDF has
   been opened, which is what turns this from browsewrap ("the terms were on the page somewhere")
   into clickwrap ("they opened it, then assented").
2. The sentence they tick is the one the certificate quotes. It is served by the API and rendered
   as received; a copy in the markup would become a second sentence the first time either one was
   edited, and the signed record would then quote wording this customer never read.
3. A Budget Pricing proposal, which carries no Terms and Conditions, is never offered a signature
   at all -- and is never blocked from being approved either.

EXECUTED, NOT GREPPED. Every claim above is about what the page does NOT do, and a source-text
assertion cannot prove an absence: "consent_text" appearing in app.js says nothing about whether
the string on screen came from the server or from a fallback three lines below. The harness runs
the real functions out of app.js against a stub DOM and reports what a customer would see.

The server's half -- api_approve refusing a `consent` that is not the literal boolean, and
signing.signing_block deciding required/blocked -- is test_signed_contract.py's.
"""
import json
import pathlib
import shutil
import subprocess

import pytest

import signing

FRONTEND = pathlib.Path(__file__).resolve().parents[2] / "frontend"
HARNESS = pathlib.Path(__file__).resolve().parent / "js" / "approve-signing-harness.js"

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


# ── the presentation gate ────────────────────────────────────────────────────
@needs_node
def test_the_button_will_not_sign_a_document_that_was_never_opened(ran):
    """Clickwrap needs the document to have been PRESENTED, not merely available. The obvious
    signal was no signal at all: signalProposalViewed() fires on navigating to the proposal step,
    which is already true by the time this card is on screen."""
    gate = ran["pdfGate"]
    assert gate["closed"]["disabled"] is True
    assert gate["closed"]["hint"] == "Please open the full proposal above before signing."
    # And a locked button always says why. A dead control with nothing beside it is how a
    # customer concludes the page is broken and phones instead of signing.
    assert gate["closed"]["hintHidden"] is False


@needs_node
def test_opening_the_document_unlocks_the_signature(ran):
    gate = ran["pdfGate"]
    assert gate["opened"]["hint"] == "Tick the box above to sign and approve."
    assert gate["ticked"]["disabled"] is False
    assert gate["ticked"]["hint"] == ""
    assert gate["ticked"]["consentAgreed"] is True, "the ticked row has to look ticked"


@needs_node
def test_opening_the_proposal_in_a_new_tab_counts_as_opening_it(ran):
    """Same bytes, same endpoint, one link apart. A customer who read the whole proposal in
    another tab and came back to a dead button would be right to think we were broken."""
    assert ran["newTabOpensToo"]["disabled"] is False


@needs_node
def test_a_proposal_with_no_pdf_does_not_lock_the_customer_out(ran):
    """THE TRAP IN THIS GATE. mountPdf() early-returns WITHOUT latching when has_pdf is false, so
    a gate written as `PDF_MOUNTED` alone would leave every customer of a PDF-less proposal
    unable to approve anything, ever. There is no document to open, so the gate is satisfied."""
    no_pdf = ran["noPdfIsNotALockout"]
    assert no_pdf["beforeTick"]["hint"] == "Tick the box above to sign and approve.", (
        "the only thing left should be the consent box, not a document that does not exist")
    assert no_pdf["afterTick"]["disabled"] is False


# ── the consent sentence is the server's, not ours ───────────────────────────
@needs_node
def test_the_consent_wording_comes_from_the_payload(ran):
    """Two payloads in, two labels out. This is the executable form of "the page does not carry
    its own copy": a hardcoded sentence would render identically for both."""
    live = ran["consentIsLive"]
    assert live["a"] != live["b"]
    assert live["a"].startswith("I have reviewed the proposal for Nearman Creek")
    assert live["b"] == "Different wording entirely, for Ashwood Middle School, agreed under v2."


def test_the_consent_wording_is_not_written_into_the_frontend():
    """The other half, and the one that catches a "helpful" fallback being added later.

    Checked against the LIVE registry rather than a copy of today's sentence, so it keeps
    checking the right thing after the attorney's revision lands: whatever signing.py is serving
    must not also exist in a file the browser downloads."""
    sentences = [s.strip() for s in signing.consent_text("Nearman Creek").split(".") if len(s.strip()) > 25]
    assert sentences, "the consent wording changed shape — this test needs rewriting, not deleting"
    for name in ("app.js", "index.html"):
        body = (FRONTEND / name).read_text(encoding="utf-8")
        for sentence in sentences:
            assert sentence not in body, (
                f"{name} carries a copy of the consent wording ({sentence[:40]}…). The "
                "certificate quotes the served string verbatim; a second copy is a second "
                "sentence the moment either one is edited.")


@needs_node
def test_a_missing_consent_sentence_refuses_the_approval(ran):
    """An empty `consent_text` is not a licence to improvise one. An approval recorded against
    wording nobody was shown is worse than an approval that did not happen."""
    r = ran["missingConsentTextRefuses"]
    assert r["disabled"] is True
    assert r["hint"] == "Something's not right — please refresh and try again."
    assert r["hintIsError"] is True


# ── what reaches the server ──────────────────────────────────────────────────
@needs_node
def test_the_approval_carries_the_literal_boolean(ran):
    """api_approve tests `body.get("consent") is not True`, so the JSON string "true" is refused
    on purpose: ESIGN turns on an affirmative act of assent and a stringified one is not evidence
    of one. `is True` here rather than a truthiness check, for exactly the same reason."""
    post = ran["signedPost"]["calls"][0]
    assert post["method"] == "POST" and post["path"] == "/approve"
    assert post["body"]["consent"] is True
    # Everything that was already on this request still is.
    assert post["body"]["name"] == "Marguerite Oyelaran"
    assert post["body"]["title"] == "Director of Facilities"
    assert post["body"]["option_labels"] == ["Base Bid", "ROOM 1"]


@needs_node
def test_an_unticked_box_sends_nothing_at_all(ran):
    """The gate makes this hard to reach with a mouse and a form is always one Enter key away
    from submitting. An unsigned approval fails here rather than travelling to be refused."""
    assert ran["submitWithoutConsent"]["calls"] == []
    assert ran["submitWithoutConsent"]["alerts"] == [
        {"id": "approve-alert", "kind": "error",
         "msg": "Please tick the box to sign and approve this proposal."}]


# ── the Terms are offered before they are confirmed ──────────────────────────
@needs_node
def test_the_read_the_terms_button_appears_with_the_tick(ran):
    """Hanz, 2026-09-22: "for the portal plase create a button I have read the terms and
    conditions".

    The consent sentence says the customer has reviewed the Terms and Conditions. Until this
    button existed the portal never put them in front of anybody -- they are their own pages of
    the proposal, reachable only by opening the PDF and scrolling past the pricing. Asking
    somebody to confirm they have read something you never showed them is the half of a clickwrap
    a dispute attacks first.

    REVEALED FROM THE SAME `required` AS THE TICK, asserted as a pair rather than separately: a
    button offering terms with nothing to agree to is noise, and a tick confirming terms nobody
    was offered is the thing the button exists to fix. One condition, so they cannot disagree.

    Read off the RENDERED node, not the markup -- index.html ships it with `hidden`, and a
    regex over the file cannot tell a button that unhides from one that never does."""
    r = ran["pdfGate"]["closed"]
    assert r["consentRowHidden"] is False, "fixture is wrong: this one is supposed to be signable"
    assert r["termsBtnHidden"] is False, (
        "the consent tick is shown but the Terms are not offered, so the customer is asked to "
        "confirm they have read a document the portal never put in front of them")


@needs_node
def test_no_document_means_no_terms_button_even_though_the_tick_stays(ran):
    """THE SECOND HALF OF THE CONDITION, and the reason it is `required && has_pdf` rather than
    `required` alone. A signable proposal whose PDF has not rendered yet still shows the tick --
    that is deliberate, and noPdfIsNotALockout is the scenario that pins it -- but the Terms
    button would open nothing, and a control that does nothing when pressed is worse than one
    that is not offered."""
    r = ran["noPdfIsNotALockout"]["beforeTick"]
    assert r["consentRowHidden"] is False, "the tick is supposed to survive a missing document"
    assert r["termsBtnHidden"] is True, (
        "a Read-the-Terms button is offered with no document behind it; pressing it opens an "
        "empty popup")


@needs_node
def test_a_proposal_with_nothing_to_sign_offers_no_terms_button(ran):
    """Budget Pricing carries no Terms and Conditions -- which is why signing.py refuses to
    certify one. A button opening terms that are not in the document would be the same false
    claim, one layer up."""
    r = ran["budget"]["rendered"]
    assert r["consentRowHidden"] is True
    assert r["termsBtnHidden"] is True, (
        "a proposal that cannot be signed is offering Terms to read; that template has none")


# ── Budget Pricing: exempt, not blocked ──────────────────────────────────────
@needs_node
def test_budget_pricing_shows_the_servers_own_sentence(ran):
    """Rendered unchanged, because the same sentence answers the same question at the download
    endpoints. Paraphrasing it here would give one refusal two wordings."""
    r = ran["budget"]["rendered"]
    assert r["blockedHidden"] is False
    assert r["blockedText"] == signing.BLOCKED_MESSAGE
    assert r["consentRowHidden"] is True, "there is nothing here to consent to"


@needs_node
def test_budget_pricing_never_offers_to_sign(ran):
    """That template carries no Terms and Conditions. A button saying "sign" over a document with
    nothing to be bound by is the same false claim signing.py refuses to put on a certificate."""
    r = ran["budget"]["rendered"]
    assert r["buttonText"] == "Approve proposal"
    assert r["disabled"] is False, "exempt from signing, not blocked from approving"
    assert r["plainNoteHidden"] is False, (
        "with no consent sentence, the plain note is the only thing telling this customer what "
        "approving means")


@needs_node
def test_budget_pricing_sends_no_consent_field(ran):
    """Not `consent: false` — absent. The server exempts these approvals rather than testing
    them, and a false flag on the row would read as a customer who declined."""
    body = ran["budget"]["calls"][0]["body"]
    assert "consent" not in body, body


# ── the shapes that would otherwise bite ─────────────────────────────────────
@needs_node
def test_a_view_model_without_a_signing_block_behaves_as_it_did_before(ran):
    """The two halves of this feature deploy separately, and this repo has no CD. Between them,
    the browser holds new code against an old payload: that has to approve normally, not refuse
    every customer for a `signing` key the server is not sending yet."""
    r = ran["noSigningBlock"]
    assert r["rendered"]["disabled"] is False
    assert r["rendered"]["consentRowHidden"] is True
    assert "consent" not in r["calls"][0]["body"]


@needs_node
def test_the_signature_field_renders_what_was_typed(ran):
    """The field has to READ as a signature, or a customer does not notice they are signing.
    It is a rendering of the input and nothing else: the recorded name is #ap-name's value."""
    assert ran["preview"]["typed"]["signature"] == "Marguerite Oyelaran"
    assert ran["preview"]["typed"]["signatureHintHidden"] is True
    # The empty state is designed rather than blank — an unexplained ruled line looks like a bug.
    assert ran["preview"]["empty"]["signature"] == ""
    assert ran["preview"]["empty"]["signatureHintHidden"] is False
