"""Deploy-day tooling, EXECUTED against a real Postgres: the dry run's read-only pool, the capability
route's follow-up DDL check, and the out-of-office backfill. (The release-day safety of the
follow-up EMAILS themselves, 2026-10-28, is test_pg_followup_auto.py.)

Run it like test_pg_scope_groups.py (its module docstring has the recipe): a throwaway local
Postgres, PORTAL_TEST_PG_URL pointing at it. Skipped without it.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import pg_fixture
from pg_fixture import needs_pg

pytestmark = [needs_pg, pytest.mark.realdb]

if pg_fixture.PG_URL:  # imported only when the tests will run
    from psycopg.types.json import Jsonb

    import db

KYLE = "kyle@estimator.example"


@pytest.fixture(scope="module")
def pgdb():
    with pg_fixture.scratch_portal_db("pf1") as pool:
        yield pool


@pytest.fixture
def clean(pgdb):
    db.execute("truncate public.portal_proposals, public.drafts, public.draft_revisions, "
               "public.portal_settings restart identity cascade")
    yield


def db_now() -> datetime:
    return db.q1("select now() as n")["n"]


def ago(days: float) -> datetime:
    return db_now() - timedelta(days=days)


def add_proposal(pid, *, status="viewed", enrolled_days=60.0, **cols):
    db.execute("insert into public.drafts (id, data, owner_email) values (%s, %s, %s)",
               (pid, Jsonb({"project_name": "Acme " + pid}), KYLE))
    db.create_portal_proposal(pid, "tok-" + pid, pid + "@customer.example", "Dana Customer",
                              "Acme " + pid, None, "pub@estimator.example", True, 1)
    base = {"created_at": ago(enrolled_days), "proposal_status": status,
            "followup_enrolled_at": ago(enrolled_days), "last_sent_at": ago(enrolled_days),
            "assigned_estimator": KYLE}
    base.update(cols)
    names = list(base)
    db.execute("update public.portal_proposals set "
               + ", ".join("%s = %%s" % n for n in names) + " where proposal_id = %s",
               tuple(base[n] for n in names) + (pid,))


def test_the_dry_run_pool_refuses_every_write(clean, pgdb):
    import pathlib
    import sys

    import psycopg

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "ops"))
    import followup_dry_run

    old = db._pool
    pool = followup_dry_run.install_read_only_pool(pgdb.conninfo)
    try:
        assert db.q1("select 1 as one")["one"] == 1
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            db.reserve_followup("nobody", "k", {"audience": "staff"})
    finally:
        db._pool = old
        pool.close()


# ── #29 / #30: the capability route reads the follow-up DDL from the catalog ───────
@pytest.fixture
def fresh_db():
    with pg_fixture.scratch_portal_db("pf1ddl") as pool:
        yield pool


def _caps(monkeypatch) -> dict:
    from fastapi.testclient import TestClient

    import main

    monkeypatch.setattr(main, "_admin_ok", lambda request: True)
    return TestClient(main.app).get("/api/admin/capabilities").json()


OLD_KIND_CHECK = ("alter table public.portal_followups add constraint portal_followups_kind_check "
                  "check (kind in ('auto_email','staff_call','staff_email','staff_text',"
                  "'staff_note','customer_status'))")


@pytest.mark.parametrize("missing", ["nothing", "kind_check", "one_open_task_v2", "rule_v2",
                                     "scope_columns"])
def test_group_followups_is_true_only_with_the_remind_only_ddl(fresh_db, monkeypatch, missing):
    if missing == "kind_check":
        db.execute("alter table public.portal_followups "
                   "drop constraint portal_followups_kind_check")
        db.execute(OLD_KIND_CHECK)
    elif missing == "one_open_task_v2":
        db.execute("drop index public.portal_followups_one_open_task_v2_uidx")
    elif missing == "rule_v2":
        db.execute("drop index public.portal_followups_rule_v2_uidx")
    elif missing == "scope_columns":
        db.execute("alter table public.portal_proposals drop column scope_group, "
                   "drop column scope_label")
    tasks_ready = missing in ("nothing", "scope_columns")
    assert db.followup_tasks_ready(max_age=0) is tasks_ready
    monkeypatch.setitem(db._FOLLOWUP_DDL_CACHE, "at", None)
    caps = _caps(monkeypatch)
    assert caps["scope_groups"] is (missing != "scope_columns")
    assert caps["group_followups"] is (missing == "nothing"), caps
    if missing == "kind_check":
        # What the flag protects: without the widened CHECK a task cannot be written at all.
        add_proposal("p1")
        with pytest.raises(Exception, match="kind_check"):
            db.reserve_followup_task("p1", "k1", {"stage": "pre"})
    # Applying schema.sql (the hand-applied remind-only block) makes it ready again.
    pg_fixture.apply_schema()
    assert db.followup_tasks_ready(max_age=0) is True


def test_a_kind_check_with_no_kind_restriction_at_all_still_reads_as_ready(fresh_db):
    db.execute("alter table public.portal_followups drop constraint portal_followups_kind_check")
    assert db.followup_tasks_ready(max_age=0) is True


# ── #13: the out-of-office backfill, on rows the OLD relay stored ──────────────────
def message(pid, author, body, meta, days_ago):
    row = db.q1("insert into public.portal_questions (proposal_id, author_kind, author_email, body, "
                "msg_type, meta, created_at) values (%s, %s, %s, %s, 'text', %s, "
                "now() - make_interval(secs => %s)) returning id",
                (pid, author, pid + "@customer.example", body,
                 Jsonb(meta) if meta is not None else None, float(days_ago) * 86400))
    return int(row["id"])


def _flags():
    return {r["id"]: r["meta"] for r in db.qall(
        "select id, meta from public.portal_questions order by id")}


def test_the_backfill_dry_runs_by_default_then_flags_only_autoresponders_once(clean, pgdb):
    import io
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "ops"))
    import backfill_auto_reply

    add_proposal("ooo", cycle_viewed_at=ago(59))
    add_proposal("real", cycle_viewed_at=ago(59))
    old = {"source": "email", "email_id": "e1", "from": "ooo@customer.example"}
    ooo = message("ooo", "customer", "I am currently out of the office and will return on "
                  "Monday. For urgent matters call the front desk.", old, 5)
    ooo2 = message("ooo", "customer", "This is an automated reply.", dict(old, email_id="e2"), 3)
    real = message("real", "customer", "Looks good, please send the contract.",
                   dict(old, email_id="e3"), 4)
    chat = message("real", "customer", "I am on vacation and will return on Monday.",
                   {"source": "portal"}, 2)            # typed in the portal: never touched
    staff = message("real", "staff", "I am out of the office and will return on Monday.",
                    {"source": "email"}, 1)            # staff rows are never touched
    done = message("ooo", "customer", "Automatic reply: away",
                   dict(old, email_id="e4", auto_reply=True), 6)
    before = _flags()
    touch_before = db.followup_signals("ooo")["customer_touch_at"]

    buf = io.StringIO()
    got = backfill_auto_reply.run(pgdb.conninfo, False, buf)
    assert _flags() == before, "the dry run wrote"
    assert sorted(m["id"] for m in got["matches"]) == [ooo, ooo2] and got["changed"] == 0
    text = buf.getvalue()
    assert "DRY RUN: 2 inbound customer row(s)" in text
    assert "I am currently out of the office" in text and "This is an automated reply." in text

    got = backfill_auto_reply.run(pgdb.conninfo, True, io.StringIO())
    assert got["changed"] == 2
    after = _flags()
    assert after[ooo]["auto_reply"] is True and after[ooo]["auto_reply_backfill"]
    assert after[ooo]["email_id"] == "e1", "the backfill dropped the row's own meta"
    assert after[ooo2]["auto_reply"] is True
    for untouched in (real, chat, staff, done):
        assert after[untouched] == before[untouched], untouched
    # The schedule stops counting them: with both flagged, "ooo" has no customer reply left.
    assert touch_before is not None and db.followup_signals("ooo")["customer_touch_at"] is None
    assert db.followup_signals("real")["customer_touch_at"] is not None
    # Idempotent.
    again = backfill_auto_reply.run(pgdb.conninfo, True, io.StringIO())
    assert again["matches"] == [] and again["changed"] == 0 and _flags() == after


def test_the_backfill_dry_run_cannot_write(clean, pgdb, monkeypatch):
    import io
    import pathlib
    import sys

    import psycopg

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "ops"))
    import backfill_auto_reply

    add_proposal("ooo")
    message("ooo", "customer", "This is an automated reply.", {"source": "email"}, 1)
    # Even a dry run that tried to write would be refused by its read-only connection.
    real_apply = backfill_auto_reply.apply

    def sneaky(conn):
        real_apply(conn, [1], "2026-10-25")
        return []

    monkeypatch.setattr(backfill_auto_reply, "find", sneaky)
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        backfill_auto_reply.run(pgdb.conninfo, False, io.StringIO())
