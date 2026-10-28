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
          send_ok=True, staff_ok=True, enabled="true", signals=None,
          reserve_task=lambda pid, key, detail: 7, pending=(), stale=(), recorded=()):
    calls = {"reserved": [], "deleted": [], "customer": [], "staff": [], "candidates": 0,
             # What the worker echoed into the project's conversation. Stubbed like every other
             # database call here: unstubbed it is a real connection, and this one runs on the
             # success path of every test in the file.
             "thread": [],
             # Remind-only (2026-10-23): the follow-up tasks the tick raised, and the decisions
             # its housekeeping took on open ones.
             "tasks": [], "decided": [],
             # The recovery of a send that was claimed and never confirmed (`sending`).
             "reopened": []}
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
    # The touches, the reminder count and the open task. None of them by default: a proposal
    # nobody has touched, with no task open — the shape every pre-remind-only test assumed.
    monkeypatch.setattr(fw.db, "followup_signals", lambda pid: dict(signals or {}))

    def _reserve_task(pid, key, detail):
        calls["tasks"].append({"key": key, "detail": detail})
        return reserve_task(pid, key, detail)

    monkeypatch.setattr(fw.db, "reserve_followup_task", _reserve_task)
    monkeypatch.setattr(fw.db, "list_pending_followup_tasks", lambda: list(pending))
    monkeypatch.setattr(fw.db, "decide_followup_task",
                        lambda tid, status, by, extra=None, from_status="pending":
                        calls["decided"].append(
                            (tid, status, (extra or {}).get("reason"))) or {"id": tid})
    monkeypatch.setattr(fw.db, "list_stale_sending_tasks", lambda minutes: list(stale))
    monkeypatch.setattr(fw.db, "followup_task_send_recorded", lambda tid: tid in recorded)
    monkeypatch.setattr(fw.db, "reopen_followup_task",
                        lambda tid, extra=None: calls["reopened"].append(
                            (tid, dict(extra or {}))) or True)
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


# 2026-10-28: the tick EMAILS THE CUSTOMER again (Hanz: "the follow ups should be a follow up email
# to the customer okay?"). The remind-only assertions that lived here went with remind-only; the
# send, the dedupe, the release-on-failure, the opt-outs and the re-read are pinned against the
# real renderer and a fake transport in test_followup_auto_worker.py. What is left here is wiring.
_PENDING = {"id": 99, "detail": {"status": "pending", "template": "not_viewed"},
            "created_at": "2026-08-04T10:00:00+00:00"}


def test_a_partial_send_still_counts_as_sent(monkeypatch):
    """One recipient bounced, another got it. Retrying would re-nag the one who did. The rule now
    lives where customer email is sent — the estimator's send of a task (send_task_email) — and
    True there is what keeps the task from being reopened."""
    _wire(monkeypatch)
    sent = {"n": 0}

    def _one_ok(addr, url, proj, tmpl, **k):
        sent["n"] += 1
        return sent["n"] == 1        # first succeeds, second fails

    monkeypatch.setattr(fw.email_sender, "send_followup", _one_ok)
    tpl = {"title": "Checking in", "body": "Hi {first_name},\n\n{link}", "cta": "View"}
    assert fw.send_task_email(_proposal(), "checkin", True, tpl) is True
    assert sent["n"] == 2


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


def test_the_wording_is_what_we_would_say_to_the_customers_face(monkeypatch):
    """The customer sees this row in their own thread when an estimator SENDS a follow-up (the
    send endpoint echoes through _echo_to_thread). Our internal vocabulary for the cadence —
    "nudge", "chase", "second nudge", the rule keys, the template names — reads as being told off
    when it is pointed at the person it describes.

    Mutation: echo `due.template` or `due.rule_key` instead of a sentence."""
    calls = _wire(monkeypatch)
    fw._echo_to_thread("p1", rules.Due("c1:nv1", "customer", "not_viewed"), {"task_id": 7})
    row = calls["thread"][0]
    body = row["body"].lower()
    for word in ("nudge", "chase", "cadence", "not_viewed", "rule", "template"):
        assert word not in body, "internal wording reached the customer: %r" % body
    assert "we emailed you" in body, body
    assert row["meta"]["followup"] is True and not row["meta"].get("internal")
    assert row["meta"]["task_id"] == 7


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
                    "staff_deposit_outstanding", "staff_pause_expired",
                    # Remind-only (2026-10-23): the card for a raised follow-up task.
                    "staff_followup_due")


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
