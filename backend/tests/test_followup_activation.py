"""Follow-ups start at ACTIVATION (Hanz, 2026-10-29: "we push these auto follow ups changes and then
we send the email at the time it was activated").

Without an anchor, the first tick after the release would email every open proposal whose step is
older than its N days, all at once, and tell an estimator about every step that ran out weeks ago.
With it, every step's clock starts at max(the step's own start, activated_at):

  * a proposal sent weeks before activation: nothing at activation, its 1st email N days after;
  * a proposal sent after activation: exactly as without the anchor;
  * a proposal that already had its 2 emails: still capped, and no "follow-ups done" note;
  * the first ENABLED tick records activated_at once (a disabled tick does not; a twin cannot
    move it), an unreadable activation row sends nothing, the ops command records it on purpose,
    and the dry run says which moment it used.

Pure rules, a FakeDB worker and the ops command here; the real-Postgres timeline and race are in
test_pg_followup_activation.py.
"""
import os
import sys
import threading
from datetime import datetime, timedelta, timezone

import pytest

import db
import followup_rules as fr
import followup_settings as fs
import followup_worker as fw
from test_followup_auto_worker import _emails, _solo, _tick, world  # noqa: F401 (fixture)

sys.path.insert(0, os.path.join(os.path.dirname(fw.__file__), "ops"))
import followup_activate  # noqa: E402
import followup_dry_run  # noqa: E402

D = timedelta(days=1)
H = timedelta(hours=1)
ACT = datetime(2026, 10, 20, 17, 0, tzinfo=timezone.utc)        # noon Chicago: activation
ALL_DAY = {"send_start_hour": 0, "send_end_hour": 24}
KYLE = "kyle@wetreadwell.com"


def cfg(act=ACT, **over):
    c = dict(ALL_DAY, **over)
    return fs.with_activation(c, act)


def _p(**over):
    p = {"proposal_id": "p1", "proposal_status": "sent", "followup_enrolled_at": ACT - 21 * D,
         "followup_disabled_at": None, "followup_paused_until": None, "cycle_viewed_at": None,
         "followup_sent_history": []}
    p.update(over)
    return p


def _h(at, template):
    return {"at": at.isoformat(), "template": template, "rule_key": "x"}


def cust(p, at, c):
    return [d.template for d in fr.due_now(p, at, c) if d.audience == "customer"]


def staff(p, at, c):
    return [d.template for d in fr.due_now(p, at, c) if d.audience == "staff"]


# ══ the rules ═════════════════════════════════════════════════════════════════
def test_an_old_unopened_proposal_gets_nothing_at_activation_and_its_first_n_days_after():
    p = _p()
    assert cust(p, ACT, cfg(None)) == ["not_viewed"], "control: without the anchor it is a burst"
    assert cust(p, ACT, cfg()) == []
    assert cust(p, ACT + 3 * D - H, cfg()) == []
    assert cust(p, ACT + 3 * D, cfg()) == ["not_viewed"]
    assert fr.next_due_at(p, ACT, cfg()) == ACT + 3 * D


def test_an_old_opened_proposal_gets_its_first_opened_email_n_days_after_activation():
    p = _p(proposal_status="viewed", cycle_viewed_at=ACT - 15 * D)
    assert cust(p, ACT, cfg()) == []
    assert fr.next_due_at(p, ACT, cfg()) == ACT + 3 * D
    assert cust(p, ACT + 3 * D, cfg()) == ["next_steps"]


def test_a_first_email_an_older_engine_sent_pushes_the_second_to_activation_plus_n():
    p = _p(followup_sent_history=[_h(ACT - 18 * D, "not_viewed")])
    assert cust(p, ACT, cfg(None)) == ["not_viewed_2"], "control"
    assert cust(p, ACT, cfg()) == []
    assert fr.next_due_at(p, ACT, cfg()) == ACT + 7 * D
    assert cust(p, ACT + 7 * D, cfg()) == ["not_viewed_2"]


@pytest.mark.parametrize("sent_after", [0, 1, 4])
def test_a_proposal_sent_after_activation_runs_exactly_as_without_the_anchor(sent_after):
    sent = ACT + sent_after * D + 2 * H
    first = sent + 3 * D
    for hist, opened in (([], None), ([_h(first, "not_viewed")], None),
                         ([], sent + D), ([_h(sent + 4 * D, "next_steps")], sent + D)):
        p = _p(followup_enrolled_at=sent, followup_sent_history=hist,
               proposal_status="viewed" if opened else "sent", cycle_viewed_at=opened)
        for at in [sent + k * 12 * H for k in range(0, 40)]:
            assert fr.plan(p, at, cfg()) == fr.plan(p, at, cfg(None)), (hist, opened, at)


def test_a_proposal_that_already_had_two_in_its_step_stays_capped_and_no_estimator_note():
    p = _p(followup_sent_history=[_h(ACT - 18 * D, "not_viewed"), _h(ACT - 11 * D, "not_viewed")])
    assert staff(p, ACT, cfg(None)) == ["staff_followups_done"], "control: a burst of notes"
    for at in (ACT, ACT + D, ACT + 10 * D, ACT + 90 * D):
        assert cust(p, at, cfg()) == [] and staff(p, at, cfg()) == [], at
    pl = fr.plan(p, ACT, cfg())
    assert pl.ran_out_before_activation and pl.estimator_at is None and pl.sent == 2


def test_a_step_that_runs_out_after_activation_still_tells_the_estimator():
    p = _p(followup_sent_history=[_h(ACT - 18 * D, "not_viewed"), _h(ACT + 7 * D, "not_viewed_2")])
    pl = fr.plan(p, ACT + 7 * D, cfg())
    assert not pl.ran_out_before_activation and pl.estimator_at == ACT + 8 * D
    assert staff(p, ACT + 8 * D, cfg()) == ["staff_followups_done"]


def test_a_second_skipped_by_contact_before_activation_owes_no_note():
    p = _p(followup_sent_history=[_h(ACT - 18 * D, "not_viewed")],
           customer_touch_at=ACT - 16 * D)
    assert staff(p, ACT, cfg(None)) == ["staff_followups_done"], "control"
    assert staff(p, ACT, cfg()) == [] and staff(p, ACT + 30 * D, cfg()) == []
    # The same contact AFTER activation: the note follows, never before activation + N.
    p = dict(p, customer_touch_at=ACT + 2 * D)
    assert fr.plan(p, ACT + 2 * D, cfg()).estimator_at == ACT + D


def test_every_email_off_a_step_begun_before_activation_owes_no_note():
    off = dict(not_opened_first_on=False, not_opened_second_on=False)
    assert staff(_p(), ACT, cfg(None, **off)) == ["staff_followups_done"], "control"
    assert staff(_p(), ACT + 5 * D, cfg(**off)) == []
    begun_after = _p(followup_enrolled_at=ACT + D)
    assert staff(begun_after, ACT + 2 * D, cfg(**off)) == ["staff_followups_done"]


def test_a_next_email_on_date_that_passed_before_activation_does_not_send_at_once():
    p = _p(followup_next_at=ACT - 5 * D)
    assert cust(p, ACT, cfg(None)) == ["not_viewed"], "control"
    assert cust(p, ACT, cfg()) == [] and fr.next_due_at(p, ACT, cfg()) == ACT + 3 * D
    later = _p(followup_next_at=ACT + D)                 # set for after activation: honoured
    assert fr.next_due_at(later, ACT, cfg()) == ACT + D


def test_the_activation_on_the_cfg_may_be_an_iso_string():
    c = dict(ALL_DAY, activated_at=ACT.isoformat())
    assert fr.next_due_at(_p(), ACT, c) == ACT + 3 * D
    assert fs.activated_at_from({"activated_at": "2026-10-20T17:00:00Z"}) == ACT
    assert fs.activated_at_from({"activated_at": "nonsense"}) is None
    assert fs.activated_at_from(None) is None


def test_the_editors_save_cannot_carry_or_wipe_the_activation():
    """It lives in its own row: the cadence PUT validates a whole new value for ROW_ID."""
    assert fs.ACTIVATION_ROW_ID != fs.ROW_ID
    saved = fs.validate({"not_opened_first_days": 4}, fs.with_activation(fs.defaults(), ACT))
    assert fs.ACTIVATED_KEY not in saved


# ══ the worker ════════════════════════════════════════════════════════════════
class Store:
    """portal_settings as the three db functions the activation uses see it."""

    def __init__(self, rows=None, fail=False):
        self.rows = dict(rows or {})
        self.fail = fail
        self.lock = threading.Lock()
        self.writes = []

    def get(self, key):
        if self.fail:
            raise db.SettingsUnreadable("connection refused")
        return self.rows.get(key)

    def insert_if_absent(self, key, value, by=None):
        with self.lock:
            if key not in self.rows:
                self.rows[key] = dict(value)
                self.writes.append((key, dict(value), by))
            return self.rows[key]

    def save(self, key, value, by=None):
        with self.lock:
            self.rows[key] = dict(value)
            self.writes.append((key, dict(value), by))
        return {}

    def install(self, monkeypatch):
        monkeypatch.setattr(db, "get_settings", self.get)
        monkeypatch.setattr(db, "insert_settings_if_absent", self.insert_if_absent)
        monkeypatch.setattr(db, "save_settings", self.save)
        return self

    def activated(self):
        return fs.activated_at_from(self.rows.get(fs.ACTIVATION_ROW_ID))


def _old_solo(pid="old", **over):
    return _solo(pid, followup_enrolled_at=ACT - 21 * D, **over)


@pytest.mark.realactivation
def test_the_first_enabled_tick_records_activation_once_and_sends_nothing_old(world, monkeypatch):
    f = world([_old_solo()])
    st = Store().install(monkeypatch)
    _tick(f, ACT)
    assert st.activated() == ACT and f.box.sent == [], "the release burst"
    assert st.rows[fs.ACTIVATION_ROW_ID]["activated_at"] == "2026-10-20T17:00:00+00:00"
    for k in range(1, 12):                       # every 6 hours, up to 3 days less a little
        _tick(f, ACT + k * 6 * H)
    assert f.box.customers() == []
    _tick(f, ACT + 3 * D)
    assert f.box.customers() == ["dana@acme.com"]
    assert [w[0] for w in st.writes] == [fs.ACTIVATION_ROW_ID], "activation written twice"
    assert st.activated() == ACT


@pytest.mark.realactivation
def test_a_disabled_tick_records_nothing(world, monkeypatch):
    f = world([_old_solo()])
    st = Store().install(monkeypatch)
    monkeypatch.setenv("FOLLOWUP_AUTOMATION_ENABLED", "false")
    _tick(f, ACT)
    assert st.rows == {} and f.box.sent == []


@pytest.mark.realactivation
def test_an_unreadable_activation_row_sends_nothing_and_records_nothing(world, monkeypatch):
    f = world([_old_solo(), _solo("new", followup_enrolled_at=ACT - 4 * D)])
    st = Store(fail=True).install(monkeypatch)
    _tick(f, ACT)
    assert f.box.sent == [] and st.writes == [] and f.followups == []


@pytest.mark.realactivation
def test_twin_workers_ticking_at_once_record_one_moment_and_both_use_it(world, monkeypatch):
    f = world([_old_solo()])
    st = Store().install(monkeypatch)
    barrier = threading.Barrier(2)
    got, errors = [], []
    real_read = fw.read_activation

    def slow_read():
        out = real_read()
        barrier.wait()                           # both read "never activated" before either writes
        return out

    monkeypatch.setattr(fw, "read_activation", slow_read)

    def tick(at):
        try:
            got.append(fw.ensure_activation(at))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=tick, args=(ACT + k * H,)) for k in (0, 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and len(got) == 2 and got[0] == got[1] == st.activated()
    assert len(st.writes) == 1


@pytest.mark.realactivation
def test_a_stored_activation_is_what_the_tick_uses(world, monkeypatch):
    f = world([_old_solo(), _solo("new", followup_enrolled_at=ACT + D)])
    st = Store({fs.ACTIVATION_ROW_ID: fs.activation_value(ACT)}).install(monkeypatch)
    for at in (ACT + H, ACT + 3 * D - H):        # the stored moment, not each tick's clock
        _tick(f, at)
    assert f.box.sent == [] and st.writes == []
    _tick(f, ACT + 3 * D)                        # old: activation + 3; new: not yet (sent + 3)
    assert f.box.customers() == ["dana@acme.com"]
    assert [e["proposal_id"] for e in _emails(f)] == ["old"]
    _tick(f, ACT + 4 * D)
    assert [e["proposal_id"] for e in _emails(f)] == ["old", "new"]


# ══ the dry run ═══════════════════════════════════════════════════════════════
@pytest.mark.realactivation
def test_the_dry_run_before_activation_evaluates_as_if_activated_now(world, monkeypatch):
    f = world([_old_solo()])
    st = Store().install(monkeypatch)
    dry = fw.dry_run_count(ACT)
    assert dry["activated_at"] is None and dry["activation_pending"] is True
    assert dry["customer_emails"] == [] and dry["staff_emails"] == []
    assert st.writes == [] and f.followups == [], "the dry run recorded the activation"
    text = followup_dry_run.table(dry)
    assert "not yet activated: the first enabled tick will set it to %s" % ACT.isoformat() in text
    later = fw.dry_run_count(ACT + 3 * D)        # "if it were activated at --now"
    assert later["customer_emails"] == []


@pytest.mark.realactivation
def test_the_dry_run_shows_the_stored_moment_and_what_it_would_send_then(world, monkeypatch):
    capped = _solo("capped", followup_enrolled_at=ACT - 30 * D, customer_email="lee@acme.com")
    f = world([_old_solo(), capped])
    f.clock = ACT - 25 * D
    f.reserve_followup("capped", "c:nv1", {"audience": "customer", "template": "not_viewed"})
    f.clock = ACT - 18 * D
    f.reserve_followup("capped", "c:nv2", {"audience": "customer", "template": "not_viewed_2"})
    Store({fs.ACTIVATION_ROW_ID: fs.activation_value(ACT)}).install(monkeypatch)
    first = fw.dry_run_count(ACT + H)
    assert first["activated_at"] == ACT.isoformat() and first["activation_pending"] is False
    assert first["customer_emails"] == [] and first["staff_emails"] == []
    assert [d["proposal_id"] for d in first["done_before_activation"]] == ["capped"]
    text = followup_dry_run.table(first)
    assert "activated at ..................... %s" % ACT.isoformat() in text
    assert "steps done before activation ...... 1" in text
    then = fw.dry_run_count(ACT + 3 * D)
    assert [e["proposal_id"] for e in then["customer_emails"]] == ["old"]


@pytest.mark.realactivation
def test_the_dry_run_with_an_unreadable_activation_lists_nothing(world, monkeypatch):
    world([_old_solo()])
    Store(fail=True).install(monkeypatch)
    dry = fw.dry_run_count(ACT + 30 * D)
    assert dry["activation_error"] and dry["customer_emails"] == [] and dry["count"] == 0
    # Unreadable is not "never activated": the tick would not record now, it would send nothing.
    assert dry["activation_pending"] is False and dry["activated_at"] is None
    text = followup_dry_run.table(dry)
    assert "activation time UNREADABLE" in text and "not yet activated" not in text


# ══ the ops command ═══════════════════════════════════════════════════════════
@pytest.mark.realactivation
def test_the_ops_command_records_then_is_idempotent_then_refuses_to_move(monkeypatch, capsys):
    st = Store().install(monkeypatch)
    assert followup_activate.main(["--at", "2026-10-20T17:00:00Z"]) == 0
    assert st.activated() == ACT
    assert "set activated_at to 2026-10-20T17:00:00+00:00" in capsys.readouterr().out
    # The same command again: unchanged, nothing written.
    assert followup_activate.main(["--at", "2026-10-20T12:00:00"]) == 0   # naive = Central
    assert "unchanged" in capsys.readouterr().out and len(st.writes) == 1
    # A different moment: refused without --replace.
    assert followup_activate.main(["--at", "2026-10-25T12:00:00"]) == 3
    assert "refused" in capsys.readouterr().err and st.activated() == ACT
    assert followup_activate.main(["--at", "2026-10-25T12:00:00", "--replace", "--by", "hanz"]) == 0
    assert "moved activated_at" in capsys.readouterr().out
    assert st.activated() == datetime(2026, 10, 25, 17, 0, tzinfo=timezone.utc)
    assert st.writes[-1][2] == "hanz" and st.rows[fs.ACTIVATION_ROW_ID]["by"] == "hanz"


@pytest.mark.realactivation
def test_the_ops_command_show_writes_nothing_and_bad_input_is_refused(monkeypatch, capsys):
    st = Store().install(monkeypatch)
    assert followup_activate.main(["--show"]) == 0
    assert "not yet activated" in capsys.readouterr().out and st.writes == []
    assert followup_activate.main(["--at", "tomorrow"]) == 2
    assert followup_activate.main(["--at"]) == 2
    assert st.writes == []


@pytest.mark.realactivation
def test_the_ops_command_defaults_to_now(monkeypatch, capsys):
    st = Store().install(monkeypatch)
    assert followup_activate.run([], now=ACT) == 0
    assert st.activated() == ACT and st.writes[0][2] == followup_activate.DEFAULT_BY


# ══ the board ═════════════════════════════════════════════════════════════════
@pytest.mark.realactivation
@pytest.mark.parametrize("stored", [True, False])
def test_the_boards_next_follow_up_date_is_the_workers(monkeypatch, stored):
    import json

    import main
    from test_admin_pipeline import _row
    row = _row(proposal_id="old", proposal_status="sent", viewed_at=None,
               created_at=ACT - 21 * D, followup_enrolled_at=ACT - 21 * D)
    monkeypatch.setattr(main, "_admin_ok", lambda request: True)
    monkeypatch.setattr(main.db, "list_all_portal_proposals", lambda: [row])
    monkeypatch.setattr(main.db, "unread_counts", lambda: {})
    monkeypatch.setattr(main, "_now_utc", lambda: ACT + 2 * D)
    Store({fs.ACTIVATION_ROW_ID: fs.activation_value(ACT)} if stored else {}).install(monkeypatch)
    card, = json.loads(bytes(main.admin_pipeline(request=None).body).decode())["proposals"]
    # Stored: activation + 3 days. Never activated: projected from now (the board's clock).
    want = ACT + 3 * D if stored else ACT + 5 * D
    assert datetime.fromisoformat(card["next_followup_at"].replace("Z", "+00:00")) == want
