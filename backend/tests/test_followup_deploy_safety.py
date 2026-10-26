"""Deploy-day safety for the remind-only follow-ups (fix round PF1, 2026-10-25). Pure rules here; the
same history seeded into a real Postgres is in test_pg_deploy_safety.py.

#10  The TOUCHED shape keys its occurrences on time ('vrt@<stamp>'), a key the old automatic ladder
     never used. A proposal with a reply weeks ago and an automatic email two days ago would come due
     on the first tick after deploy: two reminders inside one interval. The interval now runs from
     the touch or the newest COUNTED reminder, whichever is later.
#11/#3  The deposit chase used to stop at max_recurring (20). It now has its own cap (200, decision
     5), so a chase the old ladder had FINISHED would start again. A series whose old emails reached
     the old cap stays finished; every deposit reminder already sent counts toward the new cap.
"""
from datetime import datetime, timedelta, timezone

import pytest

import followup_rules as fr

NOW = datetime(2026, 10, 20, 17, 0, tzinfo=timezone.utc)      # noon Chicago
H = timedelta(hours=1)
D = timedelta(days=1)
ALL_DAY = {"send_start_hour": 0, "send_end_hour": 24}


def _viewed(**over):
    p = {"proposal_id": "p1", "proposal_status": "viewed",
         "followup_enrolled_at": NOW - 60 * D, "cycle_viewed_at": NOW - 59 * D,
         "followup_disabled_at": None, "followup_paused_until": None}
    p.update(over)
    return p


def dues(p, at=NOW, cfg=ALL_DAY):
    return fr.due_now(p, at, cfg)


def cust(p, at=NOW, cfg=ALL_DAY):
    got = [d for d in dues(p, at, cfg) if d.audience == "customer"]
    return got[0] if got else None


def staff_deposit(p, at=NOW, cfg=ALL_DAY):
    return [d for d in dues(p, at, cfg) if d.template == "staff_deposit_outstanding"]


# ── #10: the touched shape never starts before the newest counted reminder ───────
def test_an_old_automatic_email_after_the_reply_holds_the_touched_reminder_back():
    """Finding #10's scenario: replied 20 days ago, the old ladder emailed them 2 days ago. The old
    code would send nothing now; the touched shape alone would raise a vrt@ task at once."""
    p = _viewed(customer_touch_at=NOW - 20 * D,
                followup_reminder_times=[(NOW - 2 * D).isoformat()])
    assert cust(p) is None, "a task came due two days after the customer was last emailed"
    assert fr.next_due_at(p, NOW, ALL_DAY) == NOW - 2 * D + 72 * H
    later = cust(p, NOW + D + H)
    assert later is not None and ":vrt@" in later.rule_key


def test_a_reminder_older_than_the_touch_changes_nothing():
    touch = NOW - 20 * D
    p = _viewed(customer_touch_at=touch,
                followup_reminder_times=[(NOW - 30 * D).isoformat()])
    d = cust(p)
    assert d is not None and ":vrt@" in d.rule_key
    assert fr.next_due_at(p, touch + H, ALL_DAY) == touch + 72 * H


def test_a_snooze_still_wins_over_the_newest_reminder():
    """A snooze date is an estimator's explicit choice, not a reminder floor."""
    snooze = NOW + 5 * D
    p = _viewed(followup_anchor_at=NOW - 3 * D, followup_next_at=snooze,
                followup_reminder_times=[(NOW - D).isoformat()])
    assert fr.next_due_at(p, NOW, ALL_DAY) == snooze


def test_the_floor_also_holds_for_a_scope_group():
    """group_state unions every member's counted reminders, so the floor reaches the group too."""
    a = _viewed(proposal_id="a", scope_group="g", created_at=NOW - 60 * D,
                customer_touch_at=NOW - 20 * D)
    b = _viewed(proposal_id="b", scope_group="g", created_at=NOW - 59 * D,
                followup_reminder_times=[(NOW - 2 * D).isoformat()])
    view = fr.group_view([a, b], NOW)
    assert view is not None
    assert cust(view) is None
    assert fr.next_due_at(view, NOW, ALL_DAY) == NOW - 2 * D + 72 * H


# ── #11 / #3: a deposit chase the old cap finished stays finished ────────────────
def _approved(days_ago: float, legacy: int, *, deposit_status="pending", staff_legacy=None,
              staff=None, **over):
    """An approved job with the deposit outstanding and `legacy` old automatic deposit emails: the
    old ladder's shape is dep1 + `legacy` depr@ customer emails and dep_staff + `legacy` depsr@
    staff emails, so `legacy` = 20 is a chase the old default cap finished."""
    approved = NOW - days_ago * D
    sent = [(approved + D + 72 * H * i).isoformat() for i in range(legacy + 1)]
    sl = legacy if staff_legacy is None else staff_legacy
    p = _viewed(proposal_status="approved", approved_at=approved, deposit_status=deposit_status,
                followup_enrolled_at=approved - 10 * D, cycle_viewed_at=approved - 9 * D,
                followup_reminder_times=sent if deposit_status == "pending" else [],
                deposit_history={"customer_legacy": legacy if deposit_status == "pending" else 0,
                                 "staff_legacy": sl, "staff": sl + 1 if staff is None else staff})
    p.update(over)
    return p


@pytest.mark.parametrize("days_ago", [70, 100, 150])
@pytest.mark.parametrize("legacy", [20, 23])
def test_a_deposit_chase_the_old_cap_finished_does_not_restart(days_ago, legacy):
    p = _approved(days_ago, legacy)
    assert cust(p) is None, "a finished customer deposit chase raised a new task"
    assert staff_deposit(p) == [], "a finished staff deposit chase emailed the estimator again"
    assert fr.next_due_at(p, NOW, ALL_DAY) is None


@pytest.mark.parametrize("days_ago", [70, 100, 150])
def test_a_deposit_chase_with_five_old_emails_carries_on(days_ago):
    p = _approved(days_ago, 5)
    d = cust(p)
    assert d is not None and d.template == "deposit_nudge"
    assert len(staff_deposit(p)) == 1


def test_without_history_the_deposit_chase_runs_exactly_as_before():
    """No deposit_history (a stubbed row, a pre-change caller) means nothing was sent."""
    p = _approved(100, 5)
    p.pop("deposit_history")
    assert cust(p) is not None and len(staff_deposit(p)) == 1


@pytest.mark.parametrize("staff_legacy,expect", [(20, 0), (5, 1)])
def test_a_submitted_deposit_keeps_the_estimator_side_on_the_old_cap(staff_legacy, expect):
    """'submitted': the customer is no longer chased; only the estimator's emails were, and those
    are the ones finding #3 says would come back even on a database without the new DDL."""
    p = _approved(100, 0, deposit_status="submitted", staff_legacy=staff_legacy)
    assert cust(p) is None
    assert len(staff_deposit(p)) == expect


def test_a_lowered_stop_after_never_silences_an_unfinished_deposit_chase():
    """Decision 5: the per-proposal stop-after is the pre-decision cap. The old ladder never had one,
    so it is not the cap the old deposit chase ran under."""
    p = _approved(100, 5, followup_stop_after=3)
    assert cust(p) is not None and len(staff_deposit(p)) == 1


def test_a_lowered_global_cap_is_the_cap_the_old_ladder_ran_under():
    """With max_recurring saved at 3 the old ladder stopped after 3 recurring deposit emails."""
    cfg = dict(ALL_DAY, max_recurring=3)
    p = _approved(100, 3)
    assert cust(p, cfg=cfg) is None and staff_deposit(p, cfg=cfg) == []


def test_every_staff_deposit_email_counts_toward_the_deposit_cap():
    cfg = dict(ALL_DAY, deposit_max_recurring=10)
    p = _approved(100, 2, staff=10)
    assert staff_deposit(p, cfg=cfg) == []
    p = _approved(100, 2, staff=9)
    assert len(staff_deposit(p, cfg=cfg)) == 1


def test_customer_deposit_reminders_already_sent_count_toward_the_deposit_cap():
    cfg = dict(ALL_DAY, deposit_max_recurring=6)
    p = _approved(100, 5)            # dep1 + 5 depr = 6 counted customer reminders
    assert cust(p, cfg=cfg) is None


# Review of PF1: prod's REAL history. Automation was off until 2026-08-24; the old ladder then
# reserved only the LATEST matured occurrence on each tick and froze its index at the cap, so a
# chase it finished can hold far fewer than 20 rows. Counting rows alone restarted all of these.
ENABLED = datetime(2026, 8, 24, 14, 0, tzinfo=timezone.utc)


def _old_ladder_history(approved, *, enable=ENABLED, until=NOW, cfg=None, tick=timedelta(minutes=15)):
    """Run the OLD (origin/main) deposit rules on `tick` from `enable` to just before `until`, in a
    0-24 window as on prod, and return what they left in portal_followups: {(audience, key): at}."""
    c = fr._Cadence(cfg)
    ck = fr.cycle_key(approved - 10 * D)

    def index(elapsed, first):
        if elapsed < first + c.recurring:
            return 0
        return min(c.max_recurring, int((elapsed - first) // c.recurring))

    def key(tag, first, n):
        return "%s:%s@%s" % (ck, tag, fr._stamp(approved + first + c.recurring * n))

    rows, t = {}, enable
    while t < until:
        elapsed = t - approved
        if elapsed >= c.first:
            n = index(elapsed, c.first)
            rows.setdefault(("customer", key("depr", c.first, n) if n else ck + ":dep1"), t)
        if elapsed >= c.staff_personal:
            n = index(elapsed, c.staff_personal)
            rows.setdefault(("staff", key("depsr", c.staff_personal, n) if n
                             else ck + ":dep_staff"), t)
        t += tick
    return rows


def _approved_from_history(days_ago, **over):
    approved = NOW - days_ago * D
    rows = _old_ladder_history(approved)
    cust_rows = sorted(at for (aud, _k), at in rows.items() if aud == "customer")
    depr = sorted(at for (aud, k), at in rows.items() if aud == "customer" and ":depr@" in k)
    depsr = sorted(at for (aud, k), at in rows.items() if aud == "staff" and ":depsr@" in k)
    staff = [at for (aud, _k), at in rows.items() if aud == "staff"]
    p = _viewed(proposal_status="approved", approved_at=approved, deposit_status="pending",
                followup_enrolled_at=approved - 10 * D, cycle_viewed_at=approved - 9 * D,
                followup_reminder_times=[t.isoformat() for t in cust_rows],
                deposit_history={"customer_legacy": len(depr), "staff_legacy": len(depsr),
                                 "staff": len(staff),
                                 "customer_legacy_last": depr[-1].isoformat() if depr else None,
                                 "staff_legacy_last": depsr[-1].isoformat() if depsr else None})
    p.update(over)
    return p, len(depr), len(depsr)


@pytest.mark.parametrize("days_ago,expect_rows", [(70, 18), (80, 14), (100, 8), (150, 1), (200, 1)])
def test_a_chase_the_old_ladder_finished_with_few_rows_does_not_restart(days_ago, expect_rows):
    p, depr, depsr = _approved_from_history(days_ago)
    assert depr == expect_rows and depr < 20, "the history is not the enable-backlog shape"
    assert depsr < 20
    assert fr.deposit_legacy_finished(p, "customer") and fr.deposit_legacy_finished(p, "staff")
    assert cust(p) is None, "a finished customer deposit chase raised a new task"
    assert staff_deposit(p) == [], "a finished staff deposit chase emailed the estimator again"
    assert fr.next_due_at(p, NOW, ALL_DAY) is None


@pytest.mark.parametrize("days_ago", [20, 40])
def test_a_chase_the_old_ladder_had_not_finished_carries_on(days_ago):
    p, depr, _depsr = _approved_from_history(days_ago)
    assert 0 < depr < 20
    assert not fr.deposit_legacy_finished(p, "customer")
    assert fr.next_due_at(p, NOW, ALL_DAY) is not None
    assert len(staff_deposit(p)) == 1


def test_a_per_proposal_interval_does_not_move_the_old_ladders_finish_line():
    """The old ladder had no per-proposal interval; a 10-day one set since must not make a chase it
    finished look unfinished (anchor + 2d + 20*10d is long after the frozen key was reserved)."""
    p, _depr, _depsr = _approved_from_history(150, followup_interval_days=10)
    assert cust(p) is None and staff_deposit(p) == []


def test_a_pause_restart_moves_the_old_ladders_anchor():
    """An expired pause restarted the old clock; the frozen key then fell 62 days after the restart,
    so a row reserved 62 days after APPROVAL but before the restart's finish line is not the end."""
    p, _depr, _depsr = _approved_from_history(150)
    # A pause that ended 30 days ago: the old ladder's anchor for the newest row is the restart.
    p["followup_paused_until"] = (NOW - 30 * D).date().isoformat()
    last = fr._ts(p["deposit_history"]["customer_legacy_last"])
    assert last < NOW - 30 * D and fr.deposit_legacy_finished(p, "customer"), \
        "a row from before the pause still reads against the approval anchor"
    p["deposit_history"] = dict(p["deposit_history"], customer_legacy=3,
                                customer_legacy_last=(NOW - 2 * D).isoformat())
    assert not fr.deposit_legacy_finished(p, "customer"), \
        "a row 28 days into the restarted clock is not the frozen key"


# ── #12 / #18 / #4: the dry run counts every estimator email, and can run first ───
import copy as _copy                                                      # noqa: E402
import json as _json                                                      # noqa: E402
import sys as _sys                                                        # noqa: E402

import followup_worker as fw                                              # noqa: E402
from test_group_followups import NOW as G_NOW, _scope, _three, fake      # noqa: E402,F401

_ops = __import__("pathlib").Path(__file__).resolve().parents[1] / "ops"
if str(_ops) not in _sys.path:
    _sys.path.insert(0, str(_ops))
import followup_dry_run                                                   # noqa: E402


def _solo(pid, minutes, **over):
    return _scope(pid, None, minutes, scope_group=None, scope_label=None, **over)


def test_the_dry_run_counts_the_staff_emails_the_tick_sends(fake):
    """Finding #12: the real tick sent 56 estimator emails where the dry run said 41. Here the dry
    run's staff_emails must equal what the tick actually sends, email for email."""
    approved = _solo("won", 5, proposal_status="approved", approved_at=G_NOW - 10 * D,
                     deposit_status="submitted")
    viewed = _solo("chat", 6, proposal_status="viewed", cycle_viewed_at=G_NOW - 20 * D)
    f = fake(_three() + [_solo("solo", 9), approved, viewed])
    f.signals["chat"] = {"customer_touch_at": G_NOW - 10 * D}
    before = _copy.deepcopy(f.followups)
    got = fw.dry_run_count(G_NOW)
    assert f.followups == before and f.sent["staff"] == [], "the dry run wrote or sent"
    templates = sorted(e["template"] for e in got["staff_emails"])
    # One follow-up-due per task (group, solo, chat) and the deposit note on the submitted job,
    # which has no customer due at all. The not-viewed and 48h notes fold into the task emails.
    assert templates == ["staff_deposit_outstanding", "staff_followup_due", "staff_followup_due",
                         "staff_followup_due"], templates
    assert got["staff_email_count"] == 4
    fw._tick(G_NOW)
    assert len(f.sent["staff"]) == got["staff_email_count"]
    assert fw.dry_run_count(G_NOW)["staff_emails"] == []


def test_the_dry_run_buckets_the_touched_tasks(fake):
    viewed = _solo("chat", 6, proposal_status="viewed", cycle_viewed_at=G_NOW - 20 * D)
    f = fake([viewed, _solo("solo", 9)])
    f.signals["chat"] = {"customer_touch_at": G_NOW - 10 * D}
    got = fw.dry_run_count(G_NOW)
    assert [t["proposal_id"] for t in got["touched_tasks"]] == ["chat"]
    assert ":vrt@" in got["touched_tasks"][0]["rule_key"]
    assert {t["proposal_id"] for t in got["tasks"]} == {"chat", "solo"}


def _finished_deposit_job():
    return _solo("won", 5, proposal_status="approved", approved_at=G_NOW - 100 * D,
                 followup_enrolled_at=G_NOW - 110 * D, cycle_viewed_at=G_NOW - 109 * D)


def test_the_dry_run_lists_finished_deposit_chases_held_and_none_restarted(fake):
    f = fake([_finished_deposit_job()])
    f.signals["won"] = {"deposit_history": {"customer_legacy": 20, "staff_legacy": 20,
                                            "staff": 21}}
    got = fw.dry_run_count(G_NOW)
    assert got["deposit_restarts"] == [] and got["tasks"] == [] and got["staff_emails"] == []
    assert sorted(h["audience"] for h in got["deposit_held"]) == ["customer", "staff"]


def test_the_dry_run_names_a_restart_if_the_guard_ever_lets_one_through(fake, monkeypatch):
    """The bucket is the alarm for the guard: with the guard broken it must fill."""
    f = fake([_finished_deposit_job()])
    f.signals["won"] = {"deposit_history": {"customer_legacy": 20, "staff_legacy": 20,
                                            "staff": 21}}
    monkeypatch.setattr(fr, "deposit_series_done", lambda p, audience, c=None: False)
    got = fw.dry_run_count(G_NOW)
    assert sorted(r["template"] for r in got["deposit_restarts"]) == \
        ["deposit_nudge", "staff_deposit_outstanding"]


def _seed_frozen_keys(f, job):
    """The old ladder's enable-day sweep on a job approved 100 days before G_NOW: ONE row per
    series, each the frozen key (n = 20)."""
    c = fr._Cadence()
    out = {}
    for audience in ("customer", "staff"):
        (k,) = fr.legacy_frozen_keys(job, audience, c)
        f.reserve_followup("won", k, {"audience": audience, "template": "x"})
        out[audience] = k
    return out


def test_the_dry_run_holds_a_chase_the_old_ladder_finished_with_one_row(fake):
    job = _finished_deposit_job()
    f = fake([job])
    _seed_frozen_keys(f, job)
    swept = (G_NOW - 30 * D).isoformat()     # after approval + 2d/48h + 20 * 3d: the frozen key
    f.signals["won"] = {"deposit_history": {"customer_legacy": 1, "staff_legacy": 1, "staff": 1,
                                            "customer_legacy_last": swept,
                                            "staff_legacy_last": swept}}
    got = fw.dry_run_count(G_NOW)
    assert got["deposit_restarts"] == [] and got["tasks"] == [] and got["staff_emails"] == []
    assert sorted(h["audience"] for h in got["deposit_held"]) == ["customer", "staff"]


def test_the_restart_alarm_does_not_share_the_guards_predicate(fake, monkeypatch):
    """Review of PF1 (#12/#18): the alarm used the guard's own predicate, so it could never report a
    restart the guard missed. With the guard AND its predicate both blind (no history at all), the
    old ladder's frozen key in the dedupe table still names the restart."""
    job = _finished_deposit_job()
    f = fake([job])
    keys = _seed_frozen_keys(f, job)
    monkeypatch.setattr(fr, "deposit_series_done", lambda p, audience, c=None: False)
    monkeypatch.setattr(fr, "deposit_legacy_finished", lambda p, audience, c=None: False)
    got = fw.dry_run_count(G_NOW)
    assert sorted(r["template"] for r in got["deposit_restarts"]) == \
        ["deposit_nudge", "staff_deposit_outstanding"]
    assert not {r["rule_key"] for r in got["deposit_restarts"]} & set(keys.values())


def test_a_group_whose_home_holds_an_open_task_still_counts_its_staff_notes(fake):
    """The old dry run skipped such a group outright; the tick still sends its staff notes."""
    rows = [dict(r, proposal_status="viewed", cycle_viewed_at=G_NOW - 3 * D) for r in _three()]
    f = fake(rows)
    f.reserve_followup_task("d-break", "held", {"stage": "pre", "scope_group": rows[0]["scope_group"]})
    got = fw.dry_run_count(G_NOW)
    assert got["tasks"] == []
    assert [e["template"] for e in got["staff_emails"]] == ["staff_personal_followup"]
    fw._tick(G_NOW)
    assert len(f.sent["staff"]) == 1


def test_the_dry_run_entry_point_prints_json_and_a_table_and_cannot_send(monkeypatch, capsys):
    import email_sender

    seen = {}
    monkeypatch.setattr(followup_dry_run, "install_read_only_pool",
                        lambda url: seen.setdefault("url", url))
    monkeypatch.setattr(email_sender, "_send", email_sender._send)       # restored afterwards
    result = {"at": "x", "count": 1, "tasks": [{"proposal_id": "p1", "rule_key": "c1:vrt@1",
                                                "template": "checkin", "scope_group": None}],
              "skipped": [], "staff_emails": [{"template": "staff_followup_due"}],
              "staff_email_count": 1, "touched_tasks": [{"proposal_id": "p1"}],
              "deposit_restarts": [], "deposit_held": [{"proposal_id": "w", "audience": "staff"}]}
    monkeypatch.setattr(fw, "dry_run_count", lambda now=None: result)
    assert followup_dry_run.main([]) == 0
    out = capsys.readouterr().out
    head, _, tail = out.partition("\n\n")
    assert _json.loads(head) == result
    assert "tasks the tick would raise ........ 1" in tail
    assert "of which touched (deploy burst) . 1" in tail
    assert "estimator emails it would send .... 1" in tail
    assert "finished deposit chases held ...... 1" in tail
    assert "c1:vrt@1" in tail
    assert seen["url"]
    with pytest.raises(RuntimeError, match="never sends email"):
        email_sender._send(["a@b.c"], "s", "h")
    assert followup_dry_run.main(["--json"]) == 0
    assert _json.loads(capsys.readouterr().out) == result


def test_the_entry_point_evaluates_the_tick_at_now(monkeypatch, capsys):
    """Round 3 #5: `--now <ISO>` is the moment dry_run_count evaluates at; without it, the real
    clock (None). An offset-less value is Central time; a bad one exits 2 before any database."""
    seen = []
    monkeypatch.setattr(followup_dry_run, "install_read_only_pool", lambda url: None)
    monkeypatch.setattr(followup_dry_run, "refuse_email", lambda: None)
    monkeypatch.setattr(fw, "dry_run_count", lambda now=None: seen.append(now) or {
        "at": str(now), "tasks": []})
    assert followup_dry_run.main(["--json", "--now", "2026-10-27T12:00:00-05:00"]) == 0
    assert followup_dry_run.main(["--now=2026-10-27T17:00:00Z", "--json"]) == 0
    assert followup_dry_run.main(["--json", "--now", "2026-10-27T12:00:00"]) == 0
    assert followup_dry_run.main(["--json"]) == 0
    noon = datetime(2026, 10, 27, 17, 0, tzinfo=timezone.utc)
    assert seen[:3] == [noon, noon, noon] and seen[3] is None
    capsys.readouterr()
    assert followup_dry_run.main(["--now", "yesterday"]) == 2
    assert followup_dry_run.main(["--now"]) == 2
    assert len(seen) == 4, "a bad --now reached the database"
    assert "--now" in capsys.readouterr().err


def test_the_entry_point_opens_a_read_only_pool():
    assert followup_dry_run.READ_ONLY == "-c default_transaction_read_only=on"


# ── #13: the out-of-office backfill's body heuristics ─────────────────────────────
import backfill_auto_reply                                                # noqa: E402

AUTO_BODIES = [
    "I am currently out of the office and will return on Monday, October 28.",
    "Thank you for your email. I'm out of the office until 11/4 with limited access to email.",
    "I\u2019m on vacation this week. For urgent matters please call Dave at the office.",
    "I will be on leave from 10/1 to 10/15. I will respond to your message upon my return.",
    "This is an automated reply. Your message has been received.",
    "Automatic reply: Proposal for Acme HQ\nI am away from the office and will return on 10/30.",
]
REAL_BODIES = [
    "Looks good, we'd like to move forward with the epoxy option.",
    "I was out of the office last week, sorry. Yes, please send the contract.",
    "I'm out of the office today but go ahead and schedule us for November.",
    "We are on vacation next month, can you start after the 15th? I will return the signed copy.",
    "Out of office until Monday but approved - send the invoice",
    "Can you call me? " + "We have a few questions about the scope. " * 40
    + "I am out of the office and will return on Monday.",
    "",
]


@pytest.mark.parametrize("body", AUTO_BODIES)
def test_an_autoresponder_body_is_recognised(body):
    assert backfill_auto_reply.auto_reply_reason(None, body)


@pytest.mark.parametrize("body", REAL_BODIES)
def test_a_real_reply_is_never_flagged(body):
    """A false positive hides a real reply from the schedule, so these must all stay unflagged."""
    assert backfill_auto_reply.auto_reply_reason(None, body) is None


def test_a_stored_subject_uses_the_live_rule():
    assert backfill_auto_reply.auto_reply_reason("Out of Office: Re: Acme", "anything") == "subject"
    assert backfill_auto_reply.auto_reply_reason("Re: Acme proposal", REAL_BODIES[0]) is None
