"""Will #15: the deposit invoice is auto-sent to the customer on approval.
automations.request_deposit re-reads the proposal (to see the amount set_approved
just wrote) and mirrors the manual admin action; guarded so a re-approval can't
double-invoice and a zero/blank amount sends nothing.
"""
import automations
import db
import email_sender


def _wire(monkeypatch, *, deposit_amount=3316.25, deposit_status="pending"):
    sent, flags = [], {"requested": [], "msgs": []}
    monkeypatch.setattr(db, "get_proposal", lambda pid: {
        "proposal_id": pid, "token": "tok", "customer_email": "c@x.com",
        "deposit_amount": deposit_amount, "deposit_status": deposit_status})
    monkeypatch.setattr(db, "add_message", lambda pid, *a, **k: flags["msgs"].append(pid))
    monkeypatch.setattr(db, "set_deposit_requested", lambda pid: flags["requested"].append(pid))
    monkeypatch.setattr(db, "get_recipients", lambda pid: ["a@x.com", "b@x.com"])
    monkeypatch.setattr(email_sender, "proposal_reply_to", lambda t: None)
    monkeypatch.setattr(email_sender, "send_deposit_request",
                        lambda e, link, proj, amt, reply_to=None: sent.append((e, amt)))
    return sent, flags


def test_auto_deposit_sends_to_all_recipients(monkeypatch):
    sent, flags = _wire(monkeypatch)
    automations.request_deposit({"proposal_id": "p1", "token": "tok"}, "Westport")
    assert flags["requested"] == ["p1"]
    assert sent == [("a@x.com", 3316.25), ("b@x.com", 3316.25)]


def test_auto_deposit_skips_when_already_requested(monkeypatch):
    sent, flags = _wire(monkeypatch, deposit_status="requested")
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
