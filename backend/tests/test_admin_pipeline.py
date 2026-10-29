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
    """The ASSIGNED estimator owns the proposal — they get the follow-up notes and the
    daily digest, so they must win. `drafts.owner_email` is the fallback that exists
    for every proposal (written once at first save, never overwritten), and
    `published_by` covers rows predating that plumbing.

    Ordering matters: reassigning a proposal has to change who is chased, and reading
    owner_email first would silently keep pointing at whoever created the project."""
    seen = {}
    monkeypatch.setattr(main.db, "qall",
                        lambda sql, params=(): seen.update(sql=sql) or [])
    main.db.list_all_portal_proposals()
    sql = " ".join(seen["sql"].split())
    assert "left join public.drafts d on d.id = p.proposal_id" in sql
    assert ("coalesce(p.assigned_estimator, d.owner_email, p.published_by) "
            "as estimator_email") in sql


def test_pipeline_query_keeps_proposals_whose_draft_was_trashed(monkeypatch):
    """Binning the draft does not retract the proposal the customer already has.
    An inner join, or a filter on the DRAFT's deleted_at, would drop the estimator off
    exactly those rows — or drop the rows entirely.

    This used to assert `"deleted_at" not in sql`, full stop, and that stopped being the right
    claim on 2026-08-24 when the proposal row grew a deleted_at of its own (the drawer's Delete
    project button). The two columns have the same NAME and opposite meanings here: OURS is
    filtered, because a deleted project must leave the board; the DRAFT's is not, for the reason
    above. So the assertion is now about which table's column appears, which is what it always
    meant."""
    seen = {}
    monkeypatch.setattr(main.db, "qall",
                        lambda sql, params=(): seen.update(sql=sql) or [])
    main.db.list_all_portal_proposals()
    sql = " ".join(seen["sql"].split())
    assert "left join" in sql
    assert "d.deleted_at" not in sql, "the draft's deleted_at is being filtered again"
    # Not even spelled without the alias: `deleted_at is null` unqualified would be ambiguous
    # across the join, and it is the shape somebody re-adding the old bug would write.
    assert "'deleted_at') is null" in sql, "the PORTAL's own deleted_at must be filtered"


def test_pipeline_query_selects_every_milestone_the_board_dates_by(monkeypatch):
    seen = {}
    monkeypatch.setattr(main.db, "qall",
                        lambda sql, params=(): seen.update(sql=sql) or [])
    main.db.list_all_portal_proposals()
    select = " ".join(seen["sql"].split()).split(" from ")[0]
    for col in ("p.created_at", "p.viewed_at", "p.approved_at", "p.deposit_requested_at"):
        assert col in select, col


def test_bookkeeping_does_not_count_as_having_chased_the_customer(monkeypatch):
    """`staff_note` carries two different things: a note an estimator typed, and the
    system's own record of a reassignment or an automation toggle. Only the first is
    outreach.

    Counting both meant merely switching a proposal's automation ON logged it as
    "somebody chased this" — dropping it out of the next two mornings' digests and
    backdating the board's last-activity to an admin click. Found on staging, where
    assigning an estimator made the proposal vanish from the digest it had just
    become eligible for."""
    seen = {}
    monkeypatch.setattr(main.db, "qall",
                        lambda sql, params=(): seen.update(sql=sql) or [])
    main.db.list_all_portal_proposals()
    sql = " ".join(seen["sql"].split())
    assert "last_staff_followup_at" in sql
    assert "f.detail->>'action' is null" in sql


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
        # The cadence read. Unstubbed it waits out the full 30s connection-pool timeout before
        # falling back to the shipped defaults — correct, but it made these nine tests 150
        # seconds, and one that calls the pipeline twice took 60 on its own. Stubbed HERE rather
        # than in conftest because test_followup_settings has a test that deliberately makes this
        # read fail, and an autouse stub would silently override its setup.
        monkeypatch.setattr(main.db, "get_settings", lambda key: None)
        import json
        resp = main.admin_pipeline(request=None)
        return json.loads(bytes(resp.body).decode())["proposals"]
    return run


def test_card_carries_the_estimator_and_its_milestones(pipeline):
    card, = pipeline([_row()])
    assert card["estimator_email"] == "kyle@wetreadwell.com"
    # Never re-sent, so the first send stands: created_at is the fallback.
    assert card["sent_at"].startswith("2026-07-20T14:00:00")
    assert card["viewed_at"].startswith("2026-07-22T09:30:00")
    assert card["approved_at"] is None
    assert card["deposit_requested_at"] is None


def test_a_resent_card_shows_the_RESEND_date(pipeline):
    """Hanz re-sent a revision and the card kept its original date, so "Sent" on the board could
    mean "sent three weeks ago" for a proposal that went out ten minutes earlier. created_at
    records the FIRST send and never moves; last_sent_at moves with every publish and wins."""
    card, = pipeline([_row(last_sent_at=dt.datetime(2026, 8, 13, 21, 40, 0))])
    assert card["sent_at"].startswith("2026-08-13T21:40:00")


def test_a_row_without_the_column_falls_back(pipeline):
    """Prod applies its DDL by hand, so this code can arrive before the column does. Until it
    lands every row reads as never-re-sent rather than as never-sent."""
    row = _row()
    row.pop("last_sent_at", None)
    card, = pipeline([row])
    assert card["sent_at"].startswith("2026-07-20T14:00:00")


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


# ── the next follow-up date (remind-only, 2026-10-23) ────────────────────────
# THE BUG: the board's "next follow-up" is followup_rules.next_due_at over the pipeline row, and the
# pipeline query never selected cycle_viewed_at. For every VIEWED proposal next_due_at therefore saw
# no view and computed the not-viewed track's date — anchored on the send, not the view, on the
# wrong step of the ladder. The worker reads `select *`, so it acted on a different schedule from
# the one the board showed.
import followup_rules as _fr

_ALL_DAY = {"send_start_hour": 0, "send_end_hour": 24}


def test_the_board_query_selects_every_field_the_schedule_reads(monkeypatch):
    """The regression for the omission itself, and for the next one: every key next_due_at reads
    (followup_rules.INPUT_KEYS) must come out of this SELECT, as a column or an alias."""
    seen = {}
    monkeypatch.setattr(main.db, "qall", lambda sql, params=(): seen.update(sql=sql) or [])
    main.db.list_all_portal_proposals()
    flat = " ".join(seen["sql"].split())
    select = flat[:flat.rindex(" from public.portal_proposals p ")]
    missing = [k for k in _fr.INPUT_KEYS
               if ("p.%s," % k) not in select and ("p.%s " % k) not in select
               and (" as %s," % k) not in select and (" as %s " % k) not in select
               and not select.endswith(" as %s" % k)]
    assert not missing, "the board computes next_due_at without: %s" % missing


def test_a_viewed_proposal_shows_the_viewed_tracks_date(pipeline):
    now = dt.datetime.now(dt.timezone.utc)
    viewed = now - dt.timedelta(hours=2)
    sent = now - dt.timedelta(hours=100)
    nv1 = sent + dt.timedelta(days=3)             # the not-opened 1st already went
    row = _row(proposal_status="viewed", followup_enrolled_at=sent,
               cycle_viewed_at=viewed, viewed_at=viewed, created_at=sent,
               followup_sent_history=[{"at": nv1.isoformat(), "template": "not_viewed",
                                       "rule_key": "x"}])
    card, = pipeline([row])
    got = dt.datetime.fromisoformat(card["next_followup_at"])
    # Pinned against due_now's OWN math, not against next_due_at: nothing is due a minute before
    # the date the board shows, and the viewed track's first step (v1) is due a minute after.
    before = [d for d in _fr.due_now(row, got - dt.timedelta(minutes=1), _ALL_DAY)
              if d.audience == "customer"]
    after = [d for d in _fr.due_now(row, got + dt.timedelta(minutes=1), _ALL_DAY)
             if d.audience == "customer"]
    assert before == [] and [d.rule_key.split(":")[-1] for d in after] == ["v1"], (got, after)
    # The opened step's 1st email (2026-10-29): 3 days after it was SENT, and never sooner than 3
    # days after the customer's last follow-up email (the not-opened 1st here).
    assert got == nv1 + dt.timedelta(days=3)
    # And the bug's computation — the same row read as never opened — really was different (the
    # not-opened 2nd, 7 days after nv1), so this test would have caught it. Since 2026-10-29 the
    # opened step counts from the send too, so only reading the row as NOT opened moves the date:
    # a 'viewed' status with no stamp is still opened (dated by the send).
    blind = dict(row, proposal_status="sent", cycle_viewed_at=None, viewed_at=None,
                 last_viewed_at=None)
    assert _fr.next_due_at(blind, now) == nv1 + dt.timedelta(days=7) != got


def test_the_card_carries_the_open_task_and_the_plan(pipeline):
    task = {"id": 12, "created_at": "2026-10-20T15:00:00+00:00",
            "detail": {"status": "pending", "template": "checkin", "stage": "pre"}}
    card, = pipeline([_row(pending_task=task, followup_next_at="2026-11-02T13:00:00+00:00")])
    assert card["followup_task"] == {"id": 12, "status": "pending", "template": "checkin",
                                     "label": "Recurring check-in", "stage": "pre",
                                     "due_at": "2026-10-20T15:00:00+00:00"}
    # "Next email on" only (2026-10-28); the worker emails the customer itself ('auto').
    assert card["followup_plan"]["next_at"] == "2026-11-02T13:00:00+00:00"
    assert "interval_days" not in card["followup_plan"]
    assert card["followup_plan"]["mode"] == "auto"
    none, = pipeline([_row()])
    assert none["followup_task"] is None
