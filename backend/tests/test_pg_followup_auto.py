"""Follow-up emails to the customer (2026-10-28), EXECUTED against a real Postgres.

  * a timeline sent -> opened -> approved produces at most 2 + 2 customer emails, the estimator
    hears once per step, and nothing follows the approval;
  * two containers ticking at the same moment send one email;
  * release-day safety on rows exactly as the OLD engines left them: a proposal that already had 2
    reminders in its current step gets none, one with 1 gets only the 2nd, and the dry run (the
    real ops script, over a READ-ONLY pool) names exactly the customers the first tick emails;
  * the live prod settings row, saved in portal_settings, is read as its migration.

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
from datetime import datetime, timedelta, timezone

import pytest

import pg_fixture
from pg_fixture import needs_pg

pytestmark = [needs_pg, pytest.mark.realdb]

if pg_fixture.PG_URL:  # imported only when the tests will run
    from psycopg.types.json import Jsonb

    import db
    import followup_rules as rules
    import followup_settings as fs
    import followup_worker

KYLE = "kyle@estimator.example"
D = timedelta(days=1)
H = timedelta(hours=1)


@pytest.fixture(scope="module")
def pgdb():
    with pg_fixture.scratch_portal_db("fuauto") as pool:
        yield pool


@pytest.fixture
def clean(pgdb):
    db.execute("truncate public.portal_proposals, public.drafts, public.draft_revisions, "
               "public.portal_settings restart identity cascade")
    yield


@pytest.fixture
def outbox(monkeypatch):
    import types

    import email_sender

    box = types.SimpleNamespace(sent=[])
    lock = threading.Lock()

    def fake_send(to, subject, html, headers=None, reply_to=None, attachments=None):
        to = [t for t in to if t]
        if not to:
            return False
        with lock:
            box.sent.append({"to": list(to), "subject": subject, "html": html})
        return True

    def no_network(*a, **k):
        raise AssertionError("a test reached for Resend")

    monkeypatch.setattr(email_sender, "_send", fake_send)
    monkeypatch.setattr(email_sender.httpx, "post", no_network)
    monkeypatch.setenv("FOLLOWUP_AUTOMATION_ENABLED", "true")
    box.customers = lambda: [t for m in box.sent for t in m["to"] if t != KYLE]
    box.staff = lambda: [m for m in box.sent if KYLE in m["to"]]
    return box


def db_now() -> datetime:
    return db.q1("select now() as n")["n"]


def ago(days: float) -> datetime:
    return db_now() - timedelta(days=days)


def in_window() -> datetime:
    """The latest noon (Central) at or before the database's now."""
    local = db_now().astimezone(rules.BUSINESS_TZ)
    noon = local.replace(hour=12, minute=0, second=0, microsecond=0)
    if noon > local:
        noon -= timedelta(days=1)
    return noon.astimezone(timezone.utc)


def add_proposal(pid, *, status="sent", enrolled_days=10.0, **cols):
    db.execute("insert into public.drafts (id, data, owner_email) values (%s, %s, %s)",
               (pid, Jsonb({"project_name": "Acme " + pid}), KYLE))
    db.create_portal_proposal(pid, "tok-" + pid, pid + "@customer.example", "Dana Customer",
                              "Acme " + pid, None, "pub@estimator.example", True, 1)
    db.add_recipient(pid, pid + "@customer.example")
    base = {"created_at": ago(enrolled_days), "proposal_status": status,
            "followup_enrolled_at": ago(enrolled_days), "last_sent_at": ago(enrolled_days),
            "assigned_estimator": KYLE}
    base.update(cols)
    set_cols(pid, **base)


def set_cols(pid, **cols):
    names = list(cols)
    db.execute("update public.portal_proposals set "
               + ", ".join("%s = %%s" % n for n in names) + " where proposal_id = %s",
               tuple(cols[n] for n in names) + (pid,))


def old_row(pid, key, template, days_ago, *, kind="auto_email", status=None):
    """A row exactly as an OLDER engine left it: the automatic ladder's auto_email, or a
    remind-only task an estimator sent or answered with a call."""
    detail = {"audience": "customer", "template": template, "rule_key": key}
    if kind == "followup_task":
        detail.update(status=status, stage="pre")
    db.execute("insert into public.portal_followups (proposal_id, kind, detail, created_at) "
               "values (%s, %s, %s, now() - make_interval(secs => %s))",
               (pid, kind, Jsonb(detail), float(days_ago) * 86400))


def emails(pid=None):
    rows = db.qall("select proposal_id, detail, created_at from public.portal_followups "
                   "where kind = 'auto_email' and detail ->> 'engine' is not null order by id")
    return [r for r in rows if pid is None or r["proposal_id"] == pid]


# ── the timeline ──────────────────────────────────────────────────────────────
def test_sent_opened_approved_is_at_most_two_plus_two_and_nothing_after_approval(clean, outbox):
    start = in_window() - 40 * D
    add_proposal("job", enrolled_days=0)
    set_cols("job", followup_enrolled_at=start, created_at=start, last_sent_at=start)
    opened, approved = start + 13 * D, start + 27 * D
    log = []
    sim = start
    while sim <= start + 40 * D:
        if sim >= opened and db.get_proposal("job")["cycle_viewed_at"] is None:
            set_cols("job", proposal_status="viewed", cycle_viewed_at=opened)
        if sim >= approved and db.get_proposal("job")["proposal_status"] != "approved":
            set_cols("job", proposal_status="approved", approved_at=approved)
        before = len(outbox.sent)
        followup_worker._tick(sim)
        # The worker reserved its rows at the database's real clock; put them at the simulated
        # moment, so the next 7-days-after-the-1st is measured on the simulated calendar.
        db.execute("update public.portal_followups set created_at = %s where created_at > %s",
                   (sim, sim))
        for m in outbox.sent[before:]:
            log.append((sim, m["to"][0], m["subject"]))
        sim += 6 * H

    rows = emails("job")
    customer = [r for r in rows if r["detail"]["audience"] == "customer"]
    staff = [r for r in rows if r["detail"]["audience"] == "staff"]
    assert [r["detail"]["template"] for r in customer] == \
        ["not_viewed", "not_viewed_2", "next_steps", "second_nudge"], log
    assert len(outbox.customers()) == 4
    assert [r["detail"]["rule_key"].rsplit(":", 1)[1] for r in staff] == ["nv_est", "v_est"]
    assert len(outbox.staff()) == 2
    assert all(r["created_at"] < approved for r in rows), "an email after the approval"
    c = [r["created_at"] for r in customer]
    assert c[0] >= start + 3 * D and c[1] >= c[0] + 7 * D
    assert c[2] >= opened + 3 * D and c[3] >= c[2] + 7 * D


def test_two_containers_ticking_at_once_send_one_email(clean, outbox):
    add_proposal("twin", enrolled_days=5)
    now = in_window()
    barrier = threading.Barrier(2)
    errors = []

    def tick():
        try:
            barrier.wait()
            followup_worker._tick(now)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=tick) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert outbox.customers() == ["twin@customer.example"]
    assert len(emails("twin")) == 1


# ── release day ───────────────────────────────────────────────────────────────
def _release_day_rows():
    add_proposal("twice", status="viewed", enrolled_days=40, cycle_viewed_at=ago(39))
    ck = rules.cycle_key(db.get_proposal("twice")["followup_enrolled_at"])
    old_row("twice", ck + ":v1", "next_steps", 38)
    old_row("twice", ck + ":vr@20261001T15", "checkin", 30)
    add_proposal("once", status="viewed", enrolled_days=40, cycle_viewed_at=ago(39))
    old_row("once", rules.cycle_key(db.get_proposal("once")["followup_enrolled_at"]) + ":v1",
            "next_steps", 38)
    add_proposal("tasks", status="viewed", enrolled_days=40, cycle_viewed_at=ago(39))
    ck = rules.cycle_key(db.get_proposal("tasks")["followup_enrolled_at"])
    old_row("tasks", ck + ":v1", "next_steps", 38, kind="followup_task", status="sent")
    old_row("tasks", ck + ":v2", "second_nudge", 35, kind="followup_task", status="called")
    add_proposal("fresh", enrolled_days=5)
    # A revision since: the old cycle's two emails do not count against the new send.
    add_proposal("revised", enrolled_days=5)
    old_row("revised", "c1:nv1", "not_viewed", 20)
    old_row("revised", "c1:nvr@20261001T15", "not_viewed", 15)
    # Approved with the deposit outstanding: no deposit chase.
    add_proposal("won", status="approved", enrolled_days=40, cycle_viewed_at=ago(39),
                 approved_at=ago(30), deposit_status="pending")
    # An old reminder task still open: retired, not sent by hand next to the worker's email.
    add_proposal("pending", enrolled_days=1)
    db.reserve_followup_task("pending", "c1:nv1", {"template": "not_viewed", "stage": "pre"})


def test_the_first_tick_emails_only_what_is_owed_and_never_resends(clean, outbox):
    _release_day_rows()
    now = in_window()
    dry = followup_worker.dry_run_count(now)
    assert dry["skipped"] == []
    named = {e["proposal_id"]: e for e in dry["customer_emails"]}
    assert set(named) == {"once", "fresh", "revised"}, named
    assert named["once"]["template"] == "second_nudge" and named["once"]["ordinal"] == 2
    assert [t["proposal_id"] for t in dry["tasks_to_expire"]] == ["pending"]

    followup_worker._tick(now)
    assert sorted(outbox.customers()) == sorted(
        a for e in dry["customer_emails"] for a in e["recipients"])
    assert sorted(outbox.customers()) == ["fresh@customer.example", "once@customer.example",
                                          "revised@customer.example"]
    t = db.qall("select detail from public.portal_followups where proposal_id = 'pending' "
                "and kind = 'followup_task'")[0]["detail"]
    assert t["status"] == "expired" and t["reason"] == "auto_followups"
    # The next ticks, through every later day: nothing is ever re-sent, and "twice" / "tasks" /
    # "won" are never emailed at all.
    for days in (0, 1, 2):
        followup_worker._tick(now + days * H)
    assert len(outbox.customers()) == 3
    assert followup_worker.dry_run_count(now)["customer_emails"] == []


def test_the_dry_run_script_names_the_customers_and_writes_nothing(clean, pgdb):
    _release_day_rows()
    # A separate process does not see conftest's "activated long ago" stub: store it for real.
    db.save_settings(fs.ACTIVATION_ROW_ID, fs.activation_value(ago(365)), "test")
    count = "select (select count(*) from public.portal_followups) as f, " \
            "(select count(*) from public.portal_questions) as q"
    before = db.q1(count)
    backend = pathlib.Path(__file__).resolve().parents[1]
    env = dict(os.environ, DATABASE_URL=pgdb.conninfo, FOLLOWUP_AUTOMATION_ENABLED="false")
    when = in_window()
    proc = subprocess.run([sys.executable, "ops/followup_dry_run.py", "--json", "--now",
                           when.isoformat()], cwd=str(backend), env=env, capture_output=True,
                          timeout=120)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    result = json.loads(proc.stdout.decode("utf-8"))
    assert db.q1(count) == before, "the dry run wrote a row"
    assert result["at"] == when.isoformat() and result["skipped"] == []
    assert sorted(a for e in result["customer_emails"] for a in e["recipients"]) == \
        ["fresh@customer.example", "once@customer.example", "revised@customer.example"]
    proc = subprocess.run([sys.executable, "ops/followup_dry_run.py", "--now", when.isoformat()],
                          cwd=str(backend), env=env, capture_output=True, timeout=120)
    assert proc.returncode == 0
    assert b"customer emails it would send ..... 3 (to 3 addresses)" in proc.stdout
    assert b"once@customer.example" in proc.stdout


# ── settings ──────────────────────────────────────────────────────────────────
def test_the_live_prod_settings_row_is_read_as_its_migration(clean, outbox):
    prod = {"first_nudge_hours": 72, "second_nudge_hours": 168, "recurring_hours": 2160,
            "max_recurring": 1, "staff_personal_hours": 24,
            "thread_subject": "Your Treadwell proposal — {project}",
            "templates": {"not_viewed": {"title": "Is it there?",
                                         "body": "Hi {first_name},\n\n{link}", "cta": "View"}}}
    db.save_settings(fs.ROW_ID, prod, "hanz@wetreadwell.com")
    cfg = followup_worker._settings()
    assert (cfg["not_opened_first_days"], cfg["not_opened_second_days"],
            cfg["estimator_days"]) == (3, 7, 1)
    add_proposal("early", enrolled_days=2)       # 2 days: not yet (3 days after sending)
    add_proposal("due", enrolled_days=4)
    followup_worker._tick(in_window())
    assert outbox.customers() == ["due@customer.example"]
    assert "Is it there?" in outbox.sent[0]["html"], "the saved wording was not used"


# ── review 2026-10-29: rows as the OLDER engines and the schema left them ─────────
def old_staff_note(pid, key, days_ago):
    """The old ladder's estimator note, exactly as it reserved it (audience staff)."""
    db.execute("insert into public.portal_followups (proposal_id, kind, detail, created_at) "
               "values (%s, 'auto_email', %s, now() - make_interval(secs => %s))",
               (pid, Jsonb({"audience": "staff", "template": "staff_personal_followup",
                            "rule_key": key}), float(days_ago) * 86400))


def test_release_day_sends_no_second_estimator_note_and_never_says_not_opened(clean, outbox):
    # Chased by the old ladder 5 times and reported to the estimator: nothing to anybody.
    add_proposal("chased", status="viewed", enrolled_days=60, cycle_viewed_at=ago(59))
    ck = rules.cycle_key(db.get_proposal("chased")["followup_enrolled_at"])
    for i in range(5):
        old_row("chased", ck + ":vr@%d" % i, "checkin", 50 - i)
    old_staff_note("chased", ck + ":v48_staff", 58)
    # The same, never reported (no old note): the estimator hears once.
    add_proposal("unreported", status="viewed", enrolled_days=60, cycle_viewed_at=ago(59))
    ck = rules.cycle_key(db.get_proposal("unreported")["followup_enrolled_at"])
    for i in range(2):
        old_row("unreported", ck + ":vr@%d" % i, "checkin", 50 - i)
    # Opened before cycle_viewed_at existed: 'viewed', no cycle stamp. It is OPENED.
    add_proposal("legacyview", status="viewed", enrolled_days=60, viewed_at=ago(55),
                 last_viewed_at=ago(54))
    assert db.get_proposal("legacyview")["cycle_viewed_at"] is None

    hist = db.followup_signals("chased")["followup_sent_history"]
    assert [h["audience"] for h in hist].count("staff") == 1
    assert len(rules.sent_history(dict(db.get_proposal("chased"), followup_sent_history=hist))) == 5

    now = in_window()
    dry = followup_worker.dry_run_count(now)
    named = {e["proposal_id"]: e["template"] for e in dry["customer_emails"]}
    assert named == {"legacyview": "next_steps"}, dry
    assert sorted(s["proposal_id"] for s in dry["staff_emails"]) == ["unreported"], dry
    followup_worker._tick(now)
    assert outbox.customers() == ["legacyview@customer.example"]
    assert len(outbox.staff()) == 1
    staff_keys = [r["detail"]["rule_key"] for r in emails() if r["detail"]["audience"] == "staff"]
    assert [k.rsplit(":", 1)[1] for k in staff_keys] == ["v_est"]
    assert db.get_proposal("legacyview") is not None
    sent = [r["detail"]["template"] for r in emails("legacyview")]
    assert sent == ["next_steps"], "a customer who opened it was told it was not opened"


# ── decision B (Hanz, 2026-10-28): a reply skips the step's 2nd email ──────────────
def _at(sim):
    """Stamp every row written at the database's real clock with the simulated moment."""
    for t in ("portal_followups", "portal_questions"):
        db.execute("update public.%s set created_at = %%s where created_at > %%s" % t, (sim, sim))


def test_a_reply_or_logged_contact_after_the_first_email_skips_the_second(clean, outbox):
    """A timeline over real SQL: the contact predicates are the database's (auto-reply excluded,
    a note and a task's own row are not contact), the plan is the worker's."""
    start = in_window() - 20 * D
    contact_at = start + 5 * D                      # after the 1st (day 3), before the 2nd (day 10)
    jobs = ("quiet", "reply", "ooo", "call", "text", "message", "note", "taskrow", "early")
    for pid in jobs:
        add_proposal(pid, enrolled_days=0)
        set_cols(pid, followup_enrolled_at=start, created_at=start, last_sent_at=start)

    def contact(sim):
        if sim == start + D:                        # BEFORE the 1st: skips nothing
            db.add_message("early", "customer", "early@customer.example", "Got it, thanks",
                           msg_type="text")
        if sim != contact_at:
            return
        db.add_message("reply", "customer", "reply@customer.example", "Can we talk Tuesday?",
                       msg_type="text")
        db.add_message("ooo", "customer", "ooo@customer.example", "I am out of the office",
                       msg_type="text", meta={"auto_reply": True})
        db.add_followup("call", "staff_call", {"note": "Spoke to Dana"}, KYLE)
        db.add_followup("text", "staff_text", {"note": "Texted Dana"}, KYLE)
        db.add_message("message", "staff", KYLE, "Happy to walk you through it.", msg_type="text")
        db.add_followup("note", "staff_note", {"note": "Dana is on holiday"}, KYLE)
        db.add_followup("taskrow", "staff_email",
                        {"note": "reminder", "task_id": 1, "followup_task": True}, KYLE)

    sim = start
    while sim <= start + 20 * D:
        contact(sim)
        _at(sim)
        followup_worker._tick(sim)
        _at(sim)
        sim += 6 * H

    sent = {pid: [r["detail"]["template"] for r in emails(pid)
                  if r["detail"]["audience"] == "customer"] for pid in jobs}
    two, one = ["not_viewed", "not_viewed_2"], ["not_viewed"]
    assert sent == {"quiet": two, "reply": one, "ooo": two, "call": one, "text": one,
                    "message": one, "note": two, "taskrow": two, "early": two}, sent
    # Every one of them told the estimator exactly once.
    for pid in jobs:
        staff = [r for r in emails(pid) if r["detail"]["audience"] == "staff"]
        assert [r["detail"]["rule_key"].rsplit(":", 1)[1] for r in staff] == ["nv_est"], pid
    # The board reads the same contact: no 2nd-email date for a skipped proposal.
    board = {r["proposal_id"]: r for r in db.list_all_portal_proposals()}
    for pid in ("reply", "call", "text", "message"):
        sig = db.followup_signals(pid)
        row = board[pid]
        for k, _name in rules.CONTACT_KEYS:
            assert row[k] == sig[k], (pid, k)
        assert rules.plan(dict(row), start + 9 * D).skipped_key, pid
    assert board["ooo"]["customer_touch_at"] is None, "an auto-reply counted as a reply"
    assert board["note"]["last_staff_contact_at"] is None, "a note counted as contact"
    assert board["taskrow"]["last_staff_contact_at"] is None, "a task's own row counted"


def test_a_project_delayed_click_after_the_first_is_a_pause_not_a_reply(clean, outbox,
                                                                        monkeypatch):
    """The customer's own "Project delayed" click, through the real endpoint, after the 1st email.
    It is customer-authored (a status_update row), but it is the pause, not a reply: the 2nd email
    is the nudge that goes when the pause ends, and the estimator is never told they replied."""
    from fastapi.testclient import TestClient

    import main

    monkeypatch.setattr(main, "_can_access", lambda request, p: True)
    monkeypatch.setattr(main, "_session_email", lambda request: None)
    monkeypatch.setattr(main.ratelimit, "allow_ip", lambda *a, **k: True)
    client = TestClient(main.app)

    start = in_window() - 30 * D
    click_at = start + 5 * D                       # after the 1st (day 3), before the 2nd (day 10)
    until = (start + 15 * D).astimezone(rules.BUSINESS_TZ).date()
    resume = datetime(until.year, until.month, until.day, rules._schedule(None)["send_start_hour"],
                      tzinfo=rules.BUSINESS_TZ) + D
    add_proposal("held", enrolled_days=0)
    set_cols("held", followup_enrolled_at=start, created_at=start, last_sent_at=start)

    sim = start
    while sim <= start + 25 * D:
        if sim == click_at:
            r = client.post("/api/portal/tok-held/project-status",
                            json={"status": "delayed", "months": 2})
            assert r.status_code == 200, r.text
            row = db.qall("select msg_type, meta from public.portal_questions "
                          "where proposal_id = 'held' and author_kind = 'customer'")
            assert [(m["msg_type"], m["meta"]["status"]) for m in row] == \
                [("status_update", "delayed")], row
            # The endpoint paused off the real calendar; put the pause on the simulated one.
            set_cols("held", followup_paused_until=until)
            outbox.sent.clear()                     # the endpoint's own staff notice
        _at(sim)
        followup_worker._tick(sim)
        _at(sim)
        sim += 6 * H

    customer = [r for r in emails("held") if r["detail"]["audience"] == "customer"]
    assert [r["detail"]["template"] for r in customer] == ["not_viewed", "not_viewed_2"], \
        "the delayed click skipped the revisit nudge as if the customer had replied"
    assert customer[1]["created_at"] >= resume, "the 2nd went inside the pause"
    staff = [m["html"] for m in outbox.staff()]
    assert staff and not any("replied" in h or "was not sent" in h for h in staff), staff
    assert db.followup_signals("held")["customer_touch_at"] is None
    board = {r["proposal_id"]: r for r in db.list_all_portal_proposals()}
    assert board["held"]["customer_touch_at"] is None
