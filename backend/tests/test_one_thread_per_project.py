"""Every update about one project lands in one email conversation.

Hanz, 2026-08-11: "ALso for all updates to one project can we have it in one email thread?"
then, immediately: "Except for the OTP. OTPs shold always be separate threads".

WHAT WAS ALREADY RIGHT. The threading HEADERS. Every customer email about a proposal carries
`tw-proposal.<token>@wetreadwell.com` in In-Reply-To, and staff notifications started carrying it
the same day (see test_staff_reply_by_email.py — that was a routing fix, and one-thread-per-project
is what it bought as a side effect).

WHAT WAS SPLITTING IT ANYWAY. The subject. Gmail groups by the References chain AND by subject, so
one project produced a conversation per event:

    Your Treadwell proposal — Nearman Creek
    Deposit requested — Nearman Creek
    Invoice TW-1042 — deposit for Nearman Creek
    Checking in on Nearman Creek
    Quick reminder — Nearman Creek

Twelve distinct staff subjects and five customer ones, all about one job. The fix is a constant
subject per project, with the event kept as the email's HEADING — which is the line Gmail shows as
the conversation snippet, so nothing is actually lost from the inbox:

    [Treadwell] Nearman Creek        Kyle Loseke approved Epoxy at $41,250 on 8/11…

THE TWO DELIBERATE EXCLUSIONS, both pinned below:

  * the ACCESS CODE. Hanz asked for this explicitly and it was already true: `_otp_headers` gives
    codes their own per-recipient anchor and a constant subject of their own, because a customer
    may request several while reading one proposal and threading them in buried the conversation
    under a pile of expired codes.
  * the MORNING DIGEST. It spans every open proposal, so it belongs to no single project and
    passes no token at all.

The rule that produces both exclusions for free: a project subject goes on an email if and only if
that email carries a proposal token. Neither the OTP nor the digest has one.
"""
import pathlib
import re

import pytest

import config
import email_sender
import inbound

TOKEN = "tokABC123"
PROJECT = "Nearman Creek Power Station"
CUSTOMER = "gc@example.com"
STAFF = "bids@wetreadwell.com"
BACKEND = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture
def sent(monkeypatch):
    """Capture the Resend payload of every send."""
    out = []

    def fake_post(url, headers=None, json=None, timeout=None):  # noqa: A002
        out.append(json)

        class R:
            def raise_for_status(self):
                return None
        return R()

    monkeypatch.setattr(config, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(config, "EMAIL_REPLY_TO", "")
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://portal.example.com")
    monkeypatch.setattr(email_sender.httpx, "post", fake_post)
    return out


def _url():
    return "https://portal.example.com/p/" + TOKEN


# ── one subject, one anchor, for every customer email about the project ──────
def _customer_sends(sent):
    """Fire one of each customer email that belongs to a project."""
    email_sender.send_portal_link(CUSTOMER, "Dana Reed", _url(), PROJECT, token=TOKEN)
    email_sender.send_reply_notification(CUSTOMER, _url(), PROJECT, token=TOKEN,
                                         message="Sounds good")
    email_sender.send_customer_update(CUSTOMER, _url(), PROJECT, "Deposit received",
                                      "<p>Thanks.</p>", token=TOKEN)
    email_sender.send_deposit_request(CUSTOMER, _url(), PROJECT, amount=10312.50,
                                      token=TOKEN, invoice_no="TW-1042")
    for tpl in ("not_viewed", "next_steps", "second_nudge", "checkin"):
        email_sender.send_followup(CUSTOMER, _url(), PROJECT, tpl, token=TOKEN)
    return sent


def test_every_customer_email_about_one_project_shares_one_subject(sent):
    _customer_sends(sent)
    assert len(sent) == 8, "a sender was added or removed; this test needs updating"
    subjects = {m["subject"] for m in sent}
    assert subjects == {"Your Treadwell proposal — " + PROJECT}, (
        "these subjects still differ, so Gmail files them as separate conversations: %s"
        % sorted(subjects))


def test_every_customer_email_about_one_project_shares_one_anchor(sent):
    """The subject alone is not enough for a stricter client, and the anchor alone was not
    enough for Gmail. Both halves, asserted together."""
    _customer_sends(sent)
    anchors = {m["headers"]["In-Reply-To"] for m in sent}
    assert anchors == {email_sender.proposal_anchor(TOKEN)}


def test_the_event_survives_as_the_heading(sent):
    """The subject stops naming the event, so the BODY has to. Otherwise the change trades one
    problem for a worse one: a tidy thread of emails you cannot tell apart."""
    _customer_sends(sent)
    bodies = [m["html"] for m in sent]
    assert any("new reply" in b.lower() for b in bodies), "the reply email lost its heading"
    assert any("Deposit received" in b for b in bodies), "the update email lost its heading"
    assert any("Deposit invoice" in b for b in bodies), "the invoice email lost its heading"
    assert any("waiting" in b.lower() for b in bodies), "the not-viewed nudge lost its heading"


def test_the_invoice_number_is_still_somewhere_the_customer_can_see_it(sent):
    """It came out of the subject line, so it has to be in the body — a customer paying by
    check writes it on the memo line."""
    _customer_sends(sent)
    inv = [m for m in sent if "Deposit invoice" in m["html"]]
    assert inv and "TW-1042" in inv[0]["html"]


def test_two_different_projects_are_two_different_threads(sent):
    """Constant per PROJECT, not constant globally. One subject for everything would merge a
    customer's two jobs into one conversation, which is the same complaint inverted."""
    email_sender.send_reply_notification(CUSTOMER, _url(), PROJECT, token=TOKEN, message="a")
    email_sender.send_reply_notification(CUSTOMER, _url(), "Cedar Ridge", token="tokZZZ999",
                                         message="b")
    assert sent[0]["subject"] != sent[1]["subject"]
    assert sent[0]["headers"]["In-Reply-To"] != sent[1]["headers"]["In-Reply-To"]


# ── the staff side ───────────────────────────────────────────────────────────
def test_every_staff_notification_about_one_project_shares_one_subject(sent):
    for event in ("Proposal APPROVED — " + PROJECT, "Deposit RECEIVED — " + PROJECT,
                  "Project contacts submitted — " + PROJECT):
        email_sender.notify_team(event, "<p>x</p>", recipients=[STAFF],
                                 token=TOKEN, project=PROJECT)
    subjects = {m["subject"] for m in sent}
    assert subjects == {"[Treadwell] " + PROJECT}, sorted(subjects)


def test_the_staff_event_survives_as_the_heading(sent):
    """notify_team used to pass the subject as the heading too, so making the subject constant
    would have erased the event from the email entirely."""
    email_sender.notify_team("Proposal APPROVED — " + PROJECT, "<p>x</p>",
                             recipients=[STAFF], token=TOKEN, project=PROJECT)
    assert "Proposal APPROVED" in sent[0]["html"]


def test_staff_and_customer_do_not_share_a_subject(sent):
    """Different audiences. "Your Treadwell proposal" read wrong in a shared bids@ inbox."""
    email_sender.notify_team("e", "<p>x</p>", recipients=[STAFF], token=TOKEN, project=PROJECT)
    email_sender.send_reply_notification(CUSTOMER, _url(), PROJECT, token=TOKEN, message="a")
    assert sent[0]["subject"] != sent[1]["subject"]


def test_a_staff_email_with_no_project_keeps_its_own_subject(sent):
    """The unmatched-email forward has no project. Giving it a project subject would file a
    stray email into some job's conversation."""
    email_sender.notify_team("Unmatched email — stranger@x.com", "<p>x</p>", recipients=[STAFF])
    assert sent[0]["subject"] == "Unmatched email — stranger@x.com"
    assert "headers" not in sent[0]


# ── the exclusions Hanz named ────────────────────────────────────────────────
def test_the_access_code_is_its_own_thread(sent):
    """"Except for the OTP. OTPs shold always be separate threads." Already true before he
    asked; asserted here so it stays true."""
    email_sender.send_otp(CUSTOMER, "482913", PROJECT)
    assert sent[0]["subject"] == "Your Treadwell proposal access code"
    assert email_sender.proposal_anchor(TOKEN) not in sent[0]["headers"]["In-Reply-To"]
    assert "tw-proposal" not in str(sent[0]["headers"]), (
        "a login code is threaded with the proposal, which buries the conversation under "
        "expired codes")
    # Its OWN anchor, not merely "not the proposal's". A mutation survived this test when it
    # asserted only the line above: swapping _otp_headers for _thread_headers(email, None)
    # uses the per-recipient PROPOSAL-LINK anchor, which contains no "tw-proposal" either —
    # so the codes would quietly thread onto the customer's proposal emails, which is the exact
    # burying _otp_headers was written to stop.
    assert "treadwell-otp." in sent[0]["headers"]["In-Reply-To"], (
        "login codes are not on the dedicated OTP anchor")


def test_a_login_code_and_a_project_email_to_the_SAME_person_do_not_thread(sent):
    """The behavioural form of the assertion above, and the one that actually holds the line:
    whatever anchor scheme changes, these two must never land in one conversation."""
    email_sender.send_otp(CUSTOMER, "482913", PROJECT)
    email_sender.send_portal_link(CUSTOMER, "Dana Reed", _url(), PROJECT, token=TOKEN)
    assert sent[0]["headers"]["In-Reply-To"] != sent[1]["headers"]["In-Reply-To"]
    assert sent[0]["headers"]["References"] != sent[1]["headers"]["References"]
    assert sent[0]["subject"] != sent[1]["subject"]


def test_several_access_codes_thread_with_EACH_OTHER(sent):
    """Separate from the project, but not a new conversation per code — that is the other half
    of what makes them tidy."""
    email_sender.send_otp(CUSTOMER, "111111", PROJECT)
    email_sender.send_otp(CUSTOMER, "222222", PROJECT)
    assert sent[0]["headers"] == sent[1]["headers"]
    assert sent[0]["subject"] == sent[1]["subject"]


def test_one_customers_codes_do_not_thread_with_anothers(sent):
    email_sender.send_otp("a@x.com", "111111", PROJECT)
    email_sender.send_otp("b@x.com", "222222", PROJECT)
    assert sent[0]["headers"] != sent[1]["headers"]


def test_the_morning_digest_belongs_to_no_project(sent):
    """It lists every proposal that needs chasing, so it must not join any one job's thread.

    It is not header-less, though — it has its own `_digest_headers` anchor, the same idea as
    the OTP's, so the daily emails thread with each other and nothing else. Asserted as "not
    the proposal anchor" rather than "no headers", which is what this test claimed first.
    """
    email_sender.send_digest("kyle@wetreadwell.com",
                             [{"project_name": PROJECT, "why": "not viewed in 3 days",
                               "url": _url()}], name="Kyle")
    assert "proposal" in sent[0]["subject"] and PROJECT not in sent[0]["subject"]
    assert "tw-proposal" not in str(sent[0]["headers"]), (
        "the digest was filed into a single project's thread")
    assert inbound.find_thread_token(sent[0]["headers"]) is None


def test_the_digests_thread_with_each_other(sent):
    email_sender.send_digest("kyle@wetreadwell.com",
                             [{"project_name": PROJECT, "why": "a", "url": _url()}], name="Kyle")
    email_sender.send_digest("kyle@wetreadwell.com",
                             [{"project_name": "Cedar Ridge", "why": "b", "url": _url()}],
                             name="Kyle")
    assert sent[0]["headers"] == sent[1]["headers"]


# ── the sweep, so a new email cannot miss this ───────────────────────────────
def test_no_project_email_builds_its_own_subject_any_more():
    """The failure this guards is a NEW sender written by copying an old one. Twelve staff
    subjects and five customer ones existed before today; the count only ever grows.

    Any _send that passes a proposal token must take its subject from the canonical helper,
    or it silently starts its own conversation about a job that already had one.
    """
    src = (BACKEND / "email_sender.py").read_text(encoding="utf-8")
    bad = []
    for m in re.finditer(r"_send\(\s*\[?email\]?,\s*([^,]+),", src):
        # Which _send call this is: read to the closing paren and look for a token.
        depth, i = 0, m.start() + src[m.start():].index("(")
        j = i
        while j < len(src):
            if src[j] == "(":
                depth += 1
            elif src[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        call, subj = src[i:j], m.group(1).strip()
        if "token" in call and "thread_subject" not in subj:
            bad.append(src[:m.start()].count("\n") + 1)
    assert not bad, (
        "the _send at line(s) %s carries a proposal token but builds its own subject, so it "
        "starts a separate email thread about a project that already has one" % bad)


def test_the_canonical_subjects_are_stable_strings():
    """They are a Gmail THREAD KEY, not copy. Reword one and every live conversation splits in
    two at that point — the old messages keep the old subject and cannot be re-sent."""
    assert email_sender.customer_thread_subject("X") == "Your Treadwell proposal — X"
    assert email_sender.staff_thread_subject("X") == "[Treadwell] X"


@pytest.mark.parametrize("blank", ["", None])
def test_a_missing_project_name_never_produces_a_bare_dangling_subject(blank):
    assert email_sender.customer_thread_subject(blank) == "Your Treadwell proposal — your project"
    assert email_sender.staff_thread_subject(blank) == "[Treadwell] proposal"


def test_a_reply_to_the_project_thread_still_routes(sent):
    """The whole point of the anchor. Changing the subject must not have disturbed it: the
    inbound webhook reads the token back out of the headers, and on prod that is the only
    route there is."""
    email_sender.send_customer_update(CUSTOMER, _url(), PROJECT, "Deposit received",
                                      "<p>x</p>", token=TOKEN)
    assert inbound.find_thread_token(sent[0]["headers"]) == TOKEN
