"""The follow-up schedule (2026-10-28): a follow-up is an automatic email to the customer. Pure rules.

    NOT OPENED YET        1st email 3 days after sending       2nd email 7 days after the 1st
    OPENED, NO DECISION   1st email 3 days after sending (*)   2nd email 7 days after the 1st
    ESTIMATOR             "Tell the estimator" 1 day after the last email of a step

At most 2 per step, each with its own switch; nothing repeats. Stops on approved, lost, on hold
(a pause) and the project's Follow-ups switch off. The count is the SENT HISTORY of the current
send cycle, so a proposal the old ladder already emailed twice in its step gets nothing more.

(*) 2026-10-29: and never sooner than 3 days after the customer's last follow-up email, nor than
    1 day after they opened it.
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
    """Opened on day 12, after both not-opened emails (days 3 and 10): the opened step still gets
    its own two. The 1st is 3 days after the last customer email (day 13), never the next tick."""
    viewed = SENT_AT + 12 * D
    nv2 = SENT_AT + 10 * D
    hist = [_h(SENT_AT + 3 * D, "not_viewed"), _h(nv2, "not_viewed_2")]
    p = _p(proposal_status="viewed", cycle_viewed_at=viewed, followup_sent_history=hist)
    assert cust(p, viewed) == [] and cust(p, nv2 + 3 * D - H) == []
    d, = cust(p, nv2 + 3 * D)
    assert (tag(d), d.template, d.include_status_ask) == ("v1", "next_steps", False)
    v1 = nv2 + 3 * D
    hist.append(_h(v1, "next_steps"))
    assert cust(p, v1 + 7 * D - H) == []
    d, = cust(p, v1 + 7 * D)
    assert (tag(d), d.template, d.include_status_ask) == ("v2", "second_nudge", True)
    hist.append(_h(v1 + 7 * D, "second_nudge"))
    assert cust(p, viewed + 60 * D) == []
    s, = staff(p, v1 + 8 * D)
    assert tag(s) == "v_est"


# ── the opened step counts from the SENT date (Hanz, 2026-10-29) ──────────────────
# "the days should be based off the date it was sent": the opened step's 1st email is 3 days after
# the proposal was sent, not after the customer opened it. Spacing: it is never sooner than 3 days
# after the customer's last follow-up email of this send.
def test_opened_on_day_one_gets_the_opened_1st_three_days_after_SENDING():
    p = _p(proposal_status="viewed", cycle_viewed_at=SENT_AT + D)
    assert fr.next_due_at(p, SENT_AT, ALL_DAY) == SENT_AT + 3 * D
    assert cust(p, SENT_AT + 3 * D - H) == []
    d, = cust(p, SENT_AT + 3 * D)
    assert (tag(d), d.template) == ("v1", "next_steps")
    # The 2nd is 7 days after the 1st really went.
    p["followup_sent_history"] = [_h(SENT_AT + 3 * D, "next_steps")]
    assert fr.next_due_at(p, SENT_AT, ALL_DAY) == SENT_AT + 10 * D
    assert cust(p, SENT_AT + 10 * D - H) == []
    d, = cust(p, SENT_AT + 10 * D)
    assert (tag(d), d.template) == ("v2", "second_nudge")


def test_opened_on_day_five_after_the_not_opened_1st_waits_three_days_from_that_email():
    """The not-opened 1st went on day 3; they open on day 5. Sent + 3 (day 3) has passed, but the
    customer had an email on day 3, so the opened 1st waits for day 6."""
    p = _p(proposal_status="viewed", cycle_viewed_at=SENT_AT + 5 * D,
           followup_sent_history=[_h(SENT_AT + 3 * D, "not_viewed")])
    assert fr.next_due_at(p, SENT_AT, ALL_DAY) == SENT_AT + 6 * D
    assert cust(p, SENT_AT + 5 * D) == [] and cust(p, SENT_AT + 6 * D - H) == []
    d, = cust(p, SENT_AT + 6 * D)
    assert tag(d) == "v1"


def test_opened_on_day_eleven_after_both_not_opened_emails_waits_three_days_from_the_last():
    nv2 = SENT_AT + 10 * D
    p = _p(proposal_status="viewed", cycle_viewed_at=SENT_AT + 11 * D,
           followup_sent_history=[_h(SENT_AT + 3 * D, "not_viewed"), _h(nv2, "not_viewed_2")])
    assert fr.next_due_at(p, SENT_AT, ALL_DAY) == nv2 + 3 * D
    assert cust(p, SENT_AT + 11 * D) == [], "emailed the tick after they opened it"
    assert cust(p, nv2 + 3 * D - H) == []
    d, = cust(p, nv2 + 3 * D)
    assert tag(d) == "v1"
    # ...and its 2nd is 7 days after that 1st really went.
    p["followup_sent_history"].append(_h(nv2 + 4 * D, "next_steps"))
    assert fr.next_due_at(p, SENT_AT, ALL_DAY) == nv2 + 11 * D


def test_the_spacing_uses_the_opened_steps_own_1st_days():
    c = cfg(opened_first_days=5)
    p = _p(proposal_status="viewed", cycle_viewed_at=SENT_AT + 4 * D,
           followup_sent_history=[_h(SENT_AT + 3 * D, "not_viewed")])
    assert fr.next_due_at(p, SENT_AT, c) == SENT_AT + 8 * D       # day 3 + 5, not sent + 5


# ── the opened 1st waits 1 day after the open (Hanz, 2026-10-29) ──────────────────
# "Wait 1 day after they open": the opened step's 1st is due at the LATEST of sent + 3, the last
# customer email + 3, and the open + 1 day, so a late opener is never thanked minutes after opening.
def _opened_1st_due_exactly_at(p, want):
    assert fr.next_due_at(p, SENT_AT, ALL_DAY) == want
    assert fr.plan(p, SENT_AT, ALL_DAY).next_at == want            # the board's date
    assert cust(p, p["cycle_viewed_at"]) == [], "emailed the tick they opened it"
    assert cust(p, want - timedelta(minutes=1)) == []
    d, = cust(p, want)
    assert (tag(d), d.template) == ("v1", "next_steps")


def test_open_on_day_seven_after_the_not_opened_1st_waits_a_day_from_the_open():
    opened = SENT_AT + 7 * D                     # sent + 3 (day 3) and day 3 + 3 (day 6) passed
    p = _p(proposal_status="viewed", cycle_viewed_at=opened,
           followup_sent_history=[_h(SENT_AT + 3 * D, "not_viewed")])
    _opened_1st_due_exactly_at(p, opened + D)


def test_open_on_day_two_and_a_half_before_any_email_is_the_later_of_sent_plus_3_and_open_plus_1():
    opened = SENT_AT + D * 5 / 2
    p = _p(proposal_status="viewed", cycle_viewed_at=opened)
    _opened_1st_due_exactly_at(p, SENT_AT + D * 7 / 2)              # open + 1 day > sent + 3


def test_open_on_day_fourteen_after_both_not_opened_emails_waits_a_day_from_the_open():
    opened = SENT_AT + 14 * D                    # emails on day 3 and day 10; day 13 has passed
    p = _p(proposal_status="viewed", cycle_viewed_at=opened,
           followup_sent_history=[_h(SENT_AT + 3 * D, "not_viewed"),
                                  _h(SENT_AT + 10 * D, "not_viewed_2")])
    _opened_1st_due_exactly_at(p, opened + D)


def test_the_open_wait_does_not_move_the_not_opened_step_or_the_opened_2nd():
    # Not opened: sent + 3, whatever stale view stamps an older send left.
    assert fr.next_due_at(_p(viewed_at=SENT_AT + 2 * D), SENT_AT, ALL_DAY) == SENT_AT + 3 * D
    # The opened 2nd: 7 days after the 1st really went, not open + 1.
    p = _p(proposal_status="viewed", cycle_viewed_at=SENT_AT + 14 * D,
           followup_sent_history=[_h(SENT_AT + 15 * D, "next_steps")])
    assert fr.next_due_at(p, SENT_AT, ALL_DAY) == SENT_AT + 22 * D


def test_ticking_through_every_open_time_no_opened_email_goes_within_a_day_of_the_open():
    """The worker's tick, simulated: every 30 minutes, with the real send window (8-18 Central)
    and the defaults, the not-opened emails going as they fall due and the customer opening it at
    a time every 5 hours across three weeks. The opened 1st never goes less than 24 h after the
    open, never before sent + 3, and never within 3 days of the email before it."""
    c = fs.defaults()
    tick = timedelta(minutes=30)
    for k in range(0, 21 * 24, 5):
        opened = SENT_AT + k * H
        p = _p()
        hist = p["followup_sent_history"]
        at = SENT_AT
        first_opened = None
        while at < SENT_AT + 40 * D and first_opened is None:
            if at >= opened and p["proposal_status"] == "sent":
                p.update(proposal_status="viewed", cycle_viewed_at=opened)
            for d in fr.due_now(p, at, c):
                if d.audience != "customer":
                    continue
                if d.template == "next_steps":
                    first_opened = at
                prev = fr.sent_history(p)[-1][0] if hist else None
                assert prev is None or at - prev >= 3 * D, (k, at, prev)
                hist.append(_h(at, d.template))
            at += tick
        assert first_opened is not None, k
        assert first_opened - opened >= D, ("opened at hour %d, emailed %s later"
                                            % (k, first_opened - opened))
        assert first_opened >= SENT_AT + 3 * D, k


# ── the three live proposals at activation (prod, 2026-10-29) ─────────────────────
ACT = datetime(2026, 10, 27, 17, 45, tzinfo=timezone.utc)          # 12:45 Central
CT = fr.BUSINESS_TZ


def _live(sent_ct, opened_ct=None):
    sent = sent_ct.replace(tzinfo=CT)
    over = dict(followup_enrolled_at=sent.astimezone(timezone.utc))
    if opened_ct is not None:
        over.update(proposal_status="viewed",
                    cycle_viewed_at=opened_ct.replace(tzinfo=CT).astimezone(timezone.utc))
    return _p(**over)


LIVE = {
    "Bass Pro Shop": (_live(datetime(2026, 10, 27, 12, 45)), datetime(2026, 10, 30, 12, 45)),
    "Discover Academy": (_live(datetime(2026, 10, 27, 12, 59), datetime(2026, 10, 27, 13, 41)),
                         datetime(2026, 10, 30, 12, 59)),
    "Bennett Packaging": (_live(datetime(2026, 10, 28, 12, 7)), datetime(2026, 10, 31, 12, 7)),
}


@pytest.mark.parametrize("name", list(LIVE))
def test_the_live_proposals_are_due_exactly_when_hanz_was_told(name):
    p, want_ct = LIVE[name]
    c = fs.with_activation(fs.defaults(), ACT)
    assert fr.activated_at(c) == ACT
    want = want_ct.replace(tzinfo=CT)
    pl = fr.plan(p, datetime(2026, 10, 29, 17, 0, tzinfo=timezone.utc), c)
    assert pl.next_at == want, (name, pl.next_at.astimezone(CT) if pl.next_at else None)
    assert fr.next_due_at(p, want, c) == want
    # Nothing is due before 10-30 (and nothing a minute before its own time).
    for at in (ACT, datetime(2026, 10, 29, 23, 0, tzinfo=CT), want - timedelta(minutes=1)):
        assert fr.due_now(p, at.astimezone(timezone.utc), c) == [], (name, at)
    d, = fr.due_now(p, want.astimezone(timezone.utc), c)
    assert d.audience == "customer"
    assert d.template == ("next_steps" if name == "Discover Academy" else "not_viewed")


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
            opened_first_days=2, opened_second_days=9)
    p = _p()
    assert cust(p, SENT_AT + 5 * D - H, c) == [] and cust(p, SENT_AT + 5 * D, c)
    one = _p(followup_sent_history=[_h(SENT_AT + 5 * D, "not_viewed")])
    assert fr.next_due_at(one, NOW, c) == SENT_AT + 7 * D
    v = _p(proposal_status="viewed", cycle_viewed_at=SENT_AT + D / 2)
    assert fr.next_due_at(v, NOW, c) == SENT_AT + 2 * D      # opened_first_days after SENDING


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


def test_next_email_on_never_beats_the_1_day_wait_after_the_open():
    """A "Next email on" date set while it was unopened, and an open 5 minutes before that date:
    the date now moves the OPENED 1st, and it still waits a day after the open."""
    when = SENT_AT + 6 * D
    p = _p(proposal_status="viewed", cycle_viewed_at=when - timedelta(minutes=5),
           followup_next_at=when)
    assert cust(p, when) == [], "thanked them 5 minutes after they opened it"
    assert fr.next_due_at(p, when, ALL_DAY) == when - timedelta(minutes=5) + D
    # A date later than the wait is still the date.
    later = SENT_AT + 9 * D
    p["followup_next_at"] = later
    assert fr.next_due_at(p, when, ALL_DAY) == later
    # Not opened: the date still moves it earlier than the 3 days.
    assert fr.next_due_at(_p(followup_next_at=SENT_AT + D), SENT_AT, ALL_DAY) == SENT_AT + D


def test_ticking_a_next_email_date_set_before_the_open_never_thanks_them_within_a_day():
    """The review's case through the real due_now every 15 minutes, with the defaults, the
    send window and the prod activation: sent at activation (10-27 12:45 Central), "Next email
    on" 11-01 11:45 set while unopened, opened 11-01 11:40. The thanks goes a day after the open,
    and its 2nd 7 days after that."""
    c = fs.with_activation(fs.defaults(), ACT)
    opened = datetime(2026, 11, 1, 11, 40, tzinfo=CT).astimezone(timezone.utc)
    p = _p(followup_enrolled_at=ACT,
           followup_next_at=datetime(2026, 11, 1, 11, 45, tzinfo=CT).astimezone(timezone.utc))
    at, sent = ACT, []
    while at < ACT + 20 * D:
        if at >= opened and p["proposal_status"] == "sent":
            p.update(proposal_status="viewed", cycle_viewed_at=opened)
        for d in fr.due_now(p, at, c):
            if d.audience == "customer":
                sent.append((at, d.template))
                p["followup_sent_history"].append(_h(at, d.template))
        at += timedelta(minutes=15)
    first = [at for at, t in sent if t == "next_steps"]
    assert first and first[0] - opened >= D, sent
    assert [t for _, t in sent] == ["next_steps", "second_nudge"], sent


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
    # The opened 1st held back by the spacing guard (opened after the not-opened emails).
    _p(proposal_status="viewed", cycle_viewed_at=SENT_AT + 5 * D,
       followup_sent_history=[_h(SENT_AT + 3 * D, "not_viewed")]),
    _p(proposal_status="viewed", cycle_viewed_at=SENT_AT + 11 * D,
       followup_sent_history=[_h(SENT_AT + 3 * D, "not_viewed"),
                              _h(SENT_AT + 10 * D, "not_viewed_2")]),
    # The opened 1st held back by the 1-day wait after a late open.
    _p(proposal_status="viewed", cycle_viewed_at=SENT_AT + 7 * D,
       followup_sent_history=[_h(SENT_AT + 3 * D, "not_viewed")]),
    _p(proposal_status="viewed", cycle_viewed_at=SENT_AT + D * 5 / 2),
    _p(proposal_status="viewed", cycle_viewed_at=SENT_AT + 14 * D,
       followup_sent_history=[_h(SENT_AT + 3 * D, "not_viewed"),
                              _h(SENT_AT + 10 * D, "not_viewed_2")]),
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


# ── review 2026-10-29: rows as the OLDER engines and schema left them ─────────────
# A proposal opened before `cycle_viewed_at` existed is 'viewed' with no cycle stamp (schema.sql
# adds the column with no backfill). It IS opened: the customer must never be told it is not.
MIGRATED = fs.merge({"first_nudge_hours": 72, "second_nudge_hours": 168,
                     "recurring_hours": 2160, "max_recurring": 1,
                     "staff_personal_hours": 24})
OLD_SENT = NOW - 60 * D


def _mig(**over):
    c = dict(MIGRATED)
    c.update(ALL_DAY)
    c.update(over)
    return c


def test_a_viewed_row_with_no_cycle_stamp_is_opened_not_unopened():
    p = _p(proposal_status="viewed", followup_enrolled_at=OLD_SENT, cycle_viewed_at=None)
    d, = cust(p, NOW, _mig())
    assert (tag(d), d.template) == ("v1", "next_steps")
    assert fr.plan(p, NOW, _mig()).step == fr.OPENED
    # Its 2nd is the opened step's 2nd, never "we haven't seen it opened yet".
    p["followup_sent_history"] = [_h(NOW, "next_steps")]
    d, = cust(p, NOW + 7 * D, _mig())
    assert (tag(d), d.template) == ("v2", "second_nudge")
    for at in (NOW + 7 * D, NOW + 30 * D):
        assert all(x.template not in ("not_viewed", "not_viewed_2")
                   for x in fr.due_now(p, at, _mig()))


def test_a_viewed_row_with_no_cycle_stamp_is_dated_by_the_views_of_this_send():
    # The customer emails count from the send (2026-10-29); the view date moves them only through
    # the 1-day wait after the open (fr.OPEN_WAIT). It also dates the opened step itself, which is
    # when its estimator note runs from while both of its emails are switched off.
    no_emails = _mig(opened_first_on=False, opened_second_on=False)
    # viewed_at (first view EVER) after the send: the step starts then.
    seen = OLD_SENT + 50 * D
    p = _p(proposal_status="viewed", followup_enrolled_at=OLD_SENT, viewed_at=seen,
           last_viewed_at=seen + 2 * D)
    assert fr.opened_at(p) == seen
    assert fr.next_due_at(p, NOW, _mig()) == seen + D
    assert fr.plan(p, NOW, no_emails).estimator_at == seen + D
    # viewed_at from BEFORE this send (a revision): the latest view, if it is of this send.
    p = _p(proposal_status="viewed", followup_enrolled_at=OLD_SENT, viewed_at=OLD_SENT - 9 * D,
           last_viewed_at=seen)
    assert fr.opened_at(p) == seen
    assert fr.next_due_at(p, NOW, _mig()) == seen + D
    assert fr.plan(p, NOW, no_emails).estimator_at == seen + D
    # No view of this send can be seen at all: the send itself.
    p = _p(proposal_status="viewed", followup_enrolled_at=OLD_SENT, viewed_at=OLD_SENT - 9 * D,
           last_viewed_at=OLD_SENT - 8 * D)
    assert fr.opened_at(p) == OLD_SENT
    assert fr.next_due_at(p, NOW, _mig()) == OLD_SENT + 3 * D
    assert fr.plan(p, NOW, no_emails).estimator_at == OLD_SENT + D
    # A 'sent' row is not opened, whatever an older send's stamps say.
    p = _p(proposal_status="sent", followup_enrolled_at=OLD_SENT, viewed_at=OLD_SENT - 9 * D)
    assert fr.plan(p, NOW, _mig()).step == fr.NOT_OPENED


def test_the_schedule_reads_the_view_stamps_the_board_and_worker_carry():
    assert {"viewed_at", "last_viewed_at", "cycle_viewed_at"} <= set(fr.INPUT_KEYS)


def _old(at, tag_, template, audience=None):
    row = {"at": at.isoformat(), "template": template, "rule_key": "%s:%s" % (
        fr.cycle_key(OLD_SENT), tag_)}
    if audience:
        row["audience"] = audience
    return row


def test_an_old_estimator_note_means_release_day_sends_no_second_one():
    """A viewed proposal the old ladder chased 5 times and reported to the estimator: the first
    tick owes the customer nothing (the step is used up) and the estimator nothing either."""
    hist = [_old(OLD_SENT + (5 + i) * D, "vr@%d" % i, "checkin", "customer") for i in range(5)]
    hist.append(_old(OLD_SENT + 4 * D, "v48_staff", None, "staff"))
    p = _p(proposal_status="viewed", followup_enrolled_at=OLD_SENT,
           cycle_viewed_at=OLD_SENT + D, followup_sent_history=hist)
    assert fr.due_now(p, NOW, _mig()) == []
    assert fr.plan(p, NOW, _mig()).estimator_at is None
    # Without that old note the step's estimator note is still owed (once).
    p["followup_sent_history"] = hist[:-1]
    assert [tag(s) for s in staff(p, NOW, _mig())] == ["v_est"]


def test_the_old_not_opened_note_covers_only_the_not_opened_step():
    hist = [_old(OLD_SENT + 3 * D, "nv1", "not_viewed", "customer"),
            _old(OLD_SENT + 10 * D, "nvr@1", "not_viewed", "customer"),
            _old(OLD_SENT + 3 * D, "nv1_staff", None, "staff")]
    p = _p(followup_enrolled_at=OLD_SENT, followup_sent_history=hist)
    assert fr.due_now(p, NOW, _mig()) == []
    # Opened since, and the opened step used up by the old ladder too: no old v48 note -> told.
    p.update(proposal_status="viewed", cycle_viewed_at=OLD_SENT + 11 * D)
    p["followup_sent_history"] = hist + [
        _old(OLD_SENT + 14 * D, "v1", "next_steps", "customer"),
        _old(OLD_SENT + 21 * D, "v2", "second_nudge", "customer")]
    assert [tag(s) for s in staff(p, NOW, _mig())] == ["v_est"]


def test_an_old_estimator_note_is_never_counted_as_a_customer_email():
    """One old 1st email plus the old ladder's estimator note: the 2nd is still owed."""
    hist = [_old(OLD_SENT + 3 * D, "nv1", "not_viewed", "customer"),
            _old(OLD_SENT + 3 * D, "nv1_staff", None, "staff")]
    p = _p(followup_enrolled_at=OLD_SENT, followup_sent_history=hist)
    assert fr.sent_history(p) == [(OLD_SENT + 3 * D, "not_viewed")]
    d, = cust(p, NOW, _mig())
    assert (tag(d), d.template) == ("nv2", "not_viewed_2")


def test_once_this_engine_emails_in_the_step_the_estimator_note_after_it_goes():
    """The old note reported the old emails; the 2nd sent by this engine is news."""
    hist = [_old(OLD_SENT + 3 * D, "nv1", "not_viewed", "customer"),
            _old(OLD_SENT + 3 * D, "nv1_staff", None, "staff"),
            _old(NOW - 2 * D, "a:nv2", "not_viewed_2", "customer")]
    p = _p(followup_enrolled_at=OLD_SENT, followup_sent_history=hist)
    assert [tag(s) for s in staff(p, NOW, _mig())] == ["nv_est"]


# ── decision B (Hanz, 2026-10-28): a reply skips the step's 2nd email ──────────────
# "When the customer replies, or someone logs a call, that step's remaining follow-up email is
# SKIPPED (they are talking to us already)." Contact after a step's 1st email and before its 2nd
# cancels the 2nd; no re-anchor, no extra email. Contact before the 1st skips nothing.
FIRST = SENT_AT + 3 * D                              # the not-opened 1st email went


def _after_first(**over):
    return _p(followup_sent_history=[_h(FIRST, "not_viewed")], **over)


@pytest.mark.parametrize("key,name", [
    ("customer_touch_at", "customer_reply"),         # they replied (not an auto-reply: SQL)
    ("last_staff_contact_at", "staff_contact"),      # an estimator logged a call / email / text
    ("last_staff_message_at", "staff_message"),      # an estimator wrote to them in the thread
])
def test_contact_between_the_first_and_second_email_skips_the_second(key, name):
    contact = FIRST + D
    p = _after_first(**{key: contact})
    for at in (FIRST + 7 * D, FIRST + 8 * D, FIRST + 60 * D):
        assert cust(p, at) == [], "the 2nd went to a customer we are already talking to"
    assert fr.next_due_at(p, FIRST + 2 * D, ALL_DAY) is None
    pl = fr.plan(p, FIRST + 7 * D, ALL_DAY)
    assert (pl.skipped_by, pl.skipped_at, pl.skipped_template) == (name, contact, "not_viewed_2")
    assert pl.skipped_key == "%s:a:nv2" % fr.cycle_key(SENT_AT)
    assert pl.skipped_due_at == FIRST + 7 * D and pl.reason == "skipped_by_reply"
    assert pl.sent == 1, "a skipped email is not a sent one"


def test_without_contact_the_second_still_goes():
    """The counterexample: the same proposal with no contact gets its 2nd (so the test above is
    not vacuously green)."""
    d, = cust(_after_first(), FIRST + 7 * D)
    assert d.template == "not_viewed_2"
    assert fr.plan(_after_first(), FIRST + 7 * D, ALL_DAY).skipped_key is None


def test_a_pause_after_the_first_moves_the_second_to_the_pause_end_and_does_not_skip_it():
    """A "Project delayed" click after the 1st is a pause, not a reply (db._CUSTOMER_TOUCH_SQL
    leaves the click out of customer_touch_at): the 2nd waits for the pause to end and then goes,
    as the revisit nudge it was designed to be. test_pg_followup_auto drives the real endpoint."""
    until = (FIRST + 10 * D).astimezone(fr.BUSINESS_TZ).date()      # past the 2nd's day 7
    resume = datetime(until.year, until.month, until.day, tzinfo=fr.BUSINESS_TZ) + D
    p = _after_first(followup_paused_until=until)
    for at in (FIRST + 7 * D, FIRST + 10 * D):
        assert cust(p, at) == [], "the 2nd went inside the pause"
    pl = fr.plan(p, FIRST + 8 * D, ALL_DAY)
    assert (pl.next_at, pl.next_template, pl.skipped_key) == (resume, "not_viewed_2", None)
    d, = cust(p, resume)
    assert d.template == "not_viewed_2", "the pause's end did not send the revisit nudge"


def test_the_estimator_note_after_a_skipped_second_follows_its_own_switch():
    p = _after_first(customer_touch_at=FIRST + D)
    # Dated off the 1st (the step's last email), not off the reply: no re-anchor.
    assert staff(p, FIRST + D - H) == []
    s, = staff(p, FIRST + D)
    assert (tag(s), s.template) == ("nv_est", fr.ESTIMATOR_TEMPLATE)
    assert staff(p, FIRST + 30 * D, cfg(estimator_on=False)) == []
    assert fr.plan(p, NOW, cfg(estimator_on=False)).reason == "skipped_by_reply"


def test_contact_before_the_first_email_skips_nothing():
    p = _p(customer_touch_at=SENT_AT + D, last_staff_contact_at=SENT_AT + 2 * D)
    d, = cust(p, SENT_AT + 3 * D)
    assert d.template == "not_viewed", "contact before the 1st must not skip the 1st"
    p["followup_sent_history"] = [_h(FIRST, "not_viewed")]
    d, = cust(p, FIRST + 7 * D)
    assert d.template == "not_viewed_2", "contact BEFORE the 1st skipped the 2nd"


def test_contact_at_the_first_emails_own_moment_is_not_after_it():
    d, = cust(_after_first(customer_touch_at=FIRST), FIRST + 7 * D)
    assert d.template == "not_viewed_2"


def test_contact_in_the_not_opened_step_does_not_skip_the_opened_steps_second():
    viewed = FIRST + 2 * D
    v1 = viewed + 3 * D
    p = _p(proposal_status="viewed", cycle_viewed_at=viewed, customer_touch_at=FIRST + D,
           followup_sent_history=[_h(FIRST, "not_viewed")])
    d, = cust(p, v1)
    assert d.template == "next_steps"
    p["followup_sent_history"].append(_h(v1, "next_steps"))
    d, = cust(p, v1 + 7 * D)
    assert d.template == "second_nudge", "step 1's reply skipped step 2's 2nd"
    # ...and contact after the OPENED step's 1st skips its 2nd.
    p["last_staff_contact_at"] = v1 + D
    assert cust(p, v1 + 7 * D) == []
    assert fr.plan(p, v1 + 7 * D, ALL_DAY).skipped_template == "second_nudge"


def test_a_skip_is_never_undone_by_next_email_on():
    """The 2nd was cancelled; a "Next email on" date does not bring it back (fewer emails is the
    safe side, and it never adds an email past what the step allows)."""
    p = _after_first(customer_touch_at=FIRST + D, followup_next_at=FIRST + 4 * D)
    for at in (FIRST + 4 * D, FIRST + 8 * D):
        assert cust(p, at) == []


def test_with_the_first_switched_off_the_only_email_is_not_skipped_by_earlier_contact():
    """The 2nd is the step's ONLY email when the 1st is off; nothing went before it, so contact
    cannot be 'after the 1st'."""
    off1 = cfg(not_opened_first_on=False)
    d, = cust(_p(customer_touch_at=SENT_AT + 4 * D), SENT_AT + 10 * D, off1)
    assert d.template == "not_viewed_2"


def test_a_skip_reads_json_timestamps_as_the_board_and_worker_pass_them():
    p = _after_first(customer_touch_at=(FIRST + D).isoformat())
    assert cust(p, FIRST + 7 * D) == []


def test_the_contact_keys_are_schedule_inputs():
    """INPUT_KEYS is what the board's query is pinned against: a contact the worker sees and the
    board does not would show a 2nd-email date the worker will never act on."""
    assert {"customer_touch_at", "last_staff_contact_at", "last_staff_message_at"} <= \
        set(fr.INPUT_KEYS)


def _group(**sig):
    from test_group_followups import _scope
    rows = [_scope("d-a", "Breakroom", 0, followup_enrolled_at=SENT_AT,
                   followup_sent_history=[_h(FIRST, "not_viewed")]),
            _scope("d-b", "Lab 1", 1, followup_enrolled_at=SENT_AT),
            _scope("d-c", "Lab 2", 2, followup_enrolled_at=SENT_AT)]
    return [dict(r, **sig.get(r["proposal_id"], {})) for r in rows]


def test_a_reply_on_any_scope_of_a_project_skips_the_groups_second_email():
    """The group's 1st email is recorded on the HOME scope; the customer answered on another
    scope's thread. It is one conversation about one project."""
    plain = fr.group_view(_group(), FIRST + 7 * D)
    assert [d.template for d in cust(plain, FIRST + 7 * D)] == ["not_viewed_2"]
    for pid in ("d-a", "d-b", "d-c"):
        view = fr.group_view(_group(**{pid: {"customer_touch_at": FIRST + D}}), FIRST + 7 * D)
        assert view["customer_touch_at"] == FIRST + D
        assert cust(view, FIRST + 7 * D) == [], "a reply on %s did not skip the 2nd" % pid
        assert fr.plan(view, FIRST + 7 * D, ALL_DAY).skipped_by == "customer_reply"
    # A call logged on a closed former scope (a history row) is contact about the project too.
    from test_group_followups import _scope
    closed = _scope("d-old", "Old", -1, proposal_status="closed_lost",
                    last_staff_contact_at=FIRST + D)
    view = fr.group_view(_group(), FIRST + 7 * D, [closed])
    assert cust(view, FIRST + 7 * D) == []
