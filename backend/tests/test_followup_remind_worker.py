"""Settling a follow-up task whose send was claimed and never confirmed (`sending`).

The remind-only tick (2026-10-23) raised reminder TASKS; since 2026-10-28 the tick emails the
customer itself and raises none (test_followup_auto_worker.py). Old tasks can still be in the
database, and the estimator's send of one can still be interrupted, so the recovery stays.
"""
from datetime import timedelta

import followup_rules as rules
import followup_worker as fw
from test_followup_worker import ENROLLED, NOW, _wire


# ── recovering a send that was claimed and never confirmed (audit, 2026-10-23) ─
# The send endpoint claims a task as `sending` before it emails. If the email fails AND putting the
# task back fails too, or the email goes but the database is gone before `sent` is written, the
# task is left `sending`. Left there it would block every later task on the proposal for ever
# (`sending` counts as open), so the tick settles it once it is clearly not in flight any more.
def _sending_task(tid=52, **detail):
    d = {"status": "sending", "template": "checkin", "stage": "pre",
         "cycle": rules.cycle_key(ENROLLED), "decided_by": "kyle@wetreadwell.com",
         "sending_at": (NOW - timedelta(minutes=40)).isoformat()}
    d.update(detail)
    return {"id": tid, "proposal_id": "p1", "detail": d,
            "created_at": NOW - timedelta(days=1)}


def test_a_stuck_send_with_no_record_of_going_out_is_put_back_to_pending(monkeypatch):
    """Nothing says the email went, so the estimator gets the task back — flagged, so the compose
    box can say "this may already have gone; check the thread before you send it again". Reopening
    a task never emails anyone (remind-only), so a wrong guess costs a look, not a second email."""
    calls = _wire(monkeypatch, stale=[_sending_task()])
    monkeypatch.setattr(fw.db, "list_followup_candidates", lambda: [])
    fw._tick(NOW)
    assert calls["reopened"] == [(52, {"send_unconfirmed": True})]
    assert calls["decided"] == []
    assert calls["customer"] == [], "recovery emailed the customer"


def test_a_stuck_send_that_did_go_out_is_finalised_as_sent(monkeypatch):
    """The staff_email row the send endpoint writes after a real send is the evidence: the task
    is finished as `sent`, not offered to the estimator again."""
    calls = _wire(monkeypatch, stale=[_sending_task()], recorded=(52,))
    monkeypatch.setattr(fw.db, "list_followup_candidates", lambda: [])
    fw._tick(NOW)
    assert calls["decided"] == [(52, "sent", None)]
    assert calls["reopened"] == []


def test_the_recovery_asks_only_for_sends_older_than_the_in_flight_window(monkeypatch):
    asked = []
    calls = _wire(monkeypatch)
    monkeypatch.setattr(fw.db, "list_stale_sending_tasks",
                        lambda minutes: asked.append(minutes) or [])
    fw._tick(NOW)
    assert asked == [fw.SENDING_STALE_MINUTES]
    assert fw.SENDING_STALE_MINUTES >= 10, "a slow but live send could be reopened mid-flight"

