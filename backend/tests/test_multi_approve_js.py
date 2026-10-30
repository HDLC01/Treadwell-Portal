"""The "approve several proposals at once" dialog, as the customer's browser runs it.

WHAT THE SCREEN HAS TO GUARANTEE (the server re-checks every one of them; test_approve_several.py):
  1. The approve card's gates hold for EVERY selected proposal: an option chosen, its document
     opened, the served sentence on screen, the box ticked. The button stays locked and says why.
  2. The sentence ticked is the server's, rendered as received, and a tick does not survive the
     sentence changing (another option is another price, which is another agreement).
  3. The request carries the literal `consent: true` and the digest of the sentence on screen, and
     nothing about consent at all when nothing selected can be e-signed.
  4. Afterwards, several invoices are shown and paid ONE BY ONE, never merged into one payment.

EXECUTED through tests/js/multi-approve-harness.js, which lifts the real functions out of app.js.
"""
import json
import pathlib
import shutil
import subprocess

import pytest

FRONTEND = pathlib.Path(__file__).resolve().parents[2] / "frontend"
HARNESS = pathlib.Path(__file__).resolve().parent / "js" / "multi-approve-harness.js"


@pytest.fixture(scope="module")
def ran():
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    proc = subprocess.run(["node", str(HARNESS), str(FRONTEND)],
                          capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert proc.returncode == 0, (
        "the harness itself failed — read this before assuming a product bug:\n" + proc.stderr)
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_the_dialog_is_offered_only_when_two_can_still_be_approved(ran):
    assert ran["offer"]["twoApprovable"] is True
    assert ran["offer"]["oneApprovable"] is False
    assert ran["offer"]["shipsHidden"] is True, "the entry must ship hidden in index.html"


def test_a_row_that_cannot_be_approved_cannot_be_ticked_and_says_why(ran):
    closed = ran["rows"]["closed"]
    assert 'data-pick="tok-c" disabled' in closed
    assert "Breakroom was closed" in closed


def test_each_selected_row_has_its_own_options_and_its_own_document(ran):
    row = ran["rows"]["picked"]
    assert 'data-opt="tok-a" data-label="Lab 1" checked' in row
    assert 'data-opt="tok-a" data-label="Sealer">' in row, "an unticked option is offered too"
    # Opening it in a new tab, at the revision this dialog holds -- and the Terms, the same file.
    assert 'data-doc="tok-a" href="/api/portal/tok-a/pdf?rev=2#view=FitH"' in row
    assert "/api/portal/tok-a/pdf?rev=2#page=2&view=FitH" in row
    assert "Not opened yet" in row


def test_an_unsignable_row_says_it_is_approved_without_a_signature_and_escapes_its_name(ran):
    gyp = ran["rows"]["gyp"]
    assert "Approved without e-signature" in gyp
    assert "<b>" not in gyp and "&lt;b&gt;Floor&lt;/b&gt;" in gyp
    assert "data-doc" not in gyp


def test_the_button_walks_through_every_gate_in_order(ran):
    g = ran["gate"]
    assert g["none"]["disabled"] and g["none"]["hint"] == (
        "Select at least two proposals to approve together.")
    assert g["one"]["disabled"] and g["one"]["hint"] == g["none"]["hint"]
    assert g["unopened"]["disabled"] and g["unopened"]["hint"] == (
        "Open Lab 1's proposal before signing.")
    # The server's own reason, shown as it sent it.
    assert g["serverSaysNo"]["disabled"] and g["serverSaysNo"]["hint"].startswith(
        "Lab 1 was revised since you opened it")
    assert g["untickedConsent"]["disabled"] and g["untickedConsent"]["hint"] == (
        "Tick the box above to sign and approve.")
    assert g["ready"]["disabled"] is False and g["ready"]["hintHidden"] is True
    assert g["ready"]["button"] == "Sign and approve 2 proposals"
    assert g["noOption"]["disabled"] and g["noOption"]["hint"] == (
        "Select at least one option for Lab 1.")


def test_the_sentence_is_the_servers_and_a_tick_does_not_survive_it_changing(ran):
    g = ran["gate"]
    assert g["ready"]["consent"].endswith("Lab 1 ($12,000.00); Lab 2 ($8,000.00).")
    assert g["ready"]["consentHidden"] is False and g["ready"]["ticked"] is True
    assert g["sentenceChanged"]["consent"].endswith("Lab 1 ($13,000.00); Lab 2 ($8,000.00).")
    assert g["sentenceChanged"]["ticked"] is False, "a tick given to the old price carried over"
    assert g["sentenceChanged"]["disabled"] is True


def test_the_request_carries_the_literal_consent_and_the_digest_of_the_sentence_on_screen(ran):
    assert len(ran["sent"]) == 1
    body = ran["sent"][0]
    assert body["consent"] is True
    # The digest the server sent WITH the sentence on screen at the moment of signing (the harness's
    # server answers with the $13,000 sentence from its option change on), not the digest of the
    # earlier $12,000 sentence the box was first ticked under.
    assert body["consent_digest"] == "digest-of:" + "Lab 1 ($13,000.00); Lab 2 ($8,000.00)."[-30:]
    assert body["batch_id"] == "11111111-2222-3333-4444-555555555555"
    assert body["name"] == "Dana Reed" and body["title"] == "Owner"
    assert body["items"] == [
        {"token": "tok-a", "revision_no": 2, "option_labels": ["Lab 1"]},
        {"token": "tok-b", "revision_no": 2, "option_labels": ["Lab 2"]}]


def test_every_selection_change_asks_the_server_again(ran):
    """Totals and the sentence come from the server for the exact selection; nothing is summed
    or worded here."""
    assert ran["previewCalls"][-1] == {"items": [
        {"token": "tok-a", "option_labels": ["Lab 1"]},
        {"token": "tok-b", "option_labels": ["Lab 2"]}]}
    assert {"token": "tok-a", "option_labels": ["Lab 1", "Sealer"]} in ran["previewCalls"][4]["items"]


def test_the_review_list_shows_each_proposal_its_options_total_and_deposit(ran):
    s = ran["summary"]
    assert "<strong>Lab 1</strong>" in s and "$12,000.00" in s and "Deposit (25%) $3,000.00" in s
    assert "<strong>Lab 2</strong>" in s and "$8,000.00" in s and "No deposit" in s


def test_nothing_signable_sends_no_consent(ran):
    u = ran["unsigned"]
    assert u["before"]["disabled"] is False and u["before"]["consentHidden"] is True
    assert u["before"]["button"] == "Approve 2 proposals", "no 'Sign' where nothing is signed"
    assert "consent" not in u["sent"][0] and "consent_digest" not in u["sent"][0]


def test_a_refusal_is_said_and_the_dialog_does_not_claim_success(ran):
    r = ran["refused"]
    assert r["doneShown"] is False
    assert r["alerts"][-1]["msg"].startswith("Nothing was approved.")
    assert "Lab 2 is already approved." in r["alerts"][-1]["msg"]
    assert r["stillPicked"] == ["tok-a"], "the approved one is dropped from the selection"
    assert r["after"]["disabled"] is True


def test_after_approval_each_invoice_is_paid_on_its_own(ran):
    d = ran["done"]
    assert d.count("Pay this deposit") == 2, "one payment per deposit, never a merged one"
    assert 'href="/p/tok-a#proposal/deposit"' in d and 'href="/p/tok-b#proposal/deposit"' in d
    assert 'href="/api/portal/tok-a/deposit-invoice.pdf"' in d
    assert "invoice INV-A" in d and "Your invoice is on its way" in d
    assert 'href="/api/portal/tok-a/signed-contract.pdf"' in d
    assert "/api/portal/tok-c/signed-contract.pdf" not in d, "no contract for an unsigned one"
    assert "No deposit is required for this proposal." in d


def test_the_thank_you_card_lists_the_projects_other_deposits(ran):
    dep = ran["deposits"]
    assert dep["hidden"] is False
    html = dep["html"]
    # The page is tok-a: its own deposit is the card itself; the others are listed.
    assert "tok-a" not in html
    assert "/api/portal/tok-b/deposit-invoice.pdf" in html and "(received)" in html
    assert "/p/tok-b#proposal/deposit" not in html, "a received deposit is not offered again"
    assert "Lab 3" not in html, "a proposal with no deposit has nothing to pay"


def test_a_contract_still_being_built_says_so_and_appears_when_stored(ran):
    """The approval answers before the contracts are built (2026-10-31): the result view says the
    contract is being prepared instead of offering a download that is not there yet, asks the
    server lightly how it is getting on, and swaps the download in once it is stored."""
    b = ran["background"]
    assert b["first"].count("Your signed contract is being prepared — it will be emailed to you.") == 2
    assert "signed-contract.pdf" not in b["first"]
    assert 'href="/p/tok-a#proposal/deposit"' in b["first"], "Pay works before the contracts"
    assert b["firstDelay"] >= 3000, "polled harder than every few seconds"
    assert "being prepared" in b["stillPending"] and "signed-contract.pdf" not in b["stillPending"]
    assert 'href="/api/portal/tok-a/signed-contract.pdf"' in b["after"]
    assert 'href="/api/portal/tok-b/signed-contract.pdf"' in b["after"]
    assert "being prepared" not in b["after"]
    # tok-b's invoice number arrived with the status: its invoice download is offered now.
    assert 'href="/api/portal/tok-b/deposit-invoice.pdf"' in b["after"]
    assert b["asked"] == 2 and b["pollingAfterDone"] == 0, "kept polling after everything was stored"
