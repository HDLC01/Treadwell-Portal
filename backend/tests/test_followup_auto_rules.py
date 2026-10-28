"""The follow-up schedule (2026-10-28): a follow-up is an automatic email to the customer. Pure rules.

    NOT OPENED YET        1st email 3 days after sending       2nd email 7 days after the 1st
    OPENED, NO DECISION   1st email 3 days after they open it  2nd email 7 days after the 1st
    ESTIMATOR             "Tell the estimator" 1 day after the last email of a step

At most 2 per step, each with its own switch; nothing repeats. Stops on approved, lost, on hold
(a pause) and the project's Follow-ups switch off. The count is the SENT HISTORY of the current
send cycle, so a proposal the old ladder already emailed twice in its step gets nothing more.
"""
from datetime import date, datetime, timedelta, timezone

import pytest

import followup_rules as fr
import followup_settings as fs

NOW = datetime(2026, 10, 20, 17, 0, tzinfo=timezone.utc)        # noon Chicago
D = timedelta(days=1)
H = timedelta(hours=1)
ALL_DAY = {"send_start_hour": 0, "send_end_hour": 24}
SENT_AT = NOW - 20 * D


def _p(**over):
    p = {"proposal_id": "p1", "proposal_status": "sent", "followup_enrolled_at": SENT_AT,
         "followup_disabled_at": None, "followup_paused_until": None, "cycle_viewed_at": None,
         "followup_sent_history": []}
    p.update(over)
    return p


def _h(at, template):
    return {"at": at.isoformat(), "template": template, "rule_key": "x"}


def cfg(**over):
    c = dict(ALL_DAY)
    c.update(over)
    return c


def cust(p, at, c=None):
    return [d for d in fr.due_now(p, at, c or ALL_DAY) if d.audience == "customer"]


def staff(p, at, c=None):
    return [d for d in fr.due_now(p, at, c or ALL_DAY) if d.audience == "staff"]


def tag(d):
    return d.rule_key.split(":")[-1]


# ── step 1: not opened yet ────────────────────────────────────────────────────
def test_the_first_not_opened_email_is_due_three_days_after_sending():
    p = _p()
    assert cust(p, SENT_AT + 3 * D - H) == []
    d, = cust(p, SENT_AT + 3 * D)
    assert (tag(d), d.template, d.include_status_ask) == ("nv1", "not_viewed", False)
    assert d.rule_key == "%s:a:nv1" % fr.cycle_key(SENT_AT)
    assert fr.next_due_at(p, SENT_AT, ALL_DAY) == SENT_AT + 3 * D


def test_the_second_is_seven_days_after_the_first_was_REALLY_sent():
    """Not 7 days after it was due: a 1st that went late (a missed tick, the send window) pushes
    the 2nd with it, so the two can never land inside one interval."""
    first = SENT_AT + 5 * D                         # went two days late
    p = _p(followup_sent_history=[_h(first, "not_viewed")])
    assert cust(p, first + 7 * D - H) == []
    d, = cust(p, first + 7 * D)
    assert (tag(d), d.template, d.include_status_ask) == ("nv2", "not_viewed_2", True)
    assert fr.next_due_at(p, first, ALL_DAY) == first + 7 * D


def test_after_two_emails_the_step_is_done_and_only_the_estimator_is_told():
    first, second = SENT_AT + 3 * D, SENT_AT + 10 * D
    p = _p(followup_sent_history=[_h(first, "not_viewed"), _h(second, "not_viewed_2")])
    for at in (second + H, second + 30 * D, second + 365 * D):
        assert cust(p, at) == [], "a 3rd email in the step"
    assert fr.next_due_at(p, second + H, ALL_DAY) is None
    assert staff(p, second + D - H) == []
    s, = staff(p, second + D)
    assert (tag(s), s.template) == ("nv_est", fr.ESTIMATOR_TEMPLATE)
    # Its key never changes, so the dedupe index lets it go once.
    assert staff(p, second + 40 * D)[0].rule_key == s.rule_key


# ── step 2: opened, no decision ───────────────────────────────────────────────
def test_opening_it_starts_the_opened_step_with_its_own_two():
    viewed = SENT_AT + 12 * D
    hist = [_h(SENT_AT + 3 * D, "not_viewed"), _h(SENT_AT + 10 * D, "not_viewed_2")]
    p = _p(proposal_status="viewed", cycle_viewed_at=viewed, followup_sent_history=hist)
    assert cust(p, viewed + 3 * D - H) == []
    d, = cust(p, viewed + 3 * D)
    assert (tag(d), d.template, d.include_status_ask) == ("v1", "next_steps", False)
    hist.append(_h(viewed + 3 * D, "next_steps"))
    d, = cust(p, viewed + 10 * D)
    assert (tag(d), d.template, d.include_status_ask) == ("v2", "second_nudge", True)
    hist.append(_h(viewed + 10 * D, "second_nudge"))
    assert cust(p, viewed + 60 * D) == []
    s, = staff(p, viewed + 11 * D)
    assert tag(s) == "v_est"


def test_the_not_opened_estimator_note_is_not_sent_once_they_have_opened_it():
    hist = [_h(SENT_AT + 3 * D, "not_viewed"), _h(SENT_AT + 10 * D, "not_viewed_2")]
    p = _p(proposal_status="viewed", cycle_viewed_at=SENT_AT + 10 * D + H,
           followup_sent_history=hist)
    assert staff(p, SENT_AT + 12 * D) == []


# ── the switches and the days ─────────────────────────────────────────────────
def test_each_email_has_its_own_switch():
    p = _p()
    # 1st off: the 2nd goes where the 1st would have, plus the gap; then the step is done.
    off1 = cfg(not_opened_first_on=False)
    assert cust(p, SENT_AT + 9 * D, off1) == []
    d, = cust(p, SENT_AT + 10 * D, off1)
    assert d.template == "not_viewed_2"
    sent = _p(followup_sent_history=[_h(SENT_AT + 10 * D, "not_viewed_2")])
    assert cust(sent, SENT_AT + 60 * D, off1) == []
    # 2nd off: one email, then done.
    off2 = cfg(not_opened_second_on=False)
    one = _p(followup_sent_history=[_h(SENT_AT + 3 * D, "not_viewed")])
    assert cust(one, SENT_AT + 60 * D, off2) == []
    assert tag(staff(one, SENT_AT + 4 * D, off2)[0]) == "nv_est"
    # Both off: no customer email; the estimator hears N days after the step began.
    both = cfg(not_opened_first_on=False, not_opened_second_on=False)
    assert cust(p, SENT_AT + 60 * D, both) == []
    assert staff(p, SENT_AT + D - H, both) == []
    assert tag(staff(p, SENT_AT + D, both)[0]) == "nv_est"
    # Estimator off: never told.
    done = _p(followup_sent_history=[_h(SENT_AT + 3 * D, "not_viewed"),
                                     _h(SENT_AT + 10 * D, "not_viewed_2")])
    assert staff(done, SENT_AT + 60 * D, cfg(estimator_on=False)) == []


def test_the_days_are_the_saved_days():
    c = cfg(not_opened_first_days=5, not_opened_second_days=2, estimator_days=4,
            opened_first_days=1, opened_second_days=9)
    p = _p()
    assert cust(p, SENT_AT + 5 * D - H, c) == [] and cust(p, SENT_AT + 5 * D, c)
    one = _p(followup_sent_history=[_h(SENT_AT + 5 * D, "not_viewed")])
    assert fr.next_due_at(one, NOW, c) == SENT_AT + 7 * D
    v = _p(proposal_status="viewed", cycle_viewed_at=SENT_AT + D)
    assert fr.next_due_at(v, NOW, c) == SENT_AT + 2 * D


def test_an_old_shape_row_means_the_same_schedule_as_its_migration():
    """The live prod row, unmigrated, read straight by the rules (a caller that passed the raw
    row): 72h -> 3 days, 168h -> 7 days, 24h -> 1 day, every switch on."""
    prod = {"first_nudge_hours": 72, "second_nudge_hours": 168, "recurring_hours": 2160,
            "max_recurring": 1, "staff_personal_hours": 24, **ALL_DAY}
    p = _p()
    assert fr.next_due_at(p, NOW, prod) == SENT_AT + 3 * D
    one = _p(followup_sent_history=[_h(SENT_AT + 3 * D, "not_viewed")])
    assert fr.next_due_at(one, NOW, prod) == SENT_AT + 10 * D


# ── the count: sent history, current cycle, per step ──────────────────────────
@pytest.mark.parametrize("templates", [
    ("next_steps", "second_nudge"),                  # the old ladder's v1 and v2
    ("checkin", "checkin", "checkin"),               # old recurring check-ins
    ("next_steps", "", "checkin"),                   # an unknown template counts as this step
    ("", ""),
])
def test_a_proposal_that_already_had_two_reminders_in_its_step_gets_no_more(templates):
    """PROD SAFETY at release: whatever engine sent them, 2 in the current step means done."""
    viewed = SENT_AT + D
    hist = [_h(viewed + (i + 1) * D, t) for i, t in enumerate(templates)]
    p = _p(proposal_status="viewed", cycle_viewed_at=viewed, followup_sent_history=hist)
    for at in (NOW, NOW + 30 * D):
        assert cust(p, at) == []
    assert fr.next_due_at(p, NOW, ALL_DAY) is None


def test_one_old_reminder_leaves_exactly_the_second():
    viewed = SENT_AT + D
    p = _p(proposal_status="viewed", cycle_viewed_at=viewed,
           followup_sent_history=[_h(viewed + D, "next_steps")])
    d, = cust(p, NOW)
    assert d.template == "second_nudge"


def test_the_other_steps_emails_do_not_count_against_this_one():
    viewed = SENT_AT + 15 * D
    hist = [_h(SENT_AT + 3 * D, "not_viewed"), _h(SENT_AT + 10 * D, "not_viewed")]
    p = _p(proposal_status="viewed", cycle_viewed_at=viewed, followup_sent_history=hist)
    assert fr.plan(p, NOW, ALL_DAY).sent == 0
    assert [d.template for d in cust(p, viewed + 3 * D)] == ["next_steps"]


def test_a_deposit_reminder_is_not_a_follow_up_and_is_not_counted():
    viewed = SENT_AT + D
    hist = [_h(viewed + D, "deposit_nudge"), _h(viewed + 2 * D, "deposit_nudge")]
    p = _p(proposal_status="viewed", cycle_viewed_at=viewed, followup_sent_history=hist)
    assert [d.template for d in cust(p, NOW)] == ["next_steps"]


def test_a_revision_starts_a_new_cycle_with_a_fresh_two_and_new_keys():
    hist = [_h(SENT_AT + 3 * D, "not_viewed"), _h(SENT_AT + 10 * D, "not_viewed_2")]
    resent = SENT_AT + 15 * D
    p = _p(followup_enrolled_at=resent, followup_sent_history=hist)
    d, = cust(p, resent + 3 * D)
    assert d.template == "not_viewed" and d.rule_key.startswith(fr.cycle_key(resent) + ":a:")


def test_two_ticks_inside_one_occurrence_ask_for_the_same_key():
    p = _p()
    a = cust(p, SENT_AT + 3 * D)[0].rule_key
    b = cust(p, SENT_AT + 3 * D + 15 * 60 * timedelta(seconds=1))[0].rule_key
    c = cust(p, SENT_AT + 9 * D)[0].rule_key
    assert a == b == c


def test_the_engines_keys_never_collide_with_an_older_engines():
    """The old ladder and the remind-only tasks used `c<ts>:nv1`, `c<ts>:v1`, `c<ts>:v2`. A reserved
    old key would silently block the new email for ever; the history count covers what they sent."""
    keys = {d.rule_key for at in (SENT_AT + 3 * D,) for d in cust(_p(), at)}
    keys |= {d.rule_key for d in cust(_p(proposal_status="viewed", cycle_viewed_at=SENT_AT),
                                      SENT_AT + 3 * D)}
    ck = fr.cycle_key(SENT_AT)
    assert not keys & {ck + ":nv1", ck + ":v1", ck + ":v2"}


# ── what stops it ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("over,reason", [
    ({"proposal_status": "approved", "approved_at": NOW - D}, "approved"),
    ({"proposal_status": "closed_lost"}, "lost"),
    ({"followup_disabled_at": NOW - D}, "followups_off"),
    ({"deleted_at": NOW - D}, "deleted"),
    ({"followup_enrolled_at": None}, "not_enrolled"),
    ({"proposal_status": "draft"}, "finished"),
])
def test_approved_lost_off_deleted_and_unsent_get_nothing(over, reason):
    p = _p(**over)
    assert fr.due_now(p, NOW, ALL_DAY) == []
    assert fr.next_due_at(p, NOW, ALL_DAY) is None
    assert fr.plan(p, NOW, ALL_DAY).reason == reason


def test_approval_stops_the_estimator_note_too():
    hist = [_h(SENT_AT + 3 * D, "next_steps"), _h(SENT_AT + 10 * D, "second_nudge")]
    p = _p(proposal_status="approved", cycle_viewed_at=SENT_AT + D, approved_at=NOW - D,
           followup_sent_history=hist, deposit_status="pending")
    assert fr.due_now(p, NOW, ALL_DAY) == []


def test_on_hold_sends_nothing_including_its_final_day_and_resumes_the_morning_after():
    until = (NOW + 10 * D).astimezone(fr.BUSINESS_TZ).date()
    p = _p(followup_paused_until=until)
    assert fr.due_now(p, NOW, ALL_DAY) == []
    last_day = datetime(until.year, until.month, until.day, 16, tzinfo=fr.BUSINESS_TZ)
    assert fr.due_now(p, last_day, ALL_DAY) == []
    resume = datetime(until.year, until.month, until.day, fr.SEND_START_HOUR,
                      tzinfo=fr.BUSINESS_TZ) + D
    assert fr.next_due_at(p, NOW) == resume          # the shipped window opens at 8am
    assert [d.template for d in fr.due_now(p, resume)] == ["not_viewed"]


def test_the_estimator_note_waits_out_a_hold_too():
    until = (NOW + 5 * D).astimezone(fr.BUSINESS_TZ).date()
    hist = [_h(SENT_AT + 3 * D, "not_viewed"), _h(SENT_AT + 10 * D, "not_viewed_2")]
    p = _p(followup_paused_until=until, followup_sent_history=hist)
    assert fr.due_now(p, NOW, ALL_DAY) == []


def test_in_scope_is_sent_or_viewed_only():
    assert fr.in_scope({"proposal_status": "sent"}) and fr.in_scope({"proposal_status": "viewed"})
    for s in ("approved", "closed_lost", "draft", "", None):
        assert not fr.in_scope({"proposal_status": s})


# ── "Next email on" ───────────────────────────────────────────────────────────
def test_next_email_on_moves_the_next_email_later_or_earlier():
    later = SENT_AT + 6 * D
    p = _p(followup_next_at=later)
    assert cust(p, SENT_AT + 3 * D) == []
    assert [d.template for d in cust(p, later)] == ["not_viewed"]
    sooner = SENT_AT + D
    assert fr.next_due_at(_p(followup_next_at=sooner), SENT_AT, ALL_DAY) == sooner


def test_next_email_on_is_spent_once_that_email_has_gone():
    when = SENT_AT + 6 * D
    p = _p(followup_next_at=when, followup_sent_history=[_h(when, "not_viewed")])
    assert fr.next_due_at(p, NOW, ALL_DAY) == when + 7 * D


def test_next_email_on_never_adds_an_email_past_the_two():
    hist = [_h(SENT_AT + 3 * D, "not_viewed"), _h(SENT_AT + 10 * D, "not_viewed_2")]
    p = _p(followup_next_at=NOW + D, followup_sent_history=hist)
    assert cust(p, NOW + 2 * D) == [] and fr.next_due_at(p, NOW, ALL_DAY) is None


def test_a_next_email_date_from_before_this_send_is_ignored():
    p = _p(followup_next_at=SENT_AT - D)
    assert fr.next_due_at(p, SENT_AT, ALL_DAY) == SENT_AT + 3 * D


# ── the send window ───────────────────────────────────────────────────────────
def test_customer_email_waits_for_the_central_send_window_and_the_estimator_does_not():
    hist = [_h(SENT_AT + 3 * D, "not_viewed")]
    p = _p(followup_sent_history=hist)
    three_am = datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc)       # 3am Chicago
    assert [d for d in fr.due_now(p, three_am) if d.audience == "customer"] == []
    assert [d.template for d in fr.due_now(p, NOW)] == ["not_viewed_2"]
    done = _p(followup_sent_history=hist + [_h(SENT_AT + 10 * D, "not_viewed_2")])
    assert [d.audience for d in fr.due_now(done, three_am)] == ["staff"]


def test_the_window_is_evaluated_in_chicago_not_utc():
    utc_9pm = datetime(2026, 10, 20, 21, 0, tzinfo=timezone.utc)       # 4pm Chicago
    assert fr.in_send_window(utc_9pm) is True
    utc_1am = datetime(2026, 10, 21, 1, 0, tzinfo=timezone.utc)        # 8pm Chicago
    assert fr.in_send_window(utc_1am) is False


# ── the board's date and the worker's send are one computation ─────────────────
@pytest.mark.parametrize("p", [
    _p(),
    _p(followup_sent_history=[_h(SENT_AT + 4 * D, "not_viewed")]),
    _p(proposal_status="viewed", cycle_viewed_at=SENT_AT + 2 * D),
    _p(proposal_status="viewed", cycle_viewed_at=SENT_AT + 2 * D,
       followup_sent_history=[_h(SENT_AT + 6 * D, "next_steps")]),
    _p(followup_next_at=SENT_AT + 8 * D),
])
def test_next_due_at_is_exactly_when_due_now_first_says_send(p):
    at = fr.next_due_at(p, SENT_AT, ALL_DAY)
    assert at is not None
    assert cust(p, at - timedelta(minutes=1)) == []
    assert cust(p, at)


# ── small helpers ─────────────────────────────────────────────────────────────
def test_pause_months_clamp_to_the_end_of_short_months():
    assert fr.add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert fr.add_months(date(2026, 11, 30), 3) == date(2027, 2, 28)
    assert fr.add_months(date(2026, 12, 15), 1) == date(2027, 1, 15)


def test_the_mode_is_auto():
    assert fr.followup_mode({}) == "auto"


def test_the_cadence_templates_are_the_four_editable_emails():
    assert [t for step in ("not_opened", "opened") for t in fs.CADENCE_TEMPLATES[step]] == \
        list(fs.TEMPLATE_KEYS)
