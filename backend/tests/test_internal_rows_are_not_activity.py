"""A note we wrote to ourselves is not movement on the job.

WHY THIS EXISTS, because the failure is silent and self-defeating. `last_message_at` in
`db.list_all_portal_proposals` feeds `main._last_activity`, which the 6am digest scores customer
SILENCE from. When the staff follow-up reminders started echoing into the project thread on
2026-08-24, every one of them moved that timestamp to today - so a reminder whose entire content was
"nobody has opened this yet" counted as activity on the proposal, and could push that same proposal
off the estimator's morning email.

Measured end to end on one proposal (sent 20 days ago, never opened, last hand-chase 20 days ago),
feeding the portal's own _last_activity output into the tool's own digest score():

    before the staff echo:  last_activity 2026-08-04, score 70, facts include "no movement for 20 days"
    after  the staff echo:  last_activity 2026-08-24, score 50, that fact gone

The 20-point drop is exactly the digest's silence weight, against its minimum score of 40. So a
proposal sitting in the 40-60 band gets hidden by the reminder telling somebody to chase it.

THE CUSTOMER ECHO STILL COUNTS, on purpose. The customer really was emailed, and that is activity on
the job. The distinction this file defends is authorship, not automation: `meta.internal` marks the
rows written for us, and only those are excluded.

Asserted against the SQL the function actually builds. This suite has no database - the same
compromise test_admin_pipeline.py already makes - so the predicate is pinned by text and by
mutation. The behaviour it protects is measured in the docstring above rather than here.
"""

import re

import db


def _pipeline_sql(monkeypatch):
    """Capture the SELECT list_all_portal_proposals really runs."""
    seen = {}

    def fake_qall(sql, params=()):
        seen["sql"] = sql
        return []

    monkeypatch.setattr(db, "qall", fake_qall)
    db.list_all_portal_proposals()
    assert "sql" in seen, "list_all_portal_proposals did not reach qall"
    return seen["sql"]


def _last_message_subquery(sql):
    """The one subquery aliased as last_message_at, whitespace-flattened."""
    flat = " ".join(sql.split())
    m = re.search(r"\(select max\(q\.created_at\)(.*?)\) as last_message_at", flat)
    assert m, "no last_message_at subquery in the pipeline SELECT: %r" % flat[:400]
    return m.group(1)


def test_last_message_at_ignores_internal_rows(monkeypatch):
    sub = _last_message_subquery(_pipeline_sql(monkeypatch))
    assert "internal" in sub, (
        "last_message_at counts internal rows again, so a staff reminder now reads as movement on "
        "the job and can push that proposal off the 6am digest: %r" % sub)


def test_the_predicate_treats_a_missing_flag_as_not_internal(monkeypatch):
    """Most rows have no `internal` key at all, and they must still count.

    A bare `q.meta ->> 'internal' is not true` would be NULL for those rows, and NULL is not TRUE,
    so it happens to work - but `coalesce(..., false)` says the intent out loud and survives somebody
    rewriting the comparison.
    """
    sub = _last_message_subquery(_pipeline_sql(monkeypatch))
    assert "coalesce" in sub.lower(), (
        "the internal check has no coalesce, so its behaviour on the rows that have no `internal` "
        "key at all rests on NULL semantics rather than on anything stated: %r" % sub)


def test_the_customer_reply_column_is_left_alone(monkeypatch):
    """`customer_replied_at` filters on author_kind and must NOT gain an internal predicate.

    It already answers a narrower question - did the CUSTOMER come back to us - so an internal
    filter there would be redundant, and redundancy in a WHERE clause is how the next reader
    concludes the two columns mean the same thing.
    """
    flat = " ".join(_pipeline_sql(monkeypatch).split())
    m = re.search(r"\(select max\(q\.created_at\)(.*?)\) as customer_replied_at", flat)
    assert m, "no customer_replied_at subquery found"
    assert "author_kind" in m.group(1), m.group(1)


def test_only_the_message_timestamp_gained_the_filter(monkeypatch):
    """Guard against a broad find-and-replace stamping the predicate across the whole SELECT.

    The staff hand-chase column (last_staff_followup_at and friends) reads portal_followups, not
    portal_questions, and has nothing to do with message visibility.
    """
    sql = _pipeline_sql(monkeypatch)
    assert sql.lower().count("'internal'") == 1, (
        "the internal predicate appears %d times in the pipeline SELECT; it belongs to "
        "last_message_at alone" % sql.lower().count("'internal'"))
