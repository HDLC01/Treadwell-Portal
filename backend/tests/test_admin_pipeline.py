"""GET /api/admin/pipeline — the staff CRM board's row feed.

The board now dates each card and shows who owns it, so this pins the two things
that are easy to break silently: the estimator join (which must survive a trashed
draft) and the milestone timestamps (which must serialize as ISO, not datetimes).
Asserted against the SQL because there is no DB in unit tests — the live query is
covered by the staging smoke.
"""
import datetime as dt

import pytest

import main


# ── the query ────────────────────────────────────────────────────────────────
def test_pipeline_query_joins_the_estimator_off_the_draft(monkeypatch):
    """`drafts.owner_email` is the only field that exists for every proposal —
    it is written once at first save and never overwritten. `published_by` is the
    fallback for rows predating that plumbing."""
    seen = {}
    monkeypatch.setattr(main.db, "qall",
                        lambda sql, params=(): seen.update(sql=sql) or [])
    main.db.list_all_portal_proposals()
    sql = " ".join(seen["sql"].split())
    assert "left join public.drafts d on d.id = p.proposal_id" in sql
    assert "coalesce(d.owner_email, p.published_by) as estimator_email" in sql


def test_pipeline_query_keeps_proposals_whose_draft_was_trashed(monkeypatch):
    """Binning the draft does not retract the proposal the customer already has.
    An inner join, or a `deleted_at is null` filter, would drop the estimator off
    exactly those rows — or drop the rows entirely."""
    seen = {}
    monkeypatch.setattr(main.db, "qall",
                        lambda sql, params=(): seen.update(sql=sql) or [])
    main.db.list_all_portal_proposals()
    sql = " ".join(seen["sql"].split())
    assert "left join" in sql
    assert "deleted_at" not in sql


def test_pipeline_query_selects_every_milestone_the_board_dates_by(monkeypatch):
    seen = {}
    monkeypatch.setattr(main.db, "qall",
                        lambda sql, params=(): seen.update(sql=sql) or [])
    main.db.list_all_portal_proposals()
    select = " ".join(seen["sql"].split()).split(" from ")[0]
    for col in ("p.created_at", "p.viewed_at", "p.approved_at", "p.deposit_requested_at"):
        assert col in select, col


# ── _iso (pure) ──────────────────────────────────────────────────────────────
def test_iso_serializes_datetimes_and_passes_strings_through():
    assert main._iso(dt.datetime(2026, 7, 29, 9, 5, 0)).startswith("2026-07-29T09:05:00")
    assert main._iso("2026-07-29T09:05:00+00:00") == "2026-07-29T09:05:00+00:00"
    assert main._iso(None) is None
    assert main._iso("") is None


# ── the serializer ───────────────────────────────────────────────────────────
def _row(**over):
    row = {"proposal_id": "p1", "token": "tok", "customer_email": "rita@acme.com",
           "customer_name": "Rita", "project_name": "Westport", "proposal_status": "viewed",
           "deposit_status": "pending", "contacts_status": "pending", "schedule_status": "pending",
           "approved_total": None, "deposit_amount": None,
           "created_at": dt.datetime(2026, 7, 20, 14, 0, 0),
           "viewed_at": dt.datetime(2026, 7, 22, 9, 30, 0),
           "approved_at": None, "deposit_requested_at": None,
           "estimator_email": "kyle@wetreadwell.com"}
    row.update(over)
    return row


@pytest.fixture
def pipeline(monkeypatch):
    """Return a caller that runs admin_pipeline over `rows` with auth satisfied."""
    def run(rows, unread=None):
        monkeypatch.setattr(main, "_admin_ok", lambda request: True)
        monkeypatch.setattr(main.db, "list_all_portal_proposals", lambda: rows)
        monkeypatch.setattr(main.db, "unread_counts", lambda: unread or {})
        import json
        resp = main.admin_pipeline(request=None)
        return json.loads(bytes(resp.body).decode())["proposals"]
    return run


def test_card_carries_the_estimator_and_its_milestones(pipeline):
    card, = pipeline([_row()])
    assert card["estimator_email"] == "kyle@wetreadwell.com"
    assert card["sent_at"].startswith("2026-07-20T14:00:00")     # created_at IS sent-at
    assert card["viewed_at"].startswith("2026-07-22T09:30:00")
    assert card["approved_at"] is None
    assert card["deposit_requested_at"] is None


def test_card_survives_a_missing_estimator(pipeline):
    """Neither the draft nor published_by resolved — the board shows a dash rather
    than breaking."""
    card, = pipeline([_row(estimator_email=None)])
    assert card["estimator_email"] is None
    assert card["sent_at"]                                        # still dateable


def test_existing_card_keys_are_unchanged(pipeline):
    """leads.js, the bell and stageOf() all read this shape. Adding fields must
    not rename or drop one."""
    card, = pipeline([_row(approved_total=27653, deposit_amount=6913.25)], unread={"p1": 3})
    for k in ("proposal_id", "token", "customer_email", "customer_name", "project_name",
              "proposal_status", "deposit_status", "schedule_status", "contacts_status",
              "approved_total", "deposit_amount", "deposit_required", "unread"):
        assert k in card, k
    assert card["approved_total"] == 27653.0 and card["deposit_amount"] == 6913.25
    assert card["unread"] == 3


def test_deposit_required_defaults_true_for_legacy_rows(pipeline):
    """The board must not treat a pre-column row as "no deposit" — that would slide
    it past the deposit stages it genuinely still needs."""
    card, = pipeline([_row()])
    assert card["deposit_required"] is True
    off, = pipeline([_row(deposit_required=False)])
    assert off["deposit_required"] is False
