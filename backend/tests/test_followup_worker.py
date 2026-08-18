"""The tick. These guard the one thing that cannot be undone: emailing a customer
the same nag twice.

The worker reserves the right to send before sending. If it crashes in between, the
customer misses one nudge and the next cadence step covers it — recoverable. If it
sent first and crashed before recording, every restart would re-nag them, which is
not recoverable. So: reserve, then send, and release the reservation only when
nothing went out at all.
"""
from datetime import datetime, timedelta, timezone

import followup_rules as rules
import followup_worker as fw

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
    assert len(calls["thread"]) == 1, calls["thread"]
    echo = calls["thread"][0]
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
    body = calls["thread"][0]["body"].lower()
    for word in ("nudge", "chase", "cadence", "not_viewed", "rule", "template"):
        assert word not in body, "internal wording reached the customer: %r" % body
    assert "we emailed you" in body, body


def test_nothing_is_written_when_the_email_did_not_go(monkeypatch):
    """The reservation is released and retried, so a row here would claim we wrote to somebody we
    never reached — and the retry would then write a second one."""
    calls = _wire(monkeypatch, send_ok=False)
    fw._tick(NOW)
    assert calls["customer"], "the test needs a send attempt to have happened"
    assert calls["thread"] == []


def test_an_internal_note_to_the_team_never_reaches_the_customers_thread(monkeypatch):
    """THE safeguard, and the reason the echo is gated on audience rather than filtered later.

    Every staff template is written for us: "A quick call often beats another email", "this one is
    ours to chase", "Dates aren't held until the deposit is in". They also carry the customer's own
    address, the amount owed and a CRM link. The thread has no per-message visibility flag — the
    customer endpoint returns every row — so anything posted here is something the customer reads.

    Mutation: drop the `audience == "customer"` guard in _echo_to_thread."""
    # Driven straight at the guard rather than through the cadence: whether a staff-only reminder
    # happens to be due on a given fixture is a scheduling question, and this is a confidentiality
    # one. Every staff template is asserted, so adding a new one without deciding this again shows
    # up here.
    calls = _wire(monkeypatch)
    written = []
    monkeypatch.setattr(fw.db, "add_message",
                        lambda *a, **k: written.append(a) or {"id": 1})

    class _Due:
        def __init__(self, audience, template):
            self.audience, self.template = audience, template
            self.rule_key = template

    for template in ("staff_not_viewed", "staff_pause_expired",
                     "staff_personal_followup", "staff_deposit_outstanding"):
        fw._echo_to_thread("p1", _Due("staff", template))
    assert written == [], (
        "an internal note was posted into the customer's conversation: %r" % written)

    # THE AUDIENCE GUARD, ON ITS OWN. The four names above are also absent from the wording map, so
    # they would be refused by that lookup even with the guard gone — asserting only those passes
    # whether or not the guard exists, which is a test proving nothing. This case carries a template
    # the map DOES know with a staff audience, so the guard is the single thing standing between an
    # internal reminder and the customer's screen.
    fw._echo_to_thread("p1", _Due("staff", "not_viewed"))
    assert written == [], (
        "a staff-audience reminder was echoed to the customer because the audience guard is gone")

    # …and the same call with a customer audience DOES write, so the assertion above is the guard
    # working rather than the echo being broken.
    fw._echo_to_thread("p1", _Due("customer", "not_viewed"))
    assert len(written) == 1, "the customer echo stopped working, so the test above proves nothing"
