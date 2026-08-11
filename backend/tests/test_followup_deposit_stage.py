"""The cadence does not stop at approval — it stops at the deposit.

Hanz, 2026-08-12: **"remember followups should be automated until a deposit has been received."**

WHAT IT WAS BEFORE. `due_now` fired only while `proposal_status` was `sent` or `viewed`,
`db.list_followup_candidates` excluded approved rows in SQL, and the tool's 6am digest skipped them
with a comment claiming "the deposit column and its own reminders own that". There were no deposit
reminders. An approved job got one invoice email and then silence on every channel we have — which
is the exact failure the whole follow-up system exists to prevent, arriving one stage later.

THE TWO AUDIENCES STOP AT DIFFERENT POINTS. His choice, offered the alternatives:

  * the CUSTOMER stops at `submitted`. Once they have recorded ACH details or told us the cheque is
    posted, the money is in flight; chasing then reads as a mistake or a second charge.
  * the ESTIMATOR keeps going until `received`. A cheque that never arrives must not go quiet.

That split is the thing most likely to be "simplified" into one stop point by somebody reading this
engine later, so it is asserted from both sides.

WHY THE STAGE RETURNS INSTEAD OF FALLING THROUGH. An approved proposal has `cycle_viewed_at` set,
so the viewed branch would happily chase it with "getting you on the schedule — we need your signed
approval and the deposit". On a job that is already signed.

WHY THIS STAGE ALONE SUPPRESSES THE STATUS ASK. Every other recurring nudge offers "has your
timeline changed?", whose second button reads "Not moving forward" and sets closed_lost. On signed,
invoiced work that is a one-click cancel, and a stray tap kills a won job.
"""
from datetime import datetime, timedelta, timezone

import pytest

import followup_rules as fr
import followup_settings as fs

ENROLLED = datetime(2026, 8, 1, 14, 0, tzinfo=timezone.utc)
APPROVED = datetime(2026, 8, 4, 15, 0, tzinfo=timezone.utc)


def _row(**kw):
    """An approved proposal with its deposit outstanding, at the default cadence."""
    row = dict(
        proposal_id="p1",
        proposal_status="approved",
        followup_enrolled_at=ENROLLED,
        cycle_viewed_at=ENROLLED + timedelta(hours=6),
        approved_at=APPROVED,
        deposit_status="pending",
    )
    row.update(kw)
    return row


def _at(hours):
    """A time `hours` after approval, inside the send window (10:00–13:00 Central)."""
    return APPROVED + timedelta(hours=hours)


def _due(row, at, cfg=None):
    return fr.due_now(row, at, cfg)


def _templates(dues):
    return [d.template for d in dues]


# ── the customer's half ──────────────────────────────────────────────────────
def test_an_approved_proposal_with_no_deposit_gets_chased():
    """The whole ask, in one assertion. 24h after approval at the shipped cadence."""
    assert "deposit_nudge" in _templates(_due(_row(), _at(25)))


def test_nothing_fires_before_the_first_interval():
    assert _templates(_due(_row(), _at(2))) == []


def test_the_clock_starts_AT_APPROVAL_not_at_the_send():
    """Anchored on approved_at, matching how every other move in this system re-anchors rather
    than replaying what was missed. Anchored on the send instead, a proposal approved on day 20
    would be "overdue" by twenty days and fire its whole recurring series at once."""
    row = _row(followup_enrolled_at=ENROLLED - timedelta(days=30),
               cycle_viewed_at=ENROLLED - timedelta(days=29))
    assert _templates(_due(row, _at(2))) == [], (
        "the deposit stage is reading an older anchor than the approval")
    assert "deposit_nudge" in _templates(_due(row, _at(25)))


def test_it_is_never_chased_as_a_proposal_awaiting_a_decision():
    """THE structural one. An approved row has cycle_viewed_at set, so a fall-through would chase
    a signed job with "we need your signed approval and the deposit before we can book"."""
    for hours in (25, 80, 200, 600):
        got = _templates(_due(_row(), _at(hours)))
        for wrong in ("next_steps", "second_nudge", "checkin", "not_viewed"):
            assert wrong not in got, (
                "an approved proposal is being chased with %s at %sh" % (wrong, hours))


def test_the_customer_stops_the_moment_they_tell_us_it_is_on_the_way():
    """`submitted` means ACH details entered or "the cheque is posted". Chasing then reads as
    either a mistake on our side or a second charge."""
    got = _templates(_due(_row(deposit_status="submitted"), _at(25)))
    assert "deposit_nudge" not in got, "the customer is still being chased after telling us"


def test_the_estimator_does_NOT_stop_when_the_customer_does():
    """The half Hanz specifically asked for: a cheque that never arrives has to stay somebody's
    problem. This is the assertion that fails if the two stop points get "simplified" into one."""
    got = _templates(_due(_row(deposit_status="submitted"), _at(60)))
    assert "staff_deposit_outstanding" in got, (
        "nobody is reminded about a deposit the customer said was on its way")


def test_the_money_arriving_stops_both_of_them():
    assert _due(_row(deposit_status="received"), _at(600)) == []


def test_a_job_sent_without_a_deposit_is_finished_at_approval():
    """`deposit_required is False` — GC work usually is. Chasing money nobody asked for would be
    worse than silence."""
    assert _due(_row(deposit_required=False), _at(600)) == []


def test_a_deposit_INVOICED_despite_the_flag_is_still_chased():
    """`deposit_required=False` says the proposal went out without one. `deposit_requested_at` says
    somebody raised an invoice anyway, and money that has been asked for is money worth chasing.

    This is the rule crm-core.depositSatisfied already used to decide what the BOARD says, so the
    engine reading the flag alone would have shown a staff drawer "following up until the deposit
    is in" while the worker sent nothing."""
    assert "deposit_nudge" in _templates(_due(
        _row(deposit_required=False, deposit_requested_at=APPROVED), _at(25)))


def test_the_candidate_sql_agrees_about_the_invoiced_exception():
    """Same rule, third implementation. It is SQL and cannot call the predicate, so a divergence
    here means the row never reaches the engine that would have chased it."""
    import inspect

    import db
    sql = inspect.getsource(db.list_followup_candidates)
    assert "deposit_requested_at is not null" in sql, (
        "the query drops a no-deposit job that was invoiced anyway, so nothing chases it")


def test_a_legacy_row_with_no_deposit_flag_still_collects_one():
    """The column arrived after the rows did. Absent is not False: those jobs DID take a deposit,
    and reading a missing value as "none required" would silence the stage on all of them."""
    row = _row()
    row.pop("deposit_required", None)
    assert "deposit_nudge" in _templates(_due(row, _at(25)))


# ── the status ask, which this stage must not offer ──────────────────────────
def test_a_deposit_reminder_never_offers_the_not_moving_forward_button():
    """Every other recurring nudge offers "has your timeline changed?", and its second button sets
    closed_lost. On a job the customer has signed and been invoiced for, that is a one-click cancel
    of won work — the single most damaging thing this engine could put in an email."""
    for hours in (25, 100, 400, 1000):
        for d in _due(_row(), _at(hours)):
            if d.template == "deposit_nudge":
                assert d.include_status_ask is False, (
                    "the deposit reminder at %sh offers a one-click cancel of a won job" % hours)


def test_the_ask_is_still_offered_on_the_stages_it_belongs_to():
    """Guard against "fixing" the above by removing the escape hatch everywhere. A proposal nobody
    has opened for a week is exactly the one that needs a polite way out."""
    row = dict(proposal_id="p2", proposal_status="sent", followup_enrolled_at=ENROLLED)
    got = [d for d in _due(row, ENROLLED + timedelta(days=9)) if d.audience == "customer"]
    assert got and any(d.include_status_ask for d in got), (
        "the timeline-changed escape hatch is gone from the stages that should offer it")


# ── recurrence, dedupe and the caps ──────────────────────────────────────────
def test_it_repeats_rather_than_asking_once():
    """One email about money that never arrived is not a follow-up system."""
    keys = set()
    for day in range(1, 25):
        for d in _due(_row(), _at(24 * day)):
            if d.template == "deposit_nudge":
                keys.add(d.rule_key)
    assert len(keys) > 3, "the deposit reminder does not recur"


def test_the_same_tick_twice_reserves_the_same_key():
    """The worker dedupes on rule_key. A key with `now` in it would send on every tick."""
    a = [d.rule_key for d in _due(_row(), _at(100))]
    b = [d.rule_key for d in _due(_row(), _at(100) + timedelta(minutes=7))]
    assert a == b, "the rule key moves with the clock, so every tick would be a fresh send"


def test_the_recurring_series_respects_max_recurring():
    """Otherwise an unpaid job is chased for ever. The cap is the same knob the rest of the
    cadence uses, so setting it once bounds every stage."""
    cfg = fs.defaults()
    cfg["max_recurring"] = 2
    keys = set()
    for day in range(1, 60):
        for d in _due(_row(), _at(24 * day), cfg):
            if d.template == "deposit_nudge":
                keys.add(d.rule_key)
    assert len(keys) <= 4, "the deposit reminder ignores max_recurring: %s" % sorted(keys)


def test_a_revision_resets_the_stage():
    """Re-publishing moves followup_enrolled_at, which is what cycle_key reads. Every key here is
    cycle-scoped for that reason: the same reminder has to be sendable again against new numbers."""
    first = {d.rule_key for d in _due(_row(), _at(100))}
    again = {d.rule_key for d in _due(_row(followup_enrolled_at=ENROLLED + timedelta(days=40)),
                                     _at(100))}
    assert first and again and not (first & again), (
        "a revision re-send would be deduped against the previous cycle's reminders")


# ── the switches that outrank the stage ──────────────────────────────────────
def test_the_kill_switch_still_wins():
    assert _due(_row(followup_disabled_at=APPROVED), _at(600)) == []


def test_a_pause_the_customer_asked_for_still_wins():
    """They asked us to come back later. That it is now about money does not change the answer."""
    row = _row(followup_paused_until=(APPROVED + timedelta(days=40)).date())
    assert _due(row, _at(100)) == []


def test_a_closed_lost_job_is_not_chased_for_money():
    assert _due(_row(proposal_status="closed_lost"), _at(600)) == []


def test_the_send_window_holds_the_customer_and_not_the_estimator():
    """Same rule as every other stage: customers get business hours, staff mail does not wait.
    03:00 Central, which is outside the shipped 08:00–18:00 window."""
    at = datetime(2026, 8, 8, 8, 0, tzinfo=timezone.utc)   # 03:00 America/Chicago
    got = _due(_row(), at)
    assert "deposit_nudge" not in _templates(got), "a customer would be emailed at 3am"
    assert "staff_deposit_outstanding" in _templates(got)


# ── in_scope: one definition, three call sites ───────────────────────────────
def test_in_scope_is_what_the_worker_and_the_engine_both_ask():
    """The worker re-reads the row before acting and used to spell its own status test inline
    (`not in ("sent", "viewed")`). Widening the engine without it changed nothing observable:
    every approved row was dropped one line before the rules ran."""
    import followup_worker
    import inspect
    src = inspect.getsource(followup_worker._tick)
    assert "rules.in_scope(fresh)" in src, (
        "the worker judges scope on its own again, so it can disagree with the engine")
    assert '"sent", "viewed"' not in src, "the inline status test is back in the worker"


@pytest.mark.parametrize("row,expect", [
    (dict(proposal_status="sent"), True),
    (dict(proposal_status="viewed"), True),
    (dict(proposal_status="approved", deposit_status="pending"), True),
    (dict(proposal_status="approved", deposit_status="submitted"), True),
    (dict(proposal_status="approved", deposit_status="received"), False),
    (dict(proposal_status="approved", deposit_required=False), False),
    (dict(proposal_status="closed_lost"), False),
    (dict(proposal_status=""), False),
])
def test_in_scope_takes_the_staff_reading_of_the_deposit(row, expect):
    """The LATER of the two stops, deliberately: this only decides whether to look at a row at
    all. A `submitted` deposit is still in scope because the estimator is still owed a reminder,
    and `due_now` is what then declines to email the customer."""
    assert fr.in_scope(row) is expect


def test_the_candidate_sql_lets_the_approved_rows_through():
    """The third place that has to agree. It is SQL, so it cannot call in_scope — asserted against
    the query text, because a mismatch here means the stage never runs in production and every
    test above still passes."""
    import inspect

    import db
    sql = inspect.getsource(db.list_followup_candidates)
    assert "'approved'" in sql, "approved rows are still excluded before the rules ever see them"
    assert "coalesce(deposit_status, '') <> 'received'" in sql, (
        "the query does not stop at a received deposit, so paid jobs stay candidates for ever")
    assert "coalesce(deposit_required, true)" in sql, (
        "a legacy row with no deposit_required is excluded, or a no-deposit job is included")


# ── the board's next-follow-up column ────────────────────────────────────────
def test_next_due_at_mirrors_the_stage():
    """The board shows when the next email goes out. Saying "none" on a job that will be chased
    tomorrow is how staff learn to distrust the column."""
    when = fr.next_due_at(_row(), _at(2))
    assert when is not None, "the board would show no upcoming follow-up on an unpaid won job"
    assert when > _at(2)


def test_next_due_at_goes_quiet_once_the_customer_has_told_us():
    """It describes the CUSTOMER's schedule, which is what that column has always meant."""
    assert fr.next_due_at(_row(deposit_status="submitted"), _at(2)) is None
    assert fr.next_due_at(_row(deposit_status="received"), _at(2)) is None


# ── the wording it chases with ───────────────────────────────────────────────
def test_the_template_exists_and_is_editable():
    assert "deposit_nudge" in fs.TEMPLATE_KEYS
    t = fs.DEFAULT_TEMPLATES["deposit_nudge"]
    assert "{link}" in t["body"] and t["title"] and t["cta"]
    assert fs.LABELS["deposit_nudge"] == "Deposit reminder"
    assert "deposit" in fs.EDITOR_TITLES["deposit_nudge"].lower()


def test_the_shipped_wording_does_not_ask_again_for_the_approval_they_gave():
    """`{need}` renders as "your signed approval and the deposit". By the time this sends they
    have signed, and asking again reads as a mistake on our side."""
    assert "{need}" not in fs.DEFAULT_TEMPLATES["deposit_nudge"]["body"]


def test_the_reminder_links_to_the_deposit_step_not_the_top_of_the_proposal():
    """They have read and approved the proposal. Landing them back at the top of it makes them
    hunt for the one thing the email asked for."""
    import inspect

    import followup_worker
    src = inspect.getsource(followup_worker._send_customer)
    assert '"#proposal/deposit"' in src, "the deposit reminder opens the proposal, not the deposit"
    assert 'due.template == "deposit_nudge"' in src, (
        "the anchor is not gated on the deposit reminder, so every follow-up would deep-link")
