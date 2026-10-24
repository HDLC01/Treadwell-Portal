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


def test_followup_signals_reads_every_touch(clean):
    add_proposal("p1", enrolled=20, days_ago=20)
    db.execute("insert into public.portal_questions (proposal_id, author_kind, author_email, body, "
               "created_at) values ('p1', 'customer', 'dana@customer.example', 'Any update?', "
               "now() - interval '1 day')")
    db.execute("insert into public.portal_questions (proposal_id, author_kind, author_email, body, "
               "meta, created_at) values ('p1', 'customer', 'ooo@customer.example', 'Away', "
               "'{\"auto_reply\": true}'::jsonb, now())")
    db.execute("insert into public.portal_questions (proposal_id, author_kind, body, msg_type, "
               "created_at) values ('p1', 'staff', 'Following up', 'text', now() - interval '2 days')")
    add_followup("p1", "staff_call", {"note": "left a voicemail"}, days_ago=3)
    add_followup("p1", "staff_note", {"action": "reassigned"}, days_ago=0)   # bookkeeping
    sig = db.followup_signals("p1")
    assert set(sig) >= {"customer_touch_at", "last_staff_followup_at", "last_staff_message_at",
                        "followup_reminder_times", "pending_task"}
    assert sig["customer_touch_at"] is not None
    assert sig["last_staff_message_at"] is not None
    assert (db_now() - sig["last_staff_followup_at"]) > timedelta(days=2)   # the call, not the note
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

    # The approved scope's decision task is newer, so the members show it; the home still holds
    # the reminder task under its own stage.
    set_cols(c, proposal_status="approved", approved_at=ago(0))
    dec = db.reserve_followup_task(c, f"sd:{gid}:{c}", {
        "audience": "staff", "template": "scope_decision", "stage": "decision",
        "scope_group": gid, "scopes": [{"proposal_id": a}, {"proposal_id": b}]})
    board = _board()
    assert {board[i]["pending_task"]["id"] for i in ids} == {dec}
    assert board["solo"]["pending_task"]["id"] == solo_task
    open_ids = [t["id"] for t in db.list_open_group_tasks(gid)]
    assert open_ids == [group_task, dec]

    # Once decided, the members fall back to the open reminder task.
    db.decide_followup_task(dec, "decided", "kyle", {"action": "leave"})
    board = _board()
    assert {board[i]["pending_task"]["id"] for i in ids} == {group_task}


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
    out = followup_worker.dry_run_count(db_now())
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
