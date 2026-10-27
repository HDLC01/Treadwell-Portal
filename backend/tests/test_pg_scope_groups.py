"""The scope-group and remind-only SQL, EXECUTED against a real Postgres.

Every other test of these functions replaces the database (scope_send_fake.py, monkeypatched db.*),
so until this file none of the new SQL had ever run: a jsonb operator, a cast, a column name or an
ON CONFLICT that does not match a PARTIAL unique index is exactly what looks right in review and is
rejected by Postgres. Here schema.sql is applied through db.run_script (the boot path's own
splitter) to a scratch database, realistic rows are written, and the db.py functions run unmocked.

HOW TO RUN (a throwaway local Postgres; nothing real is ever touched):

    docker run -d --name tw-pg-p3-test -e POSTGRES_PASSWORD=test \
        -p 127.0.0.1:55432:5432 postgres:15-alpine
    # wait for: docker exec tw-pg-p3-test pg_isready -U postgres
    cd backend
    PORTAL_TEST_PG_URL=postgresql://postgres:test@127.0.0.1:55432/postgres \
        .venv/Scripts/python.exe -m pytest tests/test_pg_scope_groups.py -q
    docker rm -f tw-pg-p3-test

PowerShell: `$env:PORTAL_TEST_PG_URL = "postgresql://postgres:test@127.0.0.1:55432/postgres"`.

Without PORTAL_TEST_PG_URL every test here is SKIPPED, so the default suite stays fast. The URL must
be localhost; each test module creates its own `tw_p3_*` database and drops it afterwards
(tests/pg_fixture.py). Written against Postgres 15.
"""
from __future__ import annotations

import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

import pg_fixture
from pg_fixture import needs_pg

pytestmark = [needs_pg, pytest.mark.realdb, pytest.mark.realwording]

if pg_fixture.PG_URL:  # imported only when the tests will run
    import psycopg
    from psycopg.types.json import Jsonb

    import db
    import followup_rules as rules
    import followup_worker
    import scope_groups


# ── fixtures ──────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def pgdb():
    """One scratch database for the module, schema applied once."""
    with pg_fixture.scratch_portal_db("mod") as pool:
        yield pool


@pytest.fixture
def clean(pgdb):
    """Every test starts from empty portal tables and an empty drafts stub."""
    db.execute("truncate public.portal_proposals, public.drafts, public.draft_revisions, "
               "public.portal_settings restart identity cascade")
    yield


@pytest.fixture
def fresh_db():
    """A database of its own, for tests that change the schema itself."""
    with pg_fixture.scratch_portal_db("ddl") as pool:
        yield pool


# ── row helpers ───────────────────────────────────────────────────────────────
def db_now() -> datetime:
    return db.q1("select now() as n")["n"]


def ago(days: float) -> datetime:
    return db_now() - timedelta(days=days)


def in_window() -> datetime:
    """The latest noon (Central) at or before the database's now. followup_rules.due_now drops every
    CUSTOMER reminder outside the 8am-6pm Central send window, so a test that expects a task to be
    due must not depend on the hour the suite happens to run at. Rows here are dated days back, so
    a tick up to 24 hours before now still finds them due."""
    local = db_now().astimezone(rules.BUSINESS_TZ)
    noon = local.replace(hour=12, minute=0, second=0, microsecond=0)
    if noon > local:
        noon -= timedelta(days=1)
    return noon.astimezone(timezone.utc)


def gid_new() -> str:
    return "g-" + uuid.uuid4().hex[:8]


def add_draft(did, gid=None, label=None, *, archived=None, is_test=None, name="Acme HQ",
              owner="kyle@estimator.example", deleted=False):
    data = {"project_name": name}
    if gid:
        data["scope_group"] = {"id": gid, "label": label}
    if archived is not None:
        data["archived"] = archived          # the tool writes a JSON boolean (drafts.set_archived)
    if is_test is not None:
        data["is_test"] = is_test
    db.execute("insert into public.drafts (id, data, owner_email, deleted_at) "
               "values (%s, %s, %s, case when %s then now() else null end)",
               (did, Jsonb(data), owner, bool(deleted)))


def set_cols(pid, **cols):
    if not cols:
        return
    names = list(cols)
    db.execute("update public.portal_proposals set "
               + ", ".join("%s = %%s" % n for n in names) + " where proposal_id = %s",
               tuple(cols[n] for n in names) + (pid,))


def add_proposal(pid, gid=None, label=None, *, status="sent", days_ago=10.0, enrolled=10.0,
                 rev=1, sent=True, email=None, **cols):
    """A published proposal through the real insert (db.create_portal_proposal), then dated."""
    db.create_portal_proposal(pid, "tok-" + pid, email or f"{pid}@customer.example",
                              "Dana Customer", "Acme HQ", None, "pub@estimator.example",
                              True, rev, gid, label)
    db.add_recipient(pid, email or f"{pid}@customer.example")
    when = ago(days_ago)
    base = {"created_at": when, "proposal_status": status}
    if enrolled is not None:
        base["followup_enrolled_at"] = ago(enrolled)
    if sent:
        base["last_sent_at"] = when
    base.update(cols)
    set_cols(pid, **base)


def add_followup(pid, kind, detail, *, days_ago=0.0, by=None) -> int:
    row = db.q1("insert into public.portal_followups (proposal_id, kind, detail, created_by, "
                "created_at) values (%s, %s, %s, %s, now() - make_interval(secs => %s)) "
                "returning id", (pid, kind, Jsonb(detail), by, float(days_ago) * 86400))
    return int(row["id"])


def task(pid, status="pending", stage=None, key=None, **extra) -> dict:
    d = {"audience": "customer", "template": "followup_not_viewed", "status": status,
         "rule_key": key or ("k-" + uuid.uuid4().hex[:8])}
    if stage is not None:
        d["stage"] = stage
    d.update(extra)
    return d


def group_of_three(*, sent=False, archived_b=False, deleted_c=False, rev=1):
    """A 3-scope project: A (Breakroom), B (Lab 1), C (Lab 2), published in that order."""
    gid = gid_new()
    ids = [f"{gid}-a", f"{gid}-b", f"{gid}-c"]
    for i, (pid, label) in enumerate(zip(ids, ("Breakroom", "Lab 1", "Lab 2"))):
        add_draft(pid, gid, label, archived=(archived_b and i == 1) or None,
                  deleted=deleted_c and i == 2)
        add_proposal(pid, gid, label, days_ago=10 - i, sent=sent, rev=rev,
                     email=f"buyer{i}@customer.example")
    return gid, ids


def marks_for(gid):
    return db.list_scope_group_marks([gid])


def portal_rows(ids):
    return [db.q1("select proposal_id, current_revision_no, "
                  "(to_jsonb(p) ->> 'last_sent_at') as last_sent_at "
                  "from public.portal_proposals p where proposal_id = %s", (i,)) for i in ids]


def index_def(name):
    row = db.q1("select indexdef from pg_indexes where schemaname = 'public' and indexname = %s",
                (name,))
    return row["indexdef"] if row else None


# ══ a. schema.sql applies on an empty database, and again ═════════════════════
def test_schema_applies_on_an_empty_database_and_a_second_time(fresh_db):
    pg_fixture.apply_schema()                        # second application (fixture did the first)
    pg_fixture.apply_schema()                        # and a third: a reboot loop
    v2 = index_def("portal_followups_one_open_task_v2_uidx")
    assert v2 is not None
    assert "COALESCE((detail ->> 'stage'::text), 'pre'::text)" in v2
    assert "'pending'" in v2 and "'sending'" in v2 and "followup_task" in v2
    rule = index_def("portal_followups_rule_v2_uidx")
    assert rule is not None and "auto_email" in rule and "followup_task" in rule
    for gone in ("portal_followups_one_open_task_uidx", "portal_followups_one_pending_task_uidx",
                 "portal_followups_rule_uidx"):
        assert index_def(gone) is None, gone
    assert index_def("portal_proposals_scope_group_idx") is not None
    check = db.q1("select pg_get_constraintdef(oid) as d from pg_constraint "
                  "where conname = 'portal_followups_kind_check'")["d"]
    assert "followup_task" in check and "auto_email" in check
    cols = {r["column_name"] for r in db.qall(
        "select column_name from information_schema.columns "
        "where table_schema = 'public' and table_name = 'portal_proposals'")}
    assert {"scope_group", "scope_label", "followup_anchor_at", "followup_next_at",
            "followup_interval_days", "followup_stop_after", "followup_mode",
            "last_sent_at", "deleted_at"} <= cols
    assert db.scope_group_columns_ready() is True


def test_the_widened_kind_check_admits_a_task_and_still_refuses_junk(fresh_db):
    # A database from before remind-only: the original, narrow kind CHECK.
    db.execute("alter table public.portal_followups drop constraint portal_followups_kind_check")
    db.execute("alter table public.portal_followups add constraint portal_followups_kind_check "
               "check (kind in ('auto_email','staff_call','staff_email','staff_text','staff_note',"
               "'customer_status'))")
    add_proposal("p1")
    with pytest.raises(psycopg.errors.CheckViolation):
        add_followup("p1", "followup_task", task("p1"))
    pg_fixture.apply_schema()
    add_followup("p1", "followup_task", task("p1"))
    with pytest.raises(psycopg.errors.CheckViolation):
        add_followup("p1", "not_a_kind", {})


def test_reapplying_over_the_predecessor_swaps_the_open_task_index(fresh_db):
    """A database that has the REMIND-ONLY indexes (one open task per proposal, plus the even older
    pending-only one) and a narrow kind CHECK, with live rows: re-applying schema.sql builds the
    per-stage index, drops both predecessors, widens the CHECK and keeps the rows."""
    db.execute("drop index public.portal_followups_one_open_task_v2_uidx")
    db.execute("create unique index portal_followups_one_open_task_uidx "
               "on public.portal_followups (proposal_id) "
               "where kind = 'followup_task' and (detail->>'status') in ('pending','sending')")
    db.execute("create unique index portal_followups_one_pending_task_uidx "
               "on public.portal_followups (proposal_id) "
               "where kind = 'followup_task' and (detail->>'status') = 'pending'")
    add_proposal("p1", status="approved")
    add_followup("p1", "followup_task", task("p1", stage="deposit"))
    # Under the predecessor a decision task cannot join the open deposit task.
    with pytest.raises(psycopg.errors.UniqueViolation):
        add_followup("p1", "followup_task", task("p1", stage="decision"))

    pg_fixture.apply_schema()

    assert index_def("portal_followups_one_open_task_v2_uidx") is not None
    assert index_def("portal_followups_one_open_task_uidx") is None
    assert index_def("portal_followups_one_pending_task_uidx") is None
    assert db.q1("select count(*) as n from public.portal_followups")["n"] == 1
    add_followup("p1", "followup_task", task("p1", stage="decision"))   # now admitted


def test_the_new_index_is_created_before_the_old_one_is_dropped(fresh_db, monkeypatch):
    """The order run_script will send the statements in: the per-stage index exists before either
    predecessor is dropped, so there is no moment without a one-open-task guard (and a hand apply
    without ON_ERROR_STOP that fails the create keeps the old guard)."""
    seen = []

    class Conn:
        def execute(self, chunk):
            seen.append(" ".join(chunk.split()).lower())

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class Pool:
        def connection(self):
            return Conn()

    monkeypatch.setattr(db, "pool", lambda: Pool())
    pg_fixture.apply_schema()

    def at(fragment):
        hits = [i for i, c in enumerate(seen) if fragment in c]
        assert hits, fragment
        return hits[0]

    create_v2 = at("create unique index if not exists portal_followups_one_open_task_v2_uidx")
    assert create_v2 < at("drop index if exists public.portal_followups_one_open_task_uidx")
    assert create_v2 < at("drop index if exists public.portal_followups_one_pending_task_uidx")
    assert (at("create unique index if not exists portal_followups_rule_v2_uidx")
            < at("drop index if exists public.portal_followups_rule_uidx"))
    assert (at("add constraint portal_followups_kind_check")
            < create_v2), "the kind CHECK must admit followup_task before tasks are indexed"


# ══ b. DDL before code ════════════════════════════════════════════════════════
def test_without_the_scope_group_columns_the_board_and_the_worker_still_run(fresh_db):
    """Prod gets the code before an owner applies the ALTERs by hand. With the two scope columns
    absent: the capability says no, the board query and the candidate list run, the worker's
    partition and dry run work, and a GROUPED write fails loudly without leaving a row behind."""
    db.execute("alter table public.portal_proposals drop column scope_group, "
               "drop column scope_label")
    assert db.scope_group_columns_ready() is False

    add_draft("solo")
    add_proposal("solo", enrolled=30, days_ago=30)
    add_followup("solo", "followup_task", task("solo"), days_ago=1)

    board = db.list_all_portal_proposals()
    assert [r["proposal_id"] for r in board] == ["solo"]
    assert board[0]["scope_group"] is None and board[0]["scope_label"] is None
    assert board[0]["pending_task"]["detail"]["status"] == "pending"

    cands = db.list_followup_candidates()
    assert [r["proposal_id"] for r in cands] == ["solo"]
    singles, groups = followup_worker._partition(cands)
    assert [r["proposal_id"] for r in singles] == ["solo"] and groups == []
    assert db.followup_signals("solo")["pending_task"] is not None
    out = followup_worker.dry_run_count()
    assert out["skipped"] == []

    with pytest.raises(psycopg.errors.UndefinedColumn):
        db.create_portal_proposal("grouped", "tok-grouped", "x@customer.example", "X", "Acme",
                                  None, "pub@estimator.example", True, 1, "g-1", "Lab 1")
    assert db.get_proposal("grouped") is None
    set_cols("solo", last_sent_at=ago(1))
    with pytest.raises(psycopg.errors.UndefinedColumn):
        db.adopt_into_scope_group("solo", "g-1", "Lab 1")
    assert db.q1("select count(*) as n from public.portal_followups "
                 "where kind = 'staff_note'")["n"] == 0, "adopt wrote a mark before failing"

    pg_fixture.apply_schema()                        # the owner applies the DDL
    assert db.scope_group_columns_ready() is True
    db.adopt_into_scope_group("solo", "g-1", "Lab 1")
    assert db.get_proposal("solo")["scope_group"] == "g-1"


# ══ c/e. the combined send: claim, settle, retry, adopt ═══════════════════════
def test_claim_marks_every_pending_scope_once_and_a_second_claim_is_refused(clean):
    gid, ids = group_of_three()
    claim = db.claim_scope_group_send(gid, ids, by="kyle@estimator.example")
    assert claim["send_no"] == 1 and claim["first"] is True
    assert claim["pending"] == ids
    assert claim["kinds"] == {i: "first" for i in ids}
    assert claim["revisions"] == {i: 1 for i in ids}
    marks = marks_for(gid)
    assert sorted(m["proposal_id"] for m in marks) == sorted(ids)
    for m in marks:
        d = m["detail"]
        assert d["action"] == "scope_group_sent" and d["scope_group"] == gid
        assert d["send_no"] == 1 and d["revision_no"] == 1 and d["kind"] == "first"
        assert d["scopes"] == ids
        assert scope_groups.mark_status(m, db_now()) == "inflight"
    assert db.claim_scope_group_send(gid, ids) is None            # the double click
    # Marks of another group never leak into this one.
    other, other_ids = group_of_three()
    assert db.claim_scope_group_send(other, other_ids)["send_no"] == 1
    assert {m["proposal_id"] for m in marks_for(gid)} == set(ids)


def test_settle_keeps_delivered_scopes_and_frees_the_rest_for_the_next_send(clean):
    gid, ids = group_of_three()
    a, b, c = ids
    claim = db.claim_scope_group_send(gid, ids)
    db.mark_scope_group_attempt(gid, claim["send_no"], {
        a: {"note": "Hi Dana", "files": [{"id": "f1", "name": "plan.pdf"}]},
        b: {"note": None, "files": []}, c: {"note": None, "files": []}})
    db.settle_scope_group_send(gid, claim["send_no"], {
        a: {"delivered": ["buyer0@customer.example"], "failed": ["cc@customer.example"]},
        b: {"delivered": ["buyer1@customer.example"], "failed": []},
        c: {"delivered": [], "failed": ["buyer2@customer.example"]}})

    marks = marks_for(gid)
    assert sorted(m["proposal_id"] for m in marks) == [a, b]
    by = {m["proposal_id"]: m["detail"] for m in marks}
    assert by[a]["delivery"] == {"delivered": ["buyer0@customer.example"],
                                 "failed": ["cc@customer.example"]}
    assert by[a]["note"] == "Hi Dana" and by[a]["files"][0]["id"] == "f1"
    assert by[a]["attempt_at"]
    now = db_now()
    states = scope_groups.scope_states(portal_rows(ids), marks, now)
    assert states[a]["announced"] and states[b]["announced"]
    assert not states[c]["announced"] and not states[c]["ever"]

    plan = scope_groups.plan_send(portal_rows(ids), marks, now)
    assert plan["pending"] == [c] and plan["kinds"] == {c: "new"} and plan["first"] is False
    again = db.claim_scope_group_send(gid, ids)
    assert again["send_no"] == 2 and again["pending"] == [c] and again["kinds"] == {c: "new"}


def test_a_revision_after_the_email_is_claimed_as_revised(clean):
    gid, ids = group_of_three()
    claim = db.claim_scope_group_send(gid, ids)
    db.settle_scope_group_send(gid, claim["send_no"], {
        i: {"delivered": ["buyer@customer.example"], "failed": []} for i in ids})
    assert db.claim_scope_group_send(gid, ids) is None
    set_cols(ids[1], current_revision_no=2)
    nxt = db.claim_scope_group_send(gid, ids)
    assert nxt["pending"] == [ids[1]] and nxt["kinds"] == {ids[1]: "revised"}
    assert nxt["revisions"] == {ids[1]: 2} and nxt["send_no"] == 2


def test_release_undoes_a_claim_and_the_scopes_can_be_claimed_again(clean):
    gid, ids = group_of_three()
    claim = db.claim_scope_group_send(gid, ids)
    db.release_scope_group_send(gid, claim["send_no"])
    assert marks_for(gid) == []
    assert db.claim_scope_group_send(gid, ids)["pending"] == ids


def test_a_lost_result_after_the_attempt_is_unknown_and_only_a_person_resends_it(clean):
    gid, ids = group_of_three()
    claim = db.claim_scope_group_send(gid, ids)
    db.mark_scope_group_attempt(gid, claim["send_no"], {i: {} for i in ids})
    # Eleven minutes later, no result was ever recorded.
    db.execute("update public.portal_followups set created_at = now() - interval '11 minutes' "
               "where kind = 'staff_note'")
    now = db_now()
    assert {scope_groups.mark_status(m, now) for m in marks_for(gid)} == {"unknown"}
    assert db.claim_scope_group_send(gid, ids) is None
    forced = db.claim_scope_group_send(gid, ids, include_unknown=True)
    assert forced["pending"] == ids and forced["send_no"] == 2


def test_a_claim_that_died_before_emailing_expires_and_the_scope_is_pending_again(clean):
    gid, ids = group_of_three()
    db.claim_scope_group_send(gid, ids)
    db.execute("update public.portal_followups set created_at = now() - interval '11 minutes' "
               "where kind = 'staff_note'")
    again = db.claim_scope_group_send(gid, ids)
    assert again["pending"] == ids and again["send_no"] == 2


def test_retry_claims_only_the_failed_addresses_once_and_settles(clean):
    gid, ids = group_of_three()
    a, b, c = ids
    claim = db.claim_scope_group_send(gid, ids)
    db.mark_scope_group_attempt(gid, claim["send_no"], {a: {"note": "Hi", "files": []}})
    db.settle_scope_group_send(gid, claim["send_no"], {
        a: {"delivered": ["buyer0@customer.example"], "failed": ["cc@customer.example"]},
        b: {"delivered": ["buyer1@customer.example"], "failed": []},
        c: {"delivered": ["buyer2@customer.example"], "failed": []}})

    targets = db.claim_scope_group_retry(gid, ids)
    assert list(targets) == [a]
    t = targets[a]
    assert t["failed"] == ["cc@customer.example"]
    assert t["delivered"] == ["buyer0@customer.example"] and t["kind"] == "first"
    assert t["note"] == "Hi"
    mark = next(m for m in marks_for(gid) if m["proposal_id"] == a)
    assert t["mark_id"] == mark["id"] and mark["detail"]["retry_at"]
    assert db.claim_scope_group_retry(gid, ids) == {}             # a second retry: busy

    db.settle_scope_group_retry({t["mark_id"]: {
        "delivered": ["buyer0@customer.example", "cc@customer.example"], "failed": []}})
    mark = next(m for m in marks_for(gid) if m["proposal_id"] == a)
    assert "retry_at" not in mark["detail"]
    assert mark["detail"]["delivery"] == {
        "delivered": ["buyer0@customer.example", "cc@customer.example"], "failed": []}
    assert mark["detail"]["send_no"] == 1 and mark["detail"]["note"] == "Hi"
    assert db.claim_scope_group_retry(gid, ids) == {}
    assert scope_groups.retry_targets(portal_rows(ids), marks_for(gid), db_now()) == {}


def test_adopt_marks_a_scope_that_was_already_emailed_once(clean):
    gid = gid_new()
    add_draft("sent-before", gid, "Breakroom")
    add_proposal("sent-before", rev=3)                  # an ordinary proposal, emailed at rev 3
    add_draft("never-sent", gid, "Lab 1")
    add_proposal("never-sent", sent=False)
    db.adopt_into_scope_group("sent-before", gid, "Breakroom")
    db.adopt_into_scope_group("sent-before", gid, "Breakroom")       # a retried publish
    db.adopt_into_scope_group("never-sent", gid, "Lab 1")

    row = db.get_proposal("sent-before")
    assert row["scope_group"] == gid and row["scope_label"] == "Breakroom"
    marks = marks_for(gid)
    assert len(marks) == 1 and marks[0]["proposal_id"] == "sent-before"
    d = marks[0]["detail"]
    assert d["adopted"] is True and d["revision_no"] == 3 and d["send_no"] == 0
    assert marks[0]["id"] and scope_groups.mark_status(marks[0], db_now()) == "counts"
    # The next combined send names only the scope the customer has never had, as NEW, and the
    # adopted mark's send_no 0 does not confuse the numbering.
    claim = db.claim_scope_group_send(gid, ["sent-before", "never-sent"])
    assert claim["pending"] == ["never-sent"] and claim["kinds"] == {"never-sent": "new"}
    assert claim["send_no"] == 1


def test_stashed_publish_notes_round_trip(clean):
    gid, ids = group_of_three(rev=2)
    db.stash_scope_group_note(ids[0], gid, 1, "old note", [{"id": "old"}], by="k")
    db.stash_scope_group_note(ids[0], gid, 2, "current note", [{"id": "f2", "name": "x.pdf"}])
    db.stash_scope_group_note(ids[1], gid, 2, None, [])
    stashes = db.list_scope_group_notes(gid, ids)
    assert len(stashes) == 3 and all(s["detail"]["action"] == "scope_group_note" for s in stashes)
    extras = scope_groups.stashed_extras(portal_rows(ids), stashes)
    assert extras == {ids[0]: {"note": "current note", "files": [{"id": "f2", "name": "x.pdf"}]}}
    assert marks_for(gid) == [], "a stash must never read as a send mark"


# ── a SECOND send in the same group: the send_no filter is what protects the first ──
# Every test above settles, releases or stamps a group's ONLY send, so there is no older mark for
# the `(detail ->> 'send_no')::int = %s` filter to protect. Here send 1 announced all three scopes,
# Lab 1 is revised, and send 2 is about Lab 1 alone: whatever happens to send 2 must leave send 1's
# marks exactly as they were, or Lab 1 (or the whole project) reads as never announced and the next
# email tells the customer it is NEW.
def _announce_everything(gid, ids):
    claim = db.claim_scope_group_send(gid, ids)
    assert claim["send_no"] == 1
    db.mark_scope_group_attempt(gid, 1, {i: {"note": "first note", "files": [{"id": "f1"}]}
                                         for i in ids})
    db.settle_scope_group_send(gid, 1, {i: {"delivered": [f"buyer{n}@customer.example"],
                                            "failed": []} for n, i in enumerate(ids)})
    return {m["proposal_id"]: m for m in marks_for(gid)}


def _revise_and_claim(gid, ids, pid):
    set_cols(pid, current_revision_no=2)
    claim = db.claim_scope_group_send(gid, ids)
    assert claim["send_no"] == 2 and claim["pending"] == [pid]
    assert claim["kinds"] == {pid: "revised"}
    return claim


def _send_one_marks(gid):
    return {m["id"]: m for m in marks_for(gid) if m["detail"]["send_no"] == 1}


def test_settling_a_second_send_that_reached_nobody_keeps_the_first_sends_marks(clean):
    gid, ids = group_of_three()
    a, b, c = ids
    first = _announce_everything(gid, ids)
    _revise_and_claim(gid, ids, b)
    db.mark_scope_group_attempt(gid, 2, {b: {"note": "revision note", "files": []}})
    db.settle_scope_group_send(gid, 2, {b: {"delivered": [], "failed": ["buyer1@customer.example"]}})

    assert _send_one_marks(gid) == {m["id"]: m for m in first.values()}
    assert len(marks_for(gid)) == 3
    # Lab 1 is still announced at revision 1, so the next email calls revision 2 REVISED, not new.
    again = db.claim_scope_group_send(gid, ids)
    assert again["pending"] == [b] and again["kinds"] == {b: "revised"}


def test_settling_a_second_send_that_was_delivered_writes_only_its_own_mark(clean):
    gid, ids = group_of_three()
    a, b, c = ids
    first = _announce_everything(gid, ids)
    _revise_and_claim(gid, ids, b)
    db.settle_scope_group_send(gid, 2, {b: {"delivered": ["new-buyer@customer.example"],
                                            "failed": []}})
    assert _send_one_marks(gid) == {m["id"]: m for m in first.values()}
    second = [m for m in marks_for(gid) if m["detail"]["send_no"] == 2]
    assert [m["proposal_id"] for m in second] == [b]
    assert second[0]["detail"]["delivery"]["delivered"] == ["new-buyer@customer.example"]
    assert db.claim_scope_group_send(gid, ids) is None


def test_releasing_a_second_send_leaves_the_first_sends_marks(clean):
    gid, ids = group_of_three()
    a, b, c = ids
    first = _announce_everything(gid, ids)
    _revise_and_claim(gid, ids, b)
    db.release_scope_group_send(gid, 2)

    assert {m["id"] for m in marks_for(gid)} == {m["id"] for m in first.values()}
    again = db.claim_scope_group_send(gid, ids)
    assert again["pending"] == [b] and again["kinds"] == {b: "revised"}
    assert again["first"] is False


def test_a_claim_after_the_announced_scope_closed_is_not_a_first_send(clean):
    """PF4 review: the claim read only the claimed scopes' marks, so with Breakroom (the only
    scope ever emailed) closed lost, the send of Lab 1 was planned as a FIRST send again."""
    gid, ids = group_of_three()
    a, b, c = ids
    _announce_everything(gid, [a])
    set_cols(a, proposal_status="closed_lost")
    claim = db.claim_scope_group_send(gid, [b])
    assert claim["pending"] == [b]
    assert claim["first"] is False and claim["kinds"] == {b: "new"}
    assert db.claim_scope_group_send(gid, [b]) is None, "the wider read changed what is pending"


def test_stamping_a_second_send_does_not_touch_the_first_sends_mark(clean):
    gid, ids = group_of_three()
    a, b, c = ids
    first = _announce_everything(gid, ids)
    _revise_and_claim(gid, ids, b)
    db.mark_scope_group_attempt(gid, 2, {b: {"note": "revision note",
                                             "files": [{"id": "f9", "name": "rev2.pdf"}]}})

    # Send 1's mark on Lab 1 keeps its own note, files and attempt time: a retry of send 1's
    # failed addresses must carry what send 1 carried.
    assert _send_one_marks(gid) == {m["id"]: m for m in first.values()}
    second = next(m for m in marks_for(gid) if m["detail"]["send_no"] == 2)
    assert second["proposal_id"] == b and second["detail"]["note"] == "revision note"
    assert second["detail"]["files"] == [{"id": "f9", "name": "rev2.pdf"}]


# ══ e. concurrency: two real connections ══════════════════════════════════════
def _race(fn, n=2):
    barrier = threading.Barrier(n)
    out, errors = [None] * n, []

    def run(i):
        try:
            barrier.wait(timeout=10)
            out[i] = fn()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors, errors
    return out


def test_two_simultaneous_claims_exactly_one_wins(clean):
    for _ in range(6):
        gid, ids = group_of_three()
        results = _race(lambda: db.claim_scope_group_send(gid, ids))
        winners = [r for r in results if r]
        assert len(winners) == 1, results
        assert winners[0]["pending"] == ids
        assert len(marks_for(gid)) == 3


def test_the_second_claim_waits_for_the_first_to_commit(clean, monkeypatch):
    """Deterministic: the first claim holds its row lock while it plans (made slow here). The second
    must block on the lock and then see the first's marks. Without the `for update` it would plan
    from no marks and claim every scope a second time."""
    gid, ids = group_of_three()
    real = scope_groups.plan_send
    planning = threading.Event()

    def slow_first(*a, **k):
        plan = real(*a, **k)
        if threading.current_thread().name == "first":
            planning.set()
            time.sleep(0.8)
        return plan

    monkeypatch.setattr(scope_groups, "plan_send", slow_first)
    results = {}

    def go(name):
        results[name] = db.claim_scope_group_send(gid, ids)

    first = threading.Thread(target=go, args=("first",), name="first")
    first.start()
    assert planning.wait(10)
    second = threading.Thread(target=go, args=("second",), name="second")
    second.start()
    first.join(30)
    second.join(30)
    assert results["first"] and results["first"]["pending"] == ids
    assert results["second"] is None
    assert len(marks_for(gid)) == 3


def test_two_simultaneous_retries_mail_the_failed_address_once(clean):
    gid, ids = group_of_three()
    claim = db.claim_scope_group_send(gid, ids)
    db.settle_scope_group_send(gid, claim["send_no"], {
        i: {"delivered": ["ok@customer.example"], "failed": ["bounce@customer.example"]}
        for i in ids})
    results = _race(lambda: db.claim_scope_group_retry(gid, ids))
    assert sorted(len(r) for r in results) == [0, 3], results


# ══ c/d. follow-up tasks: reservation, the two partial unique indexes ═════════
def test_reserve_task_dedupes_a_rule_key_and_one_open_task_per_stage(clean):
    add_proposal("p1")
    first = db.reserve_followup_task("p1", "vnv:1", {"template": "followup_not_viewed",
                                                     "stage": "pre"})
    assert isinstance(first, int)
    assert db.reserve_followup_task("p1", "vnv:1", {"stage": "pre"}) is None     # same occurrence
    assert db.reserve_followup_task("p1", "vnv:2", {"stage": "pre"}) is None     # same open stage
    assert db.followup_rule_key_exists("p1", "vnv:1") is True
    assert db.followup_rule_key_exists("p1", "vnv:2") is False
    stored = db.get_followup_task(first)
    assert stored["detail"]["rule_key"] == "vnv:1" and stored["detail"]["status"] == "pending"

    # Once it is decided the slot is free, but the occurrence it covered stays used.
    assert db.decide_followup_task(first, "sent", "kyle")["detail"]["status"] == "sent"
    assert db.reserve_followup_task("p1", "vnv:1", {"stage": "pre"}) is None
    assert isinstance(db.reserve_followup_task("p1", "vnv:2", {"stage": "pre"}), int)


def test_a_decision_task_and_a_deposit_task_coexist_on_one_scope(clean):
    gid, ids = group_of_three()
    set_cols(ids[0], proposal_status="approved", approved_at=ago(0))
    dep = db.reserve_followup_task(ids[0], "dep:1", {"template": "deposit_nudge",
                                                     "stage": "deposit"})
    dec = db.reserve_followup_task(ids[0], f"sd:{gid}:{ids[0]}", {
        "audience": "staff", "template": rules.GROUP_TASK_TEMPLATE_DECISION,
        "stage": rules.DECISION_STAGE, "scope_group": gid,
        "scopes": [{"proposal_id": ids[1]}, {"proposal_id": ids[2]}]})
    assert isinstance(dep, int) and isinstance(dec, int)
    # A second decision task for the same approval is the same rule key: a no-op.
    assert db.reserve_followup_task(ids[0], f"sd:{gid}:{ids[0]}", {"stage": "decision"}) is None
    # And a second open deposit task is refused by the per-stage index.
    assert db.reserve_followup_task(ids[0], "dep:2", {"stage": "deposit"}) is None
    # Logging a call on the approved scope finds the deposit task, never the decision task.
    assert db.pending_followup_task_id(ids[0]) == dep
    db.decide_followup_task(dep, "called", "kyle")
    assert db.pending_followup_task_id(ids[0]) is None
    assert [t["id"] for t in db.list_open_group_tasks(gid)] == [dec]


def test_a_task_with_no_stage_is_a_pre_decision_task_for_the_index(clean):
    """coalesce(stage, 'pre'): a legacy task written without a stage and a new 'pre' task are the
    same slot. Two stage-less ones are too (a plain NULL would let both in)."""
    add_proposal("p1")
    assert isinstance(db.reserve_followup_task("p1", "a", {"template": "followup_viewed"}), int)
    assert db.reserve_followup_task("p1", "b", {"template": "followup_viewed"}) is None
    assert db.reserve_followup_task("p1", "c", {"stage": "pre"}) is None
    add_proposal("p2")
    add_followup("p2", "followup_task", task("p2", stage="pre"))
    with pytest.raises(psycopg.errors.UniqueViolation):
        add_followup("p2", "followup_task", task("p2"))


def test_a_sending_task_holds_its_slot_and_expired_or_skipped_ones_do_not(clean):
    add_proposal("p1")
    tid = db.reserve_followup_task("p1", "k1", {"stage": "pre"})
    claimed = db.decide_followup_task(tid, "sending", "kyle")
    assert claimed["detail"]["status"] == "sending"
    assert db.decide_followup_task(tid, "sending", "kyle") is None          # the second press
    assert db.reserve_followup_task("p1", "k2", {"stage": "pre"}) is None  # still open
    assert db.pending_followup_task_id("p1") is None                        # sending is not pending
    assert db.reopen_followup_task(tid, {"send_unconfirmed": True}) is True
    assert db.get_followup_task(tid)["detail"]["send_unconfirmed"] is True
    assert db.decide_followup_task(tid, "skipped", "kyle", {"note": "on holiday"})
    assert isinstance(db.reserve_followup_task("p1", "k2", {"stage": "pre"}), int)
    add_proposal("p2")
    add_followup("p2", "followup_task", task("p2", status="expired", stage="pre"))
    add_followup("p2", "followup_task", task("p2", status="snoozed", stage="pre"))
    assert isinstance(db.reserve_followup_task("p2", "k3", {"stage": "pre"}), int)


def test_a_task_shares_the_rule_key_dedupe_with_the_old_automatic_emails(clean):
    add_proposal("p1")
    assert isinstance(db.reserve_followup("p1", "vnv:1", {"audience": "customer"}), int)
    assert db.reserve_followup_task("p1", "vnv:1", {"stage": "pre"}) is None
    assert db.followup_rule_key_exists("p1", "vnv:1") is True
    # A staff_note carrying the same key is bookkeeping, not an occurrence.
    add_followup("p1", "staff_note", {"rule_key": "note:1"})
    assert db.followup_rule_key_exists("p1", "note:1") is False


# ══ f. the reminder cap counts only what reached the customer ═════════════════
def test_reminder_times_count_only_sent_and_called_tasks(clean):
    add_proposal("p1", enrolled=20, days_ago=20)
    for status, age in (("sent", 9), ("called", 8), ("skipped", 7), ("snoozed", 6),
                        ("expired", 5), ("decided", 4.5), ("sending", 4)):
        add_followup("p1", "followup_task", task("p1", status=status, stage=f"s-{status}"),
                     days_ago=age)
    add_followup("p1", "followup_task", task("p1", status="pending", stage="pre"), days_ago=3)
    add_followup("p1", "auto_email", {"audience": "customer", "rule_key": "old:1"}, days_ago=12)
    add_followup("p1", "auto_email", {"audience": "staff", "rule_key": "old:2"}, days_ago=11)
    # Before this send cycle's enrolment: never counted.
    add_followup("p1", "followup_task", task("p1", status="sent", stage="old-cycle"), days_ago=25)
    add_followup("p1", "auto_email", {"audience": "customer", "rule_key": "old:3"}, days_ago=26)

    times = db.followup_signals("p1")["followup_reminder_times"]
    assert len(times) == 3, times                       # sent, called, the customer auto_email
    board = next(r for r in db.list_all_portal_proposals() if r["proposal_id"] == "p1")
    assert sorted(board["followup_reminder_times"]) == sorted(times)
    # And the schedule reads them as three reminders.
    since = ago(20)
    assert rules._reminders_since(dict(board), since) == 3


def about(ts, days: float, slack=timedelta(minutes=5)) -> None:
    """`ts` is `days` before the database's now (a row written `now() - interval`)."""
    assert ts is not None
    assert abs((db_now() - ts) - timedelta(days=days)) < slack, (ts, days)


def test_followup_signals_reads_every_touch(clean):
    """Every touch is NEWER noise that the predicates must ignore, so a dropped predicate moves the
    value: an out-of-office now (customer, meta.auto_reply), the signed-contract row now (staff
    TEXT, meta.system_doc), a staff card now (msg_type 'system'), a bookkeeping note now."""
    add_proposal("p1", enrolled=20, days_ago=20)
    db.execute("insert into public.portal_questions (proposal_id, author_kind, author_email, body, "
               "created_at) values ('p1', 'customer', 'dana@customer.example', 'Any update?', "
               "now() - interval '1 day')")
    db.execute("insert into public.portal_questions (proposal_id, author_kind, author_email, body, "
               "meta, created_at) values ('p1', 'customer', 'ooo@customer.example', 'Away', "
               "'{\"auto_reply\": true}'::jsonb, now())")
    db.execute("insert into public.portal_questions (proposal_id, author_kind, body, msg_type, "
               "created_at) values ('p1', 'staff', 'Following up', 'text', now() - interval '2 days')")
    # Written exactly as main._post_signed_contract writes it: staff, msg_type 'text'.
    db.add_message("p1", "staff", None, "Your signed contract is attached.", msg_type="text",
                   meta={"attachments": [], "system_doc": True})
    db.add_message("p1", "staff", None, "Kyle logged a call.", msg_type="system",
                   meta={"crm": "followup", "internal": True})
    add_followup("p1", "staff_call", {"note": "left a voicemail"}, days_ago=3)
    add_followup("p1", "staff_note", {"action": "reassigned"}, days_ago=0)   # bookkeeping
    sig = db.followup_signals("p1")
    assert set(sig) >= {"customer_touch_at", "last_staff_followup_at", "last_staff_message_at",
                        "followup_reminder_times", "pending_task"}
    about(sig["customer_touch_at"], 1)              # the reply, not the out-of-office
    about(sig["last_staff_message_at"], 2)          # the message, not the contract or the card
    about(sig["last_staff_followup_at"], 3)         # the call, not the note
    # The board reads the same predicates (one copy of each, in db.py).
    board = next(r for r in db.list_all_portal_proposals() if r["proposal_id"] == "p1")
    assert board["customer_touch_at"] == sig["customer_touch_at"]
    assert board["last_staff_message_at"] == sig["last_staff_message_at"]
    assert sig["pending_task"] is None
    tid = db.reserve_followup_task("p1", "k", {"stage": "pre"})
    assert db.followup_signals("p1")["pending_task"]["id"] == tid


# ══ g/h. the board and the worker over a 3-scope group ════════════════════════
def _board():
    return {r["proposal_id"]: r for r in db.list_all_portal_proposals()}


def test_board_and_candidates_over_a_group_with_an_archived_and_a_deleted_draft(clean):
    gid, ids = group_of_three(sent=True, archived_b=True, deleted_c=True)
    a, b, c = ids
    add_draft("solo")
    add_proposal("solo")
    # A deleted SCOPE (portal row deleted) never reaches either list.
    add_draft(f"{gid}-gone", gid, "Old scope")
    add_proposal(f"{gid}-gone", gid, "Old scope")
    db.delete_proposal(f"{gid}-gone")

    board = _board()
    assert set(board) == {a, b, c, "solo"}
    assert [board[i]["scope_group"] for i in ids] == [gid] * 3
    assert [board[i]["scope_label"] for i in ids] == ["Breakroom", "Lab 1", "Lab 2"]
    assert board["solo"]["scope_group"] is None
    # The trashed draft still names the estimator: the join does not filter deleted drafts.
    assert board[c]["estimator_email"] == "kyle@estimator.example"
    assert board[a]["current_revision_no"] == 1

    cands = {r["proposal_id"]: r for r in db.list_followup_candidates()}
    assert set(cands) == {a, b, c, "solo"}
    assert cands[a]["scope_group"] == gid and cands[a]["scope_label"] == "Breakroom"

    # Membership: the deleted draft is not in the group any more, the archived one is parked.
    states = db.list_scope_group_draft_states(gid)
    assert [s["id"] for s in states] == sorted([a, b, f"{gid}-gone"])
    assert {s["id"]: s["archived"] for s in states}[b] is True
    assert db.list_scope_group_drafts(gid) == sorted([a, f"{gid}-gone"])
    assert [r["proposal_id"] for r in db.list_scope_group(gid)] == ids
    live = rules.live_members(db.list_scope_group(gid), states)
    assert [m["proposal_id"] for m in live] == [a]

    # The worker's partition, on the real rows: A is the group; B (archived) is chased neither
    # way; C, whose draft is in the trash, has left the group in the tool's eyes and is a single.
    singles, groups = followup_worker._partition(list(cands.values()))
    assert groups == [gid]
    assert sorted(r["proposal_id"] for r in singles) == sorted([c, "solo"])

    view, why, members = followup_worker.group_state_for(gid, db_now())
    assert why == "" and view["proposal_id"] == a and view["scope_group"] == gid
    assert [s["proposal_id"] for s in view["scopes"]] == [a]
    assert [m["proposal_id"] for m in members] == [a]


def test_a_test_draft_never_joins_a_real_group(clean):
    gid = gid_new()
    add_draft("real", gid, "Breakroom", name="Acme HQ")
    add_draft("flagged", gid, "Lab 1", name="Acme HQ", is_test=True)
    add_draft("named", gid, "Lab 2", name="Acme HQ test")
    add_draft("cleared", gid, "Lab 3", name="Acme HQ test", is_test=False)
    states = db.list_scope_group_draft_states(gid)
    assert {s["id"]: s["is_test"] for s in states}["flagged"] == "true"
    assert db.list_scope_group_drafts(gid) == ["cleared", "real"]


def test_the_boards_batched_draft_read_matches_the_per_group_read(clean):
    """#20: one query for every group on the board, the same states as one query per group."""
    g1, ids1 = group_of_three(sent=True, archived_b=True, deleted_c=True)
    g2, ids2 = group_of_three(sent=True)
    add_draft("flagged", g2, "Lab 9", is_test=True)
    got = db.list_scope_group_draft_states_many([g1, g2, "g-nobody"])
    assert got == {g1: db.list_scope_group_draft_states(g1),
                   g2: db.list_scope_group_draft_states(g2), "g-nobody": []}
    assert db.list_scope_group_draft_states_many([]) == {}


def test_the_group_task_shows_on_every_member_row(clean):
    gid, ids = group_of_three(sent=True)
    a, b, c = ids
    add_draft("solo")
    add_proposal("solo")
    other, other_ids = group_of_three(sent=True)
    group_task = db.reserve_followup_task(a, "grp:1", {
        "template": "followup_not_viewed", "stage": "pre", "scope_group": gid,
        "scopes": [{"proposal_id": i} for i in ids]})
    solo_task = db.reserve_followup_task("solo", "solo:1", {"stage": "pre"})
    # An OLD, finished group task must not show anywhere.
    add_followup(b, "followup_task", task(b, status="sent", stage="pre", scope_group=gid),
                 days_ago=5)

    board = _board()
    for pid in ids:
        pt = board[pid]["pending_task"]
        assert pt is not None and pt["id"] == group_task, pid
        assert pt["proposal_id"] == a and pt["detail"]["scope_group"] == gid
    assert board["solo"]["pending_task"]["id"] == solo_task
    assert all(board[i]["pending_task"] is None for i in other_ids)

    # The approved scope's decision task is newer. Fix round #8: each row shows its OWN open task
    # first (the home its reminder), an open member the group's newest (the decision task), and
    # the approved scope, with nothing else of its own open, its own decision task.
    set_cols(c, proposal_status="approved", approved_at=ago(0))
    dec = db.reserve_followup_task(c, f"sd:{gid}:{c}", {
        "audience": "staff", "template": "scope_decision", "stage": "decision",
        "scope_group": gid, "scopes": [{"proposal_id": a}, {"proposal_id": b}]})
    board = _board()
    assert [board[i]["pending_task"]["id"] for i in ids] == [group_task, dec, dec]
    assert board["solo"]["pending_task"]["id"] == solo_task
    open_ids = [t["id"] for t in db.list_open_group_tasks(gid)]
    assert open_ids == [group_task, dec]

    # Once decided, the open members fall back to the open reminder task; the approved scope has
    # nothing open of its own and is not an open member, so it shows none.
    db.decide_followup_task(dec, "decided", "kyle", {"action": "leave"})
    board = _board()
    assert [(board[i]["pending_task"] or {}).get("id") for i in ids] == [group_task, group_task, None]


def test_a_rows_own_task_is_never_hidden_by_the_groups(clean):
    """Fix round #8, on Postgres: an approved scope's deposit task and a removed scope's own task
    show on their own rows even though a newer group task exists."""
    gid, (a, b, c) = group_of_three(sent=True)
    set_cols(c, proposal_status="approved", approved_at=ago(1))
    deposit = db.reserve_followup_task(c, "dep:1", {"stage": "deposit",
                                                    "template": "deposit_nudge"})
    dec = db.reserve_followup_task(c, f"sd:{gid}:{c}:1", {
        "audience": "staff", "template": "scope_decision", "stage": "decision",
        "scope_group": gid, "scopes": [{"proposal_id": a}, {"proposal_id": b}]})
    reminder = db.reserve_followup_task(a, "grp:1", {"stage": "pre", "scope_group": gid})
    assert deposit < dec < reminder
    board = _board()
    assert board[c]["pending_task"]["id"] == deposit, "the deposit task was hidden"
    assert board[a]["pending_task"]["id"] == reminder
    assert board[b]["pending_task"]["id"] == reminder

    # A scope taken out of the group keeps the stale scope_group on its row and its own task.
    g2, (x, y, z) = group_of_three(sent=True)
    own = db.reserve_followup_task(z, "solo:z", {"stage": "pre"})
    grp = db.reserve_followup_task(x, "grp:2", {"stage": "pre", "scope_group": g2})
    assert own < grp
    board = _board()
    assert board[z]["pending_task"]["id"] == own, "the removed scope's own task was hidden"
    assert board[y]["pending_task"]["id"] == grp


def test_group_history_and_resume_reads(clean):
    gid, ids = group_of_three(sent=True)
    a, b, c = ids
    assert db.scope_group_resumed_at(gid) is None
    add_followup(b, "staff_note", {"action": "scope_group_resume", "scope_group": gid},
                 days_ago=1)
    add_followup(b, "staff_note", {"action": "scope_group_resume", "scope_group": gid},
                 days_ago=3)
    resumed = db.scope_group_resumed_at(gid)
    assert resumed is not None and timedelta(hours=23) < db_now() - resumed < timedelta(hours=25)
    # A former home, taken out of the group in the portal row, that holds a group follow-up row.
    add_draft("former", None)
    add_proposal("former")
    add_followup("former", "followup_task", task("former", status="sent", scope_group=gid),
                 days_ago=4)
    hist = {r["proposal_id"]: r for r in db.list_scope_group_history(gid)}
    assert set(hist) == {a, b, c, "former"}
    assert hist["former"]["group_tagged"] is True and hist[a]["group_tagged"] is False


def test_the_dry_run_reads_real_rows_and_writes_nothing(clean):
    gid, ids = group_of_three(sent=True)
    for pid in ids:
        set_cols(pid, followup_enrolled_at=ago(30), created_at=ago(30), last_sent_at=ago(30))
    add_draft("solo")
    add_proposal("solo", enrolled=30, days_ago=30)
    before = db.q1("select count(*) as n from public.portal_followups")["n"]
    out = followup_worker.dry_run_count(in_window())
    after = db.q1("select count(*) as n from public.portal_followups")["n"]
    assert out["skipped"] == [], out["skipped"]
    assert before == after, "the dry run wrote a row"
    by_pid = {t["proposal_id"]: t for t in out["tasks"]}
    assert set(by_pid) == {ids[0], "solo"}, out["tasks"]
    assert by_pid[ids[0]]["scope_group"] == gid and by_pid["solo"]["scope_group"] is None


# ══ the rest of the changed writes, and the worker's housekeeping on real rows ══
def test_publish_writes_enrolment_and_group_columns(clean):
    add_proposal("p1", rev=1)
    set_cols("p1", followup_paused_until=(db_now() + timedelta(days=20)).date(),
             cycle_viewed_at=ago(1), deposit_required=False)
    db.enroll_followup("p1", keep_pause=True)              # the combined email: pause survives
    row = db.get_proposal("p1")
    assert row["followup_paused_until"] is not None and row["cycle_viewed_at"] is None
    db.enroll_followup("p1")                               # an ordinary publish clears it
    assert db.get_proposal("p1")["followup_paused_until"] is None
    db.update_portal_proposal("p1", "new@customer.example", "Dana", "Acme HQ", None)
    row = db.get_proposal("p1")
    assert row["deposit_required"] is False and row["current_revision_no"] == 1   # preserved
    db.update_portal_proposal("p1", "new@customer.example", "Dana", "Acme HQ", "x.pdf",
                              deposit_required=True, revision_no=4)
    row = db.get_proposal("p1")
    assert row["deposit_required"] is True and row["current_revision_no"] == 4
    db.set_scope_group("p1", "g-9", "")
    row = db.get_proposal("p1")
    assert row["scope_group"] == "g-9" and row["scope_label"] is None


def test_twin_containers_raising_the_same_stage_create_one_task(clean):
    for n in range(6):
        pid = f"p{n}"
        add_proposal(pid)
        keys = iter(["vnv:a", "vnv:b"])
        lock = threading.Lock()

        def raise_one():
            with lock:
                key = next(keys)
            return db.reserve_followup_task(pid, key, {"stage": "pre"})

        results = _race(raise_one)
        assert sum(1 for r in results if r) == 1, results
        assert db.q1("select count(*) as n from public.portal_followups "
                     "where proposal_id = %s", (pid,))["n"] == 1


def test_housekeeping_keeps_the_decision_task_and_expires_what_moved_on(clean, monkeypatch):
    sent_staff = []
    monkeypatch.setattr(followup_worker, "_send_staff",
                        lambda *a, **k: sent_staff.append(a) or True)
    gid, ids = group_of_three(sent=True)
    a, b, c = ids
    group_task = db.reserve_followup_task(a, "grp:1", {
        "template": "followup_not_viewed", "stage": "pre", "scope_group": gid,
        "scopes": [{"proposal_id": i} for i in ids]})
    set_cols(c, proposal_status="approved", approved_at=ago(0))
    dec = db.reserve_followup_task(c, f"sd:{gid}:{c}", {
        "audience": "staff", "template": rules.GROUP_TASK_TEMPLATE_DECISION,
        "stage": rules.DECISION_STAGE, "scope_group": gid,
        "scopes": [{"proposal_id": a, "label": "Breakroom"}, {"proposal_id": b, "label": "Lab 1"}]})
    add_draft("solo")
    add_proposal("solo")
    solo_task = db.reserve_followup_task("solo", "solo:1", {"stage": "pre"})
    set_cols("solo", proposal_status="approved", approved_at=ago(0))   # signed since

    listed = {t["id"]: t for t in db.list_pending_followup_tasks()}
    assert set(listed) == {group_task, dec, solo_task}
    assert listed[dec]["estimator_email"] == "kyle@estimator.example"

    followup_worker._expire_stale_tasks(db_now())

    assert db.get_followup_task(dec)["detail"]["status"] == "pending"
    g = db.get_followup_task(group_task)["detail"]
    assert g["status"] == "expired" and g["reason"] == "scope_approved"
    s = db.get_followup_task(solo_task)["detail"]
    assert s["status"] == "expired" and s["reason"] == "stage_changed"
    assert sent_staff == [], "a fresh decision task must not re-nag"
    # Board: every member now shows the decision task (the only open group task left).
    board = _board()
    assert {board[i]["pending_task"]["id"] for i in ids} == {dec}


def test_the_hand_applied_blocks_apply_alone_and_twice(fresh_db):
    """Prod never applies schema.sql on boot: an owner pastes the new blocks by hand. The remind-only
    and scope-group sections, lifted out ALONE, must apply to a database that has neither (and has
    the predecessor index), and again on top of themselves."""
    text = pg_fixture.SCHEMA.read_text(encoding="utf-8")
    start = text.index("-- == Remind-only follow-ups")
    blocks = text[start:]
    code = re.sub(r"--[^\n]*", "", blocks).lower()
    assert "grant " not in code and "policy" not in code, "a grant would fail on staging"
    db.execute("drop index public.portal_followups_one_open_task_v2_uidx")
    db.execute("drop index public.portal_followups_rule_v2_uidx")
    db.execute("create unique index portal_followups_one_open_task_uidx "
               "on public.portal_followups (proposal_id) "
               "where kind = 'followup_task' and (detail->>'status') in ('pending','sending')")
    db.execute("alter table public.portal_proposals drop column scope_group, "
               "drop column scope_label, drop column followup_mode, "
               "drop column followup_anchor_at, drop column followup_next_at, "
               "drop column followup_interval_days, drop column followup_stop_after")
    assert db.scope_group_columns_ready() is False
    pg_fixture.apply_schema(blocks)
    pg_fixture.apply_schema(blocks)
    assert db.scope_group_columns_ready() is True
    assert index_def("portal_followups_one_open_task_v2_uidx") is not None
    assert index_def("portal_followups_one_open_task_uidx") is None
    assert index_def("portal_followups_rule_v2_uidx") is not None


# ══ the remaining follow-up SQL: the plan, the anchor, the draft, unsettled sends ══
KYLE = "kyle@estimator.example"


def all_tasks() -> list:
    return db.qall("select id, proposal_id, detail, created_at from public.portal_followups "
                   "where kind = 'followup_task' order by id")


def age_group(ids, days=30):
    """Published, emailed and enrolled `days` back, owned by Kyle: every reminder is due."""
    for pid in ids:
        set_cols(pid, followup_enrolled_at=ago(days), created_at=ago(days),
                 last_sent_at=ago(days), assigned_estimator=KYLE)


def test_the_followup_plan_checks_refuse_what_the_plan_endpoint_refuses(clean):
    """The three CHECKs schema.sql adds (and re-adds on every boot) actually refuse: the endpoint's
    1-90 / 1-60 validation is not the only thing between a typo and the worker."""
    add_proposal("p1")
    for col, bad in (("followup_mode", "ladder"), ("followup_mode", ""),
                     ("followup_interval_days", 0), ("followup_interval_days", 91),
                     ("followup_stop_after", 0), ("followup_stop_after", 61)):
        with pytest.raises(psycopg.errors.CheckViolation):
            set_cols("p1", **{col: bad})
    for col, good in (("followup_mode", "remind"), ("followup_mode", "approve"),
                      ("followup_mode", "auto"), ("followup_mode", None),
                      ("followup_interval_days", 1), ("followup_interval_days", 90),
                      ("followup_stop_after", 1), ("followup_stop_after", 60)):
        set_cols("p1", **{col: good})
    with pytest.raises(psycopg.errors.CheckViolation):
        db.set_followup_plan("p1", interval_days=91)
    with pytest.raises(psycopg.errors.CheckViolation):
        db.set_followup_plan("p1", stop_after=0)


def test_reapplying_the_schema_restores_a_missing_plan_check(fresh_db):
    """The drop-then-add pairs: a database that lost (or never had) a CHECK gets it back on the
    next apply, and an apply over a database that has them is a no-op that keeps them."""
    for name in ("portal_proposals_followup_mode_check",
                 "portal_proposals_followup_interval_days_check",
                 "portal_proposals_followup_stop_after_check"):
        db.execute("alter table public.portal_proposals drop constraint " + name)
    add_proposal("p1")
    set_cols("p1", followup_mode="ladder")              # nothing refuses it now
    set_cols("p1", followup_mode=None)
    pg_fixture.apply_schema()
    pg_fixture.apply_schema()
    for col, bad in (("followup_mode", "ladder"), ("followup_interval_days", 0),
                     ("followup_stop_after", 61)):
        with pytest.raises(psycopg.errors.CheckViolation):
            set_cols("p1", **{col: bad})


def test_reapplying_over_the_old_rule_index_drops_it_and_keeps_the_dedupe(fresh_db):
    """`drop index if exists public.portal_followups_rule_uidx` against an index that EXISTS (every
    other test only ever runs it as a no-op): a database from before remind-only, with the old
    auto_email-only index and live reservations, ends with the widened index alone, its rows kept,
    and a task refused for an occurrence the old ladder already emailed."""
    db.execute("drop index public.portal_followups_rule_v2_uidx")
    db.execute("create unique index portal_followups_rule_uidx "
               "on public.portal_followups (proposal_id, (detail->>'rule_key')) "
               "where kind = 'auto_email' and (detail->>'rule_key') is not null")
    add_proposal("p1")
    assert isinstance(db.reserve_followup("p1", "vnv:1", {"audience": "customer"}), int)
    assert db.reserve_followup("p1", "vnv:1", {"audience": "customer"}) is None   # old guard

    pg_fixture.apply_schema()

    assert index_def("portal_followups_rule_uidx") is None
    assert index_def("portal_followups_rule_v2_uidx") is not None
    assert db.q1("select count(*) as n from public.portal_followups")["n"] == 1
    assert db.reserve_followup_task("p1", "vnv:1", {"stage": "pre"}) is None
    assert db.reserve_followup("p1", "vnv:1", {"audience": "customer"}) is None
    assert isinstance(db.reserve_followup_task("p1", "vnv:2", {"stage": "pre"}), int)


def test_the_plan_writes_only_what_it_is_given_and_the_anchor_stamps_now(clean):
    add_proposal("p1")
    snooze = datetime(2030, 3, 4, 8, 0, tzinfo=rules.BUSINESS_TZ)
    set_cols("p1", updated_at=ago(5))
    db.set_followup_plan("p1", next_at=snooze)
    row = db.get_proposal("p1")
    assert row["followup_next_at"] == snooze
    assert row["followup_interval_days"] is None and row["followup_stop_after"] is None
    about(row["updated_at"], 0)
    db.set_followup_plan("p1", interval_days=7, stop_after=3)
    row = db.get_proposal("p1")
    assert (row["followup_interval_days"], row["followup_stop_after"]) == (7, 3)
    assert row["followup_next_at"] == snooze                    # not passed, not touched
    db.set_followup_plan("p1", stop_after=None)                 # None clears one field
    row = db.get_proposal("p1")
    assert row["followup_stop_after"] is None and row["followup_interval_days"] == 7
    db.set_followup_plan("p1")                                  # nothing passed: no write at all

    # A snooze keeps its date; any other decision drops it.
    db.stamp_followup_anchor("p1", clear_next=False)
    row = db.get_proposal("p1")
    about(row["followup_anchor_at"], 0)
    assert row["followup_next_at"] == snooze
    set_cols("p1", followup_anchor_at=ago(4))
    db.stamp_followup_anchor("p1")
    row = db.get_proposal("p1")
    about(row["followup_anchor_at"], 0)
    assert row["followup_next_at"] is None
    assert row["followup_interval_days"] == 7, "the anchor must not touch the plan"
    # Only this proposal.
    add_proposal("p2")
    db.stamp_followup_anchor("p1")
    assert db.get_proposal("p2")["followup_anchor_at"] is None


def test_the_plan_and_the_anchor_fail_loudly_without_their_columns(fresh_db):
    """Both are WRITES named directly (not through to_jsonb), so before the owner's ALTER they must
    raise rather than pretend; main._stamp_touch is the guard that keeps a decision recorded."""
    add_proposal("p1")
    db.execute("alter table public.portal_proposals drop column followup_anchor_at, "
               "drop column followup_next_at")
    with pytest.raises(psycopg.errors.UndefinedColumn):
        db.stamp_followup_anchor("p1")
    with pytest.raises(psycopg.errors.UndefinedColumn):
        db.set_followup_plan("p1", next_at=None)


def test_a_draft_is_stored_as_an_object_on_a_pending_task_only(clean):
    add_proposal("p1")
    tid = db.reserve_followup_task("p1", "k1", {"stage": "pre", "template": "checkin",
                                                "draft": {"title": "Default", "body": "{link}",
                                                          "source": "default"}})
    ai = {"title": "Quick check-in", "body": "Hi Dana,\n\n{link}", "cta": "Open",
          "source": "ai"}
    db.set_followup_task_draft(tid, ai)
    d = db.get_followup_task(tid)["detail"]
    assert d["draft"] == ai
    assert d["rule_key"] == "k1" and d["status"] == "pending" and d["template"] == "checkin"
    kind = db.q1("select jsonb_typeof(detail -> 'draft') as t from public.portal_followups "
                 "where id = %s", (tid,))["t"]
    assert kind == "object", "the draft must be a jsonb object, not a quoted string"
    db.decide_followup_task(tid, "skipped", KYLE)
    db.set_followup_task_draft(tid, {"title": "Too late", "body": "{link}", "source": "ai"})
    assert db.get_followup_task(tid)["detail"]["draft"] == ai
    # Never a row of another kind.
    note = add_followup("p1", "staff_note", {"action": "x"})
    db.set_followup_task_draft(note, ai)
    assert "draft" not in db.q1("select detail from public.portal_followups where id = %s",
                                (note,))["detail"]


def test_unsettled_sends_are_found_and_settled_on_the_evidence(clean):
    """list_stale_sending_tasks + followup_task_send_recorded, and the worker's recovery that reads
    them (_recover_stuck_sends), on real rows."""
    names = ("went", "lost", "fresh", "legacy", "open")
    for pid in names:
        add_proposal(pid)
    ids = {pid: db.reserve_followup_task(pid, "k-" + pid, {"stage": "pre"}) for pid in names}
    stale = (db_now() - timedelta(minutes=20)).isoformat()
    db.decide_followup_task(ids["went"], "sending", KYLE, {"sending_at": stale})
    db.decide_followup_task(ids["lost"], "sending", KYLE, {"sending_at": stale})
    # Raised long ago but claimed a moment ago: sending_at wins over created_at.
    db.decide_followup_task(ids["fresh"], "sending", KYLE,
                            {"sending_at": db_now().isoformat()})
    # Claimed by code that wrote no sending_at: dated by the row.
    db.decide_followup_task(ids["legacy"], "sending", KYLE)
    db.execute("update public.portal_followups set created_at = now() - interval '20 minutes' "
               "where id = any(%s)", ([ids["fresh"], ids["legacy"], ids["open"]],))
    # The evidence main.admin_followup_task_send writes after a real send (task_id as an int)...
    db.add_followup("went", "staff_email", {"note": "Checking in", "task_id": ids["went"],
                                            "followup_task": True}, KYLE)
    # ...and rows that are NOT evidence: another kind carrying the id, and another task's email.
    db.add_followup("lost", "staff_note", {"action": "followup_skipped", "task_id": ids["lost"]})
    db.add_followup("lost", "staff_email", {"task_id": ids["open"]})

    assert [t["id"] for t in db.list_stale_sending_tasks(15)] == sorted(
        [ids["went"], ids["lost"], ids["legacy"]])
    assert db.list_stale_sending_tasks(30) == []
    assert db.followup_task_send_recorded(ids["went"]) is True
    assert db.followup_task_send_recorded(ids["lost"]) is False
    assert db.followup_task_send_recorded(ids["legacy"]) is False

    followup_worker._recover_stuck_sends()

    went = db.get_followup_task(ids["went"])["detail"]
    assert went["status"] == "sent" and went["recovered"] is True and went["decided_by"] == KYLE
    for pid in ("lost", "legacy"):
        d = db.get_followup_task(ids[pid])["detail"]
        assert d["status"] == "pending" and d["send_unconfirmed"] is True, pid
        assert d["sending_at"] is None and d["decided_by"] is None
    assert db.get_followup_task(ids["fresh"])["detail"]["status"] == "sending"
    assert db.get_followup_task(ids["open"])["detail"]["status"] == "pending"
    assert db.list_stale_sending_tasks(15) == []
    # The reopened task holds its slot again: no second task for that stage.
    assert db.reserve_followup_task("lost", "k-lost-2", {"stage": "pre"}) is None


# ══ the worker's tick, group pass and re-nag, on real rows ═════════════════════
@pytest.fixture
def outbox(monkeypatch):
    """Every email the code tries to send, recorded at email_sender._send (the one function that
    calls Resend), with no network: Resend is replaced by a recorder and httpx.post by a tripwire.
    Addresses in `fail` bounce."""
    import types

    import email_sender

    box = types.SimpleNamespace(sent=[], fail=set())

    def fake_send(to, subject, html, headers=None, reply_to=None, attachments=None):
        to = [t for t in to if t]
        if not to:
            return False
        ok = not any(t in box.fail for t in to)
        box.sent.append({"to": list(to), "subject": subject, "html": html, "ok": ok})
        return ok

    def no_network(*a, **k):
        raise AssertionError("a test reached for Resend")

    monkeypatch.setattr(email_sender, "_send", fake_send)
    monkeypatch.setattr(email_sender.httpx, "post", no_network)
    box.to = lambda: [t for m in box.sent if m["ok"] for t in m["to"]]
    box.customers = lambda: [t for t in box.to() if t.endswith("@customer.example")]
    return box


@pytest.fixture
def automation_on(monkeypatch):
    monkeypatch.setenv("FOLLOWUP_AUTOMATION_ENABLED", "true")


def test_a_tick_raises_what_the_dry_run_promised_and_a_second_tick_raises_nothing(
        clean, outbox, automation_on):
    gid, ids = group_of_three(sent=True)
    age_group(ids)
    add_draft("solo")
    add_proposal("solo")
    age_group(["solo"])
    now = in_window()
    planned = {(t["proposal_id"], t["rule_key"])
               for t in followup_worker.dry_run_count(now)["tasks"]}
    assert {pid for pid, _ in planned} == {ids[0], "solo"}

    followup_worker._tick(now)

    raised = all_tasks()
    assert {(t["proposal_id"], t["detail"]["rule_key"]) for t in raised} == planned
    by = {t["proposal_id"]: t["detail"] for t in raised}
    g = by[ids[0]]
    assert g["scope_group"] == gid and [s["proposal_id"] for s in g["scopes"]] == ids
    assert g["status"] == "pending" and g["stage"] == "pre" and g["mode"] == "remind"
    assert g["cycle"] and g["draft"]["source"] == "default" and g["draft"]["title"]
    assert by["solo"].get("scope_group") is None
    # REMIND-ONLY: no customer was emailed and no customer email was even reserved.
    assert outbox.customers() == []
    assert db.q1("select count(*) as n from public.portal_followups where kind = 'auto_email' "
                 "and detail ->> 'audience' = 'customer'")["n"] == 0
    # The estimator was told, and each task is echoed into its thread as an INTERNAL row.
    assert set(outbox.to()) == {KYLE}
    for t in raised:
        echo = db.q1("select meta from public.portal_questions where proposal_id = %s "
                     "and (meta ->> 'task_id')::bigint = %s", (t["proposal_id"], t["id"]))
        assert echo and echo["meta"]["internal"] is True, t["proposal_id"]

    sent_before = len(outbox.sent)
    followup_worker._tick(now)
    assert len(all_tasks()) == len(raised)
    assert len(outbox.sent) == sent_before
    assert followup_worker.dry_run_count(now)["tasks"] == []


def test_a_tick_does_nothing_with_automation_off(clean, outbox, monkeypatch):
    monkeypatch.setenv("FOLLOWUP_AUTOMATION_ENABLED", "false")
    add_proposal("solo")
    age_group(["solo"])
    followup_worker._tick(in_window())
    assert all_tasks() == [] and outbox.sent == []


def test_the_group_pass_rehomes_a_stray_task_and_a_sending_one_holds_the_slot(clean, outbox):
    cfg = followup_worker._settings()
    now = in_window()
    # A pending group task on a scope that is not the home: expired, and raised on the home.
    gid, ids = group_of_three(sent=True)
    age_group(ids)
    a, b, c = ids
    stray = db.reserve_followup_task(b, "grp:stray", {
        "template": "followup_not_viewed", "stage": "pre", "scope_group": gid,
        "scopes": [{"proposal_id": i} for i in ids]})
    followup_worker._process_group(gid, now, cfg)
    s = db.get_followup_task(stray)["detail"]
    assert s["status"] == "expired" and s["reason"] == "rehomed"
    assert [t["proposal_id"] for t in db.list_open_group_tasks(gid)] == [a]

    # A `sending` one is mid-send: never expired under the estimator, and it holds the slot.
    gid2, ids2 = group_of_three(sent=True)
    age_group(ids2)
    busy = db.reserve_followup_task(ids2[1], "grp:busy", {
        "template": "followup_not_viewed", "stage": "pre", "scope_group": gid2,
        "scopes": [{"proposal_id": i} for i in ids2]})
    db.decide_followup_task(busy, "sending", KYLE)
    followup_worker._process_group(gid2, now, cfg)
    assert db.get_followup_task(busy)["detail"]["status"] == "sending"
    assert [t["id"] for t in db.list_open_group_tasks(gid2)] == [busy]

    # The chase stops when a scope is approved: the home's open reminder is expired.
    set_cols(c, proposal_status="approved", approved_at=ago(0))
    followup_worker._process_group(gid, now, cfg)
    assert db.list_open_group_tasks(gid) == []
    assert outbox.customers() == []


def test_an_unanswered_decision_task_renags_once_per_period(clean, outbox):
    gid, ids = group_of_three(sent=True)
    a, b, c = ids
    for pid in ids:
        set_cols(pid, assigned_estimator=KYLE)
    set_cols(c, proposal_status="approved", approved_at=ago(7))
    dec = db.reserve_followup_task(c, f"sd:{gid}:{c}", {
        "audience": "staff", "template": rules.GROUP_TASK_TEMPLATE_DECISION,
        "stage": rules.DECISION_STAGE, "scope_group": gid,
        "approved_scope": {"proposal_id": c, "label": "Lab 2"},
        "scopes": [{"proposal_id": a, "label": "Breakroom"}, {"proposal_id": b, "label": "Lab 1"}]})
    db.execute("update public.portal_followups set created_at = now() - interval '7 days' "
               "where id = %s", (dec,))
    task = db.get_followup_task(dec)
    now = db_now()
    key = f"sdn:{dec}:2"                          # 7 days = the second 3-day period

    outbox.fail.add(KYLE)                         # the email bounces: the reservation is released
    followup_worker._renag_decision(task, now)
    assert db.followup_rule_key_exists(c, key) is False
    outbox.fail.clear()
    followup_worker._renag_decision(task, now)
    assert db.followup_rule_key_exists(c, key) is True
    delivered = [m for m in outbox.sent if m["ok"]]
    assert len(delivered) == 1 and delivered[0]["to"] == [KYLE]
    assert "Breakroom" in delivered[0]["html"] and "Lab 1" in delivered[0]["html"]
    followup_worker._renag_decision(task, now)    # the same period again (the twin container)
    assert len([m for m in outbox.sent if m["ok"]]) == 1
    followup_worker._renag_decision(task, now + timedelta(days=60))
    assert db.followup_rule_key_exists(c, f"sdn:{dec}:{followup_worker.DECISION_RENAG_MAX}")
    assert not db.followup_rule_key_exists(c, f"sdn:{dec}:20")
    # Younger than one period: nothing at all.
    young = dict(task, created_at=db_now() - timedelta(days=1))
    before = len(outbox.sent)
    followup_worker._renag_decision(young, db_now())
    assert len(outbox.sent) == before
    assert outbox.customers() == []


# ══ the endpoints, end to end over real rows ═══════════════════════════════════
if pg_fixture.PG_URL:
    import main  # noqa: E402 — only when the tests will run

GOOD = {"title": "Checking in", "body": "Hi {first_name},\n\nAny questions on {project}?\n\n{link}",
        "by": KYLE}


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(main, "_admin_ok", lambda request: True)
    return TestClient(main.app)


def _raise_tasks() -> dict:
    followup_worker._tick(in_window())
    return {t["proposal_id"]: t for t in all_tasks() if t["detail"]["status"] == "pending"}


def _echo_of(pid, tid, internal=False):
    return db.q1("select id from public.portal_questions where proposal_id = %s "
                 "and (meta ->> 'task_id')::bigint = %s "
                 "and (coalesce(meta ->> 'internal', 'false') = 'true') = %s",
                 (pid, tid, internal))


def test_sending_a_task_emails_the_customer_once_and_records_the_touch(
        clean, outbox, automation_on, client):
    add_draft("p1")
    add_proposal("p1")
    age_group(["p1"])
    tid = _raise_tasks()["p1"]["id"]
    got = client.get(f"/api/admin/followup-task/{tid}").json()
    assert got["live"] is True and got["recipients"] == ["p1@customer.example"]
    outbox.sent.clear()

    # A bounce: nothing went, so the task is back to pending and nothing was recorded.
    outbox.fail.add("p1@customer.example")
    r = client.post(f"/api/admin/followup-task/{tid}/send", json=GOOD)
    assert r.status_code == 502 and r.json()["error"] == "send_failed"
    assert db.get_followup_task(tid)["detail"]["status"] == "pending"
    assert db.followup_task_send_recorded(tid) is False
    outbox.fail.clear()

    r = client.post(f"/api/admin/followup-task/{tid}/send", json=GOOD)
    assert r.status_code == 200, r.text
    assert r.json()["task"]["status"] == "sent" and "warnings" not in r.json()
    assert outbox.customers() == ["p1@customer.example"]
    d = db.get_followup_task(tid)["detail"]
    assert d["status"] == "sent" and d["decided_by"] == KYLE and d["sent"]["title"] == "Checking in"
    assert db.followup_task_send_recorded(tid) is True
    about(db.get_proposal("p1")["followup_anchor_at"], 0)
    sig = db.followup_signals("p1")
    about(sig["last_staff_followup_at"], 0)
    assert len(sig["followup_reminder_times"]) == 1          # a SENT task counts toward the cap
    assert sig["pending_task"] is None
    assert _echo_of("p1", tid, internal=False), "the customer's thread shows the reminder"
    assert db.list_stale_sending_tasks(0) == []

    r = client.post(f"/api/admin/followup-task/{tid}/send", json=GOOD)    # the second press
    assert r.status_code == 409
    assert outbox.customers() == ["p1@customer.example"]


def test_skip_called_snooze_and_the_ai_draft_on_real_rows(clean, outbox, automation_on, client):
    for pid in ("p-skip", "p-call", "p-snooze"):
        add_draft(pid)
        add_proposal(pid)
    age_group(["p-skip", "p-call", "p-snooze"])
    tasks = _raise_tasks()
    assert set(tasks) == {"p-skip", "p-call", "p-snooze"}
    later = db_now() + timedelta(days=40)
    set_cols("p-skip", followup_next_at=later)
    outbox.sent.clear()

    # The AI draft the tool posts back is stored on the task (set_followup_task_draft).
    tid = tasks["p-skip"]["id"]
    r = client.post(f"/api/admin/followup-task/{tid}/draft", json={"model_output": {
        "title": "Quick check-in", "body": "Hi Dana,\n\nAny questions on the floor?\n\n{link}"}})
    assert r.status_code == 200 and r.json()["fallback"] is False, r.text
    assert db.get_followup_task(tid)["detail"]["draft"]["source"] == "ai"
    assert client.get(f"/api/admin/followup-task/{tid}").json()["task"]["draft"]["title"] == \
        "Quick check-in"

    r = client.post(f"/api/admin/followup-task/{tid}/skip", json={"by": KYLE, "note": "on holiday"})
    assert r.status_code == 200 and "warnings" not in r.json(), r.text
    d = db.get_followup_task(tid)["detail"]
    assert d["status"] == "skipped" and d["note"] == "on holiday"
    row = db.get_proposal("p-skip")
    about(row["followup_anchor_at"], 0)
    assert row["followup_next_at"] is None, "a decision drops an earlier snooze date"
    assert db.q1("select count(*) as n from public.portal_followups where proposal_id = 'p-skip' "
                 "and kind = 'staff_note' and detail ->> 'action' = 'followup_skipped' "
                 "and (detail ->> 'task_id')::bigint = %s", (tid,))["n"] == 1
    sig = db.followup_signals("p-skip")
    assert sig["followup_reminder_times"] == [], "a skipped task never counts toward the cap"
    assert sig["last_staff_followup_at"] is None, "a skip is bookkeeping, not outreach"

    tid = tasks["p-call"]["id"]
    r = client.post(f"/api/admin/followup-task/{tid}/called",
                    json={"by": KYLE, "kind": "text", "note": "texted Dana"})
    assert r.status_code == 200 and "warnings" not in r.json(), r.text
    assert db.get_followup_task(tid)["detail"]["status"] == "called"
    assert db.q1("select count(*) as n from public.portal_followups where proposal_id = 'p-call' "
                 "and kind = 'staff_text' and (detail ->> 'task_id')::bigint = %s",
                 (tid,))["n"] == 1
    sig = db.followup_signals("p-call")
    about(sig["last_staff_followup_at"], 0)
    assert len(sig["followup_reminder_times"]) == 1, "a CALLED task counts toward the cap"
    about(db.get_proposal("p-call")["followup_anchor_at"], 0)

    tid = tasks["p-snooze"]["id"]
    day = db_now().astimezone(rules.BUSINESS_TZ).date() + timedelta(days=10)
    r = client.post(f"/api/admin/followup-task/{tid}/snooze",
                    json={"by": KYLE, "until": day.isoformat()})
    assert r.status_code == 200 and "warnings" not in r.json(), r.text
    row = db.get_proposal("p-snooze")
    assert row["followup_next_at"] == datetime(day.year, day.month, day.day,
                                               rules.SEND_START_HOUR, tzinfo=rules.BUSINESS_TZ)
    about(row["followup_anchor_at"], 0)
    assert db.get_followup_task(tid)["detail"]["status"] == "snoozed"
    assert db.q1("select count(*) as n from public.portal_followups where proposal_id = "
                 "'p-snooze' and detail ->> 'action' = 'followup_snoozed'")["n"] == 1

    assert outbox.customers() == [], "none of the three emails a customer"
    assert db.list_pending_followup_tasks() == []


def test_sending_the_group_task_mails_each_buyer_their_own_scope(
        clean, outbox, automation_on, client):
    gid, ids = group_of_three(sent=True)
    age_group(ids)
    a, b, c = ids
    tid = _raise_tasks()[a]["id"]
    buyers = [f"buyer{n}@customer.example" for n in range(3)]
    got = client.get(f"/api/admin/followup-task/{tid}").json()
    assert got["live"] is True and sorted(got["recipients"]) == buyers
    outbox.sent.clear()

    r = client.post(f"/api/admin/followup-task/{tid}/send", json=GOOD)
    assert r.status_code == 200, r.text
    assert sorted(r.json()["emailed"]) == buyers and "warnings" not in r.json()
    assert sorted(outbox.customers()) == buyers               # one email each
    mail = {m["to"][0]: m["html"] for m in outbox.sent if m["ok"]}
    assert "Lab 1" in mail["buyer1@customer.example"]
    assert "Breakroom" not in mail["buyer1@customer.example"]
    d = db.get_followup_task(tid)["detail"]
    assert d["status"] == "sent" and d["delivered"] == buyers
    touch = db.q1("select proposal_id, detail from public.portal_followups "
                  "where kind = 'staff_email' and (detail ->> 'task_id')::bigint = %s", (tid,))
    assert touch["proposal_id"] == a and touch["detail"]["scope_group"] == gid
    about(db.get_proposal(a)["followup_anchor_at"], 0)
    assert db.get_proposal(b)["followup_anchor_at"] is None
    for pid in ids:
        assert _echo_of(pid, tid, internal=False), pid


def test_the_decision_task_closes_the_open_scopes_or_restarts_their_chase(clean, outbox, client):
    def decided_group():
        gid, ids = group_of_three(sent=True)
        a, b, c = ids
        set_cols(c, proposal_status="approved", approved_at=ago(0))
        dec = db.reserve_followup_task(c, f"sd:{gid}:{c}", {
            "audience": "staff", "template": rules.GROUP_TASK_TEMPLATE_DECISION,
            "stage": rules.DECISION_STAGE, "scope_group": gid,
            "approved_scope": {"proposal_id": c, "label": "Lab 2"},
            "scopes": [{"proposal_id": a, "label": "Breakroom"},
                       {"proposal_id": b, "label": "Lab 1"}]})
        return gid, ids, dec

    gid, (a, b, c), dec = decided_group()
    # A decision task is not a reminder: skip refuses it.
    assert client.post(f"/api/admin/followup-task/{dec}/skip",
                       json={"by": KYLE}).status_code == 409
    r = client.post(f"/api/admin/followup-task/{dec}/scope-decision", json={
        "action": "close", "reason": "not_low_bid", "note": "12% over", "by": KYLE})
    assert r.status_code == 200, r.text
    assert r.json()["closed"] == [a, b] and r.json()["failed"] == []
    for pid in (a, b):
        row = db.get_proposal(pid)
        assert row["proposal_status"] == "closed_lost"
        assert row["closed_lost_reason"] == "not_low_bid"
        assert db.q1("select count(*) as n from public.portal_followups where proposal_id = %s "
                     "and kind = 'staff_note' and detail ->> 'action' = 'closed_lost'",
                     (pid,))["n"] == 1
    assert db.get_proposal(c)["proposal_status"] == "approved"
    d = db.get_followup_task(dec)["detail"]
    assert d["status"] == "decided" and d["outcome"] == "close" and d["scope_ids"] == [a, b]
    again = client.post(f"/api/admin/followup-task/{dec}/scope-decision",
                        json={"action": "leave", "by": KYLE})
    assert again.status_code == 409

    gid, (a, b, c), dec = decided_group()
    assert followup_worker.group_state_for(gid, db_now())[0] is None      # the chase is stopped
    r = client.post(f"/api/admin/followup-task/{dec}/scope-decision",
                    json={"action": "keep", "by": KYLE})
    assert r.status_code == 200 and "warnings" not in r.json(), r.text
    about(db.scope_group_resumed_at(gid), 0)
    about(db.get_proposal(a)["followup_anchor_at"], 0)                    # the home, re-anchored
    view = followup_worker.group_state_for(gid, db_now())[0]
    assert view is not None and view["proposal_id"] == a
    assert [db.get_proposal(p)["proposal_status"] for p in (a, b)] == ["sent", "sent"]
    assert outbox.customers() == []


def test_the_combined_send_end_to_end_and_a_failed_revision_unannounces_nothing(
        clean, outbox, client):
    gid, ids = group_of_three()                       # published, never emailed
    a, b, c = ids
    buyers = [f"buyer{n}@customer.example" for n in range(3)]
    url = f"/api/admin/scope-group/{gid}/send"

    r = client.post(url, json={"by": KYLE, "message": "Three scopes, one email"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["first"] is True and out["recorded"] is True and sorted(out["emailed"]) == buyers
    assert sorted(outbox.customers()) == buyers
    first = {m["id"]: m for m in marks_for(gid)}
    assert len(first) == 3 and all(m["detail"]["delivery"]["delivered"] for m in first.values())
    for pid in ids:
        assert portal_rows([pid])[0]["last_sent_at"] is not None, pid
        about(db.get_proposal(pid)["followup_enrolled_at"], 0)
    r = client.post(url, json={"by": KYLE})
    assert r.status_code == 409 and r.json()["error"] == "nothing_to_send"

    # Lab 1 is revised and its only buyer bounces: send 2 reached nobody.
    set_cols(b, current_revision_no=2)
    outbox.fail.add("buyer1@customer.example")
    r = client.post(url, json={"by": KYLE})
    assert r.status_code == 502 and r.json()["error"] == "delivery_failed", r.text
    assert [(s["proposal_id"], s["kind"]) for s in r.json()["scopes"]] == [(b, "revised")]
    assert {m["id"]: m for m in marks_for(gid)} == first, "send 1's marks must survive send 2"

    outbox.fail.clear()
    r = client.post(url, json={"by": KYLE})
    assert r.status_code == 200, r.text
    assert [(s["proposal_id"], s["kind"]) for s in r.json()["scopes"]] == [(b, "revised")]
    assert r.json()["emailed"] == ["buyer1@customer.example"]


def test_each_approval_of_a_scope_raises_its_own_decision_task(clean, outbox):
    """Fix round #2, on the real rule_v2 index (no status filter: a decided task keeps its key for
    ever). C approved -> task -> 'keep' -> C revised and approved again: a NEW task and email."""
    gid, (a, b, c) = group_of_three(sent=True)
    set_cols(c, proposal_status="approved", approved_at=ago(0))
    p = db.get_proposal(c)
    main._raise_scope_decision(dict(p, _approval_id=1))
    first, = [t for t in all_tasks() if t["detail"].get("stage") == rules.DECISION_STAGE]
    mails = lambda: [m["subject"] for m in outbox.sent].count(  # noqa: E731
        "Decide the other scopes — Acme HQ")
    once = mails()
    assert once >= 1, "the first approval emailed nobody"
    assert first["detail"]["rule_key"] == f"sd:{gid}:{c}:1"
    db.decide_followup_task(first["id"], "decided", KYLE, {"outcome": "keep"})
    main._raise_scope_decision(dict(p, _approval_id=2))
    main._raise_scope_decision(dict(p, _approval_id=2))          # a re-run of the same approval
    decs = [t for t in all_tasks() if t["detail"].get("stage") == rules.DECISION_STAGE]
    assert [(t["detail"]["rule_key"], t["detail"]["status"]) for t in decs] == [
        (f"sd:{gid}:{c}:1", "decided"), (f"sd:{gid}:{c}:2", "pending")]
    assert mails() == 2 * once, "the second approval's estimator email did not go (once)"


def test_a_scope_published_since_the_email_is_not_in_the_group_reminder(clean):
    """Fix round #1 on real rows: a grouped publish leaves followup_enrolled_at null (only the
    combined send enrols), and the group's reminder speaks only for the announced scopes."""
    gid, ids = group_of_three(sent=True)
    age_group(ids)
    add_draft(f"{gid}-d", gid, "Lab 3")
    add_proposal(f"{gid}-d", gid, "Lab 3", days_ago=1, enrolled=None, sent=False)
    assert db.get_proposal(f"{gid}-d")["followup_enrolled_at"] is None
    view, why, members = followup_worker.group_state_for(gid, db_now())
    assert why == "" and f"{gid}-d" in [m["proposal_id"] for m in members]
    assert [s["proposal_id"] for s in view["scopes"]] == ids
    assert [m["proposal_id"] for m in view["_open_members"]] == ids


def test_a_retired_approvals_deposit_tasks_leave_the_groups_reminder_count(clean):
    """Round 3 #4 (residual #17) on real rows: B was approved, its deposit reminders went out (SENT
    deposit-stage tasks), then a revision put it back to sent. Its own count keeps them (the single
    chase's deposit cap counts them); the GROUP's count -- worker and board -- leaves them out, so
    they never use up the siblings' "Stop after N"."""
    gid, (a, b, c) = group_of_three(sent=True)
    for age in (6, 5):
        add_followup(b, "followup_task", task(b, status="sent", stage="deposit",
                                              template="deposit_nudge"), days_ago=age)
    add_followup(b, "followup_task", task(b, status="sent", stage="pre", scope_group=gid),
                 days_ago=4)
    sig = db.followup_signals(b)
    assert len(sig["followup_reminder_times"]) == 3
    assert len(sig["group_reminder_times"]) == 1
    board = _board()
    assert sorted(board[b]["group_reminder_times"]) == sorted(sig["group_reminder_times"])
    add_draft("solo")
    add_proposal("solo")
    assert _board()["solo"]["group_reminder_times"] is None, "an ungrouped row pays nothing"
    view, why, _ = followup_worker.group_state_for(gid, db_now())
    assert why == "" and len(view["followup_reminder_times"]) == 1
