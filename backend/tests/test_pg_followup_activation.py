"""Follow-ups start at ACTIVATION, EXECUTED against a real Postgres (see test_followup_activation.py
for the rules).

  * the timeline: a proposal sent 30 days before activation gets NO email on the first enabled
    tick, its 1st email N days after activation and its 2nd M days after that; one already capped
    before activation gets nothing and no estimator note; one sent after activation runs as normal;
  * two workers recording the activation at the same moment store ONE value and both use it;
  * the ops command and the dry run, as real processes over the real database.

Only email_sender._send (the Resend call) is replaced, by a recorder; httpx.post is a tripwire.
Run with a throwaway local Postgres in PORTAL_TEST_PG_URL (see tests/pg_fixture.py). Skipped
without it.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import threading
from datetime import timedelta

import pytest

import pg_fixture
from pg_fixture import needs_pg

pytestmark = [needs_pg, pytest.mark.realdb, pytest.mark.realactivation]

if pg_fixture.PG_URL:  # imported only when the tests will run
    import db
    import followup_rules as rules
    import followup_settings as fs
    import followup_worker
    from test_pg_followup_auto import (KYLE, add_proposal, emails, in_window, old_row,  # noqa: F401
                                       outbox, set_cols)

D = timedelta(days=1)
H = timedelta(hours=1)
BACKEND = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def pgdb():
    with pg_fixture.scratch_portal_db("fuact") as pool:
        yield pool


@pytest.fixture
def clean(pgdb):
    db.execute("truncate public.portal_proposals, public.drafts, public.draft_revisions, "
               "public.portal_settings restart identity cascade")
    yield


def _activation_rows():
    return db.qall("select value, updated_by from public.portal_settings where id = %s",
                   (fs.ACTIVATION_ROW_ID,))


def _at(sim):
    """Stamp every row the worker wrote at the database's real clock with the simulated moment."""
    db.execute("update public.portal_followups set created_at = %s where created_at > %s",
               (sim, sim))


def test_a_proposal_sent_30_days_before_activation_is_followed_up_from_activation(clean, outbox):
    t0 = in_window() - 25 * D                    # the first enabled tick: activation
    add_proposal("old", enrolled_days=0)
    set_cols("old", followup_enrolled_at=t0 - 30 * D, created_at=t0 - 30 * D,
             last_sent_at=t0 - 30 * D)
    # The old ladder already sent this one's two not-opened emails: capped, and no note owed.
    add_proposal("capped", enrolled_days=0)
    set_cols("capped", followup_enrolled_at=t0 - 30 * D, created_at=t0 - 30 * D,
             last_sent_at=t0 - 30 * D)
    ck = rules.cycle_key(db.get_proposal("capped")["followup_enrolled_at"])
    old_row("capped", ck + ":nv1", "not_viewed", 0)
    old_row("capped", ck + ":nvr@1", "not_viewed", 0)
    db.execute("update public.portal_followups set created_at = %s where proposal_id = 'capped'",
               (t0 - 20 * D,))
    # Sent after activation: exactly the ordinary schedule.
    add_proposal("new", enrolled_days=0)
    set_cols("new", followup_enrolled_at=t0 + 2 * D, created_at=t0 + 2 * D,
             last_sent_at=t0 + 2 * D)
    assert _activation_rows() == []

    followup_worker._tick(t0)
    _at(t0)
    assert outbox.sent == [], "the first enabled tick sent something old"
    (row,) = _activation_rows()
    assert fs.activated_at_from(row["value"]) == t0
    assert row["updated_by"] == followup_worker.ACTIVATED_BY_WORKER

    sim = t0 + 6 * H
    while sim <= t0 + 25 * D:
        followup_worker._tick(sim)
        _at(sim)
        sim += 6 * H

    old = [r for r in emails("old") if r["detail"]["audience"] == "customer"]
    assert [r["detail"]["template"] for r in old] == ["not_viewed", "not_viewed_2"]
    first, second = old[0]["created_at"], old[1]["created_at"]
    assert t0 + 3 * D <= first < t0 + 4 * D, first - t0
    assert first + 7 * D <= second < first + 8 * D, second - first
    old_staff = [r for r in emails("old") if r["detail"]["audience"] == "staff"]
    assert len(old_staff) == 1 and old_staff[0]["created_at"] >= second + D
    assert emails("capped") == [], "a capped proposal was emailed, or its estimator told"
    new = [r for r in emails("new") if r["detail"]["audience"] == "customer"]
    assert [r["detail"]["template"] for r in new] == ["not_viewed", "not_viewed_2"]
    assert t0 + 5 * D <= new[0]["created_at"] < t0 + 6 * D
    # Ticking never moved the activation.
    (row,) = _activation_rows()
    assert fs.activated_at_from(row["value"]) == t0


def test_two_workers_recording_activation_at_once_store_one_moment(clean):
    t0 = in_window()
    barrier = threading.Barrier(2)
    got, errors = [], []

    def claim(at):
        try:
            barrier.wait()
            got.append(followup_worker.ensure_activation(at))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=claim, args=(t0 + k * H,)) for k in (0, 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    (row,) = _activation_rows()
    assert got[0] == got[1] == fs.activated_at_from(row["value"])
    assert got[0] in (t0, t0 + H)


def test_twin_first_ticks_send_nothing_old_and_record_once(clean, outbox):
    add_proposal("old", enrolled_days=30)
    t0 = in_window()
    barrier = threading.Barrier(2)
    errors = []

    def tick(at):
        try:
            barrier.wait()
            followup_worker._tick(at)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=tick, args=(t0 + k * H,)) for k in (0, 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and outbox.sent == [] and len(_activation_rows()) == 1


def _run(script, *args, env_extra=None, pgdb=None):
    env = dict(os.environ, DATABASE_URL=pgdb.conninfo, FOLLOWUP_AUTOMATION_ENABLED="false")
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "ops/" + script, *args], cwd=str(BACKEND), env=env,
                          capture_output=True, timeout=120)


def test_the_ops_command_and_the_dry_run_as_real_processes(clean, pgdb):
    add_proposal("old", enrolled_days=30)
    t0 = in_window()
    # Before activation: the dry run evaluates as if activated at --now, and writes nothing.
    proc = _run("followup_dry_run.py", "--now", t0.isoformat(), pgdb=pgdb)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert ("not yet activated: the first enabled tick will set it to %s" % t0.isoformat()).encode() \
        in proc.stdout
    assert b"customer emails it would send ..... 0" in proc.stdout
    assert _activation_rows() == []

    proc = _run("followup_activate.py", "--at", t0.isoformat(), "--by", "release-runbook",
                pgdb=pgdb)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert b"set activated_at to" in proc.stdout
    (row,) = _activation_rows()
    assert fs.activated_at_from(row["value"]) == t0 and row["updated_by"] == "release-runbook"
    again = _run("followup_activate.py", "--at", t0.isoformat(), pgdb=pgdb)
    assert again.returncode == 0 and b"unchanged" in again.stdout
    moved = _run("followup_activate.py", "--at", (t0 + D).isoformat(), pgdb=pgdb)
    assert moved.returncode == 3 and fs.activated_at_from(_activation_rows()[0]["value"]) == t0

    # Right after activation: nothing old. At activation + 3 days: the old proposal's 1st.
    proc = _run("followup_dry_run.py", "--json", "--now", (t0 + H).isoformat(), pgdb=pgdb)
    result = json.loads(proc.stdout.decode("utf-8"))
    assert fs.activated_at_from({"activated_at": result["activated_at"]}) == t0
    assert result["customer_emails"] == [] and result["activation_pending"] is False
    proc = _run("followup_dry_run.py", "--json", "--now", (t0 + 3 * D).isoformat(), pgdb=pgdb)
    result = json.loads(proc.stdout.decode("utf-8"))
    assert [e["proposal_id"] for e in result["customer_emails"]] == ["old"]
    assert db.qall("select 1 from public.portal_followups") == [], "a dry run wrote a row"
