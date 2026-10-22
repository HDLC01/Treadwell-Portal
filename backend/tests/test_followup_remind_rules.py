"""Remind-only follow-ups (2026-10-23): the schedule half. Pure functions, so exhaustive.

Hanz's decisions, as built here:
  1/2  every proposal is remind-only — that is the worker's half (test_followup_remind_worker.py);
       the schedule decides WHEN, and is the same schedule for every mode.
  3    a customer reply RESETS the remaining-reminder count, not just the next date.
  5    the pre-decision nudge cap and the deposit-chase cap are separate.

And RJ's complaint, the reason touches exist: logging a call (or the customer answering, or the
estimator replying in the chat) used to change nothing about when the next reminder fired.
"""
from datetime import datetime, timedelta, timezone

import pytest

import followup_rules as fr

ENROLLED = datetime(2026, 8, 1, 15, 0, tzinfo=timezone.utc)      # 10am Chicago
VIEWED = ENROLLED + timedelta(hours=1)
H = timedelta(hours=1)
D = timedelta(days=1)
# A cadence with the send window open around the clock, so a test about WHEN a reminder matures is
# not also a test about business hours (which test_followup_rules.py covers on its own).
ALL_DAY = {"send_start_hour": 0, "send_end_hour": 24}


def _viewed(**over):
    p = {"proposal_id": "p1", "proposal_status": "viewed", "followup_enrolled_at": ENROLLED,
         "followup_disabled_at": None, "followup_paused_until": None, "cycle_viewed_at": VIEWED}
    p.update(over)
    return p


def _sent(**over):
    return _viewed(proposal_status="sent", cycle_viewed_at=None, **over)


def _approved(**over):
    return _viewed(proposal_status="approved", approved_at=VIEWED + 2 * D,
                   deposit_status="pending", **over)


def cust(p, at, cfg=ALL_DAY):
    got = [d for d in fr.due_now(p, at, cfg) if d.audience == "customer"]
    assert len(got) <= 1, "latest-only broke: %r" % got
    return got[0] if got else None


def key(p, at, cfg=ALL_DAY):
    d = cust(p, at, cfg)
    return d.rule_key if d else None


# ── touches re-anchor the schedule ────────────────────────────────────────────
@pytest.mark.parametrize("touch_key", list(fr.TOUCH_KEYS))
def test_every_kind_of_touch_pushes_the_next_reminder_to_touch_plus_interval(touch_key):
    """RJ's complaint. Viewed, and 30h later somebody talks to the customer. The ladder would have
    fired its 72h reminder at view+72h — two days after a real conversation. It must not: the next
    reminder is one interval after the touch."""
    touch = VIEWED + 30 * H
    p = _viewed(**{touch_key: touch})
    assert key(p, VIEWED + 72 * H + H) is None, "the ladder fired over a fresh touch"
    assert fr.next_due_at(p, VIEWED + 31 * H, ALL_DAY) == touch + 72 * H
    d = cust(p, touch + 72 * H + H)
    assert d is not None and d.template == "checkin" and d.include_status_ask is True


def test_the_first_reminder_after_a_touch_does_not_reuse_a_spent_fixed_key():
    """The stress test's finding. v1/v2 are fixed per-cycle keys; a touched schedule that fell back
    onto them would collide with the row the ladder already used, be deduped away in silence, and
    the real next reminder would land a whole interval late. So the touched series is time-keyed
    from its FIRST occurrence."""
    touch = VIEWED + 30 * H
    p = _viewed(last_staff_followup_at=touch)
    k = key(p, touch + 72 * H + H)
    assert k and "@" in k, k
    for spent in (":v1", ":v2", ":nv1", ":dep1"):
        assert not k.endswith(spent), k


def test_a_per_proposal_interval_is_what_the_touch_is_followed_by():
    """'Follow up in 5 days' means touch + 5 days."""
    touch = VIEWED + 30 * H
    p = _viewed(followup_anchor_at=touch, followup_interval_days=5)
    assert fr.next_due_at(p, touch + H, ALL_DAY) == touch + 5 * D
    assert key(p, touch + 5 * D - H) is None
    assert key(p, touch + 5 * D + H) is not None


def test_a_touch_from_before_the_stage_began_is_ignored():
    """A call logged before they opened it says nothing about the viewed track."""
    p = _viewed(last_staff_followup_at=VIEWED - 5 * H)
    assert key(p, VIEWED + 25 * H).endswith(":v1")


def test_a_staff_touch_also_pushes_the_estimators_own_nudge():
    """The 48h "worth a call" note to the estimator who called this morning is the same nag."""
    touch = VIEWED + 40 * H
    p = _viewed(last_staff_followup_at=touch)
    staff = [d for d in fr.due_now(p, VIEWED + 50 * H, ALL_DAY) if d.audience == "staff"]
    assert staff == [], staff
    staff = [d for d in fr.due_now(p, touch + 49 * H, ALL_DAY) if d.audience == "staff"]
    assert [d.template for d in staff] == ["staff_personal_followup"]


def test_the_deposit_staff_chase_re_anchors_on_a_touch_without_eating_its_first_key():
    p = _approved()
    approved = p["approved_at"]
    assert [d.rule_key for d in fr.due_now(p, approved + 49 * H, ALL_DAY)
            if d.audience == "staff"][0].endswith(":dep_staff")
    touched = _approved(customer_touch_at=approved + 60 * H)
    staff = [d for d in fr.due_now(touched, approved + 60 * H + 49 * H, ALL_DAY)
             if d.audience == "staff"]
    assert staff and "@" in staff[0].rule_key, staff


# ── decision 3: a customer reply resets the count ─────────────────────────────
def _times(*hours):
    return [(VIEWED + h * H).isoformat() for h in hours]


def test_the_count_caps_reminders_since_the_reset_point():
    p = _viewed(followup_stop_after=2, followup_reminder_times=_times(25, 73))
    assert cust(p, VIEWED + 300 * H) is None
    assert fr.next_due_at(p, VIEWED + 300 * H, ALL_DAY) is None


def test_a_customer_reply_puts_the_full_count_back():
    """Decision 3: not just a pushed-out date — the remaining count goes back to the full cap."""
    reply = VIEWED + 100 * H
    p = _viewed(followup_stop_after=2, followup_reminder_times=_times(25, 73),
                customer_touch_at=reply)
    assert fr.next_due_at(p, reply + H, ALL_DAY) == reply + 72 * H
    assert cust(p, reply + 73 * H) is not None
    # And it really is the FULL count: one more reminder after the reply still leaves room.
    p2 = dict(p, followup_reminder_times=_times(25, 73) + [(reply + 72 * H).isoformat()])
    assert fr.next_due_at(p2, reply + 73 * H, ALL_DAY) is not None


def test_a_staff_touch_moves_the_date_but_not_the_count():
    """Otherwise every "skip" would hand the estimator a fresh set and the cap would never bind."""
    p = _viewed(followup_stop_after=2, followup_reminder_times=_times(25, 73),
                last_staff_followup_at=VIEWED + 100 * H, followup_anchor_at=VIEWED + 101 * H)
    assert fr.next_due_at(p, VIEWED + 102 * H, ALL_DAY) is None
    assert cust(p, VIEWED + 400 * H) is None


def test_old_automatic_emails_count_toward_the_cap():
    """A proposal the old ladder already emailed twenty times does not start a fresh twenty on
    the day remind-only ships."""
    p = _viewed(followup_reminder_times=_times(*range(25, 25 + 72 * 20, 72)))
    assert fr.next_due_at(p, VIEWED + 2000 * H, ALL_DAY) is None


def test_reminders_from_before_the_stage_do_not_count():
    """The deposit stage starts its own count at approval."""
    p = _approved(followup_reminder_times=_times(25, 73, 145))
    assert cust(p, p["approved_at"] + 25 * H) is not None


# ── decision 5: two caps ──────────────────────────────────────────────────────
def test_the_pre_decision_cap_does_not_reach_the_deposit_stage():
    approved = _approved()
    a = approved["approved_at"]
    keys_low = {key(approved, a + n * D, dict(ALL_DAY, max_recurring=1)) for n in range(1, 30)}
    keys_std = {key(approved, a + n * D) for n in range(1, 30)}
    assert keys_low == keys_std, "lowering max_recurring changed the deposit stage"


def test_the_deposit_cap_does_not_reach_the_pre_decision_stage():
    p = _viewed()
    keys_low = {key(p, VIEWED + n * D, dict(ALL_DAY, deposit_max_recurring=1)) for n in range(1, 40)}
    keys_std = {key(p, VIEWED + n * D) for n in range(1, 40)}
    assert keys_low == keys_std


def test_the_deposit_cap_is_effectively_unbounded_by_default():
    assert fr.DEPOSIT_MAX_RECURRING >= 100
    assert fr._Cadence().deposit_max == fr.DEPOSIT_MAX_RECURRING


# ── a snooze to a date ────────────────────────────────────────────────────────
def test_a_snooze_lands_the_next_reminder_on_its_date():
    stamp = VIEWED + 30 * H
    until = VIEWED + 10 * D
    p = _viewed(followup_anchor_at=stamp, followup_next_at=until)
    assert fr.next_due_at(p, stamp + H, ALL_DAY) == until
    assert key(p, until - H) is None
    assert key(p, until + H) is not None
    assert fr.next_due_at(p, until + H, ALL_DAY) == until + 72 * H


def test_a_decision_after_the_snooze_date_supersedes_it():
    until = VIEWED + 10 * D
    later = until + 2 * D
    p = _viewed(followup_anchor_at=later, followup_next_at=until)
    assert fr.next_due_at(p, later + H, ALL_DAY) == later + 72 * H


def test_a_snooze_from_an_earlier_stage_is_ignored():
    """Snoozed while deciding, then they approved: the deposit stage runs on its own clock."""
    p = _approved(followup_anchor_at=VIEWED + 30 * H, followup_next_at=VIEWED + 30 * D)
    assert cust(p, p["approved_at"] + 25 * H).rule_key.endswith(":dep1")


# ── the board and the worker are mirrors ──────────────────────────────────────
def _corpus():
    t = VIEWED + 30 * H
    return [
        ("sent", _sent()),
        ("viewed", _viewed()),
        ("viewed, touched", _viewed(last_staff_followup_at=t)),
        ("viewed, replied", _viewed(customer_touch_at=t)),
        ("viewed, chat", _viewed(last_staff_message_at=t)),
        ("viewed, snoozed", _viewed(followup_anchor_at=t, followup_next_at=t + 6 * D)),
        ("viewed, own interval", _viewed(followup_anchor_at=t, followup_interval_days=4)),
        ("viewed, capped soon", _viewed(followup_stop_after=3,
                                        followup_reminder_times=_times(25, 73))),
        ("sent, touched", _sent(customer_touch_at=ENROLLED + 5 * H)),
        ("approved", _approved()),
        ("approved, touched", _approved(last_staff_followup_at=VIEWED + 3 * D)),
    ]


@pytest.mark.parametrize("name,p", _corpus())
def test_next_due_at_names_exactly_when_due_now_next_changes(name, p):
    """The invariant, stated as behaviour rather than as shared code: walk forward in time. Wherever
    next_due_at says the next reminder is, due_now's customer key changes there and nowhere before
    it. And when next_due_at says None, the key never changes again."""
    t = ENROLLED + H
    end = ENROLLED + 120 * D
    steps = 0
    while t < end and steps < 200:
        steps += 1
        nxt = fr.next_due_at(p, t, ALL_DAY)
        here = key(p, t)
        if nxt is None:
            for probe in (t + D, t + 10 * D, t + 60 * D):
                assert key(p, probe) == here, "%s: next_due_at said nothing more, due_now moved" % name
            return
        assert nxt > t, "%s: next_due_at %s is not ahead of %s" % (name, nxt, t)
        before = key(p, nxt - timedelta(minutes=1))
        assert before == here, "%s: due_now moved before next_due_at said (%s)" % (name, nxt)
        after = key(p, nxt + timedelta(minutes=1))
        assert after is not None and after != here, "%s: nothing matured at %s" % (name, nxt)
        t = nxt + timedelta(minutes=1)
    assert steps > 1, "%s: the walk never moved, so this proved nothing" % name


def test_the_mirror_walk_would_notice_a_drift(monkeypatch):
    """Vacuous-invariant guard: break next_due_at by an hour and the walk above must fail."""
    real = fr.next_due_at

    def off_by_an_hour(p, now, cfg=None):
        got = real(p, now, cfg)
        return got + H if got else got

    monkeypatch.setattr(fr, "next_due_at", off_by_an_hour)
    with pytest.raises(AssertionError):
        test_next_due_at_names_exactly_when_due_now_next_changes("viewed", _viewed())


# ── the pieces ────────────────────────────────────────────────────────────────
def test_effective_cfg_lays_the_proposal_over_the_global_cadence():
    base = {"recurring_hours": 72, "max_recurring": 20, "deposit_max_recurring": 200}
    got = fr.effective_cfg(base, {"followup_interval_days": 5, "followup_stop_after": 3})
    assert got["recurring_hours"] == 120 and got["max_recurring"] == 3
    assert got["deposit_max_recurring"] == 200, "stop-after reached the deposit cap"
    assert fr.effective_cfg(base, {}) == base
    assert fr.effective_cfg(base, {"followup_interval_days": 0, "followup_stop_after": None}) == base
    assert base["recurring_hours"] == 72, "the global cfg was mutated"


def test_every_proposal_is_in_remind_mode_in_v1():
    for raw in (None, "remind", "auto", "approve", "nonsense"):
        assert fr.followup_mode({"followup_mode": raw}) == "remind"


@pytest.mark.parametrize("change,reason", [
    ({"proposal_status": "closed_lost"}, "closed_lost"),
    ({"followup_disabled_at": VIEWED}, "automation_off"),
    ({"deleted_at": VIEWED}, "deleted"),
    ({"proposal_status": "approved", "deposit_status": "received"}, "finished"),
    ({"proposal_status": "approved", "deposit_status": "submitted"}, "customer_done"),
    ({"proposal_status": "approved", "deposit_status": "pending"}, "stage_changed"),
    ({"followup_enrolled_at": ENROLLED + 5 * D}, "resent"),
])
def test_a_task_stops_being_live_when_the_proposal_moves_on(change, reason):
    task = {"stage": "pre", "cycle": fr.cycle_key(ENROLLED)}
    now = ENROLLED + 6 * D
    assert fr.task_is_live(task, _viewed(), now) == (True, "")
    assert fr.task_is_live(task, _viewed(**change), now) == (False, reason)


def test_a_paused_proposal_has_no_live_task():
    now = ENROLLED + 6 * D
    p = _viewed(followup_paused_until=(now + 30 * D).date())
    assert fr.task_is_live({"stage": "pre"}, p, now) == (False, "paused")
