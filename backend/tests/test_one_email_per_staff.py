"""A staff notification is one email per person, so no colleague's address travels in another's To.

Hanz, 2026-08-19: "for sending out multiple emails to the staff and customers can it be BCC so we
dont see the cross talk of the emails of the receivers".

ONLY THE STAFF PATH HAD THE PROBLEM, which is why this file is about `notify_team` alone. Both
customer paths already send separately: `admin_publish` loops `send_portal_link` once per recipient
and `admin_reply` loops `send_reply_notification`. `notify_team` resolved the whole roster into a
list and called `_send` once, and `_send` puts that list straight into Resend's "to" — so every
"Proposal APPROVED", every customer question, every deposit alert published the addresses of
everyone on the notification roster, plus anyone a per-project add or an assignment had folded in.

BCC IS NOT THE FIX AND IS DELIBERATELY NOT USED. It needs something in To, and an empty or
self-addressed To reads as machine mail and costs deliverability; and a BCC send comes back as one
pass/fail for the whole batch, so a dead address is indistinguishable from a working one.
Per-recipient sending keeps a verdict per address — the same property that lets `admin_publish`
send "Proposal sent, with failures" and name the address that bounced (test_send_confirmation.py) —
and it leaves one rule for all outbound mail instead of two.

THE THING A REVIEWER WORRIES ABOUT IS THREADING, and it survives untouched:
`project_thread_headers(token)` derives the anchor from the PROJECT and takes nothing from the
recipient — unlike `_thread_headers`, the customer equivalent, which hashes an address. So all N
copies carry identical References/In-Reply-To under one constant subject, and each person's client
files them into the single conversation for that job. Pinned below rather than argued, because a
regression here silently splits every project's staff thread N ways.
"""
import logging
import sys

import pytest

import config
import email_sender
import followup_rules
import followup_worker

PID = "p-oak-grove"
TOKEN = "tokABC123"
PROJECT = "Nearman Creek Power Station"
HANZ = "hanz@wetreadwell.com"
WILL = "will@wetreadwell.com"
KYLE = "kyle@wetreadwell.com"
ENABLED = [HANZ, WILL, KYLE]

# Three enabled and one toggled off — production's real shape, so "three sends" is a fact about the
# roster rather than a number chosen to match the loop.
ROSTER = [
    {"email": HANZ, "kind": "general", "enabled": True},
    {"email": WILL, "kind": "general", "enabled": True},
    {"email": KYLE, "kind": "general", "enabled": True},
    {"email": "rj@wetreadwell.com", "kind": "general", "enabled": False},
]


class FakeDB:
    """The roster, in the shape `_resolve_notify` imports at call time."""

    def __init__(self, rows):
        self._rows = rows

    def list_notify_recipients(self):
        return self._rows

    def list_notify_overrides(self, proposal_id):
        return []


@pytest.fixture
def roster(monkeypatch):
    def install(rows=ROSTER):
        monkeypatch.setitem(sys.modules, "db", FakeDB(rows))
    return install


@pytest.fixture
def resend(monkeypatch):
    """Every payload Resend would receive, captured at `httpx.post` rather than at `_send`.

    One level lower than the other email tests on purpose: the claim is about the "to" field of the
    real API call, and `_send` is the function that builds it from the list it is handed. Spying on
    `_send` would prove notify_team's loop and nothing about what actually leaves the process.

    `broken` fails chosen addresses the way a bounce does — a non-2xx from the API, which `_send`
    catches and reports as False. The payload is recorded BEFORE the failure, so the list is every
    ATTEMPT and the return value is what landed; that pair is what tells a stopped loop apart from
    a failed send."""
    sent = []
    broken = set()

    def fake_post(url, headers=None, json=None, timeout=None):  # noqa: A002
        sent.append(json)
        addressed = json.get("to") or []

        class R:
            def raise_for_status(self):
                if any(a in broken for a in addressed):
                    raise RuntimeError("Resend refused %s" % addressed)

        return R()

    monkeypatch.setattr(config, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(config, "EMAIL_REPLY_TO", "")
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://portal.example.com")
    monkeypatch.setattr(email_sender.httpx, "post", fake_post)
    return sent, broken


def _notify(**kw):
    """The real shape of a project notification: the event as the subject, the project and token
    that put it in the staff thread, and the proposal id that makes the roster resolve."""
    kw.setdefault("proposal_id", PID)
    kw.setdefault("token", TOKEN)
    kw.setdefault("project", PROJECT)
    return email_sender.notify_team("Proposal APPROVED — " + PROJECT, "<p>Approved.</p>", **kw)


# ── one send per person ──────────────────────────────────────────────────────
def test_three_on_the_roster_get_three_separate_emails(resend, roster):
    """THE regression test. One `_send` for the whole list is what put three addresses in one To.

    The exact list, not just the count: it also pins that each send carries its OWN address and
    that the toggled-off row is still excluded — resolution the loop must not have started
    second-guessing."""
    sent, _ = resend
    roster()
    _notify()
    assert [m["to"] for m in sent] == [[HANZ], [WILL], [KYLE]], (
        "the roster is not being mailed one person at a time: %s" % [m["to"] for m in sent])


def test_no_recipient_can_see_who_else_was_told(resend, roster):
    """The ask, stated as the property it is: cross-talk. Asserted over the WHOLE payload and not
    just the To field, because an address leaks just as badly from a "sent to: …" recital in the
    body — which is a recital this codebase has already had to remove once (test_send_confirmation).
    """
    sent, _ = resend
    roster()
    _notify()
    for m in sent:
        assert len(m["to"]) == 1, m["to"]
        mine = m["to"][0]
        blob = str(m)
        for other in ENABLED:
            if other != mine:
                assert other not in blob, (
                    "%s can read %s's address in their own copy" % (mine, other))


def test_a_single_recipient_still_gets_exactly_one_email(resend):
    """The common case, which the loop must not have turned into a per-address multiplier."""
    sent, _ = resend
    _notify(recipients=[HANZ])
    assert [m["to"] for m in sent] == [[HANZ]]


# ── threading: N copies, one conversation ────────────────────────────────────
def test_every_copy_threads_under_the_one_project_conversation(resend, roster):
    """Verified from `project_thread_headers` itself: its only argument is `token`, and it returns
    the proposal anchor with nothing derived from the address — so splitting one send into three
    cannot split the thread. Pinned here because the failure is invisible from the sending side:
    the emails still arrive, and each staff member's inbox quietly grows a conversation per event
    for every job.

    Subject and body are asserted alongside the headers because Gmail threads on the References
    graph AND the subject (see test_one_thread_per_project.py) — an identical anchor under three
    different subjects is still three conversations."""
    sent, _ = resend
    roster()
    _notify()
    anchor = email_sender.proposal_anchor(TOKEN)
    assert len(sent) == 3
    for m in sent:
        assert m["headers"] == {"References": anchor, "In-Reply-To": anchor}, m["headers"]
    assert {m["subject"] for m in sent} == {email_sender.staff_thread_subject(PROJECT)}
    assert len({m["html"] for m in sent}) == 1, "the three copies are not the same email"


def test_a_staff_email_with_no_project_still_carries_no_headers(resend, roster):
    """The unmatched-email forward has no token. Building the headers once outside the loop must
    not have turned that None into an anchor on some other job's conversation."""
    sent, _ = resend
    roster()
    email_sender.notify_team("Unmatched email — stranger@x.com", "<p>x</p>", proposal_id=PID)
    assert len(sent) == 3
    for m in sent:
        assert "headers" not in m, m.get("headers")


# ── a failure reaches one person, not the whole team ─────────────────────────
def test_one_dead_address_does_not_silence_the_rest_of_the_team(resend, roster):
    """The middle address fails, so an abort loses Kyle — the person a `break` or an early return
    would cost. This is the one behaviour BCC could not give us at all: there, one bad address
    either takes the batch down or hides inside a single "sent" verdict."""
    sent, broken = resend
    broken.add(WILL)
    roster()
    delivered = _notify()
    assert [m["to"][0] for m in sent] == ENABLED, (
        "the loop stopped at the failure: %s" % [m["to"] for m in sent])
    assert delivered == [HANZ, KYLE], delivered


def test_a_sender_that_RAISES_does_not_abort_the_loop_either(monkeypatch, roster):
    """`_send` swallows transport errors and returns False, so the test above is the realistic
    failure. This is the one it cannot swallow — a raise before its own try — and it matters
    because five of notify_team's call sites in main.py are unwrapped, so an escape would 500 a
    customer's route over an undeliverable staff email."""
    tried = []

    def flaky(to, subject, html, headers=None, reply_to=None, attachments=None):
        tried.append(to[0])
        if to[0] == WILL:
            raise RuntimeError("socket died mid-write")
        return True

    monkeypatch.setattr(email_sender, "_send", flaky)
    roster()
    delivered = _notify()
    assert tried == ENABLED, tried
    assert delivered == [HANZ, KYLE], delivered


def test_a_total_failure_is_reported_as_nothing_delivered(resend, roster):
    """Every address dead is not "sent". The follow-up worker branches on exactly this to decide
    whether to release its reservation and try again — see the caller test below."""
    sent, broken = resend
    broken.update(ENABLED)
    roster()
    assert _notify() == []
    assert len(sent) == 3, "it gave up before trying everybody"


# ── the empty roster: unchanged behaviour, pinned ────────────────────────────
def test_an_all_off_roster_sends_nothing_and_says_so(resend, roster, caplog):
    """A CONFIGURED roster with everyone toggled off deliberately emails NOBODY rather than
    falling back to the env inbox (resolve_notify_recipients owns that rule). The log line is the
    only trace it leaves, so it is asserted too — silent-and-logged and silent-and-broken look
    identical in production otherwise."""
    sent, _ = resend
    roster(rows=[{"email": HANZ, "kind": "general", "enabled": False}])
    with caplog.at_level(logging.INFO, logger="portal.email"):
        assert _notify() == []
    assert sent == []
    assert "no recipients after roster" in caplog.text, caplog.text


# ── the return value, read through the caller that branches on it ────────────
# notify_team returns the addresses it DELIVERED to, where it used to return one bool. The only
# caller that reads it is followup_worker._send_staff, which wraps it in bool(): the sweep releases
# its send reservation and retries next tick when nothing went out. An empty list is falsy for the
# same reason a False was, so the branch is unchanged — and asserted here through _send_staff
# rather than on notify_team's return, because that equivalence is the whole claim.
def _due(template="staff_not_viewed"):
    return followup_rules.Due(rule_key="k1", audience="staff", template=template)


@pytest.fixture
def staff_note(resend, roster, monkeypatch):
    """`_send_staff` for a proposal with no assigned estimator, so its recipients come from the
    roster and there are three of them to succeed or fail independently."""
    roster()
    monkeypatch.setattr(config, "PROPOSAL_TOOL_PUBLIC_URL", "https://proposals.example.com")
    p = {"proposal_id": PID, "token": TOKEN, "project_name": PROJECT,
         "customer_name": "Dana Reed", "customer_email": "gc@example.com",
         "assigned_estimator": "", "approved_total": 41250.0}
    sent, broken = resend
    return lambda: followup_worker._send_staff(p, _due()), sent, broken


def test_the_worker_treats_a_delivered_note_as_sent(staff_note):
    run, sent, _ = staff_note
    assert run() is True
    assert [m["to"] for m in sent] == [[HANZ], [WILL], [KYLE]], (
        "the worker's staff note is not per-recipient either")


def test_the_worker_keeps_the_reservation_when_one_of_three_landed(staff_note):
    """"Reached nobody", not "reached everybody" — the same rule the customer half uses. One
    colleague's dead address must not make the sweep re-send the note to the other two next tick."""
    run, _, broken = staff_note
    broken.add(WILL)
    assert run() is True


def test_the_worker_releases_the_reservation_when_nothing_landed(staff_note):
    """The other direction, or the line above is indistinguishable from ignoring failure. Falsy
    return → the sweep deletes its reservation and the next tick tries again."""
    run, _, broken = staff_note
    broken.update(ENABLED)
    assert run() is False
