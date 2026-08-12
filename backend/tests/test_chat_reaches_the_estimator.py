"""A chat message reaches the person who owns the job, not just the org-wide roster.

Hanz, 2026-08-13:

    "When a customer or a treadwell employee chats it should email. For example a customer
     replied or chatted through the portal chatbox. Email that to whoever is set for the
     notification sending of that project. And if a treadwell employee sends a message
     throuh the chatbox. It should email them as well"

BOTH DIRECTIONS ALREADY EXISTED. A customer message calls `notify_team` (main.py, the
`/api/portal/{token}/questions` route) and a staff reply calls `send_reply_notification` for
every project recipient (`/api/admin/proposal/{pid}/reply`). Nothing needed building there,
and these tests pin them precisely because nothing did — a customer-facing email path with no
test is one deploy away from going quiet.

WHAT WAS ACTUALLY MISSING was WHO. Checked against production on 2026-08-13: of nine staff on
`portal_notify_recipients`, exactly two were enabled — hanz@ and will@. Everyone else, Kyle
included, was toggled off. So a customer question on a job assigned to Kyle emailed Hanz and
Will and never Kyle. "Whoever is set for the notification sending of that project" plainly
includes the estimator the project is assigned to, and the codebase already agreed in one
place: customer STATUS updates prepended the assigned estimator. Chat did not.

THE ESTIMATOR IS NOW A PER-PROJECT ADD, which is what it is, and that choice carries a real
consequence worth stating: a per-project MUTE beats it. Somebody who explicitly silenced one
job is not dragged back in by being its estimator. The status path used to prepend the
estimator unconditionally and therefore ignored mutes — both paths now share one rule, so they
cannot disagree about who hears from a project.
"""
import sys

import pytest

import email_sender


class FakeDB:
    """The roster and overrides, in the shape `_resolve_notify` imports at call time."""

    def __init__(self, rows, overrides=None, fail_rows=False, fail_overrides=False):
        self._rows, self._ov = rows, overrides or []
        self._fail_rows, self._fail_ov = fail_rows, fail_overrides

    def list_notify_recipients(self):
        if self._fail_rows:
            raise RuntimeError("db down")
        return self._rows

    def list_notify_overrides(self, proposal_id):
        if self._fail_ov:
            raise RuntimeError("overrides unreachable")
        return self._ov


# Production's real shape on 2026-08-13: two enabled, the rest toggled off.
PROD_ROSTER = [
    {"email": "hanz@wetreadwell.com", "kind": "general", "enabled": True},
    {"email": "will@wetreadwell.com", "kind": "general", "enabled": True},
    {"email": "kyle@wetreadwell.com", "kind": "general", "enabled": False},
    {"email": "rj@wetreadwell.com", "kind": "general", "enabled": False},
]


@pytest.fixture
def roster(monkeypatch):
    def install(rows=PROD_ROSTER, overrides=None, **kw):
        monkeypatch.setitem(sys.modules, "db", FakeDB(rows, overrides, **kw))
    return install


def resolve(est=None, pid="p1", kind="general"):
    return email_sender._resolve_notify(kind, pid, est)


# ── the gap that was real ────────────────────────────────────────────────────
def test_the_assigned_estimator_hears_about_their_own_job(roster):
    """The whole point. Kyle is on the roster but disabled, so before this a question on his
    job emailed Hanz and Will and never him."""
    roster()
    assert resolve() == ["hanz@wetreadwell.com", "will@wetreadwell.com"]
    with_est = resolve("kyle@wetreadwell.com")
    assert "kyle@wetreadwell.com" in with_est, with_est


def test_an_estimator_who_is_not_on_the_roster_at_all_still_hears(roster):
    """Being assigned a job is enough. Nobody should have to be added to an org-wide list to
    be told a customer asked them a question."""
    roster()
    assert "dane@wetreadwell.com" in resolve("dane@wetreadwell.com")


def test_the_roster_is_never_replaced_only_extended(roster):
    """The estimator is an addition, not a redirect: whoever asked to hear about every project
    keeps hearing about this one."""
    got = resolve("kyle@wetreadwell.com")
    roster()
    got = resolve("kyle@wetreadwell.com")
    for e in ("hanz@wetreadwell.com", "will@wetreadwell.com"):
        assert e in got, got


# ── the consequences of modelling it as an ADD ───────────────────────────────
def test_a_per_project_MUTE_beats_being_the_estimator(roster):
    """The deliberate half of the design. An explicit "don't email me about this job" must not
    be overruled by assignment — and the status path, which prepended the estimator itself,
    silently did overrule it until both paths were merged."""
    roster(overrides=[{"email": "kyle@wetreadwell.com", "mode": "mute"}])
    assert "kyle@wetreadwell.com" not in resolve("kyle@wetreadwell.com")


def test_a_mute_in_different_casing_still_silences(roster):
    """Emails are typed by people. A mute stored as HANZ@ must silence hanz@."""
    roster(rows=[{"email": "hanz@wetreadwell.com", "kind": "general", "enabled": True}],
           overrides=[{"email": "HANZ@WeTreadwell.com", "mode": "mute"}])
    assert resolve("hanz@wetreadwell.com") == []


def test_an_estimator_already_on_the_roster_is_not_emailed_twice(roster):
    """Two copies of the same email reads as a broken system and trains people to ignore it."""
    roster()
    got = resolve("hanz@wetreadwell.com")
    assert got.count("hanz@wetreadwell.com") == 1, got


def test_dedupe_is_case_insensitive(roster):
    """`Hanz@WeTreadwell.com` on the roster and `HANZ@WETREADWELL.COM` as the estimator are one
    person and must produce one email."""
    roster(rows=[{"email": "Hanz@WeTreadwell.com", "kind": "general", "enabled": True}])
    assert resolve("HANZ@WETREADWELL.COM") == ["Hanz@WeTreadwell.com"]


def test_a_blank_or_missing_estimator_changes_nothing(roster):
    """Unassigned projects exist. Empty strings must not become an empty recipient."""
    roster()
    base = resolve()
    for empty in (None, "", "   "):
        assert resolve(empty) == base, empty


def test_an_everyone_toggled_off_roster_still_reaches_the_estimator(roster):
    """A configured roster with nothing enabled deliberately emails NOBODY rather than falling
    back to the env inbox. That rule stands — but the person assigned to the job is not
    "nobody", and a customer question landing nowhere is the worst outcome available."""
    roster(rows=[{"email": "hanz@wetreadwell.com", "kind": "general", "enabled": False}])
    assert resolve() == []
    assert resolve("kyle@wetreadwell.com") == ["kyle@wetreadwell.com"]


def test_the_estimator_survives_an_overrides_lookup_failure(roster):
    """The overrides fetch has its own try so a blip cannot discard the roster. The estimator
    is added after that, so it must survive the same blip."""
    roster(fail_overrides=True)
    assert "kyle@wetreadwell.com" in resolve("kyle@wetreadwell.com")


def test_notify_team_FORWARDS_the_estimator_to_the_resolver(roster, monkeypatch):
    """The wiring between the two halves. Every route test stubs `notify_team` itself, so they
    prove the kwarg is PASSED and nothing about whether notify_team does anything with it — a
    mutation dropping it from the `_resolve_notify` call survived until this existed. Spied at
    `_send`, the last hop before Resend, so this is who would really be emailed."""
    roster()
    seen = {}
    monkeypatch.setattr(email_sender, "_send",
                        lambda to, subject, html, headers=None, **kw:
                        seen.setdefault("to", list(to)) is None or True)
    email_sender.notify_team("New proposal question — Nearman Creek", "<p>hi</p>",
                             proposal_id="p1", assigned_estimator="kyle@wetreadwell.com")
    assert "kyle@wetreadwell.com" in seen.get("to", []), (
        "notify_team resolved recipients without the assigned estimator: %s" % seen)


# ── the routes: both directions, which nothing covered before ────────────────
def test_a_customer_chat_emails_the_roster_AND_the_estimator(monkeypatch):
    """The customer→staff half of the ask, end to end through the real route. There was no test
    that a chatbox message emailed anybody at all."""
    import main
    from fastapi.testclient import TestClient

    p = {"proposal_id": "pid1", "token": "tok", "project_name": "Nearman Creek",
         "assigned_estimator": "kyle@wetreadwell.com", "customer_email": "c@x.com"}
    seen = {}
    monkeypatch.setattr(main, "_require", lambda request, token: p)
    monkeypatch.setattr(main, "_session_email", lambda request: "c@x.com")
    monkeypatch.setattr(main.db, "add_message",
                        lambda *a, **k: {"id": 1, "body": a[3], "author_kind": "customer",
                                         "created_at": None, "msg_type": "text"})
    monkeypatch.setattr(main, "_q", lambda row: {"id": 1})
    monkeypatch.setattr(main, "_customer_msg", lambda row, who: {"id": 1})

    def fake_notify(subject, body, **kw):
        seen.update(subject=subject, kw=kw)
        return True
    monkeypatch.setattr(main.email_sender, "notify_team", fake_notify)

    r = TestClient(main.app).post("/api/portal/tok/questions", json={"body": "Any update?"})
    assert r.status_code == 200, r.text
    assert seen, "a customer chat message emailed nobody"
    assert seen["kw"]["assigned_estimator"] == "kyle@wetreadwell.com", (
        "the customer chat notification does not carry the assigned estimator: %s" % seen["kw"])
    # The threading pair that keeps a project in one conversation. `reply_to` is asserted as
    # PASSED rather than truthy: its value comes from the inbound address in config, which is
    # unset under test, and a test that needs env to be configured tells you about the
    # environment rather than the code. test_staff_reply_by_email.py owns the rule that a
    # reply_to never travels without a token.
    assert seen["kw"]["token"] == "tok"
    assert "reply_to" in seen["kw"]


def test_a_staff_chat_emails_the_customer(monkeypatch):
    """The staff→customer half. Sends to every project recipient, not just customer_email, so a
    second contact on the job sees the reply too."""
    import main
    from fastapi.testclient import TestClient

    p = {"proposal_id": "pid1", "token": "tok", "project_name": "Nearman Creek",
         "customer_email": "c@x.com"}
    sent = []
    monkeypatch.setattr(main, "_admin_ok", lambda request: True)
    monkeypatch.setattr(main.db, "get_proposal", lambda pid: p)
    monkeypatch.setattr(main.db, "add_question", lambda *a, **k: {"id": 2})
    monkeypatch.setattr(main, "_q", lambda row: {"id": 2})
    monkeypatch.setattr(main.db, "get_recipients", lambda pid: ["c@x.com", "ap@acme.com"])
    monkeypatch.setattr(main.email_sender, "send_reply_notification",
                        lambda email, link, project, **kw: sent.append((email, kw.get("message"))))

    r = TestClient(main.app).post("/api/admin/proposal/pid1/reply",
                                 json={"body": "On site Tuesday.", "by": "kyle@wetreadwell.com"})
    assert r.status_code == 200, r.text
    assert [e for e, _ in sent] == ["c@x.com", "ap@acme.com"], sent
    assert all(msg == "On site Tuesday." for _, msg in sent), (
        "the customer's email does not carry the message text: %s" % sent)


def test_the_status_path_and_the_chat_path_now_share_one_rule():
    """They used to disagree: status prepended the estimator itself (ignoring mutes) while chat
    left them out entirely. One resolution function, one answer."""
    import inspect

    import main
    src = inspect.getsource(main._notify_staff_status)
    assert "assigned_estimator=" in src, "the status path no longer passes the estimator"
    assert "[assigned] + to" not in src, (
        "the status path is prepending the estimator by hand again, which ignores mutes")
