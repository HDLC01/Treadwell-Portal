"""Deposit-request + staff Reply-in-Portal links. _staff_link is pure; the email
builders are smoke-tested in dev mode (no RESEND key → they log + return True),
which still exercises the amount/None formatting paths."""
import config
import email_sender
import main


def test_staff_link_points_at_public_proposal_tool():
    link = main._staff_link("abc-123")
    assert link == f"{config.PROPOSAL_TOOL_PUBLIC_URL}/portal.html?open=abc-123"
    assert link.startswith("http")


def test_send_deposit_request_formats_amount(monkeypatch):
    monkeypatch.setattr(config, "RESEND_API_KEY", "")   # dev mode → logs, returns True
    assert email_sender.send_deposit_request("c@x.com", "https://p/x", "Job", 1234.5) is True


def test_send_deposit_request_without_amount(monkeypatch):
    monkeypatch.setattr(config, "RESEND_API_KEY", "")
    assert email_sender.send_deposit_request("c@x.com", "https://p/x", "Job", None) is True


def test_notify_team_appends_reply_link_in_dev(monkeypatch):
    monkeypatch.setattr(config, "RESEND_API_KEY", "")
    assert email_sender.notify_team("Subj", "<p>hi</p>", recipients=["a@x.com"],
                                    reply_link="https://tool/portal.html?open=x") is True
