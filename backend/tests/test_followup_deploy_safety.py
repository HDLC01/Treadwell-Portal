"""Deploy-day safety for the remind-only follow-ups (fix round PF1, 2026-10-25). Pure rules here; the
same history seeded into a real Postgres is in test_pg_deploy_safety.py.

#10  The TOUCHED shape keys its occurrences on time ('vrt@<stamp>'), a key the old automatic ladder
     never used. A proposal with a reply weeks ago and an automatic email two days ago would come due
     on the first tick after deploy: two reminders inside one interval. The interval now runs from
     the touch or the newest COUNTED reminder, whichever is later.
#11/#3  The deposit chase used to stop at max_recurring (20). It now has its own cap (200, decision
     5), so a chase the old ladder had FINISHED would start again. A series whose old emails reached
     the old cap stays finished; every deposit reminder already sent counts toward the new cap.
"""
from datetime import datetime, timedelta, timezone

import pytest

import followup_rules as fr

NOW = datetime(2026, 10, 20, 17, 0, tzinfo=timezone.utc)      # noon Chicago
H = timedelta(hours=1)
D = timedelta(days=1)
ALL_DAY = {"send_start_hour": 0, "send_end_hour": 24}


def _viewed(**over):
    p = {"proposal_id": "p1", "proposal_status": "viewed",
         "followup_enrolled_at": NOW - 60 * D, "cycle_viewed_at": NOW - 59 * D,
         "followup_disabled_at": None, "followup_paused_until": None}
    p.update(over)
    return p


def dues(p, at=NOW, cfg=ALL_DAY):
    return fr.due_now(p, at, cfg)


def cust(p, at=NOW, cfg=ALL_DAY):
    got = [d for d in dues(p, at, cfg) if d.audience == "customer"]
    return got[0] if got else None


def staff_deposit(p, at=NOW, cfg=ALL_DAY):
    return [d for d in dues(p, at, cfg) if d.template == "staff_deposit_outstanding"]


# ── #10: the touched shape never starts before the newest counted reminder ───────
def test_an_old_automatic_email_after_the_reply_holds_the_touched_reminder_back():
    """Finding #10's scenario: replied 20 days ago, the old ladder emailed them 2 days ago. The old
    code would send nothing now; the touched shape alone would raise a vrt@ task at once."""
    p = _viewed(customer_touch_at=NOW - 20 * D,
                followup_reminder_times=[(NOW - 2 * D).isoformat()])
    assert cust(p) is None, "a task came due two days after the customer was last emailed"
    assert fr.next_due_at(p, NOW, ALL_DAY) == NOW - 2 * D + 72 * H
    later = cust(p, NOW + D + H)
    assert later is not None and ":vrt@" in later.rule_key


def test_a_reminder_older_than_the_touch_changes_nothing():
    touch = NOW - 20 * D
    p = _viewed(customer_touch_at=touch,
                followup_reminder_times=[(NOW - 30 * D).isoformat()])
    d = cust(p)
    assert d is not None and ":vrt@" in d.rule_key
    assert fr.next_due_at(p, touch + H, ALL_DAY) == touch + 72 * H


def test_a_snooze_still_wins_over_the_newest_reminder():
    """A snooze date is an estimator's explicit choice, not a reminder floor."""
    snooze = NOW + 5 * D
    p = _viewed(followup_anchor_at=NOW - 3 * D, followup_next_at=snooze,
                followup_reminder_times=[(NOW - D).isoformat()])
    assert fr.next_due_at(p, NOW, ALL_DAY) == snooze


def test_the_floor_also_holds_for_a_scope_group():
    """group_state unions every member's counted reminders, so the floor reaches the group too."""
    a = _viewed(proposal_id="a", scope_group="g", created_at=NOW - 60 * D,
                customer_touch_at=NOW - 20 * D)
    b = _viewed(proposal_id="b", scope_group="g", created_at=NOW - 59 * D,
                followup_reminder_times=[(NOW - 2 * D).isoformat()])
    view = fr.group_view([a, b], NOW)
    assert view is not None
    assert cust(view) is None
    assert fr.next_due_at(view, NOW, ALL_DAY) == NOW - 2 * D + 72 * H


# ── #11 / #3: a deposit chase the old cap finished stays finished ────────────────
def _approved(days_ago: float, legacy: int, *, deposit_status="pending", staff_legacy=None,
              staff=None, **over):
    """An approved job with the deposit outstanding and `legacy` old automatic deposit emails: the
    old ladder's shape is dep1 + `legacy` depr@ customer emails and dep_staff + `legacy` depsr@
    staff emails, so `legacy` = 20 is a chase the old default cap finished."""
    approved = NOW - days_ago * D
    sent = [(approved + D + 72 * H * i).isoformat() for i in range(legacy + 1)]
    sl = legacy if staff_legacy is None else staff_legacy
    p = _viewed(proposal_status="approved", approved_at=approved, deposit_status=deposit_status,
                followup_enrolled_at=approved - 10 * D, cycle_viewed_at=approved - 9 * D,
                followup_reminder_times=sent if deposit_status == "pending" else [],
                deposit_history={"customer_legacy": legacy if deposit_status == "pending" else 0,
                                 "staff_legacy": sl, "staff": sl + 1 if staff is None else staff})
    p.update(over)
    return p


@pytest.mark.parametrize("days_ago", [70, 100, 150])
@pytest.mark.parametrize("legacy", [20, 23])
def test_a_deposit_chase_the_old_cap_finished_does_not_restart(days_ago, legacy):
    p = _approved(days_ago, legacy)
    assert cust(p) is None, "a finished customer deposit chase raised a new task"
    assert staff_deposit(p) == [], "a finished staff deposit chase emailed the estimator again"
    assert fr.next_due_at(p, NOW, ALL_DAY) is None


@pytest.mark.parametrize("days_ago", [70, 100, 150])
def test_a_deposit_chase_with_five_old_emails_carries_on(days_ago):
    p = _approved(days_ago, 5)
    d = cust(p)
    assert d is not None and d.template == "deposit_nudge"
    assert len(staff_deposit(p)) == 1


def test_without_history_the_deposit_chase_runs_exactly_as_before():
    """No deposit_history (a stubbed row, a pre-change caller) means nothing was sent."""
    p = _approved(100, 5)
    p.pop("deposit_history")
    assert cust(p) is not None and len(staff_deposit(p)) == 1


@pytest.mark.parametrize("staff_legacy,expect", [(20, 0), (5, 1)])
def test_a_submitted_deposit_keeps_the_estimator_side_on_the_old_cap(staff_legacy, expect):
    """'submitted': the customer is no longer chased; only the estimator's emails were, and those
    are the ones finding #3 says would come back even on a database without the new DDL."""
    p = _approved(100, 0, deposit_status="submitted", staff_legacy=staff_legacy)
    assert cust(p) is None
    assert len(staff_deposit(p)) == expect


def test_a_lowered_stop_after_never_silences_an_unfinished_deposit_chase():
    """Decision 5: the per-proposal stop-after is the pre-decision cap. The old ladder never had one,
    so it is not the cap the old deposit chase ran under."""
    p = _approved(100, 5, followup_stop_after=3)
    assert cust(p) is not None and len(staff_deposit(p)) == 1


def test_a_lowered_global_cap_is_the_cap_the_old_ladder_ran_under():
    """With max_recurring saved at 3 the old ladder stopped after 3 recurring deposit emails."""
    cfg = dict(ALL_DAY, max_recurring=3)
    p = _approved(100, 3)
    assert cust(p, cfg=cfg) is None and staff_deposit(p, cfg=cfg) == []


def test_every_staff_deposit_email_counts_toward_the_deposit_cap():
    cfg = dict(ALL_DAY, deposit_max_recurring=10)
    p = _approved(100, 2, staff=10)
    assert staff_deposit(p, cfg=cfg) == []
    p = _approved(100, 2, staff=9)
    assert len(staff_deposit(p, cfg=cfg)) == 1


def test_customer_deposit_reminders_already_sent_count_toward_the_deposit_cap():
    cfg = dict(ALL_DAY, deposit_max_recurring=6)
    p = _approved(100, 5)            # dep1 + 5 depr = 6 counted customer reminders
    assert cust(p, cfg=cfg) is None
