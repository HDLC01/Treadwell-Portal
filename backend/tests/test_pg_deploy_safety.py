"""Deploy-day safety (fix round PF1), EXECUTED against a real Postgres seeded with the old automatic
ladder's history: the rows the old followup_rules left in portal_followups (kind auto_email, detail
{audience, template, rule_key}, no `engine` mark) on proposals still in flight when remind-only ships.

Run it like test_pg_scope_groups.py (its module docstring has the recipe): a throwaway local
Postgres, PORTAL_TEST_PG_URL pointing at it. Skipped without it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import pg_fixture
from pg_fixture import needs_pg

pytestmark = [needs_pg, pytest.mark.realdb]

if pg_fixture.PG_URL:  # imported only when the tests will run
    from psycopg.types.json import Jsonb

    import db
    import followup_rules as rules
    import followup_worker

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


@pytest.fixture
def outbox(monkeypatch):
    """Every email the code tries to send, recorded with no network."""
    import types

    import email_sender

    box = types.SimpleNamespace(sent=[])

    def fake_send(to, subject, html, headers=None, reply_to=None, attachments=None):
        to = [t for t in to if t]
        if not to:
            return False
        box.sent.append({"to": list(to), "subject": subject})
        return True

    def no_network(*a, **k):
        raise AssertionError("a test reached for Resend")

    monkeypatch.setattr(email_sender, "_send", fake_send)
    monkeypatch.setattr(email_sender.httpx, "post", no_network)
    monkeypatch.setenv("FOLLOWUP_AUTOMATION_ENABLED", "true")
    return box


def db_now() -> datetime:
    return db.q1("select now() as n")["n"]


def ago(days: float) -> datetime:
    return db_now() - timedelta(days=days)


def in_window() -> datetime:
    """The latest noon (Central) at or before the database's now (see test_pg_scope_groups)."""
    local = db_now().astimezone(rules.BUSINESS_TZ)
    noon = local.replace(hour=12, minute=0, second=0, microsecond=0)
    if noon > local:
        noon -= timedelta(days=1)
    return noon.astimezone(timezone.utc)


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


def old_email(pid, key, *, audience="customer", template="checkin", days_ago=0.0):
    """One row exactly as the OLD automatic ladder reserved it (followup_worker on origin/main)."""
    db.execute("insert into public.portal_followups (proposal_id, kind, detail, created_at) "
               "values (%s, 'auto_email', %s, now() - make_interval(secs => %s))",
               (pid, Jsonb({"audience": audience, "template": template, "rule_key": key}),
                float(days_ago) * 86400))


def customer_message(pid, days_ago):
    db.execute("insert into public.portal_questions (proposal_id, author_kind, author_email, body, "
               "msg_type, meta, created_at) values (%s, 'customer', %s, 'Sounds good', 'text', "
               "%s, now() - make_interval(secs => %s))",
               (pid, pid + "@customer.example", Jsonb({"source": "email"}), float(days_ago) * 86400))


def cycle(pid) -> str:
    return rules.cycle_key(db.get_proposal(pid)["followup_enrolled_at"])


def tasks(pid=None):
    rows = db.qall("select proposal_id, detail from public.portal_followups "
                   "where kind = 'followup_task' order by id")
    return [r for r in rows if pid is None or r["proposal_id"] == pid]


# ── #10: an automatic email after the reply holds the touched reminder back ────────
def test_a_reply_weeks_ago_and_an_automatic_email_two_days_ago_raise_nothing(clean, outbox):
    add_proposal("replied", cycle_viewed_at=ago(59))
    customer_message("replied", 20)
    ck = cycle("replied")
    old_email("replied", ck + ":v1", template="next_steps", days_ago=58)
    old_email("replied", ck + ":v2", template="second_nudge", days_ago=56)
    old_email("replied", ck + ":vr@20990101T16", days_ago=2)      # the old ladder, 2 days ago
    now = in_window()
    p = dict(db.get_proposal("replied"), **{k: v for k, v in
                                            db.followup_signals("replied").items()
                                            if k != "pending_task"})
    assert [d for d in rules.due_now(p, now) if d.audience == "customer"] == []
    newest = max(rules._ts(t) for t in p["followup_reminder_times"])
    assert rules.next_due_at(p, now) == newest + timedelta(hours=72)
    assert followup_worker.dry_run_count(now)["tasks"] == []
    followup_worker._tick(now)
    assert tasks("replied") == []
    # A reply with no automatic email after it still gets its touched reminder.
    add_proposal("quiet", cycle_viewed_at=ago(59))
    customer_message("quiet", 20)
    followup_worker._tick(now)
    got = tasks("quiet")
    assert len(got) == 1 and ":vrt@" in got[0]["detail"]["rule_key"]


# ── #11 / #3: a deposit chase the old cap had finished stays finished ──────────────
def approved_job(pid, approved_days, n_recurring, *, deposit_status="pending"):
    """An approved job whose old ladder sent dep1 + n depr@ customer emails and dep_staff +
    n depsr@ staff emails, every 3 days from approval (the old cadence)."""
    add_proposal(pid, status="approved", enrolled_days=approved_days + 10,
                 cycle_viewed_at=ago(approved_days + 9), approved_at=ago(approved_days),
                 deposit_status=deposit_status)
    ck = cycle(pid)
    if deposit_status == "pending":
        old_email(pid, ck + ":dep1", template="deposit_nudge", days_ago=approved_days - 1)
    old_email(pid, ck + ":dep_staff", audience="staff", template="staff_deposit_outstanding",
              days_ago=approved_days - 2)
    for i in range(1, n_recurring + 1):
        if deposit_status == "pending":
            old_email(pid, "%s:depr@old%02d" % (ck, i), template="deposit_nudge",
                      days_ago=approved_days - 1 - 3 * i)
        old_email(pid, "%s:depsr@old%02d" % (ck, i), audience="staff",
                  template="staff_deposit_outstanding", days_ago=approved_days - 2 - 3 * i)


def test_deposit_chases_the_old_cap_finished_do_not_restart(clean, outbox):
    for days, n in ((70, 20), (100, 22), (150, 23)):
        approved_job("done%d" % days, days, n)
    approved_job("submitted", 100, 20, deposit_status="submitted")
    approved_job("young", 30, 5)
    now = in_window()
    plan = followup_worker.dry_run_count(now)
    assert {t["proposal_id"] for t in plan["tasks"]} == {"young"}
    assert {e["proposal_id"] for e in plan["staff_emails"]} == {"young"}
    assert plan["deposit_restarts"] == []
    held = {(h["proposal_id"], h["audience"]) for h in plan["deposit_held"]}
    assert held == {("done70", "customer"), ("done70", "staff"), ("done100", "customer"),
                    ("done100", "staff"), ("done150", "customer"), ("done150", "staff"),
                    ("submitted", "staff")}

    followup_worker._tick(now)
    assert {t["proposal_id"] for t in tasks()} == {"young"}
    assert tasks("young")[0]["detail"]["template"] == "deposit_nudge"
    staff_rows = db.qall("select proposal_id, detail from public.portal_followups "
                         "where kind = 'auto_email' and detail ->> 'engine' is not null")
    assert [r["proposal_id"] for r in staff_rows] == ["young"]
    assert staff_rows[0]["detail"]["template"] == "staff_deposit_outstanding"
    # Two estimator emails, both about the young job: the task and the deposit note.
    assert len(outbox.sent) == 2 and {m["to"][0] for m in outbox.sent} == {KYLE}


def test_the_deposit_history_signal_counts_only_the_old_ladders_staff_rows(clean):
    approved_job("mixed", 40, 6)
    ck = cycle("mixed")
    db.reserve_followup("mixed", ck + ":depsr@new01", {"audience": "staff",
                                                       "template": "staff_deposit_outstanding",
                                                       "engine": followup_worker.ENGINE_MARK})
    db.reserve_followup("mixed", ck + ":depst@new02", {"audience": "staff",
                                                       "template": "staff_deposit_outstanding",
                                                       "engine": followup_worker.ENGINE_MARK})
    db.reserve_followup("mixed", ck + ":v48_staff", {"audience": "staff"})
    sig = db.followup_signals("mixed")["deposit_history"]
    assert sig == {"customer_legacy": 6, "staff_legacy": 6, "staff": 9}
    board = {r["proposal_id"]: r for r in db.list_all_portal_proposals()}
    assert board["mixed"]["deposit_history"] == sig


# ── #12 / #18 / #4: the entry point, run the way Hanz runs it in the container ─────
def test_the_dry_run_script_runs_against_the_database_and_writes_nothing(clean, pgdb):
    import json
    import os
    import pathlib
    import subprocess
    import sys

    approved_job("young", 30, 5)
    approved_job("done", 100, 20)
    add_proposal("chat", cycle_viewed_at=ago(30))
    customer_message("chat", 10)
    count = "select (select count(*) from public.portal_followups) as f, " \
            "(select count(*) from public.portal_questions) as q"
    before = db.q1(count)
    backend = pathlib.Path(__file__).resolve().parents[1]
    env = dict(os.environ, DATABASE_URL=pgdb.conninfo, FOLLOWUP_AUTOMATION_ENABLED="false")
    proc = subprocess.run([sys.executable, "ops/followup_dry_run.py", "--json"], cwd=str(backend),
                          env=env, capture_output=True, timeout=120)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    result = json.loads(proc.stdout.decode("utf-8"))
    assert db.q1(count) == before, "the dry run wrote a row"
    assert result["skipped"] == []
    assert {t["proposal_id"] for t in result["tasks"]} == {"young", "chat"}
    assert [t["proposal_id"] for t in result["touched_tasks"]] == ["chat"]
    assert sorted(e["template"] for e in result["staff_emails"]) == \
        ["staff_deposit_outstanding", "staff_followup_due", "staff_followup_due"]
    assert result["deposit_restarts"] == []
    assert {h["proposal_id"] for h in result["deposit_held"]} == {"done"}
    # The table form runs too.
    proc = subprocess.run([sys.executable, "ops/followup_dry_run.py"], cwd=str(backend), env=env,
                          capture_output=True, timeout=120)
    assert proc.returncode == 0 and b"estimator emails it would send .... 3" in proc.stdout


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
