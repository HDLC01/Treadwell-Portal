"""Per-project (scope group) follow-ups, 2026-10-24 (build spec P1).

RJ's "one project, many proposals": a project priced as several separately approved scopes is
chased as ONE project. These pin the properties Hanz settled:

  * a group raises ONE task per due occurrence, filed on a stable HOME scope, listing its scopes;
  * a touch on ANY scope re-anchors the group; only sent/called reminders count toward the cap;
  * the group is paused only while EVERY open scope is (decision 7);
  * approving one scope stops the group chase and raises ONE "decide the other scopes" task,
    never closing a sibling (decisions 2 and 9), and that task survives the housekeeping;
  * the approved scope still raises its own deposit reminder;
  * a deleted or closed home re-homes the task;
  * the deploy-day dry run counts without writing anything.

The ungrouped path is covered by the existing remind-only files, which run unchanged.
"""
import copy
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import db
import followup_rules as rules
import followup_worker as fw
import main

NOW = datetime(2026, 8, 4, 15, 0, tzinfo=timezone.utc)     # 10am Chicago, inside the send window
ENROLLED = NOW - timedelta(hours=30)                       # the 24h not-opened step is due
T0 = NOW - timedelta(days=5)
GID = "grp-acme-0001"
H = timedelta(hours=1)
ALL_DAY = {"send_start_hour": 0, "send_end_hour": 24}


def _scope(pid, label, minutes, **over):
    row = {"proposal_id": pid, "token": "tok-" + pid, "customer_email": "dana@acme.com",
           "customer_name": "Dana Reed", "project_name": "Acme HQ", "scope_group": GID,
           "scope_label": label, "proposal_status": "sent", "deposit_status": "pending",
           "deposit_required": True, "assigned_estimator": "kyle@wetreadwell.com",
           "schedule_status": "pending", "contacts_status": "pending",
           "created_at": T0 + timedelta(minutes=minutes), "followup_enrolled_at": ENROLLED,
           "followup_disabled_at": None, "followup_paused_until": None,
           "cycle_viewed_at": None, "deleted_at": None, "approved_at": None}
    row.update(over)
    return row


def _three(**over):
    return [_scope("d-break", "Breakroom", 0, **over), _scope("d-lab1", "Lab 1", 1, **over),
            _scope("d-lab2", "Lab 2", 2, **over)]


# ══ a fake database: the db functions the worker and the endpoints call ════════
class FakeDB:
    def __init__(self, rows, drafts=None):
        self.rows = {r["proposal_id"]: dict(r) for r in rows}
        self.drafts = drafts if drafts is not None else [
            {"id": r["proposal_id"], "archived": False} for r in rows]
        self.followups = []
        self.signals = {}
        self.messages = []
        self.clock = NOW
        self._id = 100
        self.reads = []

    def _new(self, pid, kind, detail, by=None):
        self._id += 1
        f = {"id": self._id, "proposal_id": pid, "kind": kind, "detail": dict(detail or {}),
             "created_by": by, "created_at": self.clock}
        self.followups.append(f)
        return f

    # ── proposals ──
    def get_proposal(self, pid):
        r = self.rows.get(pid)
        return dict(r) if r else None

    def list_followup_candidates(self):
        out = []
        for r in self.rows.values():
            if r.get("deleted_at") or not r.get("followup_enrolled_at") or \
                    r.get("followup_disabled_at"):
                continue
            if rules.in_scope(r):
                out.append(dict(r))
        return out

    def list_scope_group(self, gid):
        self.reads.append(("list_scope_group", gid))
        rows = [dict(r) for r in self.rows.values()
                if r.get("scope_group") == gid and not r.get("deleted_at")]
        return sorted(rows, key=lambda r: (r["created_at"], r["proposal_id"]))

    def list_scope_group_draft_states(self, gid):
        self.reads.append(("drafts", gid))
        return [dict(d) for d in self.drafts]

    def list_scope_group_drafts(self, gid):
        return [d["id"] for d in self.drafts]

    def close_lost(self, pid, reason):
        if pid not in self.rows:
            return False
        self.rows[pid].update(proposal_status="closed_lost", closed_lost_reason=reason)
        return True

    def set_approved(self, pid, *a, **k):
        self.rows[pid].update(proposal_status="approved", approved_at=self.clock)

    def stamp_followup_anchor(self, pid, clear_next=True):
        self.rows[pid]["followup_anchor_at"] = self.clock

    def set_followup_plan(self, pid, **kw):
        for k, v in kw.items():
            self.rows[pid]["followup_" + k] = v

    # ── follow-up rows ──
    def _open(self, f):
        return f["kind"] == "followup_task" and f["detail"].get("status") in ("pending", "sending")

    def followup_signals(self, pid):
        out = dict(self.signals.get(pid, {}))
        if "followup_reminder_times" not in out:
            out["followup_reminder_times"] = [
                f["created_at"].isoformat() for f in self.followups
                if f["proposal_id"] == pid and f["kind"] == "followup_task"
                and f["detail"].get("status") in ("sent", "called")]
        mine = [f for f in self.followups if f["proposal_id"] == pid and self._open(f)]
        out["pending_task"] = ({"id": mine[-1]["id"], "detail": dict(mine[-1]["detail"]),
                                "created_at": mine[-1]["created_at"]} if mine else None)
        return out

    def reserve_followup_task(self, pid, key, detail):
        for f in self.followups:
            if f["proposal_id"] == pid and f["kind"] in ("auto_email", "followup_task") \
                    and f["detail"].get("rule_key") == key:
                return None                                     # portal_followups_rule_v2_uidx
        stage = detail.get("stage") or "pre"
        for f in self.followups:
            if f["proposal_id"] == pid and self._open(f) \
                    and (f["detail"].get("stage") or "pre") == stage:
                return None                                     # one_open_task_v2_uidx
        d = dict(detail, rule_key=key)
        d.setdefault("status", "pending")
        return self._new(pid, "followup_task", d)["id"]

    def reserve_followup(self, pid, key, detail):
        for f in self.followups:
            if f["proposal_id"] == pid and f["kind"] in ("auto_email", "followup_task") \
                    and f["detail"].get("rule_key") == key:
                return None
        return self._new(pid, "auto_email", dict(detail, rule_key=key))["id"]

    def delete_followup(self, fid):
        self.followups = [f for f in self.followups if f["id"] != fid]

    def tasks(self, **match):
        out = [f for f in self.followups if f["kind"] == "followup_task"]
        for k, v in match.items():
            out = [f for f in out if f["detail"].get(k) == v]
        return out

    def get_followup_task(self, tid):
        for f in self.followups:
            if f["id"] == int(tid) and f["kind"] == "followup_task":
                return copy.deepcopy(f)
        return None

    def list_pending_followup_tasks(self, limit=500):
        return [dict(copy.deepcopy(f), project_name="Acme HQ", estimator_email="kyle@x")
                for f in self.followups if f["kind"] == "followup_task"
                and f["detail"].get("status") == "pending"
                and not (self.rows.get(f["proposal_id"]) or {}).get("deleted_at")]

    def list_open_group_tasks(self, gid):
        return [copy.deepcopy(f) for f in self.followups
                if self._open(f) and f["detail"].get("scope_group") == gid]

    def decide_followup_task(self, tid, status, by, extra=None, from_status="pending"):
        for f in self.followups:
            if f["id"] == int(tid) and f["kind"] == "followup_task":
                if f["detail"].get("status") != from_status:
                    return None
                f["detail"].update(dict(extra or {}, status=status, decided_by=by))
                return copy.deepcopy(f)
        return None

    def reopen_followup_task(self, tid, extra=None):
        return bool(self.decide_followup_task(tid, "pending", None, extra, "sending"))

    def list_stale_sending_tasks(self, minutes):
        return []

    def followup_task_send_recorded(self, tid):
        return False

    def scope_group_resumed_at(self, gid):
        got = [f["created_at"] for f in self.followups if f["kind"] == "staff_note"
               and f["detail"].get("action") == "scope_group_resume"
               and f["detail"].get("scope_group") == gid]
        return max(got) if got else None

    def followup_rule_key_exists(self, pid, key):
        return any(f["proposal_id"] == pid and f["kind"] in ("auto_email", "followup_task")
                   and f["detail"].get("rule_key") == key for f in self.followups)

    def pending_followup_task_id(self, pid):
        return None

    def add_followup(self, pid, kind, detail=None, created_by=None):
        f = self._new(pid, kind, detail, created_by)
        return {"kind": kind, "detail": f["detail"], "created_by": created_by,
                "created_at": f["created_at"]}

    def add_message(self, pid, kind, who, body, msg_type="text", meta=None):
        self.messages.append({"pid": pid, "body": body, "meta": meta or {}})
        return {"id": len(self.messages)}

    def get_recipients(self, pid):
        r = self.rows.get(pid) or {}
        return [r["customer_email"]] if r.get("customer_email") else []

    get_followup_recipients = get_recipients


@pytest.fixture
def fake(monkeypatch):
    """Install a FakeDB (call it with the rows) and record every email that would leave."""
    sent = {"staff": [], "customer": [], "group": []}
    holder = {}

    def install(rows, drafts=None):
        f = FakeDB(rows, drafts)
        holder["db"] = f
        for name in dir(FakeDB):
            if not name.startswith("_") and name not in ("tasks",):
                monkeypatch.setattr(db, name, getattr(f, name), raising=False)
        monkeypatch.setattr(db, "get_settings", lambda key: None)
        f.sent = sent
        return f

    monkeypatch.setenv("FOLLOWUP_AUTOMATION_ENABLED", "true")
    monkeypatch.setattr(fw.email_sender, "notify_team",
                        lambda subject, body, **k: sent["staff"].append(
                            {"subject": subject, "body": body, "to": k.get("recipients"),
                             "proposal_id": k.get("proposal_id"),
                             "link": k.get("reply_link")}) or ["kyle@wetreadwell.com"])
    monkeypatch.setattr(fw.email_sender, "send_followup",
                        lambda *a, **k: sent["customer"].append(a) or True)
    monkeypatch.setattr(fw.email_sender, "send_scope_group_followup",
                        lambda email, name, project, scopes, tpl, **k: sent["group"].append(
                            {"to": email, "scopes": [s["label"] for s in scopes],
                             "body": tpl["body"]}) or True)
    monkeypatch.setattr(fw.email_sender, "proposal_reply_to", lambda t: None)
    monkeypatch.setattr(fw.email_sender, "_resolve_notify", lambda kind, pid=None: [])
    monkeypatch.setattr(fw.email_sender, "send_customer_update", lambda *a, **k: True,
                        raising=False)
    monkeypatch.setattr(main, "_now_utc", lambda: NOW)
    return install


def _members(rows, **sig):
    """Rows as group_view's input: signals laid over, as the worker's _with_signals does."""
    return [dict(r, **sig.get(r["proposal_id"], {})) for r in rows]


# ══ B. group_view — pure ═══════════════════════════════════════════════════════
def test_group_view_folds_the_open_scopes_into_one_proposal_on_the_home_scope():
    rows = _three()
    rows[1].update(cycle_viewed_at=NOW - 10 * H, followup_enrolled_at=ENROLLED - 2 * H)
    rows[2].update(cycle_viewed_at=NOW - 20 * H)
    v = rules.group_view(rows, NOW)
    assert v["proposal_id"] == "d-break", "home = earliest (created_at, proposal_id)"
    assert v["proposal_status"] == "viewed"
    assert v["followup_enrolled_at"] == ENROLLED - 2 * H
    assert v["cycle_viewed_at"] == NOW - 20 * H
    assert [s["label"] for s in v["scopes"]] == ["Breakroom", "Lab 1", "Lab 2"]


def test_a_touch_on_any_scope_reanchors_the_whole_group():
    viewed = _three(proposal_status="viewed", cycle_viewed_at=NOW - 80 * H)
    due = [d for d in rules.due_now(rules.group_view(viewed, NOW), NOW, ALL_DAY)
           if d.audience == "customer"]
    assert due, "the control case is not due, so this proves nothing"
    touched = _members(viewed, **{"d-lab2": {"customer_touch_at": NOW - H}})
    view = rules.group_view(touched, NOW)
    assert view["customer_touch_at"] == NOW - H
    assert [d for d in rules.due_now(view, NOW, ALL_DAY) if d.audience == "customer"] == []
    assert rules.next_due_at(view, NOW, ALL_DAY) == NOW - H + rules.RECURRING


def test_the_cap_is_per_group_and_counts_every_members_reminders():
    """One counted reminder on Lab 1 and one on Lab 2 make two for the GROUP's stop-after-2."""
    viewed = _three(proposal_status="viewed", cycle_viewed_at=NOW - 300 * H)
    viewed[0]["followup_stop_after"] = 2
    one = [(NOW - 290 * H).isoformat()]
    capped = _members(viewed, **{"d-lab1": {"followup_reminder_times": one},
                                 "d-lab2": {"followup_reminder_times": one}})
    assert _customer_due(rules.group_view(capped, NOW)) is None
    under = _members(viewed, **{"d-lab1": {"followup_reminder_times": one}})
    assert _customer_due(rules.group_view(under, NOW)) is not None


def _customer_due(view):
    got = [d for d in rules.due_now(view, NOW, ALL_DAY) if d.audience == "customer"]
    return got[0] if got else None


def _reminder_times_admitted(statuses):
    """Which follow-up rows the REAL _REMINDER_TIMES_SQL predicate counts, run by SQLite."""
    sql = db._REMINDER_TIMES_SQL.format(pid="'p1'", enrolled="null")
    pred = sql[sql.index(" where ") + len(" where "):-1]
    con = sqlite3.connect(":memory:")
    con.execute("attach database ':memory:' as public")
    con.execute("create table public.portal_followups (id int, proposal_id text, kind text, "
                "detail text, created_at text)")
    for i, st in enumerate(statuses):
        con.execute("insert into public.portal_followups values (?,?,?,?,?)",
                    (i, "p1", "followup_task", '{"status": "%s"}' % st, "2026-08-01"))
    con.execute("insert into public.portal_followups values (99,'p1','auto_email',"
                "'{\"audience\": \"customer\"}','2026-08-01')")
    got = con.execute("select f.id from public.portal_followups f where " + pred).fetchall()
    return {statuses[i] if i < len(statuses) else "auto_email" for (i,) in got}


def test_only_sent_and_called_reminders_count_toward_the_cap():
    """Decision 6, run through the SQL the worker and the board both read."""
    got = _reminder_times_admitted(
        ["sent", "called", "skipped", "snoozed", "expired", "pending", "decided"])
    assert got == {"sent", "called", "auto_email"}, got


def test_one_paused_scope_does_not_pause_the_group_but_all_paused_does():
    later = (NOW + timedelta(days=30)).date()
    one = _three()
    one[1]["followup_paused_until"] = later
    v = rules.group_view(one, NOW)
    assert not rules.is_paused(v, NOW)
    assert [d for d in rules.due_now(v, NOW) if d.audience == "customer"]
    all_paused = _three(followup_paused_until=later)
    all_paused[2]["followup_paused_until"] = later + timedelta(days=9)
    v = rules.group_view(all_paused, NOW)
    assert rules.is_paused(v, NOW) and rules.due_now(v, NOW) == []
    assert v["followup_paused_until"] == later, "the group resumes when the FIRST pause ends"


def test_an_approved_member_stops_the_group_until_keep_is_chosen():
    rows = _three()
    rows[1].update(proposal_status="approved", approved_at=NOW - 5 * H)
    assert rules.group_state(rows, NOW) == (None, "scope_approved")
    resumed = [dict(r, scope_group_resumed_at=NOW - H) for r in rows]
    v = rules.group_view(resumed, NOW)
    assert v is not None and [s["proposal_id"] for s in v["scopes"]] == ["d-break", "d-lab2"]
    stale = [dict(r, scope_group_resumed_at=NOW - 9 * H) for r in rows]
    assert rules.group_view(stale, NOW) is None, "a keep from BEFORE the approval does not count"


def test_the_decision_task_is_live_while_a_listed_sibling_is_open():
    d = {"stage": "decision", "scopes": [{"proposal_id": "d-lab1"}, {"proposal_id": "d-lab2"}]}
    rows = _three()
    assert rules.task_is_live(d, {"scope_members": rows}, NOW) == (True, "")
    rows[1]["proposal_status"] = rows[2]["proposal_status"] = "closed_lost"
    assert rules.task_is_live(d, {"scope_members": rows}, NOW) == (False, "decision_moot")


# ══ C. the worker: one task per group ══════════════════════════════════════════
def test_a_group_of_three_raises_exactly_one_task_listing_all_three(fake):
    f = fake(_three())
    fw._tick(NOW)
    tasks = f.tasks()
    assert len(tasks) == 1, tasks
    t = tasks[0]
    assert t["proposal_id"] == "d-break", "filed on the home scope"
    assert t["detail"]["scope_group"] == GID
    assert [s["label"] for s in t["detail"]["scopes"]] == ["Breakroom", "Lab 1", "Lab 2"]
    assert t["detail"]["rule_key"] == "%s:nv1" % rules.cycle_key(ENROLLED)
    # One estimator email for the moment, naming the scopes, with the 24h note folded into it.
    assert [s["subject"] for s in f.sent["staff"]] == ["Follow-up due — Acme HQ"]
    assert "Breakroom, Lab 1, Lab 2" in f.sent["staff"][0]["body"]
    assert f.sent["customer"] == [] and f.sent["group"] == []


def test_two_ticks_and_the_twin_container_raise_it_once(fake):
    f = fake(_three())
    fw._tick(NOW)
    fw._tick(NOW + timedelta(minutes=15))
    fw._tick(NOW + timedelta(minutes=15))      # the twin, same moment
    assert len(f.tasks()) == 1
    assert len(f.sent["staff"]) == 1


def test_a_reply_on_any_scope_holds_the_groups_next_task(fake):
    viewed = _three(proposal_status="viewed", cycle_viewed_at=NOW - 80 * H)
    f = fake(viewed)
    f.signals["d-lab2"] = {"customer_touch_at": NOW - H}
    fw._tick(NOW)
    assert f.tasks() == []
    f = fake(viewed)
    fw._tick(NOW)
    assert len(f.tasks()) == 1, "the control case raised nothing, so this proved nothing"


def test_an_approved_scope_still_raises_its_own_deposit_task(fake):
    rows = _three()
    rows[1].update(proposal_status="approved", approved_at=NOW - 30 * H,
                   cycle_viewed_at=NOW - 40 * H)
    f = fake(rows)
    # Its decision task is open on the same scope: it must not hold the deposit chase's slot.
    f.reserve_followup_task("d-lab1", "sd:%s:d-lab1" % GID, {
        "stage": "decision", "template": "scope_decision", "scope_group": GID,
        "scopes": [{"proposal_id": "d-break"}, {"proposal_id": "d-lab2"}]})
    fw._tick(NOW)
    dep = [t for t in f.tasks(template="deposit_nudge")]
    assert [t["proposal_id"] for t in dep] == ["d-lab1"]
    assert not f.tasks(scope_group=GID, stage="pre"), "the stopped group raised a reminder"
    assert f.tasks(stage="decision")[0]["detail"]["status"] == "pending"


def test_the_decision_task_survives_housekeeping(fake):
    """task_is_live(approved scope) would say 'customer_done' / 'stage_changed' and expire it on
    the next tick; it is judged against its siblings instead."""
    rows = _three()
    rows[1].update(proposal_status="approved", approved_at=NOW - 30 * H, deposit_status="received")
    f = fake(rows)
    f.reserve_followup_task("d-lab1", "sd:%s:d-lab1" % GID, {
        "stage": "decision", "template": "scope_decision", "scope_group": GID,
        "scopes": [{"proposal_id": "d-break"}, {"proposal_id": "d-lab2"}]})
    fw._tick(NOW)
    fw._tick(NOW + H)
    assert f.tasks(stage="decision")[0]["detail"]["status"] == "pending"
    # ...and it goes once every sibling it lists has been dealt with.
    f.rows["d-break"]["proposal_status"] = f.rows["d-lab2"]["proposal_status"] = "closed_lost"
    fw._tick(NOW + 2 * H)
    t = f.tasks(stage="decision")[0]["detail"]
    assert t["status"] == "expired" and t["reason"] == "decision_moot"


def test_an_unresolved_decision_task_renags_the_estimator(fake):
    rows = _three()
    rows[1].update(proposal_status="approved", approved_at=NOW - 30 * H, deposit_status="received")
    f = fake(rows)
    f.clock = NOW - timedelta(days=4)
    f.reserve_followup_task("d-lab1", "sd:%s:d-lab1" % GID, {
        "stage": "decision", "template": "scope_decision", "scope_group": GID,
        "approved_scope": {"proposal_id": "d-lab1", "label": "Lab 1"},
        "scopes": [{"proposal_id": "d-break", "label": "Breakroom"}]})
    f.clock = NOW
    fw._tick(NOW)
    fw._tick(NOW + H)
    nags = [s for s in f.sent["staff"] if s["subject"].startswith("Decide the other scopes")]
    assert len(nags) == 1, nags


def test_a_deleted_home_rehomes_the_task(fake):
    f = fake(_three())
    fw._tick(NOW)
    first, = f.tasks()
    assert first["proposal_id"] == "d-break"
    f.rows["d-break"]["deleted_at"] = NOW
    fw._tick(NOW + H)
    assert first["detail"]["status"] == "expired" and first["detail"]["reason"] == "rehomed"
    live = [t for t in f.tasks() if t["detail"]["status"] == "pending"]
    assert [t["proposal_id"] for t in live] == ["d-lab1"], "not re-raised on the new home"
    assert [s["label"] for s in live[0]["detail"]["scopes"]] == ["Lab 1", "Lab 2"]


def test_a_closed_home_rehomes_the_task(fake):
    f = fake(_three())
    fw._tick(NOW)
    first, = f.tasks()
    f.rows["d-break"]["proposal_status"] = "closed_lost"
    fw._tick(NOW + H)
    assert first["detail"]["status"] == "expired"
    assert [t["proposal_id"] for t in f.tasks() if t["detail"]["status"] == "pending"] == \
        ["d-lab1"]


def test_a_scope_taken_out_of_the_group_is_chased_on_its_own(fake):
    rows = _three()
    f = fake(rows, drafts=[{"id": "d-break", "archived": False},
                           {"id": "d-lab1", "archived": False}])
    fw._tick(NOW)
    owners = sorted(t["proposal_id"] for t in f.tasks())
    assert owners == ["d-break", "d-lab2"], owners
    solo = [t for t in f.tasks() if t["proposal_id"] == "d-lab2"][0]
    assert "scope_group" not in solo["detail"], "the detached scope got a group task"


def test_an_archived_scope_is_chased_neither_way(fake):
    f = fake(_three(), drafts=[{"id": "d-break", "archived": False},
                               {"id": "d-lab1", "archived": False},
                               {"id": "d-lab2", "archived": True}])
    fw._tick(NOW)
    t, = f.tasks()
    assert [s["proposal_id"] for s in t["detail"]["scopes"]] == ["d-break", "d-lab1"]


def test_an_unreadable_group_is_skipped_not_chased_per_scope(fake, monkeypatch):
    f = fake(_three())
    monkeypatch.setattr(db, "list_scope_group_draft_states",
                        lambda gid: (_ for _ in ()).throw(RuntimeError("db blip")))
    fw._tick(NOW)
    assert f.tasks() == []


def test_an_ungrouped_tick_never_reads_a_scope_group(fake):
    f = fake([_scope("solo", None, 0, scope_group=None, scope_label=None)])
    fw._tick(NOW)
    assert f.reads == []
    t, = f.tasks()
    assert "scope_group" not in t["detail"] and "scopes" not in t["detail"]


# ══ D. approving one scope ═══════════════════════════════════════════════════
@pytest.fixture
def approve(fake, monkeypatch, tmp_path):
    f = fake(_three(proposal_status="viewed", cycle_viewed_at=NOW - 30 * H))
    monkeypatch.setattr(main.config, "UPLOAD_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(main, "_require",
                        lambda request, token: f.get_proposal(token.replace("tok-", "")))
    monkeypatch.setattr(main, "_session_email", lambda request: "dana@acme.com")
    monkeypatch.setattr(main.db, "get_pinned_draft_data",
                        lambda p: {"project_name": "Acme HQ", "work_type": "epoxy",
                                   "proposal_lump_sum": 10000.0})
    monkeypatch.setattr(main.signing, "signing_block",
                        lambda p, data: {"required": False, "blocked_reason": None})
    monkeypatch.setattr(main.db, "add_approval", lambda *a, **k: 1)
    monkeypatch.setattr(main.automations, "run_on_approval", lambda p, project: None)
    client = TestClient(main.app)

    def go(pid="d-lab1"):
        return client.post("/api/portal/tok-%s/approve" % pid,
                           json={"name": "Dana Reed", "date": "2026-08-04"})

    return f, go


def test_approving_one_scope_raises_one_decision_task_and_stops_the_group(approve):
    f, go = approve
    fw._tick(NOW)                                   # the group's reminder is open on the home
    reminder, = f.tasks()
    assert reminder["detail"]["status"] == "pending"
    assert go().status_code == 200
    assert reminder["detail"]["status"] == "expired"
    assert reminder["detail"]["reason"] == "scope_approved"
    dec, = f.tasks(stage="decision")
    assert dec["proposal_id"] == "d-lab1"
    assert dec["detail"]["rule_key"] == "sd:%s:d-lab1" % GID
    assert [s["proposal_id"] for s in dec["detail"]["scopes"]] == ["d-break", "d-lab2"]
    assert [s["subject"] for s in f.sent["staff"]].count("Decide the other scopes — Acme HQ") == 1
    # NEVER auto-closed.
    assert [f.rows[p]["proposal_status"] for p in ("d-break", "d-lab2")] == ["viewed", "viewed"]
    # The chase has stopped: a later tick raises no reminder for the group.
    fw._tick(NOW + timedelta(days=5))
    assert [t for t in f.tasks(scope_group=GID) if t["detail"]["status"] == "pending"] == [dec]
    # A double submit raises nothing more.
    go()
    assert len(f.tasks(stage="decision")) == 1


def _decision(f, go):
    go()
    dec, = f.tasks(stage="decision")
    return dec["id"]


def _decide(body, tid):
    return TestClient(main.app).post("/api/admin/followup-task/%s/scope-decision" % tid,
                                     json=body)


@pytest.fixture
def admin(monkeypatch):
    monkeypatch.setattr(main, "_admin_ok", lambda request: True)


@pytest.mark.parametrize("body,error", [
    ({"action": "close", "note": "went elsewhere"}, "invalid_reason"),
    ({"action": "close", "reason": "price"}, "note_required"),
    ({"action": "close", "reason": "made_up", "note": "x"}, "invalid_reason"),
    ({"action": "explode"}, "invalid_action"),
    ({"action": "close", "reason": "price", "note": "x", "scope_ids": ["nope"]}, "unknown_scope"),
])
def test_close_without_reason_and_note_is_refused(approve, admin, body, error):
    f, go = approve
    tid = _decision(f, go)
    r = _decide(body, tid)
    assert r.status_code == 400 and r.json()["error"] == error
    assert f.tasks(stage="decision")[0]["detail"]["status"] == "pending"
    assert [f.rows[p]["proposal_status"] for p in ("d-break", "d-lab2")] == ["viewed", "viewed"]


def test_close_closes_only_the_chosen_scopes_through_the_staff_path(approve, admin):
    f, go = approve
    tid = _decision(f, go)
    r = _decide({"action": "close", "scope_ids": ["d-lab2"], "reason": "price",
                 "note": "Went with the cheaper quote", "by": "kyle@wetreadwell.com"}, tid)
    assert r.status_code == 200, r.text
    assert r.json()["closed"] == ["d-lab2"]
    assert f.rows["d-lab2"]["proposal_status"] == "closed_lost"
    assert f.rows["d-break"]["proposal_status"] == "viewed"
    note = [x for x in f.followups if x["kind"] == "staff_note"
            and x["detail"].get("action") == "closed_lost"]
    assert note and note[0]["detail"]["reason"] == "price"
    assert f.tasks(stage="decision")[0]["detail"]["outcome"] == "close"
    again = _decide({"action": "leave"}, tid)
    assert again.status_code == 409


def test_keep_resumes_the_group_chase_for_the_remaining_scopes(approve, admin):
    f, go = approve
    tid = _decision(f, go)
    f.clock = NOW + H
    r = _decide({"action": "keep", "by": "kyle@wetreadwell.com"}, tid)
    assert r.status_code == 200, r.text
    assert f.scope_group_resumed_at(GID) == NOW + H
    assert f.rows["d-break"]["followup_anchor_at"] == NOW + H, "keep did not re-anchor the home"
    members = fw._with_signals(fw.group_members(GID), GID)
    v = rules.group_view(members, NOW + 2 * H)
    assert v is not None and [s["proposal_id"] for s in v["scopes"]] == ["d-break", "d-lab2"]


def test_leave_only_resolves_the_task(approve, admin):
    f, go = approve
    tid = _decision(f, go)
    assert _decide({"action": "leave"}, tid).status_code == 200
    assert f.tasks(stage="decision")[0]["detail"]["status"] == "decided"
    assert f.scope_group_resumed_at(GID) is None
    assert [f.rows[p]["proposal_status"] for p in ("d-break", "d-lab2")] == ["viewed", "viewed"]


@pytest.mark.parametrize("path", ["send", "skip", "called", "snooze"])
def test_a_decision_task_is_not_a_reminder(approve, admin, path):
    f, go = approve
    tid = _decision(f, go)
    r = TestClient(main.app).post("/api/admin/followup-task/%s/%s" % (tid, path), json={
        "title": "Hi", "body": "Hello {link}",
        "until": (NOW + timedelta(days=3)).date().isoformat()})
    assert r.status_code == 409 and r.json()["error"] == "decision_task"
    assert f.tasks(stage="decision")[0]["detail"]["status"] == "pending"


# ══ E. the task endpoints for a group task ═════════════════════════════════════
def test_sending_a_group_task_emails_each_recipient_only_their_scopes(fake, admin):
    rows = _three()
    rows[2]["customer_email"] = "pat@acme.com"          # Lab 2 went to somebody else
    f = fake(rows)
    fw._tick(NOW)
    t, = f.tasks()
    r = TestClient(main.app).post("/api/admin/followup-task/%s/send" % t["id"], json={
        "title": "Checking in", "body": "Hi {first_name},\n\nAny questions?\n\n{link}",
        "by": "kyle@wetreadwell.com"})
    assert r.status_code == 200, r.text
    got = {g["to"]: g["scopes"] for g in f.sent["group"]}
    assert got == {"dana@acme.com": ["Breakroom", "Lab 1"], "pat@acme.com": ["Lab 2"]}
    assert all("Any questions?" in g["body"] for g in f.sent["group"])
    assert t["detail"]["status"] == "sent"
    touch = [x for x in f.followups if x["kind"] == "staff_email"]
    assert [x["proposal_id"] for x in touch] == ["d-break"], "the touch is on the home row"


def test_a_group_task_send_is_refused_once_a_scope_is_approved(fake, admin):
    f = fake(_three())
    fw._tick(NOW)
    t, = f.tasks()
    f.rows["d-lab1"].update(proposal_status="approved", approved_at=NOW)
    r = TestClient(main.app).post("/api/admin/followup-task/%s/send" % t["id"], json={
        "title": "Checking in", "body": "Hi\n\n{link}"})
    assert r.status_code == 409 and r.json()["reason"] == "scope_approved"
    assert f.sent["group"] == []


def test_the_task_view_links_an_open_scope_and_lists_the_scopes():
    row = {"id": 5, "proposal_id": "d-break", "created_at": NOW,
           "detail": {"status": "pending", "template": "checkin", "stage": "pre",
                      "scope_group": GID,
                      "scopes": [{"proposal_id": "d-lab1", "label": "Lab 1", "status": "sent"}]}}
    v = main._task_view(row)
    assert v["scope_group"] == GID and v["link_proposal_id"] == "d-lab1"
    assert [s["label"] for s in v["scopes"]] == ["Lab 1"]
    dec = dict(row, detail=dict(row["detail"], stage="decision", template="scope_decision"))
    assert main._task_view(dec)["label"] == "Decide the other scopes"
    assert main._task_summary(dec)["stage"] == "decision"


def test_the_plan_is_written_to_the_home_scope(fake, admin):
    f = fake(_three())
    r = TestClient(main.app).post("/api/admin/proposal/d-lab2/followup-plan",
                                  json={"interval_days": 5})
    assert r.status_code == 200, r.text
    assert r.json()["home_proposal_id"] == "d-break"
    assert f.rows["d-break"].get("followup_interval_days") == 5
    assert "followup_interval_days" not in f.rows["d-lab2"]


def test_the_pipeline_shows_the_groups_schedule_on_every_open_scope(fake, admin, monkeypatch):
    rows = _three()
    rows[0]["followup_interval_days"] = 9
    f = fake(rows)
    monkeypatch.setattr(main.db, "unread_counts", lambda: {})
    monkeypatch.setattr(main.db, "list_all_portal_proposals",
                        lambda: [dict(r) for r in f.rows.values()])
    out = {r["proposal_id"]: r for r in
           TestClient(main.app).get("/api/admin/pipeline").json()["proposals"]}
    nexts = {out[p]["next_followup_at"] for p in ("d-break", "d-lab1", "d-lab2")}
    assert len(nexts) == 1, nexts
    assert {out[p]["followup_plan"]["interval_days"] for p in out} == {9}
    assert {out[p]["followup_home_id"] for p in out} == {"d-break"}


def test_the_pipeline_query_shows_a_groups_task_on_every_member_row():
    sql = " ".join(db._ROW_OPEN_TASK_SQL.split())
    assert "f2.detail->>'scope_group' = (to_jsonb(p) ->> 'scope_group')" in sql
    assert sql.startswith("(case when (to_jsonb(p) ->> 'scope_group') is null then "
                          + " ".join(db._PENDING_TASK_SQL.format(pid="p.proposal_id").split()))


# ══ H. the deploy-day dry run ════════════════════════════════════════════════
def test_the_dry_run_counts_what_the_next_tick_would_raise_and_writes_nothing(fake):
    rows = _three() + [_scope("solo", None, 9, scope_group=None, scope_label=None),
                       _scope("quiet", None, 10, scope_group=None, scope_label=None,
                              followup_enrolled_at=NOW - 2 * H)]
    f = fake(rows)
    before = copy.deepcopy(f.followups)
    got = fw.dry_run_count(NOW)
    assert f.followups == before, "the dry run wrote something"
    assert f.sent["staff"] == [] and f.sent["customer"] == []
    assert got["count"] == 2
    assert sorted((t["proposal_id"], t["scope_group"]) for t in got["tasks"]) == \
        [("d-break", GID), ("solo", None)]
    # It agrees with the real tick.
    fw._tick(NOW)
    assert sorted(t["proposal_id"] for t in f.tasks()) == ["d-break", "solo"]
    assert fw.dry_run_count(NOW)["count"] == 0, "an already-raised occurrence was counted again"


def test_the_dry_run_runs_with_automation_off(fake, monkeypatch):
    fake(_three())
    monkeypatch.setenv("FOLLOWUP_AUTOMATION_ENABLED", "false")
    assert fw.dry_run_count(NOW)["count"] == 1


def test_the_dry_run_reports_the_touched_burst(fake):
    """The critic's case: a proposal with an old reply, quiet for more than one interval, matures
    on a time-keyed occurrence no old automatic email used."""
    viewed = _scope("solo", None, 0, scope_group=None, scope_label=None,
                    proposal_status="viewed", cycle_viewed_at=NOW - 20 * 24 * H)
    f = fake([viewed])
    f.signals["solo"] = {"customer_touch_at": NOW - 10 * 24 * H}
    got = fw.dry_run_count(NOW)
    assert got["count"] == 1 and re.search(r"vrt@\d{8}T\d{2}$", got["tasks"][0]["rule_key"])
