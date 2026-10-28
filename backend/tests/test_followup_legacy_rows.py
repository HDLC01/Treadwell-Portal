"""Follow-ups on rows exactly as the OLDER engines and the schema left them (review, 2026-10-29).

  * a proposal opened before `cycle_viewed_at` existed is 'viewed' with no cycle stamp; a scope group
    built from such rows is OPENED too (the old group fold treated `viewed` as opened);
  * the sent history carries the old ladder's two estimator notes (`…:nv1_staff`, `…:v48_staff`) so
    release day does not tell the estimator again, and carries no other staff email.
"""
import sqlite3
from datetime import datetime, timedelta, timezone

import db
import followup_rules as rules

NOW = datetime(2026, 8, 4, 15, 0, tzinfo=timezone.utc)     # 10am Chicago
ENROLLED = NOW - timedelta(days=4)
H = timedelta(hours=1)
ALL_DAY = {"send_start_hour": 0, "send_end_hour": 24}


def _scope(pid, minutes, **over):
    row = {"proposal_id": pid, "scope_group": "grp-1", "scope_label": pid,
           "proposal_status": "sent", "created_at": NOW - timedelta(days=9, minutes=-minutes),
           "followup_enrolled_at": ENROLLED, "followup_disabled_at": None,
           "followup_paused_until": None, "cycle_viewed_at": None, "deleted_at": None,
           "approved_at": None, "followup_sent_history": []}
    row.update(over)
    return row


def test_a_group_with_a_viewed_scope_and_no_cycle_stamp_is_opened():
    rows = [_scope("home", 0), _scope("lab1", 1, proposal_status="viewed",
                                      viewed_at=ENROLLED + H, last_viewed_at=ENROLLED + 5 * H),
            _scope("lab2", 2)]
    view, why = rules.group_state(rows, NOW)
    assert why == "" and view["proposal_status"] == "viewed"
    assert view["cycle_viewed_at"] == ENROLLED + H
    due = [d for d in rules.due_now(view, NOW, ALL_DAY) if d.audience == "customer"]
    assert [d.template for d in due] == ["next_steps"], due
    # Nobody opened anything: still the not-opened step.
    view, _ = rules.group_state([_scope("home", 0), _scope("lab1", 1)], NOW)
    assert view["proposal_status"] == "sent" and view["cycle_viewed_at"] is None


def _admitted(rows):
    """Which rows the REAL _SENT_HISTORY_SQL predicate admits, run by SQLite."""
    sql = db._SENT_HISTORY_SQL.format(pid="'p1'", enrolled="null")
    pred = sql[sql.index(" where ") + len(" where "):-1]
    con = sqlite3.connect(":memory:")
    con.execute("attach database ':memory:' as public")
    con.execute("create table public.portal_followups (id int, proposal_id text, kind text, "
                "detail text, created_at text)")
    for i, (kind, detail) in enumerate(rows):
        con.execute("insert into public.portal_followups values (?,?,?,?,?)",
                    (i, "p1", kind, detail, "2026-08-01"))
    return {i for (i,) in con.execute(
        "select f.id from public.portal_followups f where " + pred).fetchall()}


def test_the_history_carries_the_old_estimator_notes_and_no_other_staff_email():
    rows = [("auto_email", '{"audience": "staff", "rule_key": "c1:nv1_staff"}'),
            ("auto_email", '{"audience": "staff", "rule_key": "c1:v48_staff"}'),
            ("auto_email", '{"audience": "staff", "rule_key": "c1:dep_staff"}'),
            ("auto_email", '{"audience": "staff", "rule_key": "c1:a:nv_est"}'),
            ("auto_email", '{"audience": "staff"}'),
            ("auto_email", '{"audience": "customer", "rule_key": "c1:nv1"}'),
            ("staff_email", '{"rule_key": "c1:nv1_staff"}')]
    assert _admitted(rows) == {0, 1, 5}


def test_the_history_names_each_rows_audience_for_auto_emails_only():
    sql = db._SENT_HISTORY_SQL
    assert "'audience', case when f.kind = 'auto_email' then f.detail->>'audience' end" in sql
