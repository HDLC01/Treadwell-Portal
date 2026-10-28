"""The worker SENDS the follow-up emails to the customer (2026-10-28), through a FAKE transport.

"the follow ups should be a follow up email to the customer okay?" — so a due follow-up emails the
proposal's customer and every recipient who has not opted out, with the saved template, under the
project's one thread subject, inside the Central send window, reserved first so no tick, restart
or twin container sends it twice. No reminder TASK is raised any more.

The real email_sender renders every email here; only email_sender._send (the one function that
calls Resend) is replaced, by a recorder, and httpx.post by a tripwire. Nothing leaves the box.
"""
import copy
import re
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import db
import email_sender
import followup_rules as rules
import followup_settings as fs
import followup_worker as fw
from test_group_followups import FakeDB, _scope

NOW = datetime(2026, 10, 20, 17, 0, tzinfo=timezone.utc)      # noon Chicago
D = timedelta(days=1)
H = timedelta(hours=1)
SENT = NOW - 4 * D                                             # the 1st not-opened email is due
KYLE = "kyle@wetreadwell.com"


def _solo(pid="p1", **over):
    base = dict(scope_group=None, scope_label=None, followup_enrolled_at=SENT,
                customer_email="dana@acme.com", project_name="Acme HQ")
    base.update(over)
    return _scope(pid, None, 0, **base)


@pytest.fixture
def world(monkeypatch):
    """A FakeDB holding `rows`, the real email rendering, and a recording transport."""
    box = SimpleNamespace(sent=[], fail=set())

    def fake_send(to, subject, html, headers=None, reply_to=None, attachments=None):
        to = [t for t in to if t]
        if not to:
            return False
        ok = not any(t in box.fail for t in to)
        box.sent.append({"to": list(to), "subject": subject, "html": html,
                         "headers": dict(headers or {}), "reply_to": reply_to, "ok": ok})
        return ok

    def no_network(*a, **k):
        raise AssertionError("a test reached for Resend")

    monkeypatch.setattr(email_sender, "_send", fake_send)
    monkeypatch.setattr(email_sender.httpx, "post", no_network)
    monkeypatch.setattr(email_sender, "_resolve_notify", lambda kind, pid=None: [KYLE])
    monkeypatch.setenv("FOLLOWUP_AUTOMATION_ENABLED", "true")
    holder = {}

    def install(rows, drafts=None, recipients=None, followup_recipients=None, settings=None):
        f = FakeDB(rows, drafts)
        for name in dir(FakeDB):
            if not name.startswith("_") and name not in ("tasks",):
                monkeypatch.setattr(db, name, getattr(f, name), raising=False)
        monkeypatch.setattr(db, "get_settings", lambda key: settings)
        if recipients is not None:
            monkeypatch.setattr(db, "get_recipients", lambda pid: list(recipients.get(pid, [])))
        if followup_recipients is not None:
            monkeypatch.setattr(db, "get_followup_recipients",
                                lambda pid: list(followup_recipients.get(pid, [])))
        f.box = box
        holder["db"] = f
        return f

    box.customers = lambda: [t for m in box.sent if m["ok"] for t in m["to"] if t != KYLE]
    box.staff = lambda: [m for m in box.sent if m["ok"] and KYLE in m["to"]]
    return install


def _emails(f):
    return [x for x in f.followups if x["kind"] == "auto_email"
            and x["detail"].get("audience") == "customer"]


def _tick(f, at):
    f.clock = at
    fw._tick(at)


# ── it sends, to whom, with what ──────────────────────────────────────────────
def test_a_due_follow_up_emails_the_customer_and_every_recipient(world):
    f = world([_solo()], recipients={"p1": ["dana@acme.com", "pat@acme.com"]},
              followup_recipients={"p1": ["dana@acme.com", "pat@acme.com"]})
    _tick(f, NOW)
    assert sorted(f.box.customers()) == ["dana@acme.com", "pat@acme.com"]
    mail = {m["to"][0]: m for m in f.box.sent}
    m = mail["dana@acme.com"]
    assert m["subject"] == "Your Treadwell proposal — Acme HQ", "not the project's one thread"
    assert fs.DEFAULT_TEMPLATES["not_viewed"]["title"] in m["html"]
    assert "/p/tok-p1" in m["html"], "no button to the proposal"
    assert "Hi Dana" in m["html"] and "Hi Dana" not in mail["pat@acme.com"]["html"]
    assert m["headers"], "no thread headers"
    (row,) = _emails(f)
    assert row["proposal_id"] == "p1"
    assert row["detail"]["rule_key"] == "%s:a:nv1" % rules.cycle_key(SENT)
    assert row["detail"]["template"] == "not_viewed" and row["detail"]["engine"] == fw.ENGINE_MARK
    assert f.tasks() == [], "a reminder task was raised; the worker emails the customer itself"
    (echo,) = f.messages
    assert echo["meta"]["followup"] and not echo["meta"].get("internal")
    assert f.box.staff() == [], "the estimator is told only after the step's last email"


def test_the_saved_wording_and_subject_are_what_goes_out(world, monkeypatch):
    saved = fs.validate({"thread_subject": "Acme bid — {project}", "templates": {
        "not_viewed": {"title": "Did it arrive?", "body": "Hello {first_name}.\n\n{link}",
                       "cta": "Open it"}}})
    f = world([_solo()], settings=saved)
    # conftest pins the subject to the shipped one; read the saved one, as production does.
    monkeypatch.setattr(email_sender, "_thread_subject_template",
                        lambda: fs.merge(db.get_settings(fs.ROW_ID))["thread_subject"])
    _tick(f, NOW)
    (m,) = f.box.sent
    assert "Did it arrive?" in m["html"] and "Open it" in m["html"]
    assert m["subject"] == "Acme bid — Acme HQ"


def test_per_contact_opt_outs_are_honoured(world):
    f = world([_solo()], recipients={"p1": ["dana@acme.com", "ap@acme.com"]},
              followup_recipients={"p1": ["dana@acme.com"]})
    _tick(f, NOW)
    assert f.box.customers() == ["dana@acme.com"]


def test_everybody_opted_out_means_nothing_is_sent_and_nothing_is_counted(world):
    f = world([_solo()], recipients={"p1": ["dana@acme.com"]}, followup_recipients={"p1": []})
    _tick(f, NOW)
    assert f.box.sent == [] and _emails(f) == []


def test_customer_email_waits_for_the_central_send_window(world):
    f = world([_solo()])
    _tick(f, datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc))       # 3am Chicago
    assert f.box.sent == [] and _emails(f) == []
    _tick(f, datetime(2026, 10, 20, 14, 0, tzinfo=timezone.utc))      # 9am Chicago
    assert f.box.customers() == ["dana@acme.com"]


def test_a_scope_group_gets_one_email_per_recipient_listing_their_open_proposals(world):
    rows = [_scope("d-a", "Breakroom", 0, followup_enrolled_at=SENT),
            _scope("d-b", "Lab 1", 1, followup_enrolled_at=SENT),
            _scope("d-c", "Lab 2", 2, followup_enrolled_at=SENT, customer_email="pat@acme.com")]
    f = world(rows)
    _tick(f, NOW)
    mail = {m["to"][0]: m for m in f.box.sent}
    assert set(mail) == {"dana@acme.com", "pat@acme.com"}, "not one email per recipient"
    dana = mail["dana@acme.com"]["html"]
    assert "Breakroom" in dana and "Lab 1" in dana and "Lab 2" not in dana
    assert "Lab 2" in mail["pat@acme.com"]["html"]
    assert len(re.findall(r'href="[^"]*/p/tok-', dana)) == 1, "one button to the project page"
    assert mail["dana@acme.com"]["subject"] == "Your Treadwell proposal — Acme HQ"
    (row,) = _emails(f)
    assert row["proposal_id"] == "d-a" and row["detail"]["scope_group"]
    _tick(f, NOW + H)
    assert len(f.box.sent) == 2, "the group was emailed twice"


# ── never twice ───────────────────────────────────────────────────────────────
def test_no_tick_restart_or_twin_sends_it_twice_and_there_are_never_more_than_two(world):
    f = world([_solo()])
    _tick(f, NOW)
    fw._tick(NOW)                                   # the twin container, same moment
    _tick(f, NOW + 6 * H)                           # a later tick, nothing new due
    assert len(f.box.customers()) == 1
    # The worker holds nothing in memory: a "restart" is the same database read again.
    before = copy.deepcopy(f.followups)
    _tick(f, NOW + 3 * D)
    assert f.followups == before and len(f.box.customers()) == 1
    _tick(f, NOW + 7 * D)                           # 7 days after the 1st REALLY went
    assert len(f.box.customers()) == 2
    assert [x["detail"]["template"] for x in _emails(f)] == ["not_viewed", "not_viewed_2"]
    assert "Has your timeline changed?" in f.box.sent[-1]["html"], "the way out on the last one"
    for days in (8, 30, 120):
        _tick(f, NOW + days * D)
    assert len(f.box.customers()) == 2, "a 3rd email in one step"
    est = f.box.staff()
    assert len(est) == 1 and est[0]["subject"] == "Follow-ups done — Acme HQ"
    assert "still hasn't opened the proposal" in est[0]["html"]
    assert "/p/tok-p1" not in est[0]["html"], "the customer's link in a staff email"
    internal = [m for m in f.messages if m["meta"].get("internal")]
    assert len(internal) == 1


def test_a_twin_that_read_the_history_before_the_first_send_is_stopped_by_the_reservation(
        world, monkeypatch):
    """Two containers during a deploy can both read "nothing sent yet" before either sends. The
    sent history cannot stop the second; the reservation (unique rule key) must."""
    f = world([_solo()])
    monkeypatch.setattr(db, "followup_signals", lambda pid: {"followup_sent_history": []})
    _tick(f, NOW)
    _tick(f, NOW)                                   # the twin, with the stale read
    assert f.box.customers() == ["dana@acme.com"]
    assert len(_emails(f)) == 1


def test_a_failed_send_releases_its_reservation_and_the_next_tick_retries(world):
    f = world([_solo()])
    f.box.fail.add("dana@acme.com")
    _tick(f, NOW)
    assert _emails(f) == [] and f.messages == [], "a failed send was recorded as sent"
    f.box.fail.clear()
    _tick(f, NOW + H)
    assert f.box.customers() == ["dana@acme.com"] and len(_emails(f)) == 1


def test_a_partial_send_still_counts_as_sent(world):
    f = world([_solo()], followup_recipients={"p1": ["dana@acme.com", "bounce@acme.com"]})
    f.box.fail.add("bounce@acme.com")
    _tick(f, NOW)
    _tick(f, NOW + H)
    assert f.box.customers() == ["dana@acme.com"], "the one who got it was emailed again"
    assert len(_emails(f)) == 1


# ── release-day safety: what the old engines already sent ──────────────────────
def _old(f, pid, key, template, at, kind="auto_email", status=None):
    f.clock = at
    if kind == "auto_email":
        f.reserve_followup(pid, key, {"audience": "customer", "template": template})
    else:
        f.reserve_followup_task(pid, key, {"template": template, "stage": "pre",
                                           "status": status})


def test_a_proposal_the_old_ladder_emailed_twice_in_its_step_gets_nothing(world):
    viewed = SENT - 30 * D
    rows = [_solo("old2", followup_enrolled_at=SENT - 40 * D, cycle_viewed_at=viewed,
                  proposal_status="viewed"),
            _solo("old1", followup_enrolled_at=SENT - 40 * D, cycle_viewed_at=viewed,
                  proposal_status="viewed", customer_email="lee@acme.com"),
            _solo("tasked", followup_enrolled_at=SENT - 40 * D, cycle_viewed_at=viewed,
                  proposal_status="viewed", customer_email="kim@acme.com")]
    f = world(rows)
    ck = rules.cycle_key(SENT - 40 * D)
    _old(f, "old2", ck + ":v1", "next_steps", viewed + D)
    _old(f, "old2", ck + ":vr@20261001T15", "checkin", viewed + 10 * D)
    _old(f, "old1", ck + ":v1", "next_steps", viewed + D)
    # Remind-only: two tasks the estimator SENT by hand count exactly like emails.
    _old(f, "tasked", ck + ":v1", "next_steps", viewed + D, kind="task", status="sent")
    _old(f, "tasked", ck + ":v2", "second_nudge", viewed + 5 * D, kind="task", status="called")
    dry = fw.dry_run_count(NOW)
    assert [(e["proposal_id"], e["template"]) for e in dry["customer_emails"]] == \
        [("old1", "second_nudge")]
    _tick(f, NOW)
    assert f.box.customers() == ["lee@acme.com"], "an old proposal was emailed again"
    _tick(f, NOW + 60 * D)
    assert f.box.customers() == ["lee@acme.com"]


def test_old_pending_reminder_tasks_are_retired_and_the_decision_task_is_kept(world):
    f = world([_solo()])
    rem = f.reserve_followup_task("p1", "c1:v1", {"template": "next_steps", "stage": "pre"})
    dec = f.reserve_followup_task("p1", "sd:g:p1", {"stage": "decision", "scope_group": "g",
                                                    "template": "scope_decision", "scopes": []})
    dry = fw.dry_run_count(NOW)
    assert [t["task_id"] for t in dry["tasks_to_expire"]] == [rem]
    _tick(f, NOW)
    t = {x["id"]: x["detail"] for x in f.tasks()}
    assert t[rem]["status"] == "expired" and t[rem]["reason"] == "auto_followups"
    # The decision task is judged against its siblings (none open here: moot), never retired as
    # a cadence reminder.
    assert t[dec].get("reason") == "decision_moot"


# ── what stops it, re-read at send time ───────────────────────────────────────
@pytest.mark.parametrize("change", [
    {"proposal_status": "approved", "approved_at": NOW},
    {"proposal_status": "closed_lost"},
    {"followup_disabled_at": NOW},
    {"followup_paused_until": (NOW + 30 * D).date()},
    {"deleted_at": NOW},
])
def test_a_proposal_decided_or_switched_off_since_the_list_was_built_gets_nothing(world,
                                                                                change):
    f = world([_solo()])
    listed = [dict(r) for r in f.rows.values()]
    f.rows["p1"].update(change)
    db.list_followup_candidates = lambda: listed        # the stale list
    _tick(f, NOW)
    assert f.box.sent == [] and _emails(f) == []


def test_one_bad_proposal_does_not_end_the_sweep(world, monkeypatch):
    f = world([_solo("bad"), _solo("good", customer_email="good@acme.com")])
    real = db.followup_signals

    def flaky(pid):
        if pid == "bad":
            raise RuntimeError("malformed row")
        return real(pid)

    monkeypatch.setattr(db, "followup_signals", flaky)
    _tick(f, NOW)
    assert f.box.customers() == ["good@acme.com"]


def test_the_kill_switch_stops_everything(world, monkeypatch):
    f = world([_solo()])
    monkeypatch.setenv("FOLLOWUP_AUTOMATION_ENABLED", "false")
    _tick(f, NOW)
    assert f.box.sent == [] and f.followups == []


def test_the_estimator_note_is_not_sent_when_switched_off(world):
    f = world([_solo()], settings=dict(fs.DEFAULTS, estimator_on=False))
    for days in (0, 7, 8, 30):
        _tick(f, NOW + days * D)
    assert len(f.box.customers()) == 2 and f.box.staff() == []


def test_a_failed_estimator_note_is_released_and_retried(world):
    f = world([_solo()])
    _tick(f, NOW)
    _tick(f, NOW + 7 * D)
    f.box.fail.add(KYLE)
    _tick(f, NOW + 8 * D)
    assert f.box.staff() == [] and not [x for x in f.followups
                                        if x["detail"].get("audience") == "staff"]
    f.box.fail.clear()
    _tick(f, NOW + 8 * D + H)
    assert len(f.box.staff()) == 1


# ── the dry run ───────────────────────────────────────────────────────────────
def test_the_dry_run_names_exactly_who_the_tick_emails_and_writes_nothing(world):
    rows = [_solo("p1"), _solo("p2", customer_email="lee@acme.com"),
            _solo("p3", customer_email="kim@acme.com"),
            _solo("young", followup_enrolled_at=NOW - D)]
    f = world(rows, recipients={"p1": ["dana@acme.com", "pat@acme.com"], "p2": ["lee@acme.com"],
                                "p3": ["kim@acme.com"]},
              followup_recipients={"p1": ["dana@acme.com", "pat@acme.com"],
                                   "p2": ["lee@acme.com"], "p3": []})
    before = copy.deepcopy(f.followups)
    dry = fw.dry_run_count(NOW)
    assert f.followups == before and f.box.sent == [] and f.messages == []
    assert dry["in_send_window"] is True
    promised = sorted(a for e in dry["customer_emails"] for a in e["recipients"])
    assert promised == ["dana@acme.com", "lee@acme.com", "pat@acme.com"]
    assert dry["recipient_count"] == 3 and dry["customer_email_count"] == 2
    assert dry["opted_out"] == [{"proposal_id": "p3",
                                 "rule_key": "%s:a:nv1" % rules.cycle_key(SENT)}]
    _tick(f, NOW)
    assert sorted(f.box.customers()) == promised, "the tick and the dry run disagree"
    assert fw.dry_run_count(NOW)["customer_emails"] == []


def test_the_dry_run_outside_the_window_says_so(world):
    world([_solo()])
    dry = fw.dry_run_count(datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc))
    assert dry["in_send_window"] is False and dry["customer_emails"] == []


def test_the_dry_run_table_lists_every_customer(world):
    sys.path.insert(0, str(__import__("pathlib").Path(fw.__file__).parent / "ops"))
    import followup_dry_run
    world([_solo()])
    text = followup_dry_run.table(fw.dry_run_count(NOW))
    assert "customer emails it would send ..... 1 (to 1 addresses)" in text
    assert re.search(r"Acme HQ\s+not_viewed\s+1\s+dana@acme.com", text), text


# ── decision B (Hanz, 2026-10-28): a reply skips the step's 2nd email ──────────────
@pytest.mark.parametrize("key", ["customer_touch_at", "last_staff_contact_at",
                                 "last_staff_message_at"])
def test_contact_after_the_first_email_means_the_second_is_never_sent(world, key):
    f = world([_solo()])
    _tick(f, NOW)
    assert len(f.box.customers()) == 1
    f.signals["p1"] = {key: NOW + D + H}           # they replied / we logged a call, after the 1st
    dry = fw.dry_run_count(NOW + 7 * D)
    assert dry["customer_emails"] == []
    (skip,) = dry["skipped_by_reply"]
    assert skip["proposal_id"] == "p1" and skip["template"] == "not_viewed_2"
    assert skip["would_have_gone_now"] is True and skip["rule_key"].endswith(":a:nv2")
    # Outside the send window it would not have gone this tick either; it is still listed.
    (night,) = fw.dry_run_count(datetime(2026, 10, 28, 8, 0, tzinfo=timezone.utc))[
        "skipped_by_reply"]                         # 3am Chicago, after the 2nd's date
    assert night["would_have_gone_now"] is False
    for days in (7, 8, 30, 120):
        _tick(f, NOW + days * D)
    assert len(f.box.customers()) == 1, "the 2nd went to a customer we are already talking to"
    assert [x["detail"]["template"] for x in _emails(f)] == ["not_viewed"]
    assert not any(x["detail"].get("rule_key", "").endswith(":a:nv2") for x in f.followups), \
        "a skipped email was reserved (and would count as sent)"
    # The estimator's note still goes, once, and says why the 2nd did not.
    (est,) = f.box.staff()
    assert "The second one was not sent because" in est["html"]


def test_without_contact_the_worker_sends_the_second(world):
    """The counterexample for the test above: same proposal, no contact, two emails."""
    f = world([_solo()])
    _tick(f, NOW)
    _tick(f, NOW + 7 * D)
    assert len(f.box.customers()) == 2
    assert fw.dry_run_count(NOW + 7 * D)["skipped_by_reply"] == []
    _tick(f, NOW + 8 * D)
    (est,) = f.box.staff()
    assert "was not sent because" not in est["html"]


def test_contact_before_the_first_email_does_not_stop_either(world):
    f = world([_solo()])
    f.signals["p1"] = {"customer_touch_at": SENT + D}
    _tick(f, NOW)
    _tick(f, NOW + 7 * D)
    assert len(f.box.customers()) == 2


def test_a_reply_on_another_scope_skips_the_groups_second_email(world):
    rows = [_scope("d-a", "Breakroom", 0, followup_enrolled_at=SENT),
            _scope("d-b", "Lab 1", 1, followup_enrolled_at=SENT)]
    f = world(rows)
    _tick(f, NOW)
    assert len(f.box.sent) == 1
    f.signals["d-b"] = {"customer_touch_at": NOW + D}
    (skip,) = fw.dry_run_count(NOW + 7 * D)["skipped_by_reply"]
    assert skip["proposal_id"] == "d-a" and skip["scope_group"]
    _tick(f, NOW + 7 * D)
    _tick(f, NOW + 9 * D)
    assert f.box.customers() == ["dana@acme.com"], "the group's 2nd went after a reply"


def test_the_dry_run_table_counts_the_skipped(world):
    sys.path.insert(0, str(__import__("pathlib").Path(fw.__file__).parent / "ops"))
    import followup_dry_run
    f = world([_solo()])
    _tick(f, NOW)
    f.signals["p1"] = {"customer_touch_at": NOW + D}
    text = followup_dry_run.table(fw.dry_run_count(NOW + 7 * D))
    assert "2nd email skipped, they replied ... 1 (1 would have gone this tick)" in text, text
    assert re.search(r"Acme HQ\s+not_viewed_2\s+customer_reply", text), text
