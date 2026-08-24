"""A staff email may never carry the customer's magic link.

Hanz, 2026-08-24: "can we remove the Proposal link in the email for the treadwell staff". It read as
clutter on the card, and it was worse than clutter: `/p/{token}` IS the credential. There is no
password behind it - the token is the whole of the customer's access to their proposal, their chat
thread and their deposit page.

A staff reminder goes to every estimator on the roster, gets forwarded, and gets quoted in replies.
Every one of those copies was another way into that customer's proposal for anybody who saw the mail.
Staff never needed it either: the email's own button is `reply_link=crm_url`, which lands on the CRM
card where the same proposal is reachable behind a login.

THIS FILE EXISTS BECAUSE THE FIX ALONE WAS NOT ENOUGH. When the two `<li>Proposal: ...</li>` lines
were removed, a mutation putting one back passed the entire suite - 34 tests green with a customer
credential in a staff email. Removing a line does not stop it being re-added by somebody adding a
"handy link". This does.

Deliberately broad: it does not look for the old wording, it looks for the SHAPE of the leak (a
/p/ URL, or the token, anywhere in the body). A future reinstatement will not use the same markup.
"""

import re

import pytest

import config
import followup_worker


STAFF_TEMPLATES = [
    "staff_not_viewed",
    "staff_personal_followup",
    "staff_deposit_outstanding",
    "staff_pause_expired",
]

TOKEN = "gZ3liSuON-bK-jR37bxIb0psjkXmAKp8"      # the shape of a real one


def _row():
    return {
        "proposal_id": "p1",
        "project_name": "Combo Test",
        "customer_name": "HANZ URIEL A DE LA CRUZ",
        "customer_email": "hdlcruz03@gmail.com",
        "token": TOKEN,
        "approved_total": 5690.75,
        "deposit_amount": 5690.75,
        "assigned_estimator": "hanz@wetreadwell.com",
        "proposal_status": "approved",
        "approved_at": "2026-08-11T13:00:00+00:00",
        "sent_at": "2026-08-10T13:00:00+00:00",
        "followup_enrolled_at": "2026-08-10T13:00:00+00:00",
    }


class _Due:
    def __init__(self, template):
        self.rule_key = "k:" + template
        self.audience = "staff"
        self.template = template
        self.include_status_ask = False


@pytest.mark.parametrize("template", STAFF_TEMPLATES)
def test_no_staff_reminder_carries_the_customers_link(template, monkeypatch):
    sent = {}

    def fake_notify_team(subject, body_html, **kw):
        sent["subject"] = subject
        sent["body"] = body_html
        sent["kw"] = kw
        return ["hanz@wetreadwell.com"]

    monkeypatch.setattr(followup_worker.email_sender, "notify_team", fake_notify_team)
    followup_worker._send_staff(_row(), _Due(template))

    assert "body" in sent, "%s sent nothing at all" % template
    body = sent["body"]

    assert TOKEN not in body, (
        "%s puts the customer's token in a staff email. That token is the whole credential - "
        "anybody the mail is forwarded to can open the proposal, the chat and the deposit page."
        % template)
    assert "/p/" not in body, (
        "%s contains a /p/ customer URL: %r" % (template, re.findall(r"\S*/p/\S*", body)[:3]))
    assert config.PUBLIC_BASE_URL not in body, (
        "%s links the customer-facing site; staff links belong on the CRM." % template)


@pytest.mark.parametrize("template", STAFF_TEMPLATES)
def test_the_staff_button_still_points_at_the_crm(template, monkeypatch):
    """The counterpart. Taking the link out must not leave the estimator with no way in.

    If this fails while the test above passes, the leak was closed by removing the only route to the
    project rather than by replacing it, which trades a security problem for a usability one.
    """
    sent = {}
    monkeypatch.setattr(followup_worker.email_sender, "notify_team",
                        lambda subject, body_html, **kw: sent.update(kw=kw) or ["x@y.com"])
    followup_worker._send_staff(_row(), _Due(template))

    link = (sent.get("kw") or {}).get("reply_link") or ""
    assert link, "%s gives the estimator no link at all" % template
    assert "/p/" not in link, "%s reply_link is a customer URL: %r" % (template, link)
    assert "portal.html" in link or "open=" in link, (
        "%s reply_link does not look like a CRM link: %r" % (template, link))


def test_the_customer_emails_still_get_their_link():
    """The rule is about the AUDIENCE, not about links.

    A customer follow-up must still carry /p/{token} - it is the entire point of the email. If this
    ever fails, somebody has applied the staff rule too widely and the customer can no longer reach
    their own proposal.
    """
    import inspect
    src = inspect.getsource(followup_worker._send_customer)
    assert "/p/" in src, (
        "the customer follow-up no longer builds a /p/ link; the staff rule has been applied to the "
        "customer path, which leaves the customer with no way to open their proposal")
