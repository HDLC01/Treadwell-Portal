"""Remind-only follow-ups (2026-10-23): the worker half.

Decisions 1 and 2: every proposal — including the ones already mid-cadence when this ships — is
remind-only. A customer reminder coming due raises a follow-up TASK and tells the estimator; the
customer hears nothing until the estimator sends it. These pin the properties that make that safe:
the customer send is unreachable from the tick, one open task per proposal, and a task whose
proposal has moved on is expired rather than left blocking the queue.

The existing worker tests (test_followup_worker.py) carry the shared wiring.
"""
from datetime import datetime, timedelta, timezone

import followup_rules as rules
import followup_worker as fw
from test_followup_worker import ENROLLED, NOW, _proposal, _wire

PENDING = {"id": 99, "detail": {"status": "pending", "template": "not_viewed", "stage": "pre"},
           "created_at": (NOW - timedelta(hours=2)).isoformat()}


def test_one_open_task_per_proposal(monkeypatch):
    """The human decision resolves a task, not the clock: while one is open, a later tick that
    finds a newer matured occurrence raises nothing."""
    late = _proposal(proposal_status="viewed", cycle_viewed_at=NOW - timedelta(hours=200))
    calls = _wire(monkeypatch, proposal=late, signals={"pending_task": PENDING})
    fw._tick(NOW)
    assert calls["tasks"] == [], "a second task was raised while one is open"
    assert calls["customer"] == []


def test_the_twin_container_cannot_raise_a_second_task(monkeypatch):
    """The database's answer (unique index -> reserve returns None) means no estimator email. The
    twin reserved the staff note of the same step too, so both reservations come back empty."""
    calls = _wire(monkeypatch, reserve=lambda pid, key, detail: None,
                  reserve_task=lambda pid, key, detail: None)
    fw._tick(NOW)
    assert calls["tasks"] and calls["staff"] == [] and calls["thread"] == []


def test_an_occurrence_the_old_ladder_already_emailed_is_not_raised_again(monkeypatch):
    """Decision 2's switch-over. The untouched schedule keeps the old ladder's keys, and the task
    reserves through the SAME unique index, so the row the automatic email left behind dedupes the
    task to nothing. Pinned here as: the key the task asks for is exactly the ladder's key."""
    calls = _wire(monkeypatch)
    fw._tick(NOW)
    assert calls["tasks"][0]["key"] == "%s:nv1" % rules.cycle_key(ENROLLED)


def test_a_task_carries_what_the_estimators_send_needs(monkeypatch):
    calls = _wire(monkeypatch)
    fw._tick(NOW)
    d = calls["tasks"][0]["detail"]
    assert d["status"] == "pending" and d["audience"] == "customer"
    assert d["stage"] == "pre" and d["cycle"] == rules.cycle_key(ENROLLED)
    assert d["mode"] == "remind"
    assert "{link}" in d["draft"]["body"] and d["draft"]["source"] == "default"


def test_the_24h_staff_note_is_folded_into_the_task_email(monkeypatch):
    """Not-opened at 24h fires the customer reminder and the estimator's note together. One email
    about one moment: the note is consumed (reserved) but not sent."""
    calls = _wire(monkeypatch)
    fw._tick(NOW)
    assert len(calls["staff"]) == 1 and calls["staff"][0]["subject"].startswith("Follow-up due")
    assert any(k.endswith(":nv1_staff") for k in calls["reserved"])


def test_a_staff_note_is_still_sent_when_no_task_was_raised(monkeypatch):
    calls = _wire(monkeypatch, signals={"pending_task": PENDING})
    fw._tick(NOW)
    assert [s["subject"] for s in calls["staff"]] == ["Not viewed yet — Westport"]


def test_the_deposit_chase_is_never_folded(monkeypatch):
    """Different news from a follow-up-due email, and the one decision 5 protects."""
    approved = _proposal(proposal_status="approved", cycle_viewed_at=ENROLLED,
                         approved_at=NOW - timedelta(hours=50), deposit_status="pending")
    calls = _wire(monkeypatch, proposal=approved)
    fw._tick(NOW)
    subjects = [s["subject"] for s in calls["staff"]]
    assert any(s.startswith("Follow-up due") for s in subjects), subjects
    assert any(s.startswith("Deposit not in yet") for s in subjects), subjects


def test_a_customer_reply_holds_the_next_task(monkeypatch):
    """The touch reaches the worker through followup_signals: a reply an hour ago means nothing is
    due today, where the untouched ladder would have raised one."""
    viewed = _proposal(proposal_status="viewed", cycle_viewed_at=NOW - timedelta(hours=80))
    calls = _wire(monkeypatch, proposal=viewed,
                  signals={"customer_touch_at": NOW - timedelta(hours=1)})
    fw._tick(NOW)
    assert calls["tasks"] == []
    calls = _wire(monkeypatch, proposal=viewed)
    fw._tick(NOW)
    assert calls["tasks"], "the control case raised nothing either, so this proved nothing"


# ── housekeeping ──────────────────────────────────────────────────────────────
def _pending_task(pid="p1", **detail):
    d = {"status": "pending", "template": "checkin", "stage": "pre",
         "cycle": rules.cycle_key(ENROLLED)}
    d.update(detail)
    return {"id": 41, "proposal_id": pid, "detail": d, "created_at": NOW - timedelta(days=1)}


def test_a_dangling_task_is_expired_when_the_proposal_leaves_scope(monkeypatch):
    for change, why in (({"proposal_status": "closed_lost"}, "closed_lost"),
                        ({"proposal_status": "approved", "deposit_status": "received"},
                         "finished"),
                        ({"followup_disabled_at": NOW}, "automation_off"),
                        ({"followup_paused_until": (NOW + timedelta(days=30)).date()}, "paused")):
        p = _proposal(**change)
        calls = _wire(monkeypatch, proposal=p, pending=[_pending_task()])
        monkeypatch.setattr(fw.db, "list_followup_candidates", lambda: [])
        fw._tick(NOW)
        assert calls["decided"] == [(41, "expired", why)], (change, calls["decided"])


def test_a_live_task_is_left_alone(monkeypatch):
    calls = _wire(monkeypatch, pending=[_pending_task()])
    monkeypatch.setattr(fw.db, "list_followup_candidates", lambda: [])
    fw._tick(NOW)
    assert calls["decided"] == []


def test_a_task_from_the_previous_stage_is_expired_and_unblocks_the_new_one(monkeypatch):
    """Approved since the task was raised: the 'are you still deciding?' task is stale, and the
    deposit reminder the proposal is now owed must not be blocked behind it."""
    approved = _proposal(proposal_status="approved", cycle_viewed_at=ENROLLED,
                         approved_at=NOW - timedelta(hours=30), deposit_status="pending")
    calls = _wire(monkeypatch, proposal=approved, pending=[_pending_task()])
    fw._tick(NOW)
    assert calls["decided"] == [(41, "expired", "stage_changed")]
    assert [t["detail"]["template"] for t in calls["tasks"]] == ["deposit_nudge"]


def test_housekeeping_failure_does_not_stop_the_sweep(monkeypatch):
    calls = _wire(monkeypatch)

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(fw.db, "list_pending_followup_tasks", boom)
    fw._tick(NOW)
    assert calls["tasks"], "the sweep stopped because housekeeping failed"
