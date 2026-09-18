"""The approval IS the signature: consent, hashes, the built contract, and who receives it.

WHAT THIS FEATURE IS. A customer types their legal name, ticks a consent box, and ends up holding
a PDF of the proposal with a signature certificate bound to it. Typed name plus recorded intent,
on the federal ESIGN Act (15 U.S.C. 7001) and the Kansas UETA (K.S.A. 16-1601 et seq.). No drawn
signature, because neither statute asks for one.

THE GAP THAT ACTUALLY MATTERED, and the reason several of these tests are about email and
downloads rather than about hashes: ESIGN 7001(d)/(e) requires that the signer be ABLE TO RETAIN
their own copy. Before this, nobody could. An approval left a row in portal_approvals and an email
naming a total, and that was the whole record the customer ever held.

THE ONE GUARANTEE WORTH BREAKING ON PURPOSE is that a failed or slow PDF build must not cost the
customer their approval. `test_the_approval_survives_*` are that guarantee, stated three ways --
the renderer refusing, the renderer timing out, and the storage write failing -- because it
protects a live customer flow from a dependency in another container.

THESE RUN THE REAL ENDPOINTS. Every seam that leaves the process (the database, Resend, the
proposal tool) is stubbed; everything between the request and those seams is the shipped code.
A source-text assertion here would have been worthless: the thing most likely to break is an
argument that stops being threaded through, and only execution can see that.
"""
import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import email_sender
import main
import signing


PID = "p-signed-0001"
TOKEN = "tok-signed"
PROPOSAL_PDF = b"%PDF-1.7 the document the customer read"
SIGNED_PDF = b"%PDF-1.7 proposal + certificate page"


def _draft(**over):
    d = {"project_name": "Nearman Creek", "work_type": "epoxy", "proposal_lump_sum": 13265.0}
    d.update(over)
    return d


def _row(**over):
    p = {"proposal_id": PID, "token": TOKEN, "customer_email": "dana@acme.com",
         "customer_name": "Dana Reed", "project_name": "Nearman Creek",
         "proposal_status": "viewed", "approved_at": None, "deposit_required": True,
         "current_revision_no": 2, "deposit_amount": 3316.25}
    p.update(over)
    return p


@pytest.fixture
def portal(monkeypatch):
    """The real app with only the seams that leave the process replaced.

    The proposal PDF is seeded straight into main._PDF_CACHE rather than served over HTTP, which
    is not a shortcut: it is the case the feature is built around. The customer has just read the
    document, so its bytes are already cached, and the signature has to be taken over THOSE bytes.
    A test that let the fetch happen would be testing httpx.
    """
    calls = {"approvals": [], "approved": [], "messages": [], "team": [], "customer": [],
             "contracts": [], "automations": []}
    state = {"proposal": _row(), "draft": _draft(), "approval_id": 77,
             "contract_row": None, "approval_record": None,
             "render": lambda pdf, cert, **k: SIGNED_PDF}

    main._PDF_CACHE.clear()
    main._pdf_cache_put(PID, 2, PROPOSAL_PDF)

    monkeypatch.setattr(main, "_require", lambda request, token: state["proposal"])
    monkeypatch.setattr(main, "_session_email", lambda request: "dana@acme.com")
    monkeypatch.setattr(main.db, "get_proposal", lambda pid: state["proposal"])
    monkeypatch.setattr(main.db, "get_pinned_draft_data", lambda p: state["draft"])

    def _add_approval(pid, *a, **k):
        calls["approvals"].append({"args": a, "kwargs": k})
        return state["approval_id"]
    monkeypatch.setattr(main.db, "add_approval", _add_approval)
    monkeypatch.setattr(main.db, "set_approved",
                        lambda pid, total, *a, **k: calls["approved"].append(total))
    monkeypatch.setattr(main.db, "add_message",
                        lambda pid, kind, who, body, **k: calls["messages"].append(body))

    def _upsert(approval_id, proposal_id, **k):
        calls["contracts"].append(dict(approval_id=approval_id, proposal_id=proposal_id, **k))
        state["contract_row"] = {"approval_id": approval_id, "proposal_id": proposal_id,
                                 "pdf": k.get("pdf"), "pdf_sha256": k.get("pdf_sha256"),
                                 "built_at": k.get("built_at")}
    monkeypatch.setattr(main.db, "upsert_signed_contract", _upsert)
    monkeypatch.setattr(main.db, "get_signed_contract", lambda pid: state["contract_row"])
    monkeypatch.setattr(main.db, "latest_approval_record", lambda pid: state["approval_record"])

    # Left REAL: main._notify_customer, so the attachment has to survive its actor/peer fan-out
    # too. Only the address list and the send itself are stubbed.
    monkeypatch.setattr(main.db, "get_recipients", lambda pid: ["dana@acme.com", "pat@acme.com"])
    monkeypatch.setattr(main.email_sender, "proposal_reply_to", lambda t: None)
    monkeypatch.setattr(main.email_sender, "notify_team",
                        lambda subject, body, **k: calls["team"].append({"subject": subject,
                                                                        "body": body, **k}))
    monkeypatch.setattr(main.email_sender, "send_customer_update",
                        lambda email, url, project, heading, body, **k: calls["customer"].append(
                            {"to": email, "heading": heading, "body": body, **k}))
    monkeypatch.setattr(main.automations, "run_on_approval",
                        lambda p, project: calls["automations"].append(project))
    # The one call that leaves for the proposal tool. Its own wire format is covered further down
    # against a stubbed httpx, so here it is a seam and nothing more.
    monkeypatch.setattr(main.signing, "render_signed_contract",
                        lambda pdf, cert, **k: state["render"](pdf, cert, **k))

    tc = TestClient(main.app)
    tc.calls = calls
    tc.state = state
    tc.monkeypatch = monkeypatch
    yield tc
    main._PDF_CACHE.clear()


def _approve(client, **over):
    body = {"name": "Dana Reed", "title": "Owner", "option_labels": ["Base Bid"],
            "date": "2026-09-18", "consent": True}
    body.update(over)
    if body.get("consent") is None:
        body.pop("consent")
    return client.post("/api/portal/%s/approve" % TOKEN, json=body)


def _approval_kwargs(client):
    return client.calls["approvals"][0]["kwargs"]


# ── the consent gate ──────────────────────────────────────────────────────────
def test_consent_missing_is_a_400_and_writes_nothing(portal):
    """THE ORDER IS THE TEST, not just the 400. An approval is the moment a customer reads
    "approved" and believes a contract now exists, so a reason to refuse one has to be spent
    before the first insert -- a refusal arriving after the row is written is a customer told no
    about something that already happened."""
    r = _approve(portal, consent=None)
    assert r.status_code == 400
    assert "tick the box" in r.json()["error"], "the refusal has to name what the customer must do"
    c = portal.calls
    assert c["approvals"] == [], "an approval row was written for an unsigned approval"
    assert c["approved"] == [], "the proposal was marked approved without a signature"
    assert c["team"] == [] and c["customer"] == [], "the approval emails went out"
    assert c["contracts"] == [], "a signed-contract row was written with nothing signed"


@pytest.mark.parametrize("value", [False, "true", 1, "yes", None, {}])
def test_only_the_boolean_true_counts_as_assent(portal, value):
    """ESIGN turns on an AFFIRMATIVE act of assent. A client sending consent:"true" or consent:1
    has sent something a truthiness test would wave through, and a string is what a hand-rolled
    form post produces -- so the check is `is not True` and this is why."""
    r = _approve(portal, consent=value)
    assert r.status_code == 400, "consent=%r was accepted as a signature" % (value,)
    assert portal.calls["approvals"] == []


def test_consent_given_records_the_version_the_customer_actually_saw(portal):
    """The stored version and the wording shown on the page must be the same one, or the
    certificate quotes a sentence nobody read. Both come from signing_block, which is why the
    view model serves the text rather than the browser owning a copy."""
    shown = main.proposals.build_view_model(portal.state["proposal"], portal.state["draft"])
    assert _approve(portal).status_code == 200
    stored = _approval_kwargs(portal)["consent_version"]
    assert stored == shown["signing"]["consent_version"] == signing.CONSENT_VERSION
    assert shown["signing"]["consent_text"] == signing.consent_text("Nearman Creek", stored), (
        "the sentence on screen is not the sentence the certificate will quote")


def test_a_renamed_project_shows_and_certifies_the_same_name(portal):
    """Two places need the project name -- the sentence on screen and the certificate recording
    what was agreed -- and they must not disagree.

    THE FIXTURE CANNOT PROVE THIS, which is why this test overrides it. Its draft and its portal
    row carry the same name, so either resolution order passes; a mutation reversing them was
    caught by nothing. A project renamed in the staff tool AFTER publication is the real case
    that separates the two, and it is the case that would have put one name in front of the
    customer and a different one in the legal record of the same click."""
    portal.state["proposal"] = _row(project_name="Stale Name On The Portal Row")
    portal.state["draft"] = _draft(project_name="Nearman Creek Phase 2")
    shown = main.proposals.build_view_model(portal.state["proposal"], portal.state["draft"])
    seen = {}
    portal.state["render"] = lambda pdf, cert, **k: (seen.update(cert), SIGNED_PDF)[1]
    assert _approve(portal).status_code == 200
    # The blob the customer was SENT wins, matching build_view_model's own order.
    assert "Nearman Creek Phase 2" in shown["signing"]["consent_text"]
    assert "Stale Name On The Portal Row" not in shown["signing"]["consent_text"]
    assert seen["consent_text"] == shown["signing"]["consent_text"], (
        "the certificate quotes a sentence the customer was never shown")
    assert seen["project_name"] == "Nearman Creek Phase 2"


# ── what gets captured ────────────────────────────────────────────────────────
def test_the_approval_row_records_who_signed_from_where_and_on_what(portal):
    """Every field a certificate has to be able to state, taken at the moment of signing rather
    than looked up later."""
    r = portal.post("/api/portal/%s/approve" % TOKEN,
                    json={"name": "Dana Reed", "title": "Owner", "consent": True,
                          "option_labels": ["Base Bid"], "date": "2026-09-18"},
                    headers={"user-agent": "Mozilla/5.0 (Macintosh)",
                             "x-forwarded-for": "203.0.113.9, 10.0.0.1"})
    assert r.status_code == 200
    k = _approval_kwargs(portal)
    assert k["consent_version"] == signing.CONSENT_VERSION
    assert k["user_agent"] == "Mozilla/5.0 (Macintosh)"
    assert k["revision_no"] == 2, "the revision on screen, not whatever it later becomes"
    assert k["revision_sha256"] == signing.revision_sha256(portal.state["draft"])
    assert k["contract_sha256"] == signing.sha256_hex(PROPOSAL_PDF), (
        "the hash is not of the document the customer actually had open")


def test_the_client_ip_on_the_row_is_the_customers_not_the_proxys(portal):
    """_client_ip already reads the left-most X-Forwarded-For hop; this pins that an approval
    goes on carrying it, because on a legal record the IP is evidence."""
    portal.post("/api/portal/%s/approve" % TOKEN,
                json={"name": "Dana Reed", "consent": True, "option_labels": ["Base Bid"]},
                headers={"x-forwarded-for": "203.0.113.9, 10.0.0.1"})
    assert portal.calls["approvals"][0]["args"][5] == "203.0.113.9"


def test_a_hostile_user_agent_cannot_write_an_essay_onto_the_record(portal):
    """User-Agent is attacker-controlled free text that lands on a legal record and is rendered
    on a certificate page. Capped, so it cannot be used to pad the document."""
    portal.post("/api/portal/%s/approve" % TOKEN,
                json={"name": "Dana Reed", "consent": True, "option_labels": ["Base Bid"]},
                headers={"user-agent": "A" * 5000})
    assert len(_approval_kwargs(portal)["user_agent"]) == 500


def test_the_revision_hash_does_not_move_when_the_blob_is_reordered(portal):
    """A fingerprint of the project state has to be a fingerprint of the STATE. psycopg can hand
    the same jsonb back with its keys in another order, and a hash that moved with it would
    report every proposal as altered."""
    a = signing.revision_sha256({"a": 1, "b": {"x": 2, "y": 3}})
    b = signing.revision_sha256({"b": {"y": 3, "x": 2}, "a": 1})
    assert a == b
    assert a != signing.revision_sha256({"a": 1, "b": {"x": 2, "y": 4}}), (
        "the hash ignored a changed value, so it fingerprints nothing")


def test_the_signature_is_taken_over_the_bytes_the_customer_read(portal):
    """No second render. The viewer cached these bytes; the signature hashes the same ones.

    Two fetches would be two LibreOffice passes for one approval and -- far worse -- could
    disagree, putting a hash on the certificate for a document nobody ever saw."""
    def _explode(*a, **k):
        raise AssertionError("the signing path re-fetched the proposal instead of using the cache")
    portal.monkeypatch.setattr(main.httpx, "get", _explode)
    assert _approve(portal).status_code == 200
    assert _approval_kwargs(portal)["contract_sha256"] == signing.sha256_hex(PROPOSAL_PDF)


# ── the contract itself ───────────────────────────────────────────────────────
def test_a_signed_approval_stores_the_built_contract(portal):
    assert _approve(portal).status_code == 200
    assert len(portal.calls["contracts"]) == 1
    row = portal.calls["contracts"][0]
    assert row["approval_id"] == 77 and row["proposal_id"] == PID
    assert row["pdf"] == SIGNED_PDF
    assert row["pdf_sha256"] == signing.sha256_hex(SIGNED_PDF)
    assert row["built_at"] is not None, "a stored contract with no built_at reads as unbuilt"


def test_the_certificate_says_what_was_signed_by_whom_and_when(portal):
    """The certificate is the evidence page. Captured off the real call rather than rebuilt here,
    so a field that stops being passed shows up as a missing key instead of as nothing."""
    seen = {}
    portal.state["render"] = lambda pdf, cert, **k: (seen.update(cert), SIGNED_PDF)[1]
    portal.post("/api/portal/%s/approve" % TOKEN,
                json={"name": "Dana Reed", "title": "Owner", "consent": True,
                      "option_labels": ["Base Bid"], "date": "2026-09-18"},
                headers={"user-agent": "Mozilla/5.0", "x-forwarded-for": "203.0.113.9"})
    assert seen["project_name"] == "Nearman Creek"
    assert seen["proposal_id"] == PID and seen["revision_no"] == 2
    assert seen["signer_name"] == "Dana Reed" and seen["signer_title"] == "Owner"
    assert seen["signer_email"] == "dana@acme.com", "the SESSION decides who signed, not the body"
    assert seen["ip_address"] == "203.0.113.9" and seen["user_agent"] == "Mozilla/5.0"
    assert seen["options_summary"] == "Base Bid" and seen["total"] == 13265.0
    assert seen["deposit_amount"] == 3316.25
    assert seen["consent_version"] == signing.CONSENT_VERSION
    assert seen["consent_text"] == signing.consent_text("Nearman Creek")
    assert seen["proposal_pdf_sha256"] == signing.sha256_hex(PROPOSAL_PDF)
    assert seen["revision_sha256"] == signing.revision_sha256(portal.state["draft"])
    assert json.dumps(seen), "the certificate has to survive being JSON-encoded for the wire"


def test_the_certificate_timestamp_is_central_not_the_containers_utc(portal):
    """The container clock is UTC. A 7pm Kansas signature stamped off it lands on the following
    morning, which on a contract is the wrong DAY. Reuses followup_rules.BUSINESS_TZ -- this
    repo's one America/Chicago definition -- rather than a second opinion about the hour."""
    seen = {}
    portal.state["render"] = lambda pdf, cert, **k: (seen.update(cert), SIGNED_PDF)[1]
    _approve(portal)
    utc = datetime.fromisoformat(seen["signed_at_utc"])
    assert utc.tzinfo is not None, "the machine timestamp has to be unambiguous"
    assert seen["signed_at_central"] == signing.central_stamp(utc)
    assert seen["signed_at_central"].endswith(("CDT", "CST")), seen["signed_at_central"]


def test_a_july_evening_in_kansas_is_not_the_next_day(portal):
    """The concrete version of the rule above, because a timezone test that only checks a suffix
    passes on a converter that does nothing."""
    evening = datetime(2026, 7, 4, 2, 30, tzinfo=timezone.utc)   # 9:30pm July 3rd in Olathe
    assert signing.central_stamp(evening).startswith("July 3, 2026 at 9:30 PM")


# ── the guarantee: a slow dependency never costs an approval ──────────────────
def test_the_approval_survives_the_renderer_refusing(portal):
    """THE GUARANTEE. The proposal tool is another container running LibreOffice; letting it
    decide whether an approval happened is how a customer ends up pressing Approve three times."""
    def _refuse(pdf, cert, **k):
        raise signing.ContractUnavailable("proposal tool returned 502: template missing")
    portal.state["render"] = _refuse
    r = _approve(portal)
    assert r.status_code == 200 and r.json()["ok"] is True
    assert portal.calls["approved"] == [13265.0], "the approval itself was lost"
    assert len(portal.calls["approvals"]) == 1
    assert portal.calls["automations"] == ["Nearman Creek"], "the downstream automations were skipped"
    # The row is written UNBUILT, which is what the download path rebuilds from.
    assert len(portal.calls["contracts"]) == 1
    assert portal.calls["contracts"][0]["pdf"] is None
    assert portal.calls["contracts"][0]["built_at"] is None


def test_the_approval_survives_the_renderer_timing_out(portal):
    """A timeout is the failure that actually happens in production, and it is not the same code
    path as a refusal: httpx raises rather than answering, so ContractUnavailable is raised from
    a different place. Both have to land on the customer as a completed approval."""
    import httpx as _httpx

    def _hang(pdf, cert, **k):
        raise _httpx.ReadTimeout("timed out")
    portal.state["render"] = _hang
    r = _approve(portal)
    assert r.status_code == 200 and r.json()["ok"] is True
    assert portal.calls["approved"] == [13265.0]
    assert portal.calls["contracts"][0]["pdf"] is None


def test_the_approval_survives_the_contract_store_failing(portal):
    """The other half. A database that refuses the blob must not take the approval with it --
    and must not throw away a PDF we already hold and are about to email."""
    portal.monkeypatch.setattr(
        main.db, "upsert_signed_contract",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("relation does not exist")))
    r = _approve(portal)
    assert r.status_code == 200 and r.json()["ok"] is True
    assert portal.calls["approved"] == [13265.0]
    staff = portal.calls["team"][0]
    assert staff["attachments"], "the built contract was thrown away because storing it failed"


def test_a_failed_build_never_surfaces_to_the_customer_as_an_error(portal):
    """Not merely a 200: the body must not carry an error the page would render. The customer
    approved; the renderer's problem is ours."""
    def _explode(pdf, cert, **k):
        raise ValueError("something nobody anticipated")
    portal.state["render"] = _explode
    r = _approve(portal)
    assert r.status_code == 200
    assert r.json() == {"ok": True}


# ── who is told, and what they are told ───────────────────────────────────────
def test_both_emails_carry_the_signed_contract(portal):
    """Staff AND customer. The customer's copy is the ESIGN 7001(d)/(e) retention requirement,
    not a courtesy -- before this the only thing they kept was an email naming a total."""
    assert _approve(portal).status_code == 200
    staff = portal.calls["team"][0]
    assert staff["attachments"] == [("Nearman Creek - Signed Contract.pdf", SIGNED_PDF)]
    assert signing.short_hash(signing.sha256_hex(SIGNED_PDF)) in staff["body"], (
        "the staff email does not name the fingerprint of what it attached")
    sent = portal.calls["customer"]
    assert len(sent) == 2, "one of the project's contacts got no copy"
    for msg in sent:
        assert msg["attachments"] == [("Nearman Creek - Signed Contract.pdf", SIGNED_PDF)], (
            "%s did not receive the contract" % msg["to"])


def test_the_peer_copy_carries_it_too(portal):
    """_notify_customer sends the actor a receipt and everyone else a third-person heads-up. The
    wording differs; what is enclosed must not. A signed contract belongs to every contact on the
    project, not to whoever happened to click."""
    assert _approve(portal).status_code == 200
    actor = [m for m in portal.calls["customer"] if m["to"] == "dana@acme.com"][0]
    peer = [m for m in portal.calls["customer"] if m["to"] == "pat@acme.com"][0]
    assert actor["heading"] != peer["heading"], "the two versions collapsed into one"
    assert actor["attachments"] == peer["attachments"] == [
        ("Nearman Creek - Signed Contract.pdf", SIGNED_PDF)]


def test_a_failed_build_says_so_in_both_emails_instead_of_arriving_thinner(portal):
    """THE PUBLISH BUG, IN ITS OTHER SHAPE. A publish once refused an attachment after the row
    was written, logged it, and returned 200 -- the estimator read "Sent" and the customer got an
    email with nothing attached. An estimator must never read "APPROVED" and assume paperwork
    exists."""
    def _refuse(pdf, cert, **k):
        raise signing.ContractUnavailable("proposal tool unreachable: ConnectError: refused")
    portal.state["render"] = _refuse
    assert _approve(portal).status_code == 200
    staff = portal.calls["team"][0]
    assert staff["attachments"] is None
    assert "could not be built" in staff["body"] and "staff tool" in staff["body"]
    for msg in portal.calls["customer"]:
        assert msg["attachments"] is None
        assert "being prepared" in msg["body"], (
            "the customer was told nothing about the copy they are entitled to")


def test_the_filename_survives_a_project_name_with_slashes_in_it(portal):
    """Project names come straight from an estimator's typing and reach a mail client as a
    filename. A slash there is a broken attachment, not a subdirectory."""
    portal.state["draft"] = _draft(project_name='Bldg "A" / Phase 2')
    assert _approve(portal).status_code == 200
    name = portal.calls["team"][0]["attachments"][0][0]
    assert name == "Bldg A Phase 2 - Signed Contract.pdf"
    assert "/" not in name and '"' not in name


# ── Budget Pricing: no Terms and Conditions, so no signature ──────────────────
def test_a_budget_proposal_is_approved_without_being_signed(portal):
    """The Budget Pricing template carries no Terms and Conditions section at all. A certificate
    saying "I have reviewed the proposal, including its Terms and Conditions" against a document
    with none is a false statement in a legal record.

    So the work type is EXEMPT here rather than blocked: requiring consent would lock those
    customers out of approving anything, and the page steers them to talk to us instead."""
    portal.state["draft"] = _draft(work_type="budget")
    r = _approve(portal, consent=None)
    assert r.status_code == 200 and r.json()["ok"] is True
    assert portal.calls["approved"] == [13265.0]


def test_a_budget_approval_records_no_consent_and_no_hashes(portal):
    """Hashes with no certificate attesting to them would read, later, as evidence of a signature
    that was never taken."""
    portal.state["draft"] = _draft(work_type="budget")
    _approve(portal, consent=None)
    k = _approval_kwargs(portal)
    assert k["consent_version"] is None
    assert k["revision_sha256"] is None and k["contract_sha256"] is None


def test_a_budget_approval_creates_no_signed_contract_row(portal):
    """ABSENCE, not a marker column, and that is a deliberate choice. A row with a NULL pdf is
    indistinguishable from a build that failed and is waiting for the lazy rebuild -- and that
    rebuild would then try to certify a proposal with no Terms and Conditions, which is exactly
    the falsehood this whole exemption exists to prevent. No row is unambiguous."""
    portal.state["draft"] = _draft(work_type="budget")
    _approve(portal, consent=None)
    assert portal.calls["contracts"] == []


def test_a_budget_approval_email_promises_no_contract(portal):
    """Silence, not an apology. A line saying the contract is "being prepared" would invent an
    expectation the customer never had and we are never going to meet."""
    portal.state["draft"] = _draft(work_type="budget")
    _approve(portal, consent=None)
    staff = portal.calls["team"][0]
    assert staff["attachments"] is None
    assert "contract" not in staff["body"].lower()
    for msg in portal.calls["customer"]:
        assert "being prepared" not in msg["body"]


def test_ticking_consent_on_a_budget_proposal_still_certifies_nothing(portal):
    """A hand-rolled client can send consent:true on anything. The exemption is decided by the
    WORK TYPE, never by what the body claims -- otherwise the block is advice, not a rule."""
    portal.state["draft"] = _draft(work_type="budget")
    assert _approve(portal, consent=True).status_code == 200
    assert portal.calls["contracts"] == []
    assert _approval_kwargs(portal)["consent_version"] is None


def test_the_page_is_told_why_a_budget_proposal_cannot_be_signed(portal):
    """A sentence the UI shows as-is, and it has to point somewhere real. This portal HAS a way
    to reach the estimator -- the project thread every customer already uses."""
    block = main.proposals.build_view_model(portal.state["proposal"], _draft(work_type="budget"))["signing"]
    assert block["required"] is False
    assert block["consent_text"] is None and block["consent_version"] is None
    assert "Terms and Conditions" in block["blocked_reason"]
    assert "thread" in block["blocked_reason"]


# ── the customer's copy, and the lazy rebuild ─────────────────────────────────
def _signed_row(pdf=None, **over):
    row = {"approval_id": 77, "proposal_id": PID, "pdf": pdf,
           "pdf_sha256": signing.sha256_hex(pdf) if pdf else None,
           "built_at": datetime.now(timezone.utc) if pdf else None}
    row.update(over)
    return row


def _approval_record(**over):
    rec = {"id": 77, "name": "Dana Reed", "title": "Owner", "approver_email": "dana@acme.com",
           "signed_at": "2026-09-18T23:04:00+00:00", "ip": "203.0.113.9",
           "user_agent": "Mozilla/5.0", "option_label": "Base Bid", "total": 13265.0,
           "consent_version": signing.CONSENT_VERSION, "revision_no": 2,
           "revision_sha256": "deadbeef", "contract_sha256": signing.sha256_hex(PROPOSAL_PDF)}
    rec.update(over)
    return rec


def test_the_customer_can_download_a_contract_that_was_already_built(portal):
    portal.state["contract_row"] = _signed_row(SIGNED_PDF)
    r = portal.get("/api/portal/%s/signed-contract.pdf" % TOKEN)
    assert r.status_code == 200
    assert r.content == SIGNED_PDF
    assert r.headers["content-type"] == "application/pdf"


def test_the_download_is_private_and_never_stored_in_a_shared_cache(portal):
    """One customer's executed contract. A shared cache holding it is a disclosure, not a saving
    -- the same posture the deposit invoice already takes."""
    portal.state["contract_row"] = _signed_row(SIGNED_PDF)
    r = portal.get("/api/portal/%s/signed-contract.pdf" % TOKEN)
    assert "no-store" in r.headers["cache-control"] and "private" in r.headers["cache-control"]
    assert 'filename="Nearman Creek - Signed Contract.pdf"' in r.headers["content-disposition"]


def test_a_null_pdf_is_built_on_the_first_download(portal):
    """THE SELF-HEAL. api_approve deliberately leaves the row unbuilt when the renderer is down,
    so this is a normal path, not an error path -- the same shape as the proposal tool's Done page
    re-generating a download token a restart expired."""
    portal.state["contract_row"] = _signed_row(None)
    portal.state["approval_record"] = _approval_record()
    r = portal.get("/api/portal/%s/signed-contract.pdf" % TOKEN)
    assert r.status_code == 200 and r.content == SIGNED_PDF
    assert len(portal.calls["contracts"]) == 1, "the rebuilt contract was not stored"
    assert portal.calls["contracts"][0]["pdf"] == SIGNED_PDF
    assert portal.calls["contracts"][0]["built_at"] is not None


def test_the_rebuild_certifies_the_signature_that_was_given_not_this_moment(portal):
    """A rebuilt certificate stamped with the time somebody pressed Download would be a confident
    statement of something false. Everything on it comes off the approval ROW."""
    seen = {}
    portal.state["render"] = lambda pdf, cert, **k: (seen.update(cert), SIGNED_PDF)[1]
    portal.state["contract_row"] = _signed_row(None)
    portal.state["approval_record"] = _approval_record()
    assert portal.get("/api/portal/%s/signed-contract.pdf" % TOKEN).status_code == 200
    assert seen["signed_at_utc"] == "2026-09-18T23:04:00+00:00"
    assert seen["signer_name"] == "Dana Reed" and seen["ip_address"] == "203.0.113.9"
    assert seen["user_agent"] == "Mozilla/5.0"
    assert seen["consent_version"] == signing.CONSENT_VERSION


def test_a_rebuild_quotes_the_wording_that_customer_ticked(portal):
    """Consent wording is versioned and attorney review is still pending, so it WILL change. A
    rebuild that quoted today's sentence for a signature given under an older one would rewrite
    the record. The old template stays in the map; an absent one is refused rather than guessed."""
    portal.state["contract_row"] = _signed_row(None)
    portal.state["approval_record"] = _approval_record(consent_version="1999-v0")
    r = portal.get("/api/portal/%s/signed-contract.pdf" % TOKEN)
    assert r.status_code == 502
    assert "this version of the portal" in r.json()["error"]
    assert portal.calls["contracts"] == [], "a certificate was built quoting invented wording"


def test_a_rebuild_refuses_when_the_document_moved_under_the_signature(portal):
    """Binding a certificate that attests to one hash onto bytes carrying a different one would
    produce a contract that misstates what was agreed -- the single worst thing this feature
    could emit."""
    portal.state["contract_row"] = _signed_row(None)
    portal.state["approval_record"] = _approval_record(contract_sha256="a" * 64)
    r = portal.get("/api/portal/%s/signed-contract.pdf" % TOKEN)
    assert r.status_code == 409
    assert "changed since it was signed" in r.json()["error"]
    assert portal.calls["contracts"] == []


def test_a_proposal_nobody_signed_has_no_contract_to_download(portal):
    portal.state["contract_row"] = None
    portal.state["approval_record"] = None
    r = portal.get("/api/portal/%s/signed-contract.pdf" % TOKEN)
    assert r.status_code == 404
    assert "has not been signed" in r.json()["error"]


def test_a_budget_proposal_refuses_the_download_with_the_same_sentence(portal):
    """One refusal, one sentence. The download says exactly what the approve card says."""
    portal.state["draft"] = _draft(work_type="budget")
    r = portal.get("/api/portal/%s/signed-contract.pdf" % TOKEN)
    assert r.status_code == 400
    assert r.json()["error"] == signing.BLOCKED_MESSAGE


def test_the_download_needs_a_session_not_just_the_link(portal):
    """`/p/<token>` is a deep link, never the authorization gate -- the same rule the whole portal
    runs on. Someone forwarded the email is not someone entitled to the executed contract."""
    portal.monkeypatch.setattr(main, "_require", lambda request, token: None)
    r = portal.get("/api/portal/%s/signed-contract.pdf" % TOKEN)
    assert r.status_code == 401


def test_a_rebuild_that_fails_is_a_502_and_not_an_empty_pdf(portal):
    """The download is the customer's retention right. A zero-byte "contract" would look like one
    until they opened it."""
    def _refuse(pdf, cert, **k):
        raise signing.ContractUnavailable("proposal tool returned 500")
    portal.state["render"] = _refuse
    portal.state["contract_row"] = _signed_row(None)
    portal.state["approval_record"] = _approval_record()
    r = portal.get("/api/portal/%s/signed-contract.pdf" % TOKEN)
    assert r.status_code == 502 and r.json()["ok"] is False


# ── the staff/admin retrieval endpoint ────────────────────────────────────────
def test_the_admin_endpoint_refuses_without_the_service_token(portal):
    """Everything under /api/admin/* here is server-to-server, and this one serves a customer's
    executed contract to anything that asks."""
    portal.monkeypatch.setattr(main.config, "SERVICE_TOKEN", "s3cret")
    portal.state["contract_row"] = _signed_row(SIGNED_PDF)
    assert portal.get("/api/admin/signed-contract.pdf?proposal_id=%s" % PID).status_code == 401
    r = portal.get("/api/admin/signed-contract.pdf?proposal_id=%s" % PID,
                   headers={"X-Service-Token": "wrong"})
    assert r.status_code == 401


def test_the_admin_endpoint_serves_the_contract_with_a_valid_token(portal):
    portal.monkeypatch.setattr(main.config, "SERVICE_TOKEN", "s3cret")
    portal.state["contract_row"] = _signed_row(SIGNED_PDF)
    r = portal.get("/api/admin/signed-contract.pdf?proposal_id=%s" % PID,
                   headers={"X-Service-Token": "s3cret"})
    assert r.status_code == 200 and r.content == SIGNED_PDF


def test_the_admin_endpoint_also_lazy_builds(portal):
    """Staff asking first is the common case -- they read the approval email before the customer
    opens the portal. Both sides go through the same builder so neither can serve a stale answer."""
    portal.monkeypatch.setattr(main.config, "SERVICE_TOKEN", "s3cret")
    portal.state["contract_row"] = _signed_row(None)
    portal.state["approval_record"] = _approval_record()
    r = portal.get("/api/admin/signed-contract.pdf?proposal_id=%s" % PID,
                   headers={"X-Service-Token": "s3cret"})
    assert r.status_code == 200 and r.content == SIGNED_PDF
    assert portal.calls["contracts"][0]["pdf"] == SIGNED_PDF


def test_an_unknown_proposal_is_a_404_not_a_500(portal):
    portal.monkeypatch.setattr(main.config, "SERVICE_TOKEN", "s3cret")
    portal.monkeypatch.setattr(main.db, "get_proposal", lambda pid: None)
    r = portal.get("/api/admin/signed-contract.pdf?proposal_id=nope",
                   headers={"X-Service-Token": "s3cret"})
    assert r.status_code == 404


# ── the attachment reaches the transport, not just the caller ───────────────
def test_notify_team_hands_the_attachment_to_every_copy(monkeypatch):
    """A staff notification is ONE message sent N times, not N different messages. A colleague
    who received the copy without the signed contract would have no way to know one existed --
    and the per-recipient loop is exactly where a threaded argument goes missing."""
    sent = []

    def _send(to, subject, html, headers=None, reply_to=None, attachments=None):
        sent.append((to, attachments))
        return True

    monkeypatch.setattr(email_sender, "_send", _send)
    email_sender.notify_team("Proposal APPROVED", "<p>x</p>", kind="approved",
                             recipients=["kyle@wetreadwell.com", "troy@wetreadwell.com"],
                             attachments=[("c.pdf", b"%PDF")])
    assert len(sent) == 2
    for to, atts in sent:
        assert atts == [("c.pdf", b"%PDF")], "%s got a copy with no contract" % to


def test_send_customer_update_hands_the_attachment_to_the_transport(monkeypatch):
    """The customer half of the same thread. _send already base64-encodes attachments for
    Resend; what was missing was any way for this function to be handed one."""
    seen = {}

    def _send(to, subject, html, headers=None, reply_to=None, attachments=None):
        seen["attachments"] = attachments
        return True

    monkeypatch.setattr(email_sender, "_send", _send)
    email_sender.send_customer_update("dana@acme.com", "http://x", "Nearman Creek",
                                      "Approved", "<p>y</p>",
                                      attachments=[("c.pdf", b"%PDF")])
    assert seen["attachments"] == [("c.pdf", b"%PDF")]


# ── the wire format of the call to the proposal tool ──────────────────────────
def test_the_request_to_the_proposal_tool_is_the_shape_it_expects(monkeypatch):
    """multipart/form-data: the proposal PDF as a file part, the certificate as a JSON string
    part, and the SERVICE_TOKEN header every other /api/admin/* call on that side uses.

    Exercised against a stubbed httpx rather than asserted from the source, because the thing
    that breaks here is an argument name, and only a call can see one."""
    monkeypatch.setattr(signing.config, "PROPOSAL_TOOL_URL", "http://tool:8888")
    monkeypatch.setattr(signing.config, "SERVICE_TOKEN", "s3cret")
    seen = {}

    class _Resp:
        status_code = 200
        content = SIGNED_PDF

    def _post(url, **kw):
        seen["url"] = url
        seen.update(kw)
        return _Resp()

    monkeypatch.setattr(signing.httpx, "post", _post)
    cert = {"proposal_id": PID, "signer_name": "Dana Reed"}
    assert signing.render_signed_contract(PROPOSAL_PDF, cert) == SIGNED_PDF
    assert seen["url"] == "http://tool:8888/api/admin/signed-contract"
    assert seen["headers"]["X-Service-Token"] == "s3cret"
    name, blob, ctype = seen["files"]["proposal_pdf"]
    assert blob == PROPOSAL_PDF and ctype == "application/pdf"
    assert json.loads(seen["data"]["certificate"]) == cert


def test_an_unconfigured_proposal_tool_is_refused_before_any_request(monkeypatch):
    monkeypatch.setattr(signing.config, "PROPOSAL_TOOL_URL", "")
    monkeypatch.setattr(signing.httpx, "post",
                        lambda *a, **k: pytest.fail("a request went out with no tool configured"))
    with pytest.raises(signing.ContractUnavailable):
        signing.render_signed_contract(PROPOSAL_PDF, {})


def test_a_non_pdf_answer_is_refused_rather_than_stored_as_a_contract(monkeypatch):
    """An nginx error page is 200 OK with a body. Storing it as a signed contract would hand a
    customer an HTML page named "Signed Contract.pdf"."""
    monkeypatch.setattr(signing.config, "PROPOSAL_TOOL_URL", "http://tool:8888")
    monkeypatch.setattr(signing.config, "SERVICE_TOKEN", "s3cret")

    class _Resp:
        status_code = 200
        content = b"<html>502 Bad Gateway</html>"

    monkeypatch.setattr(signing.httpx, "post", lambda url, **kw: _Resp())
    with pytest.raises(signing.ContractUnavailable) as exc:
        signing.render_signed_contract(PROPOSAL_PDF, {})
    assert "not a PDF" in str(exc.value)


def test_a_refusal_says_why_and_not_merely_that_it_refused(monkeypatch):
    """A log line reading "refused" with no reason has cost an SSH session and a container probe
    before now. The upstream error text travels."""
    monkeypatch.setattr(signing.config, "PROPOSAL_TOOL_URL", "http://tool:8888")
    monkeypatch.setattr(signing.config, "SERVICE_TOKEN", "s3cret")

    class _Resp:
        status_code = 400
        content = b'{"ok": false, "error": "certificate is missing signer_name"}'

        def json(self):
            return {"ok": False, "error": "certificate is missing signer_name"}

    monkeypatch.setattr(signing.httpx, "post", lambda url, **kw: _Resp())
    with pytest.raises(signing.ContractUnavailable) as exc:
        signing.render_signed_contract(PROPOSAL_PDF, {})
    assert "400" in str(exc.value) and "missing signer_name" in str(exc.value)
