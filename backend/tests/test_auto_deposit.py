"""Will #15: the deposit invoice is auto-issued to the customer on approval.
automations.request_deposit re-reads the proposal (to see the amount set_approved
just wrote) and delegates to issue_deposit_invoice — the SAME helper the manual
staff action uses. Guarded so a re-approval can't double-invoice and a zero/blank
amount sends nothing.
"""
import automations
import db
import email_sender
import invoice as invoice_mod


def _wire(monkeypatch, *, deposit_amount=3316.25, deposit_status="pending",
          deposit_requested_at=None, deposit_invoice_no=None):
    sent, flags = [], {"requested": [], "msgs": [], "invoice_calls": []}

    def _proposal(pid):
        return {"proposal_id": pid, "token": "tok", "customer_email": "c@x.com",
                "project_name": "Westport", "approved_total": 13265.0,
                "deposit_amount": deposit_amount, "deposit_status": deposit_status,
                "deposit_requested_at": deposit_requested_at,
                "deposit_invoice_no": deposit_invoice_no or flags.get("assigned")}

    def _assign(pid):
        flags["assigned"] = "TW-INV-01001"
        flags["invoice_calls"].append(pid)
        return flags["assigned"]

    monkeypatch.setattr(db, "get_proposal", _proposal)
    monkeypatch.setattr(db, "assign_invoice_no", _assign)
    def _new(pid):
        flags["assigned"] = "TW-INV-01002"
        flags.setdefault("minted", []).append(pid)
        return flags["assigned"]
    monkeypatch.setattr(db, "issue_new_invoice_no", _new)
    monkeypatch.setattr(db, "supersede_invoice_cards",
                        lambda pid, by: flags.setdefault("superseded", []).append(by))
    # The invoice PDF is rendered by the proposal tool over HTTP — stub that seam
    # so the suite never reaches the network.
    monkeypatch.setattr(db, "get_draft_data", lambda pid: {})
    monkeypatch.setattr(invoice_mod, "render_invoice_pdf",
                        lambda payload, **k: flags.setdefault("payloads", []).append(payload) or b"%PDF-1.4 stub")
    monkeypatch.setattr(db, "set_invoice_no",
                        lambda pid, no: flags.setdefault("set_no", []).append(no))
    monkeypatch.setattr(db, "set_deposit_amount",
                        lambda pid, amt: flags.setdefault("amounts", []).append(amt))
    monkeypatch.setattr(db, "add_message",
                        lambda pid, kind, who, body, **k: flags["msgs"].append((body, k)))
    monkeypatch.setattr(db, "set_deposit_requested", lambda pid: flags["requested"].append(pid))
    monkeypatch.setattr(db, "get_recipients", lambda pid: ["a@x.com", "b@x.com"])
    monkeypatch.setattr(email_sender, "proposal_reply_to", lambda t: None)
    monkeypatch.setattr(email_sender, "send_deposit_request",
                        lambda e, link, proj, amt, **k: sent.append((e, amt, k.get("invoice_no"),
                                                                     bool(k.get("invoice_pdf")))))
    return sent, flags


def test_auto_deposit_sends_to_all_recipients(monkeypatch):
    sent, flags = _wire(monkeypatch)
    automations.request_deposit({"proposal_id": "p1", "token": "tok"}, "Westport")
    assert flags["requested"] == ["p1"]
    # Every recipient gets the invoice number AND the PDF attached.
    assert sent == [("a@x.com", 3316.25, "TW-INV-01001", True),
                    ("b@x.com", 3316.25, "TW-INV-01001", True)]


def test_auto_deposit_posts_invoice_chat_card(monkeypatch):
    """The chat card is the customer's in-portal copy — it must carry the invoice
    number, amount and reference in meta so the UI can render the download."""
    _sent, flags = _wire(monkeypatch)
    automations.request_deposit({"proposal_id": "p1", "token": "tok"}, "Westport")
    assert len(flags["msgs"]) == 1
    body, kw = flags["msgs"][0]
    assert kw["msg_type"] == "deposit_request"
    assert kw["meta"]["invoice_no"] == "TW-INV-01001"
    assert kw["meta"]["amount"] == 3316.25
    assert kw["meta"]["reference"] == "TW-P1"          # deposit_ref('p1')
    assert "TW-INV-01001" in body and "3,316.25" in body


def test_auto_deposit_does_not_rewrite_matching_amount(monkeypatch):
    """The auto path uses the stored 25% — no pointless write."""
    _sent, flags = _wire(monkeypatch)
    automations.request_deposit({"proposal_id": "p1", "token": "tok"}, "Westport")
    assert flags.get("amounts") is None


def test_staff_override_amount_is_persisted_before_render(monkeypatch):
    """A staff-adjusted amount must be written to deposit_amount, otherwise the
    downloadable invoice (rebuilt from that column) would disagree with the PDF
    that was emailed."""
    sent, flags = _wire(monkeypatch)
    automations.issue_deposit_invoice({"proposal_id": "p1", "token": "tok"}, "Westport", 999.0)
    assert flags["amounts"] == [999.0]
    assert sent[0][1] == 999.0
    assert "999.00" in flags["msgs"][0][0]


def test_auto_deposit_skips_when_already_invoiced(monkeypatch):
    """Re-approval must not issue a second invoice. Guarded on
    deposit_requested_at — NOT deposit_status, whose check constraint permits only
    'pending'/'submitted'/'received' (the old 'requested' guard was unreachable;
    'submitted' means the CUSTOMER paid, which is not the same as us asking)."""
    sent, flags = _wire(monkeypatch, deposit_requested_at="2026-07-27T13:36:44+00:00")
    automations.request_deposit({"proposal_id": "p1", "token": "tok"}, "Westport")
    assert sent == [] and flags["requested"] == [] and flags["invoice_calls"] == []


def test_auto_deposit_skips_when_deposit_already_received(monkeypatch):
    sent, flags = _wire(monkeypatch, deposit_status="received")
    automations.request_deposit({"proposal_id": "p1", "token": "tok"}, "Westport")
    assert sent == [] and flags["requested"] == []


def test_auto_deposit_skips_when_amount_zero_or_none(monkeypatch):
    sent, flags = _wire(monkeypatch, deposit_amount=0)
    automations.request_deposit({"proposal_id": "p1", "token": "tok"}, "Westport")
    sent2, flags2 = _wire(monkeypatch, deposit_amount=None)
    automations.request_deposit({"proposal_id": "p2", "token": "tok"}, "Westport")
    assert sent == [] and sent2 == [] and flags["requested"] == [] and flags2["requested"] == []


def test_auto_deposit_never_raises(monkeypatch):
    # A DB/email hiccup must not break approval — request_deposit swallows + logs.
    _wire(monkeypatch)
    monkeypatch.setattr(db, "set_deposit_requested",
                        lambda pid: (_ for _ in ()).throw(RuntimeError("db down")))
    automations.request_deposit({"proposal_id": "p1", "token": "tok"}, "Westport")   # no exception


# ── staff edits from the review form ─────────────────────────────────────────
def test_staff_overrides_reach_the_document(monkeypatch):
    """Edit-before-send: whatever staff corrected must be what gets rendered."""
    _sent, flags = _wire(monkeypatch)
    automations.issue_deposit_invoice({"proposal_id": "p1", "token": "tok"}, "Westport", 3316.25,
                                      overrides={"customer_name": "Acme Holdings LLC",
                                                 "customer_address": "1 Main St"})
    payload = flags["payloads"][0]
    assert payload["customer_name"] == "Acme Holdings LLC"
    assert payload["customer_address"] == "1 Main St"


def test_an_edited_invoice_number_is_persisted(monkeypatch):
    """The customer quotes this number back and the portal download rebuilds from
    it, so an edit that only lived in the emailed PDF would leave them disagreeing."""
    sent, flags = _wire(monkeypatch)
    automations.issue_deposit_invoice({"proposal_id": "p1", "token": "tok"}, "Westport", 3316.25,
                                      overrides={"invoice_no": "26.114-01"})
    assert flags["set_no"] == ["26.114-01"]
    assert sent[0][2] == "26.114-01"          # the email cites the edited number
    assert "26.114-01" in flags["msgs"][0][0] # so does the chat card


def test_unedited_number_is_not_rewritten(monkeypatch):
    _sent, flags = _wire(monkeypatch)
    automations.issue_deposit_invoice({"proposal_id": "p1", "token": "tok"}, "Westport", 3316.25)
    assert "set_no" not in flags


# ── resend mints a NEW invoice number (Hanz's call) ──────────────────────────
def test_resend_mints_a_new_number_and_supersedes_the_old_card(monkeypatch):
    sent, flags = _wire(monkeypatch, deposit_invoice_no="TW-INV-01001")
    automations.issue_deposit_invoice({"proposal_id": "p1", "token": "tok"}, "Westport", 3316.25,
                                      new_number=True)
    assert flags["minted"] == ["p1"]                  # fresh number, not reused
    assert flags["superseded"] == ["TW-INV-01002"]    # prior cards point at it
    assert sent[0][2] == "TW-INV-01002"


def test_approval_path_reuses_its_number(monkeypatch):
    """Only the staff resend mints; a re-approval must not renumber."""
    _sent, flags = _wire(monkeypatch)
    automations.issue_deposit_invoice({"proposal_id": "p1", "token": "tok"}, "Westport", 3316.25)
    assert "minted" not in flags
    assert flags["invoice_calls"] == ["p1"]           # went through assign_invoice_no


def test_first_send_does_not_supersede_anything(monkeypatch):
    """Nothing to replace when there was no prior invoice."""
    _sent, flags = _wire(monkeypatch, deposit_invoice_no=None)
    automations.issue_deposit_invoice({"proposal_id": "p1", "token": "tok"}, "Westport", 3316.25,
                                      new_number=True)
    assert "superseded" not in flags
