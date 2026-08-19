"""Staff hear that the customer OPENED the proposal — once per send, in the thread and by email.

Until now POST /api/portal/{token}/viewed wrote two rows and told nobody. The board changed colour
and `last_viewed_at` moved, so an estimator watching the pipeline could find out by looking; an
estimator doing anything else could not. Hanz asked for the opening to reach people: a card in the
project's conversation and an email. (The notification bell already reports it.)

WHY A TRANSITION AND NOT A VIEW. This endpoint is not a one-shot. It fires when the customer opens
the proposal step, again on a hash change, and again from a tab left open over lunch — and
`mark_viewed` writes `last_viewed_at = now()` on every one of them. Alerting per call would have
mailed the estimator on every poll, which is the same as not alerting at all: nobody reads the
tenth "they opened it". So `mark_viewed` now reports the sent -> viewed transition and the alerts
hang off that. A RE-SEND makes it news again, on purpose — `reset_for_revision` puts the row back
to 'sent', so whether the customer read the revision is asked and answered separately from whether
they read what it replaced.

WHY meta.view. The thread is shared. The card is written for the estimator, and the customer's
notification bell is where they would otherwise be pinged about their own click — an alert caused
by reading, delivered because they read. `db.list_customer_events` filters it out, exactly as it
already filters the automated follow-up echo, and for the same reason.

WHAT IS DELIBERATELY BEST-EFFORT. All of it. This runs inside the customer's own request for the
page they are trying to read, which is why `mark_viewed` was moved off the main GET in the first
place. An estimator losing a notification is a bad afternoon; a customer losing their proposal is
the failure this route exists not to have.
"""
import pytest

import db


# ── 2a. mark_viewed reports the transition ───────────────────────────────────
class _Row:
    """The one column the statement returns, over a status that behaves like the row's.

    Emulated rather than asserted-on-SQL because the QUESTION is the return value: does a second
    open of the same send report itself as news? A fake that flips sent -> viewed the way the CASE
    does is the smallest thing that can answer it without a database. The atomicity that stops two
    tabs both seeing 'sent' is a property of the statement, so it is pinned separately below."""

    def __init__(self, status="sent"):
        self.status = status
        self.calls = 0

    def q1(self, sql, params=()):
        self.calls += 1
        old = self.status
        if self.status == "sent":
            self.status = "viewed"
        return {"old_status": old}


def test_the_first_view_of_a_send_is_a_transition(monkeypatch):
    row = _Row("sent")
    monkeypatch.setattr(db, "q1", row.q1)
    assert db.mark_viewed("pid-1") is True
    assert row.status == "viewed", "the status flip stopped happening"


def test_the_second_view_is_not(monkeypatch):
    """The reload, the hash change, the tab left open. Every one of these still WRITES —
    last_viewed_at moves — and none of them is news."""
    row = _Row("sent")
    monkeypatch.setattr(db, "q1", row.q1)
    assert db.mark_viewed("pid-1") is True
    assert db.mark_viewed("pid-1") is False
    assert db.mark_viewed("pid-1") is False
    assert row.calls == 3, "a later view stopped writing at all"


@pytest.mark.parametrize("status", ["viewed", "approved", "closed_lost"])
def test_a_row_that_was_never_in_sent_reports_nothing(monkeypatch, status):
    """Only sent -> viewed is a transition. An approved proposal being re-read is not the customer
    opening it for the first time, and mailing the estimator about it would undo the guard."""
    monkeypatch.setattr(db, "q1", _Row(status).q1)
    assert db.mark_viewed("pid-1") is False


def test_a_proposal_that_is_not_there_reports_nothing(monkeypatch):
    """No row updated means no `returning` row. False, not a crash — and not a notification about
    a project that does not exist."""
    monkeypatch.setattr(db, "q1", lambda sql, params=(): None)
    assert db.mark_viewed("pid-gone") is False


def test_the_old_status_is_read_in_the_SAME_statement(monkeypatch):
    """A select-then-update leaves an application-level gap of a whole round trip in which the
    status can move — the customer's other tab, a re-send, staff closing the job — so the fact we
    notify about stops being the fact we wrote. One statement removes that gap.

    Not a serialization guarantee, and db.mark_viewed says so rather than letting the SQL imply it.
    This pins the shape that closes the round-trip window, which is the part that is achievable
    while `last_viewed_at` still has to move on every view."""
    seen = {}
    monkeypatch.setattr(db, "q1", lambda sql, params=(): seen.update(sql=sql, params=params))
    db.mark_viewed("pid-1")
    sql = " ".join(seen["sql"].split())
    assert sql.startswith("with prev as ("), "the old status is not captured in this statement"
    assert "select proposal_status as old_status" in sql
    assert "returning prev.old_status" in sql, "the caller is told nothing"
    assert sql.count("update public.portal_proposals") == 1, "two writes where there was one"
    assert seen["params"] == ("pid-1", "pid-1"), "the CTE and the update target different rows"


def test_it_still_writes_all_three_viewed_facts(monkeypatch):
    """Reporting the transition is additive. Dropping any of these would break the board's dates,
    its sort, or the follow-up cadence anchor — see db.mark_viewed."""
    seen = {}
    monkeypatch.setattr(db, "q1", lambda sql, params=(): seen.update(sql=sql))
    db.mark_viewed("pid-1")
    sql = " ".join(seen["sql"].split())
    assert "viewed_at = coalesce(p.viewed_at, now())" in sql
    assert "last_viewed_at = now()" in sql
    assert "cycle_viewed_at = case when p.proposal_status = 'sent'" in sql
    assert "proposal_status = case when p.proposal_status = 'sent' then 'viewed'" in sql


# ── the customer is not notified about their own click ────────────────────────
def test_a_view_card_is_kept_out_of_the_customers_bell(monkeypatch):
    """WITHOUT THIS the customer is told they opened their own proposal. The card says "Kevin
    Stucky opened the proposal" and is written for the estimator; the bell is a customer surface.

    Asserted on the query because that is where the exclusion lives — the bell has no idea who any
    given card is for, so a row that reaches this SELECT reaches the customer."""
    seen = {}
    monkeypatch.setattr(db, "qall", lambda sql, params=(): seen.update(sql=sql) or [])
    db.list_customer_events("dana@acme.com")
    sql = " ".join(seen["sql"].split())
    assert "coalesce((q.meta->>'view')::boolean, false) = false" in sql, (
        "list_customer_events does not exclude view cards — the customer gets a notification "
        "generated by their own click")
    assert "coalesce((q.meta->>'followup')::boolean, false) = false" in sql, (
        "the follow-up echo exclusion went with it")


def test_the_card_is_still_there_for_STAFF(monkeypatch):
    """The other half, or the exclusion above is indistinguishable from not writing the card. The
    thread is the whole point — it is where staff read the project's history."""
    seen = {}
    monkeypatch.setattr(db, "qall", lambda sql, params=(): seen.update(sql=sql) or [])
    db.list_messages("pid-1")
    sql = " ".join(seen["sql"].split())
    assert "meta" in sql, "the thread does not even fetch meta, so no card can render as one"
    assert "meta->>" not in sql, (
        "list_messages filters on meta — the estimator cannot see the view card either, and the "
        "notification has nowhere to land")


# ── 2b. the route ────────────────────────────────────────────────────────────
@pytest.fixture
def portal(monkeypatch):
    """The viewed route with the database and the mail server stubbed, and a `status` a test can
    move the way a re-send does.

    `mark_viewed` is emulated rather than hard-coded to a boolean: "notifies once per send, again
    after a re-send" is a statement about the ROW's history, and a fixture that just returns True
    could not tell the two apart."""
    from fastapi.testclient import TestClient
    import main

    calls = {"notify": [], "cards": [], "recorded": []}
    row = {"proposal_id": "pid-1", "token": "tok-1", "project_name": "Printing Co Test Hanz",
           "current_revision_no": 2, "proposal_status": "sent",
           "assigned_estimator": "kyle@wetreadwell.com"}

    def mark_viewed(pid):
        was = row["proposal_status"]
        if was == "sent":
            row["proposal_status"] = "viewed"
        return was == "sent"

    monkeypatch.setattr(main, "_require", lambda request, token: (row if token != "denied" else None))
    monkeypatch.setattr(main, "_session_email", lambda request: "kevin.stucky@printingco.com")
    monkeypatch.setattr(main.db, "mark_viewed", mark_viewed)
    monkeypatch.setattr(main.db, "record_view",
                        lambda pid, email: calls["recorded"].append((pid, email)))
    monkeypatch.setattr(main.db, "add_message",
                        lambda pid, kind, who, body, msg_type="text", meta=None:
                        calls["cards"].append({"pid": pid, "kind": kind, "author": who,
                                               "body": body, "msg_type": msg_type,
                                               "meta": meta}) or {"id": 1})
    monkeypatch.setattr(main.email_sender, "proposal_reply_to", lambda tok: "reply@notify.x")
    monkeypatch.setattr(main.email_sender, "notify_team",
                        lambda heading, body_html, **kw: calls["notify"].append(
                            {"heading": heading, "body": body_html, **kw}) or True)

    tc = TestClient(main.app)
    tc.calls = calls
    tc.row = row
    tc.main = main
    tc.monkeypatch = monkeypatch
    return tc


def _view(client, token="tok"):
    return client.post("/api/portal/%s/viewed" % token,
                       json={"revision_no": client.row["current_revision_no"]})


def test_opening_it_tells_the_team_and_the_thread(portal):
    r = _view(portal)
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "marked": True}

    assert len(portal.calls["notify"]) == 1, "nobody was emailed that the customer opened it"
    n = portal.calls["notify"][0]
    assert n["heading"] == "Proposal opened — Printing Co Test Hanz"

    assert len(portal.calls["cards"]) == 1, "nothing was written to the project's thread"
    c = portal.calls["cards"][0]
    assert c["body"] == "Kevin opened the proposal."
    assert c["msg_type"] == "system", "a new msg_type would need a migration and renders nowhere"
    assert c["kind"] == "staff" and c["author"] is None


def test_the_card_is_marked_as_a_view(portal):
    """`meta.view` is the ONLY thing keeping this out of the customer's notification bell. Writing
    the card without it ships the customer an alert about their own click."""
    _view(portal)
    assert portal.calls["cards"][0]["meta"] == {"view": True}


def test_the_email_names_no_address(portal):
    """Same rule as the send confirmation (2026-08-19): staff notifications do not recite customer
    contact details. The reader is named, and a first name is enough to act on."""
    _view(portal)
    n = portal.calls["notify"][0]
    assert "kevin.stucky@printingco.com" not in n["body"], "the customer's address is in the email"
    assert "Kevin" in n["body"] and "Printing Co Test Hanz" in n["body"]


def test_the_notification_carries_every_hook_the_roster_rules_need(portal):
    """Who hears about a project is notify_team's business — but only if this call HANDS it the
    project and the estimator. Dropping one kwarg silently narrows the audience, which is the same
    quiet miss the send confirmation was built to end."""
    _view(portal)
    n = portal.calls["notify"][0]
    assert n["proposal_id"] == "pid-1", "per-project adds and mutes cannot apply without the id"
    assert n["assigned_estimator"] == "kyle@wetreadwell.com", (
        "the estimator who owns the job would not be folded in")
    assert n["token"] == "tok-1" and n["project"] == "Printing Co Test Hanz", (
        "without token+project the email cannot join the project's staff thread")
    assert "portal.html?open=pid-1" in (n["reply_link"] or ""), "no deep link into the staff tool"
    assert n["reply_to"] == "reply@notify.x"


# ── once per send ────────────────────────────────────────────────────────────
def test_a_reload_does_not_tell_anybody_twice(portal):
    """THE GUARD. The endpoint fires on a hash change and repeats from a tab left open, and every
    call still writes last_viewed_at. Without the transition check the estimator gets an email per
    poll and learns to filter the lot."""
    for _ in range(4):
        assert _view(portal).status_code == 200
    assert len(portal.calls["notify"]) == 1, (
        "%d emails for one opening" % len(portal.calls["notify"]))
    assert len(portal.calls["cards"]) == 1, (
        "%d thread cards for one opening" % len(portal.calls["cards"]))
    assert len(portal.calls["recorded"]) == 4, (
        "the per-recipient view record stopped counting repeats — that one SHOULD fire every time")


def test_a_re_send_makes_it_news_again(portal):
    """reset_for_revision puts the row back to 'sent', so the transition is available a second
    time. Whether the customer opened the REVISION is a different question from whether they opened
    what it replaced, and the estimator chasing revision 2 needs the new answer."""
    _view(portal)
    assert len(portal.calls["notify"]) == 1

    portal.row["proposal_status"] = "sent"          # the re-send
    portal.row["current_revision_no"] = 3
    _view(portal)

    assert len(portal.calls["notify"]) == 2, "the estimator never heard about the revision opening"
    assert len(portal.calls["cards"]) == 2
    assert _view(portal).status_code == 200
    assert len(portal.calls["notify"]) == 2, "and then it went back to alerting on every reload"


def test_an_already_viewed_proposal_being_re_read_alerts_nobody(portal):
    """A customer who comes back a week later to re-read what they already opened. Nothing has
    changed, so there is nothing to tell anybody."""
    portal.row["proposal_status"] = "viewed"
    assert _view(portal).status_code == 200
    assert portal.calls["notify"] == [] and portal.calls["cards"] == []


def test_a_stale_tab_marks_nothing_and_alerts_nobody(portal):
    """The revision guard runs first. A tab still showing the previous revision must not resurrect
    the phantom view in the shape of a notification either."""
    portal.row["current_revision_no"] = 3
    r = portal.post("/api/portal/tok/viewed", json={"revision_no": 2})
    assert r.json() == {"ok": True, "marked": False}
    assert portal.calls["notify"] == [] and portal.calls["cards"] == []


def test_without_access_nothing_is_written_or_sent(portal):
    r = _view(portal, token="denied")
    assert r.status_code == 401
    assert portal.calls["notify"] == [] and portal.calls["cards"] == []


# ── 2c. none of it may cost the customer their page ──────────────────────────
def test_a_broken_email_still_serves_the_customer(portal):
    """This runs inside the request for the page the customer is reading. Resend being down is an
    estimator's missed notification, not a customer's missing proposal."""
    def boom(*a, **k):
        raise RuntimeError("resend 503")
    portal.monkeypatch.setattr(portal.main.email_sender, "notify_team", boom)
    r = _view(portal)
    assert r.status_code == 200
    assert r.json() == {"ok": True, "marked": True}
    assert len(portal.calls["cards"]) == 1, "the thread card went down with the email"


def test_a_broken_card_still_serves_the_customer_AND_still_emails(portal):
    """Guarded separately from the email on purpose: one try around both would let a missing
    column swallow the notification as well, and the estimator would hear nothing at all."""
    def boom(*a, **k):
        raise RuntimeError("relation missing")
    portal.monkeypatch.setattr(portal.main.db, "add_message", boom)
    r = _view(portal)
    assert r.status_code == 200
    assert r.json() == {"ok": True, "marked": True}
    assert len(portal.calls["notify"]) == 1, "the email went down with the thread card"


def test_nothing_is_announced_when_the_write_never_happened(portal):
    """ORDER MATTERS. The alerts hang off mark_viewed's RESULT, so they cannot run before it has
    succeeded. Announcing first would tell the estimator the customer opened a proposal whose row
    still says 'sent' — and the follow-up cadence, anchored on cycle_viewed_at, would go on
    chasing them for not having opened it."""
    def boom(pid):
        raise RuntimeError("connection pool exhausted")
    portal.monkeypatch.setattr(portal.main.db, "mark_viewed", boom)
    r = _view(portal)
    assert r.status_code == 200
    assert r.json() == {"ok": True, "marked": False}
    assert portal.calls["notify"] == [], "the team was told about a view that was never recorded"
    assert portal.calls["cards"] == [], "a card claims a view the row does not have"
    assert portal.calls["recorded"] == [], "a view was recorded against a status that never moved"


# ── who the card names ───────────────────────────────────────────────────────
@pytest.mark.parametrize("email,expected", [
    ("kevin.stucky@printingco.com", "Kevin opened the proposal."),
    ("kevin@printingco.com", "Kevin opened the proposal."),
    ("ap@printingco.com", "Ap opened the proposal."),
])
def test_the_reader_is_named_from_the_session(portal, email, expected):
    portal.monkeypatch.setattr(portal.main, "_session_email", lambda request: email)
    _view(portal)
    assert portal.calls["cards"][0]["body"] == expected


@pytest.mark.parametrize("email", [None, "", "   "])
def test_an_unnamed_reader_gets_a_sentence_we_would_show_the_customer(portal, email):
    """The card lands in the SHARED thread, so the fallback has to read as English to the customer
    as well. "None opened the proposal" is the version of this bug that ships to a customer."""
    portal.monkeypatch.setattr(portal.main, "_session_email", lambda request: email)
    _view(portal)
    assert portal.calls["cards"][0]["body"] == "The customer opened the proposal."
    assert "None" not in portal.calls["notify"][0]["body"]
