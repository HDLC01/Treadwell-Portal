"""The tick. These guard the one thing that cannot be undone: emailing a customer
the same nag twice.

The worker reserves the right to send before sending. If it crashes in between, the
customer misses one nudge and the next cadence step covers it — recoverable. If it
sent first and crashed before recording, every restart would re-nag them, which is
not recoverable. So: reserve, then send, and release the reservation only when
nothing went out at all.
"""
import re
from datetime import datetime, timedelta, timezone

import pytest

import db
import followup_rules as rules
import followup_worker as fw

# The real writer, captured before any test stubs it. _wire replaces db.add_message with a
# recorder, and `db` here is the same module object the worker holds, so a test that wants the
# genuine INSERT back has to have kept a reference to it from before that happened.
_REAL_ADD_MESSAGE = db.add_message

NOW = datetime(2026, 8, 4, 15, 0, tzinfo=timezone.utc)     # 10am Chicago
ENROLLED = NOW - timedelta(hours=30)                       # first nudge is due


def _proposal(**over):
    p = {"proposal_id": "p1", "token": "tok", "customer_email": "c@x.com",
         "customer_name": "Cust", "project_name": "Westport",
         "proposal_status": "sent", "followup_enrolled_at": ENROLLED,
         "followup_disabled_at": None, "followup_paused_until": None,
         "cycle_viewed_at": None, "deposit_required": True,
         "assigned_estimator": "kyle@wetreadwell.com", "approved_total": 27653.0}
    p.update(over)
    return p


def _wire(monkeypatch, *, proposal=None, reserve=lambda pid, key, detail: 1,
          send_ok=True, staff_ok=True, enabled="true"):
    calls = {"reserved": [], "deleted": [], "customer": [], "staff": [], "candidates": 0,
             # What the worker echoed into the project's conversation. Stubbed like every other
             # database call here: unstubbed it is a real connection, and this one runs on the
             # success path of every test in the file.
             "thread": []}
    p = proposal if proposal is not None else _proposal()

    monkeypatch.setenv("FOLLOWUP_AUTOMATION_ENABLED", enabled)

    def _candidates():
        calls["candidates"] += 1
        return [p]

    # The cadence settings, stubbed as ABSENT — which is the path these tests already took, just
    # 30 seconds faster each. `_settings()` was the only unstubbed database call in the tick, so
    # every test here paid a full connection timeout before falling back to the shipped cadence.
    # Twenty tests, thirty seconds apiece: seven minutes of every CI run, and the assertions were
    # identical either way because `merge(None)` IS the shipped cadence. A test that wants a
    # specific cadence overrides this after _wire, as monkeypatch allows.
    monkeypatch.setattr(fw.db, "get_settings", lambda key: None)
    monkeypatch.setattr(fw.db, "list_followup_candidates", _candidates)
    monkeypatch.setattr(fw.db, "get_proposal", lambda pid: p)
    monkeypatch.setattr(fw.db, "get_recipients", lambda pid: ["c@x.com", "b@x.com"])
    # The worker asks who should be CHASED, not who is on the proposal — a contact can be opted
    # out of the automated follow-ups while still receiving the proposal, the invoice and every
    # reply (see test_followup_optout.py). Both are stubbed here: get_recipients is still read to
    # tell "nobody opted in" apart from "the read failed".
    monkeypatch.setattr(fw.db, "get_followup_recipients", lambda pid: ["c@x.com", "b@x.com"])

    def _reserve(pid, key, detail):
        calls["reserved"].append(key)
        return reserve(pid, key, detail)

    monkeypatch.setattr(fw.db, "reserve_followup", _reserve)
    monkeypatch.setattr(fw.db, "delete_followup", lambda rid: calls["deleted"].append(rid))
    monkeypatch.setattr(
        fw.db, "add_message",
        lambda pid, kind, who, body, **k: calls["thread"].append(
            {"body": body, "msg_type": k.get("msg_type"), "meta": k.get("meta")}) or {"id": 1})
    monkeypatch.setattr(fw.email_sender, "proposal_reply_to", lambda t: "proposals@notify.x")
    monkeypatch.setattr(fw.email_sender, "_resolve_notify", lambda kind, pid=None: ["bids@x.com"])
    monkeypatch.setattr(
        fw.email_sender, "send_followup",
        lambda addr, url, proj, tmpl, **k: calls["customer"].append(
            {"to": addr, "template": tmpl, "ask": k.get("include_status_ask"),
             "deposit": k.get("deposit_required")}) or send_ok)
    monkeypatch.setattr(
        fw.email_sender, "notify_team",
        lambda subject, body, **k: calls["staff"].append(
            {"subject": subject, "to": k.get("recipients")}) or staff_ok)
    return calls


def test_a_due_proposal_emails_every_recipient_and_tells_the_estimator(monkeypatch):
    calls = _wire(monkeypatch)
    fw._tick(NOW)
    assert [c["to"] for c in calls["customer"]] == ["c@x.com", "b@x.com"]
    assert calls["customer"][0]["template"] == "not_viewed"
    assert calls["staff"][0]["to"] == ["kyle@wetreadwell.com"]      # the assigned estimator
    assert calls["deleted"] == []


def test_the_reservation_is_taken_before_anything_is_sent(monkeypatch):
    """Ordering is the whole safety property, so assert it directly."""
    order = []
    calls = _wire(monkeypatch, reserve=lambda pid, key, detail: order.append("reserve") or 1)
    monkeypatch.setattr(fw.email_sender, "send_followup",
                        lambda *a, **k: order.append("send") or True)
    fw._tick(NOW)
    assert order[0] == "reserve" and "send" in order


def test_an_already_reserved_rule_sends_nothing(monkeypatch):
    """A prior tick, or the twin container during a deploy, already sent this one."""
    calls = _wire(monkeypatch, reserve=lambda pid, key, detail: None)
    fw._tick(NOW)
    assert calls["customer"] == [] and calls["staff"] == []
    assert calls["reserved"]              # it did try


def test_a_total_send_failure_releases_the_reservation_so_the_next_tick_retries(monkeypatch):
    calls = _wire(monkeypatch, send_ok=False, staff_ok=False)
    fw._tick(NOW)
    assert calls["deleted"], "a failed send must not leave the rule marked as sent"


def test_a_partial_send_keeps_the_reservation(monkeypatch):
    """One recipient bounced, another got it. Retrying would re-nag the one who did."""
    calls = _wire(monkeypatch)
    sent = {"n": 0}

    def _one_ok(addr, url, proj, tmpl, **k):
        sent["n"] += 1
        return sent["n"] == 1        # first succeeds, second fails

    monkeypatch.setattr(fw.email_sender, "send_followup", _one_ok)
    fw._tick(NOW)
    assert calls["deleted"] == []


def test_the_kill_switch_stops_everything(monkeypatch):
    calls = _wire(monkeypatch, enabled="false")
    fw._tick(NOW)
    assert calls["candidates"] == 0 and calls["customer"] == []


def test_the_flag_is_read_every_tick_not_at_import(monkeypatch):
    """Turning automation off in production is an env change plus a restart, so the
    worker must believe the environment now rather than at load time."""
    monkeypatch.setenv("FOLLOWUP_AUTOMATION_ENABLED", "false")
    assert fw._enabled() is False
    monkeypatch.setenv("FOLLOWUP_AUTOMATION_ENABLED", "true")
    assert fw._enabled() is True


def test_a_proposal_approved_since_the_list_was_built_is_not_nagged_to_approve(monkeypatch):
    """The candidate list is minutes old by the time we reach a row, so the worker re-reads it.
    Someone who approved in between must not get the reminder that was queued for them.

    This test used to assert that they got NOTHING. Hanz, 2026-08-12: "followups should be
    automated until a deposit has been received" — so approving moves them to the deposit stage
    rather than ending the cadence. The re-read still has to be honoured, which is what this asserts:
    not one of the four proposal-chasing emails goes out, and the deposit reminder is what does.

    Sending "we need your signed approval and the deposit before we can book your dates" to
    somebody who signed four minutes ago is the exact failure the re-read exists to prevent, and it
    would be the DEFAULT if the stage fell through to the viewed branch instead of returning."""
    stale = _proposal()
    calls = _wire(monkeypatch, proposal=stale)
    fresh = dict(stale, proposal_status="approved", approved_at=ENROLLED,
                 cycle_viewed_at=ENROLLED, deposit_status="pending")
    monkeypatch.setattr(fw.db, "get_proposal", lambda pid: fresh)
    fw._tick(NOW)
    sent = [c["template"] for c in calls["customer"]]
    for chasing in ("not_viewed", "next_steps", "second_nudge", "checkin"):
        assert chasing not in sent, (
            "a customer who approved since the list was built is still being asked to approve")
    assert sent and set(sent) == {"deposit_nudge"}, sent


def test_a_proposal_approved_AND_PAID_since_the_list_was_built_is_skipped(monkeypatch):
    """The case that really does end everything, and the one the old version of the test above was
    reaching for. Nothing is even reserved: `in_scope` is checked before the rules run, so a row
    that settled in the last few minutes costs one read and no send."""
    stale = _proposal()
    calls = _wire(monkeypatch, proposal=stale)
    monkeypatch.setattr(fw.db, "get_proposal",
                        lambda pid: dict(stale, proposal_status="approved",
                                         deposit_status="received"))
    fw._tick(NOW)
    assert calls["customer"] == [] and calls["reserved"] == []


def test_a_proposal_closed_lost_since_the_list_was_built_is_skipped(monkeypatch):
    """Same guard, the other terminal state — and the one that must never depend on a deposit."""
    stale = _proposal()
    calls = _wire(monkeypatch, proposal=stale)
    monkeypatch.setattr(fw.db, "get_proposal",
                        lambda pid: dict(stale, proposal_status="closed_lost"))
    fw._tick(NOW)
    assert calls["customer"] == [] and calls["reserved"] == []


def test_a_proposal_taken_off_automation_since_the_list_was_built_is_skipped(monkeypatch):
    stale = _proposal()
    calls = _wire(monkeypatch, proposal=stale)
    monkeypatch.setattr(fw.db, "get_proposal",
                        lambda pid: dict(stale, followup_disabled_at=NOW))
    fw._tick(NOW)
    assert calls["reserved"] == []


def test_one_bad_proposal_does_not_end_the_sweep(monkeypatch):
    good = _proposal(proposal_id="good")
    bad = _proposal(proposal_id="bad")
    calls = _wire(monkeypatch)
    monkeypatch.setattr(fw.db, "list_followup_candidates", lambda: [bad, good])

    def _get(pid):
        if pid == "bad":
            raise RuntimeError("row is a mess")
        return good

    monkeypatch.setattr(fw.db, "get_proposal", _get)
    fw._tick(NOW)
    assert calls["customer"], "the healthy proposal must still be chased"


def test_the_deposit_sentence_follows_the_proposal_flag(monkeypatch):
    """Telling a GC we need a deposit on a job sent without one is simply wrong."""
    viewed = _proposal(proposal_status="viewed", cycle_viewed_at=NOW - timedelta(hours=25),
                       deposit_required=False)
    calls = _wire(monkeypatch, proposal=viewed)
    fw._tick(NOW)
    assert calls["customer"][0]["deposit"] is False
    assert calls["customer"][0]["template"] == "next_steps"


def test_the_status_ask_appears_only_on_the_recurring_stage(monkeypatch):
    early = _proposal(proposal_status="viewed", cycle_viewed_at=NOW - timedelta(hours=25))
    calls = _wire(monkeypatch, proposal=early)
    fw._tick(NOW)
    assert calls["customer"][0]["ask"] is False

    late = _proposal(proposal_status="viewed", cycle_viewed_at=NOW - timedelta(hours=150))
    calls2 = _wire(monkeypatch, proposal=late)
    fw._tick(NOW)
    assert calls2["customer"][0]["ask"] is True


def test_an_unassigned_proposal_still_reaches_a_human(monkeypatch):
    """Proposals published before assignment was required have no owner; the note
    falls back to the notification roster rather than vanishing."""
    calls = _wire(monkeypatch, proposal=_proposal(assigned_estimator=None))
    fw._tick(NOW)
    assert calls["staff"][0]["to"] == ["bids@x.com"]


def test_the_worker_never_starts_under_pytest(monkeypatch):
    """Every test file builds a TestClient, which runs app startup. Without this the
    suite spawns a thread that blocks on a database the tests deliberately lack."""
    monkeypatch.setenv("FOLLOWUP_AUTOMATION_ENABLED", "true")
    assert fw.ensure_started() is False


def test_the_tick_interval_is_clamped(monkeypatch):
    monkeypatch.setenv("FOLLOWUP_TICK_SECONDS", "5")
    assert fw._interval() == 60
    monkeypatch.setenv("FOLLOWUP_TICK_SECONDS", "99999")
    assert fw._interval() == 3600
    monkeypatch.setenv("FOLLOWUP_TICK_SECONDS", "nonsense")
    assert fw._interval() == 900


# ── the default, pinned ───────────────────────────────────────────────
# Added 2026-08-04 on Hanz's instruction: "email follow ups should be automatically off."
#
# The two defaults are not symmetric. Default ON and be wrong, and automated follow-up mail
# goes to real customers from whatever box happens to be running. Default OFF and be wrong,
# and nothing sends until somebody notices a missing reminder. Nothing pinned this before,
# so it defaulted to "true" and production was safe only because the compose file happened
# to say otherwise.
def test_follow_up_automation_is_off_when_nobody_has_said_otherwise(monkeypatch):
    monkeypatch.delenv("FOLLOWUP_AUTOMATION_ENABLED", raising=False)
    assert fw._enabled() is False


def test_a_missing_config_attribute_does_not_re_enable_automation(monkeypatch):
    """The fallback used to be `getattr(config, ..., True)`, so a build where the config
    attribute went missing would have quietly switched customer email back ON — the one case
    you least want it guessing."""
    import config
    monkeypatch.delenv("FOLLOWUP_AUTOMATION_ENABLED", raising=False)
    monkeypatch.delattr(config, "FOLLOWUP_AUTOMATION_ENABLED", raising=False)
    assert fw._enabled() is False


def test_anything_that_is_not_an_explicit_yes_leaves_automation_off(monkeypatch):
    """Fails closed on junk, including near-misses: "of" and "truee" are typos somebody
    will make in a compose file, and neither should mail a customer."""
    for junk in ("", "of", "truee", "maybe", "disabled", "off", "0", "no"):
        monkeypatch.setenv("FOLLOWUP_AUTOMATION_ENABLED", junk)
        assert fw._enabled() is False, junk


# ── the reminder appears in the conversation ──────────────────────────────────
# Hanz, 2026-08-19: "For the Email follow ups, can it appear in the ChatBox and a history of the
# follow ups." A customer who reads the portal rather than their inbox used to watch the thread go
# silent while six emails went out, and staff had no shared record of what had been chased.
def test_a_sent_reminder_appears_in_the_thread(monkeypatch):
    calls = _wire(monkeypatch)
    fw._tick(NOW)
    # TWO rows on this step, because it sends twice: the customer's reminder and the
    # estimator's. The customer's is the one WITHOUT meta.internal, and it is picked by that
    # flag rather than by position so this keeps asserting the customer's card if the order
    # the cadence returns its dues ever changes.
    assert len(calls["thread"]) == 2, calls["thread"]
    echo = next(m for m in calls["thread"] if not (m["meta"] or {}).get("internal"))
    # `system` because both screens already render that as a card — the customer's app.js and the
    # staff drawer's portal.js — and because it sits inside the existing msg_type CHECK constraint,
    # so this needed no migration.
    assert echo["msg_type"] == "system"
    assert echo["meta"]["followup"] is True, "not marked machine-sent, so it will ring the bell"
    # Written as "Heading — detail": both renderers split on the dash for the card's title.
    assert " — " in echo["body"], echo["body"]


def test_the_wording_is_what_we_would_say_to_the_customers_face(monkeypatch):
    """The customer sees this row in their own thread. Our internal vocabulary for the cadence —
    "nudge", "chase", "second nudge", the rule keys, the template names — reads as being told off
    when it is pointed at the person it describes.

    Mutation: echo `due.template` or `due.rule_key` instead of a sentence."""
    calls = _wire(monkeypatch)
    fw._tick(NOW)
    body = next(m for m in calls["thread"]
                if not (m["meta"] or {}).get("internal"))["body"].lower()
    for word in ("nudge", "chase", "cadence", "not_viewed", "rule", "template"):
        assert word not in body, "internal wording reached the customer: %r" % body
    assert "we emailed you" in body, body


def test_nothing_is_written_when_the_email_did_not_go(monkeypatch):
    """The reservation is released and retried, so a row here would claim we wrote to somebody we
    never reached — and the retry would then write a second one.

    BOTH halves of the step fail here, because both halves now write. With only the customer
    send failing, the estimator's email still went out and its own row is legitimate — which
    is what the pair of assertions below pins: an attempt was made on each side, and neither
    left a record."""
    calls = _wire(monkeypatch, send_ok=False, staff_ok=False)
    fw._tick(NOW)
    assert calls["customer"], "the test needs a send attempt to have happened"
    assert calls["staff"], "the estimator's half was never attempted either"
    assert calls["thread"] == []


# ── the STAFF reminders are in the thread too, and only staff can read them ─────
# Hanz, 2026-08-24: "make sure all follow up emails are shown in the Chat box and in the Follow Ups
# section." Four of the reminders go to the ESTIMATOR and nobody else, and until now the Chat box
# showed none of them: the echo returned early on anything that was not a customer send, so a
# project could have been chased four times with a thread that looked untouched.
#
# They are echoed as `meta.internal` rows. That flag did not exist when the early return was
# written; it does now, `db.list_messages` excludes it by DEFAULT, and the staff drawer is the one
# reader that opts out (main.py, include_internal=True). So wording written for an estimator
# reaches the estimator and stops there.
_STAFF_TEMPLATES = ("staff_not_viewed", "staff_personal_followup",
                    "staff_deposit_outstanding", "staff_pause_expired")


class _Due:
    """What rules.Due presents to the echo: an audience, a template, a rule key."""

    def __init__(self, audience, template):
        self.audience, self.template = audience, template
        self.rule_key = template


class _Thread:
    """portal_questions, driven by the REAL db functions with only the connection replaced.

    A row goes in through db.add_message's own INSERT and comes back out through
    db.list_messages' own SELECT, so what these tests assert is the visibility those two
    statements actually carry rather than a restatement of it here. The internal predicate is
    read OFF the SQL: if list_messages stops emitting it, this store stops filtering, exactly as
    Postgres would, and the customer-side assertions go red.

    Only the two statements this file exercises are accepted — anything else asserts, so a future
    caller that reaches the database through here fails loudly instead of getting an empty list.
    """

    _HIDES_INTERNAL = "coalesce((meta->>'internal')::boolean, false) = false"

    def __init__(self):
        self.rows = []

    def q1(self, sql, params=()):
        assert "insert into public.portal_questions" in sql, sql
        # Column names off the statement, not hard-coded: a reordered INSERT would otherwise file
        # the body under msg_type here and pass anyway.
        cols = [c.strip() for c in re.search(r"\(([^)]*)\)", sql).group(1).split(",")]
        # psycopg wraps a jsonb parameter in Jsonb(...); `.obj` is the dict the caller passed.
        row = dict(zip(cols, [getattr(v, "obj", v) for v in params]))
        row["id"] = len(self.rows) + 1
        row["created_at"] = NOW
        self.rows.append(row)
        return dict(row)

    def qall(self, sql, params=()):
        assert "from public.portal_questions" in sql, sql
        flat = " ".join(sql.split())
        pid, after = params[0], int(params[1] or 0)
        rows = [r for r in self.rows if r["proposal_id"] == pid and r["id"] > after]
        if self._HIDES_INTERNAL in flat:
            rows = [r for r in rows if not (r.get("meta") or {}).get("internal")]
        return [dict(r) for r in rows]


@pytest.fixture()
def thread(monkeypatch):
    t = _Thread()
    monkeypatch.setattr(db, "q1", t.q1)
    monkeypatch.setattr(db, "qall", t.qall)
    return t


def test_every_staff_reminder_is_recorded_in_the_thread(thread):
    """One row per reminder, so the drawer shows what has been chased and when.

    Driven at the echo rather than through the cadence: which of the four happens to be due on a
    given fixture is a scheduling question, and this is a record-keeping one. Every staff template
    is named, so adding a fifth without deciding its wording shows up here.

    Mutation: drop one entry from _ECHO_STAFF."""
    for t in _STAFF_TEMPLATES:
        fw._echo_to_thread("p1", _Due("staff", t))
    rows = db.list_messages("p1", include_internal=True)
    assert [r["meta"]["template"] for r in rows] == list(_STAFF_TEMPLATES)
    for r in rows:
        assert r["msg_type"] == "system", "a new msg_type renders nowhere and needs a migration"
        assert r["author_kind"] == "staff" and r["author_email"] is None
        assert r["meta"]["followup"] is True, "not marked machine-sent"
        # "Heading — detail". Both renderers split the card title off the first dash and give
        # up past 60 characters (splitSystem in portal.js), and a line that misses the window
        # renders under the generic title "Update" with the whole sentence in the body.
        assert 0 < r["body"].find(" — ") <= 60, r["body"]


def test_a_staff_reminder_is_invisible_to_the_customer(thread):
    """THE safeguard. Every staff template is written for us — "a call usually beats another
    email", "dates are not held until it is in" — and the emails behind them carry the customer's
    own address, the amount outstanding and a CRM link.

    Asserted through the CUSTOMER's read: db.list_messages with the DEFAULT argument, which is
    what the portal's view and its poll call. Inspecting the meta we wrote would only prove we set
    a flag; this proves the flag is the one that query filters on.

    Mutation: drop "internal": True from the staff meta in _echo_to_thread."""
    for t in _STAFF_TEMPLATES:
        fw._echo_to_thread("p1", _Due("staff", t))
    assert db.list_messages("p1", include_internal=True), (
        "nothing was written at all, so this proves nothing")
    assert db.list_messages("p1") == [], (
        "an internal reminder is in the customer's copy of the thread: %r"
        % db.list_messages("p1"))


def test_the_customer_echo_is_still_exactly_the_row_it_has_always_been(thread):
    """LIVE IN PRODUCTION, with rows already in real threads. The customer half must post the same
    visible card it posts today: same wording, same meta, and NO internal flag — marking it would
    hide from the customer the record of an email they actually received.

    Mutation: add "internal": True to the customer branch's meta."""
    fw._echo_to_thread("p1", _Due("customer", "not_viewed"))
    visible = db.list_messages("p1")
    assert len(visible) == 1, visible
    assert visible[0]["meta"] == {"followup": True, "template": "not_viewed"}
    assert visible[0]["body"] == (
        "Reminder sent — we emailed you a link to the proposal in case it got buried.")


def test_the_staff_wording_is_written_for_an_estimator(thread):
    """"Reminder sent — we emailed you" is the customer's line and would be false here: on these
    four steps nothing went to the customer at all. Each line says which fact triggered the email
    the estimator just received, in the terms that email uses.

    Mutation: point _ECHO_STAFF at the customer wording, or echo due.template."""
    for t in _STAFF_TEMPLATES:
        fw._echo_to_thread("p1", _Due("staff", t))
    rows = db.list_messages("p1", include_internal=True)
    by = {r["meta"]["template"]: r["body"].lower() for r in rows}
    for template, body in by.items():
        assert "we emailed you" not in body, template
        assert "reminder sent" not in body, template
        for word in ("nudge", "cadence", "staff_", "template", "rule_key"):
            assert word not in body, (template, word)
    # The trigger, per template: these are four different phone calls, and one shared line saying
    # "a follow-up was sent" would be the version of this that tells the estimator nothing.
    assert "opened" in by["staff_not_viewed"]
    assert "read" in by["staff_personal_followup"]
    assert "deposit" in by["staff_deposit_outstanding"]
    assert "delay" in by["staff_pause_expired"]
    assert len(set(by.values())) == len(_STAFF_TEMPLATES), "two reminders say the same thing"


def test_an_unmapped_template_still_says_nothing(thread):
    """Kept from the original: a template with no wording posts nothing rather than guessing, so a
    fifth reminder added without a line here is silent instead of showing an estimator
    "staff_whatever_we_called_it".

    The last two cases are the maps not leaking across audiences. A customer-audience send must
    never pick up staff wording (it would post a note ABOUT the customer TO the customer), and a
    staff-audience send must never pick up the customer's (it would post a non-internal "we
    emailed you" for an email the customer never got).

    Mutation: fall back to the other map, or to `due.template`, when a lookup misses."""
    fw._echo_to_thread("p1", _Due("staff", "staff_invented_tomorrow"))
    fw._echo_to_thread("p1", _Due("customer", "invented_tomorrow"))
    fw._echo_to_thread("p1", _Due("", "staff_not_viewed"))
    fw._echo_to_thread("p1", _Due("staff", "not_viewed"))
    fw._echo_to_thread("p1", _Due("customer", "staff_not_viewed"))
    assert db.list_messages("p1", include_internal=True) == []


def test_the_tick_records_both_halves_of_the_same_step(monkeypatch, thread):
    """End to end through the cadence rather than at the helper. The not-viewed step sends twice —
    the customer's reminder and the estimator's — and the Chat box has to show both, with only one
    of them visible to the customer.

    Mutation: restore the `if due.audience != "customer": return` early return."""
    calls = _wire(monkeypatch)
    # Un-stub the writer: this test wants the real INSERT, into the fake thread above.
    monkeypatch.setattr(fw.db, "add_message", _REAL_ADD_MESSAGE)
    fw._tick(NOW)
    assert [c["template"] for c in calls["customer"]] == ["not_viewed", "not_viewed"]
    assert calls["staff"], "the estimator's half of the step never sent"
    both = db.list_messages("p1", include_internal=True)
    assert [r["meta"]["template"] for r in both] == ["not_viewed", "staff_not_viewed"]
    assert [r["meta"]["template"] for r in db.list_messages("p1")] == ["not_viewed"]


def test_a_staff_reminder_cannot_reach_the_customers_notification_bell(monkeypatch):
    """list_customer_events is a SECOND, INDEPENDENT customer surface with its own filter — its
    docstring says so — and it selects exactly the shape these rows have: author_kind 'staff',
    msg_type 'system'. Two of its predicates cover them and both are asserted, because the row
    carries both flags and neither is redundant: `internal` says the row is not the customer's at
    all, `followup` says a machine sent it.

    Asserted on the statement because that is where the exclusion lives; a row that reaches this
    SELECT reaches the customer's bell."""
    seen = {}
    monkeypatch.setattr(db, "qall", lambda sql, params=(): seen.update(sql=sql) or [])
    db.list_customer_events("c@x.com")
    sql = " ".join(seen["sql"].split())
    assert "coalesce((q.meta->>'internal')::boolean, false) = false" in sql, (
        "the customer's bell does not exclude internal rows — an estimator's reminder about the "
        "customer would ring the customer's own bell")
    assert "coalesce((q.meta->>'followup')::boolean, false) = false" in sql, (
        "the follow-up echo exclusion went with it")
