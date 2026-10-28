"""Follow-up TASK endpoints the staff tool's email-writer proxies. Since 2026-10-28 the cadence emails
the customer itself and raises no reminder tasks, so these answer for old tasks still open and for
a scope group's decision task; the plan route keeps only "Next email on".

  GET  /api/admin/followup-tasks                     the bell's feed of open tasks
  GET  /api/admin/followup-task/{id}                 one task + whether it can still be sent
  GET  /api/admin/followup-task/{id}/draft           the compose box's draft + the AI request
  POST /api/admin/followup-task/{id}/draft           hand back what the model wrote
  POST /api/admin/followup-task/{id}/send            send the estimator's reviewed email
  POST /api/admin/followup-task/{id}/skip            not now
  POST /api/admin/followup-task/{id}/called          followed up another way
  POST /api/admin/followup-task/{id}/snooze          remind me on a date
  POST /api/admin/proposal/{pid}/followup-plan       this proposal's own schedule

The properties that matter most: a stale task can NEVER email a customer who has signed, paid,
paused or been closed out; a decision is a touch; a double click sends once; and a failed AI draft
always leaves a usable compose box.
"""
import copy
import datetime as dt

import pytest
from fastapi.testclient import TestClient

import followup_draft
import followup_rules
import main

NOW = dt.datetime.now(dt.timezone.utc)
ENROLLED = NOW - dt.timedelta(days=3)

ROW = {
    "proposal_id": "pid-1", "token": "tok-1", "project_name": "Westport",
    "customer_email": "rita@acme.com", "customer_name": "Rita Jones",
    "proposal_status": "viewed", "cycle_viewed_at": ENROLLED + dt.timedelta(hours=2),
    "followup_enrolled_at": ENROLLED, "followup_disabled_at": None,
    "followup_paused_until": None, "deposit_status": "pending", "deposit_required": True,
    "assigned_estimator": "kyle@wetreadwell.com",
}
GOOD = {"title": "Checking in", "body": "Hi {first_name},\n\nAny questions on {project}?\n\n{link}",
        "by": "kyle@wetreadwell.com"}


def _task(**detail):
    d = {"status": "pending", "template": "checkin", "stage": "pre", "rule_key": "c1:vr@x",
         "cycle": followup_rules.cycle_key(ENROLLED), "include_status_ask": True,
         "draft": {"title": "Checking in", "body": "Hi {first_name},\n\n{link}",
                   "source": "default"}}
    d.update(detail)
    return {"id": 5, "proposal_id": "pid-1", "kind": "followup_task", "detail": d,
            "created_by": None, "created_at": NOW - dt.timedelta(hours=1)}


@pytest.fixture
def api(monkeypatch):
    calls = {"sent": [], "followups": [], "cards": [], "decided": [], "reopened": [],
             "stamped": [], "plan": [], "drafts": []}
    state = {"row": dict(ROW), "task": _task(), "send_ok": True}

    monkeypatch.setattr(main, "_admin_ok", lambda request: True)
    monkeypatch.setattr(main.db, "get_proposal", lambda pid: dict(state["row"]))
    monkeypatch.setattr(main.db, "get_settings", lambda key: None)
    monkeypatch.setattr(main.db, "get_followup_task",
                        lambda tid: copy.deepcopy(state["task"]) if tid == 5 else None)

    def decide(tid, status, by, extra=None, from_status="pending"):
        t = state["task"]
        if t["detail"].get("status") != from_status:
            return None                                   # the WHERE status=<from> guard
        t["detail"].update(dict(extra or {}, status=status, decided_by=by, decided_at="now"))
        calls["decided"].append((tid, status))
        return copy.deepcopy(t)

    def reopen(tid, extra=None):
        if state.get("reopen_fails", 0) > 0:
            state["reopen_fails"] -= 1
            raise RuntimeError("the connection the pool handed out is dead")
        if state["task"]["detail"].get("status") != "sending":
            return False                                  # the WHERE status='sending' guard
        calls["reopened"].append(tid)
        state["task"]["detail"].update(dict(extra or {}, status="pending"))
        return True

    monkeypatch.setattr(main.db, "decide_followup_task", decide)
    monkeypatch.setattr(main.db, "reopen_followup_task", reopen)
    monkeypatch.setattr(main.db, "set_followup_task_draft",
                        lambda tid, draft: calls["drafts"].append(draft))
    monkeypatch.setattr(main.db, "stamp_followup_anchor",
                        lambda pid, clear_next=True: calls["stamped"].append((pid, clear_next)))
    monkeypatch.setattr(main.db, "set_followup_plan",
                        lambda pid, **kw: calls["plan"].append(kw))

    def add_followup(pid, kind, detail=None, created_by=None):
        calls["followups"].append({"kind": kind, "detail": detail or {}, "by": created_by})
        return {"kind": kind, "detail": detail or {}, "created_by": created_by,
                "created_at": NOW}

    monkeypatch.setattr(main.db, "add_followup", add_followup)
    monkeypatch.setattr(main.db, "add_message",
                        lambda pid, kind, who, body, msg_type="text", meta=None:
                        calls["cards"].append({"kind": kind, "body": body, "msg_type": msg_type,
                                               "meta": meta or {}}) or {"id": 1})
    monkeypatch.setattr(main.db, "list_messages", lambda pid, *a, **k: [
        {"author_kind": "customer", "msg_type": "text", "body": "Can you do it in March?",
         "meta": None, "created_at": NOW - dt.timedelta(days=1)}])
    monkeypatch.setattr(main.db, "get_followup_recipients", lambda pid: ["rita@acme.com"])
    monkeypatch.setattr(main.db, "get_recipients", lambda pid: ["rita@acme.com"])
    monkeypatch.setattr(main.email_sender, "proposal_reply_to", lambda tok: "p+tok@x")
    monkeypatch.setattr(main.email_sender, "send_followup",
                        lambda addr, url, proj, tmpl, **k: calls["sent"].append(
                            {"to": addr, "url": url, "template": tmpl, **k}) or state["send_ok"])
    followup_draft.set_run_claude(None)

    tc = TestClient(main.app)
    tc.calls, tc.state = calls, state
    yield tc
    followup_draft.set_run_claude(None)


# ── send ──────────────────────────────────────────────────────────────────────
def test_send_emails_the_edited_text_records_a_touch_and_echoes_it(api):
    r = api.post("/api/admin/followup-task/5/send", json=GOOD)
    assert r.status_code == 200, r.text
    assert r.json()["task"]["status"] == "sent"
    sent, = api.calls["sent"]
    assert sent["to"] == "rita@acme.com" and sent["url"].endswith("/p/tok-1")
    # The estimator's words, through the saved-template rendering path.
    assert sent["templates"]["checkin"]["body"] == GOOD["body"]
    assert sent["include_status_ask"] is True
    # A touch: a staff_email row (re-anchors via last_staff_followup_at) AND the anchor stamp.
    assert [f["kind"] for f in api.calls["followups"]] == ["staff_email"]
    assert api.calls["followups"][0]["detail"]["task_id"] == 5
    assert api.calls["stamped"] == [("pid-1", True)]
    # Echoed to the customer's thread exactly like every reminder, plus a staff card.
    echo = [c for c in api.calls["cards"] if c["meta"].get("followup")]
    assert echo and not echo[0]["meta"].get("internal") and echo[0]["meta"]["task_id"] == 5
    assert "we emailed you" in echo[0]["body"].lower()
    assert any(c["meta"].get("crm") == "followup_sent" for c in api.calls["cards"])


@pytest.mark.parametrize("change,reason", [
    ({"proposal_status": "closed_lost"}, "closed_lost"),
    ({"proposal_status": "approved", "deposit_status": "received",
      "approved_at": NOW - dt.timedelta(days=1)}, "finished"),
    # Approved with the deposit still out: "finished" since 2026-10-28 (no deposit chase). It was
    # "stage_changed" while the cadence chased deposits.
    ({"proposal_status": "approved", "approved_at": NOW - dt.timedelta(days=1)}, "finished"),
    ({"followup_disabled_at": NOW}, "automation_off"),
    ({"followup_paused_until": (NOW + dt.timedelta(days=40)).date()}, "paused"),
    ({"followup_enrolled_at": NOW - dt.timedelta(hours=2)}, "resent"),
])
def test_a_stale_task_never_emails_the_customer(api, change, reason):
    """The rule that matters most. A task raised Tuesday must not email a customer who signed,
    paid, asked for a pause or was closed out on Wednesday."""
    api.state["row"].update(change)
    r = api.post("/api/admin/followup-task/5/send", json=GOOD)
    assert r.status_code == 409 and r.json() == {"ok": False, "error": "out_of_scope",
                                                  "reason": reason}
    assert api.calls["sent"] == [], "a stale task emailed the customer"
    assert api.calls["decided"] == [(5, "expired")]
    assert api.calls["followups"] == [] and api.calls["stamped"] == []


def test_a_double_click_sends_once(api):
    assert api.post("/api/admin/followup-task/5/send", json=GOOD).status_code == 200
    again = api.post("/api/admin/followup-task/5/send", json=GOOD)
    assert again.status_code == 409 and again.json()["error"] == "already_decided"
    assert len(api.calls["sent"]) == 1


def test_a_failed_send_puts_the_task_back(api):
    api.state["send_ok"] = False
    r = api.post("/api/admin/followup-task/5/send", json=GOOD)
    assert r.status_code == 502 and r.json()["error"] == "send_failed"
    assert api.calls["reopened"] == [5]
    assert api.state["task"]["detail"]["status"] == "pending"
    assert api.calls["followups"] == [] and api.calls["stamped"] == []
    assert not [c for c in api.calls["cards"] if c["meta"].get("followup")], (
        "the thread claims an email that never went")


# ── a send that half-fails (audit, 2026-10-23) ───────────────────────────────
# The claim used to flip the task straight to `sent` BEFORE the email. When the email then failed
# and the one reopen ALSO failed (portal-pool-has-no-liveness-check: a DB restart hands out dead
# connections), the task stayed `sent` for ever: every retry was 409 already_decided, and its
# rule_key blocked the worker from raising that occurrence again. Nobody was emailed and nothing
# would ever ask again. The claim is now `sending` — still not pending, so a double click sends
# once — and a `sending` task is never final.
def test_the_claim_is_sending_not_sent(api, monkeypatch):
    """While the email is in flight the task is `sending`; it only becomes `sent` after it went."""
    seen = []
    real = main.followup_worker.send_task_email

    def spy(*a, **k):
        seen.append(api.state["task"]["detail"]["status"])
        return real(*a, **k)

    monkeypatch.setattr(main.followup_worker, "send_task_email", spy)
    r = api.post("/api/admin/followup-task/5/send", json=GOOD)
    assert r.status_code == 200, r.text
    assert seen == ["sending"]
    assert api.state["task"]["detail"]["status"] == "sent"
    assert r.json()["task"]["status"] == "sent"


def test_a_reopened_unconfirmed_send_is_flagged_for_the_compose_box(api):
    api.state["task"] = _task(send_unconfirmed=True)
    task = api.get("/api/admin/followup-task/5").json()["task"]
    assert task["status"] == "pending" and task["send_unconfirmed"] is True
    assert api.get("/api/admin/followup-task/5").status_code == 200
    api.state["task"] = _task()
    assert api.get("/api/admin/followup-task/5").json()["task"]["send_unconfirmed"] is False


def test_a_failed_send_whose_reopen_fails_once_still_puts_the_task_back(api):
    """One dead pooled connection must not strand the task: the reopen is retried."""
    api.state["send_ok"] = False
    api.state["reopen_fails"] = 1
    r = api.post("/api/admin/followup-task/5/send", json=GOOD)
    assert r.status_code == 502 and r.json()["error"] == "send_failed"
    assert api.state["task"]["detail"]["status"] == "pending"


def test_a_failed_send_whose_reopen_keeps_failing_is_never_left_sent(api):
    """The auditor's reproduction. Both reopen attempts fail: the task must NOT read `sent` — it
    stays `sending`, which the worker's recovery puts back (test_followup_remind_worker)."""
    api.state["send_ok"] = False
    api.state["reopen_fails"] = 99
    r = api.post("/api/admin/followup-task/5/send", json=GOOD)
    assert r.status_code == 502 and r.json()["error"] == "send_failed"
    assert api.state["task"]["detail"]["status"] == "sending", (
        "a task whose email never went is recorded as sent")
    assert api.calls["followups"] == [] and api.calls["stamped"] == []
    again = api.post("/api/admin/followup-task/5/send", json=GOOD)
    assert again.status_code == 409 and again.json()["status"] == "sending"
    assert len(api.calls["sent"]) == 1, "the retry emailed again"


def test_a_failed_record_after_a_real_send_still_answers_ok_and_still_re_anchors(api, monkeypatch):
    """The email went. A database blip on the staff_email row must not turn that into a 500 (the
    estimator would believe it failed and send it again), and must not skip the anchor stamp, the
    thread echo or the card that come after it."""
    def boom(*a, **k):
        raise RuntimeError("db blip")

    monkeypatch.setattr(main.db, "add_followup", boom)
    r = api.post("/api/admin/followup-task/5/send", json=GOOD)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["task"]["status"] == "sent"
    assert "touch_not_recorded" in body.get("warnings", [])
    assert len(api.calls["sent"]) == 1
    assert api.calls["stamped"] == [("pid-1", True)], "the schedule did not re-anchor"
    assert [c for c in api.calls["cards"] if c["meta"].get("followup")], "no thread echo"
    assert any(c["meta"].get("crm") == "followup_sent" for c in api.calls["cards"])


def test_a_failed_finalise_after_a_real_send_still_answers_ok(api, monkeypatch):
    """sending -> sent failing is the same story: the email is away, so the answer is 200; the
    staff_email row it writes is the evidence the worker's recovery finalises the task from."""
    real = main.db.decide_followup_task

    def decide(tid, status, by, extra=None, from_status="pending"):
        if status == "sent":
            raise RuntimeError("db blip")
        return real(tid, status, by, extra, from_status=from_status)

    monkeypatch.setattr(main.db, "decide_followup_task", decide)
    r = api.post("/api/admin/followup-task/5/send", json=GOOD)
    assert r.status_code == 200, r.text
    assert r.json()["task"]["status"] == "sent"
    assert "task_not_finalised" in r.json().get("warnings", [])
    f, = api.calls["followups"]
    assert f["kind"] == "staff_email" and f["detail"]["task_id"] == 5
    assert api.calls["stamped"] == [("pid-1", True)]


def test_a_sending_task_still_counts_as_the_open_task():
    """`sending` must hold the one-open-task slot, in the database and in the worker's read.
    Otherwise a tick during the send raises a second task, and the reopen of the first then
    collides with the unique index — the other way the task got stranded."""
    import pathlib
    import re
    import db
    schema = (pathlib.Path(__file__).resolve().parents[1] / "schema.sql").read_text(encoding="utf-8")
    # Widened to one open task per (proposal, stage) for scope groups (2026-10-24): the v2 index.
    m = re.search(r"create unique index if not exists portal_followups_one_open_task_v2_uidx"
                  r"(.*?);", schema, re.S)
    assert m, "the one-open-task index is missing"
    assert "'pending'" in m.group(1) and "'sending'" in m.group(1)
    assert "coalesce(detail->>'stage', 'pre')" in m.group(1)
    assert "drop index if exists public.portal_followups_one_open_task_uidx" in schema
    assert "drop index if exists public.portal_followups_one_pending_task_uidx" in schema
    assert "'sending'" in db._PENDING_TASK_SQL


def test_reopen_only_reopens_a_task_that_is_sending(monkeypatch):
    """The reopen is guarded like the claim: it must never resurrect a task the worker expired,
    or one a second tab already finished."""
    import db
    seen = []
    monkeypatch.setattr(db, "q1", lambda sql, params=(): seen.append((sql, params)) or None)
    assert db.reopen_followup_task(5, {"send_unconfirmed": True}) is False
    sql, params = seen[0]
    assert "detail->>'status' = 'sending'" in sql and 5 in params


@pytest.mark.parametrize("path,body", [
    ("called", {"by": "kyle@wetreadwell.com", "note": "spoke to Rita"}),
    ("skip", {"by": "kyle@wetreadwell.com"}),
])
def test_a_failed_record_after_a_decision_still_answers_ok_and_re_anchors(api, monkeypatch,
                                                                           path, body):
    """Same class on called and skip: the task is already decided (a retry would be 409), so a
    500 here would tell the estimator it failed when it did not, AND skip the anchor stamp."""
    def boom(*a, **k):
        raise RuntimeError("db blip")

    monkeypatch.setattr(main.db, "add_followup", boom)
    r = api.post("/api/admin/followup-task/5/%s" % path, json=body)
    assert r.status_code == 200, r.text
    assert "touch_not_recorded" in r.json().get("warnings", [])
    assert api.calls["stamped"] == [("pid-1", True)], "the schedule did not re-anchor"


@pytest.mark.parametrize("body,error", [
    ({"title": "Hi", "body": "No button in this one."}, "invalid_draft"),
    ({"title": "Hi", "body": "Try {discount}.\n\n{link}"}, "invalid_draft"),
    ({"title": "Hi", "body": "   "}, "empty"),
    ({"title": "Hi"}, "empty"),
])
def test_the_text_is_validated_like_a_customer_template(api, body, error):
    """{link} is required, and no unknown placeholders — the rule every customer template has."""
    r = api.post("/api/admin/followup-task/5/send", json=body)
    assert r.status_code == 400 and r.json()["error"] == error
    assert api.calls["sent"] == [] and api.calls["decided"] == []


def test_html_is_stripped_not_sent(api):
    api.post("/api/admin/followup-task/5/send",
             json={"title": "<b>Hi</b>", "body": "<script>x</script>Hello\n\n{link}"})
    sent, = api.calls["sent"]
    assert "<" not in sent["templates"]["checkin"]["body"]
    assert sent["templates"]["checkin"]["title"] == "Hi"


def test_an_old_deposit_reminder_task_can_no_longer_email_the_customer(api):
    """There is no deposit chase from the cadence (2026-10-28). An old deposit task still open is
    refused and retired, never sent. Replaced test_the_deposit_reminder_never_carries_the_cancel_
    buttons, which sent it."""
    api.state["row"].update(proposal_status="approved", approved_at=NOW - dt.timedelta(days=1))
    api.state["task"] = _task(template="deposit_nudge", stage="deposit", include_status_ask=True)
    r = api.post("/api/admin/followup-task/5/send", json=GOOD)
    assert r.status_code == 409 and r.json()["reason"] == "finished", r.text
    assert api.calls["sent"] == []


# ── skip / called / snooze ────────────────────────────────────────────────────
def test_skip_is_a_touch_but_not_outreach(api):
    r = api.post("/api/admin/followup-task/5/skip", json={"by": "kyle@wetreadwell.com",
                                                          "note": "waiting on their GC"})
    assert r.status_code == 200 and r.json()["task"]["status"] == "skipped"
    assert api.calls["stamped"] == [("pid-1", True)], "a skip did not re-anchor the schedule"
    f, = api.calls["followups"]
    # `action` set: bookkeeping, so the digest does not read a skip as "somebody chased this".
    assert f["kind"] == "staff_note" and f["detail"]["action"] == "followup_skipped"
    assert api.calls["sent"] == []


def test_called_reuses_the_drawers_log_a_followup_path(api):
    r = api.post("/api/admin/followup-task/5/called",
                 json={"by": "kyle@wetreadwell.com", "note": "left a voicemail"})
    assert r.status_code == 200 and r.json()["task"]["status"] == "called"
    f, = api.calls["followups"]
    assert f["kind"] == "staff_call" and f["detail"]["note"] == "left a voicemail"
    assert f["detail"]["task_id"] == 5
    assert any(c["meta"].get("crm") == "followup" for c in api.calls["cards"]), (
        "not the same card the drawer's log-a-call writes")
    assert api.calls["stamped"]


def test_called_refuses_an_unknown_kind(api):
    r = api.post("/api/admin/followup-task/5/called", json={"kind": "carrier pigeon"})
    assert r.status_code == 400 and api.calls["decided"] == []


def test_snooze_sets_the_date_and_does_not_clear_it(api):
    day = (NOW + dt.timedelta(days=9)).astimezone(followup_rules.BUSINESS_TZ).date()
    r = api.post("/api/admin/followup-task/5/snooze", json={"until": day.isoformat()})
    assert r.status_code == 200 and r.json()["task"]["status"] == "snoozed"
    assert api.calls["stamped"] == [("pid-1", False)]
    plan, = api.calls["plan"]
    assert plan["next_at"].date() == day and plan["next_at"].hour == followup_rules.SEND_START_HOUR


@pytest.mark.parametrize("until,error", [("", "invalid_date"), ("nope", "invalid_date"),
                                          ("2020-01-01", "date_in_past"),
                                          ("2099-01-01", "date_too_far")])
def test_snooze_refuses_a_useless_date(api, until, error):
    r = api.post("/api/admin/followup-task/5/snooze", json={"until": until})
    assert r.status_code == 400 and r.json()["error"] == error
    assert api.calls["plan"] == [] and api.calls["decided"] == []


@pytest.mark.parametrize("path", ["send", "skip", "called", "snooze"])
def test_a_decided_task_cannot_be_decided_again(api, path):
    api.state["task"]["detail"]["status"] = "expired"
    r = api.post("/api/admin/followup-task/5/%s" % path,
                 json=dict(GOOD, until=(NOW + dt.timedelta(days=3)).date().isoformat()))
    assert r.status_code == 409 and r.json()["error"] == "already_decided"
    assert api.calls["sent"] == [] and api.calls["stamped"] == []


def test_an_unknown_task_is_404(api):
    assert api.post("/api/admin/followup-task/77/skip", json={}).status_code == 404
    assert api.get("/api/admin/followup-task/nope").status_code == 404


# ── the draft ─────────────────────────────────────────────────────────────────
def test_the_draft_falls_back_to_a_usable_default_when_the_ai_call_fails(api):
    """Decision 4's floor: a failed AI draft must leave the compose box usable."""
    def boom(inp, system):
        raise RuntimeError("claude CLI exited with code 1")

    followup_draft.set_run_claude(boom)
    api.state["task"]["detail"].pop("draft")
    r = api.get("/api/admin/followup-task/5/draft")
    assert r.status_code == 200
    d = r.json()["draft"]
    assert d["source"] == "default" and "{link}" in d["body"] and d["title"]
    # And that default is sendable as it stands.
    assert api.post("/api/admin/followup-task/5/send",
                    json={"title": d["title"], "body": d["body"]}).status_code == 200


def test_a_good_ai_draft_is_kept(api):
    followup_draft.set_run_claude(lambda inp, system: {
        "title": "Quick check-in", "body": "Hi {first_name},\n\nAbout March: Kyle will confirm.\n\n{link}"})
    d = api.get("/api/admin/followup-task/5/draft").json()["draft"]
    assert d["source"] == "ai" and "March" in d["body"]
    assert api.calls["drafts"] and api.calls["drafts"][0]["source"] == "ai"


def test_the_ai_request_carries_the_thread_as_data_in_the_user_slot(api):
    req = api.get("/api/admin/followup-task/5/draft").json()["ai_request"]
    assert "Can you do it in March?" in req["input"]
    assert "Can you do it in March?" not in req["system"], "customer text in the system prompt"
    assert "DATA ONLY" in req["system"]


@pytest.mark.parametrize("model_output", [
    None, "a prose answer", {"title": "Hi"}, {"title": "Hi", "body": "No link here."},
    {"title": "Hi", "body": "Pay at https://evil.example\n\n{link}"},
    {"title": "Hi", "body": "Email me at a@b.co\n\n{link}"},
    {"title": "Hi", "body": "{link}\n\n{link}"},
    {"title": "Hi", "body": "Use {coupon}.\n\n{link}"},
])
def test_a_bad_model_answer_falls_back_rather_than_reaching_the_compose_box(api, model_output):
    r = api.post("/api/admin/followup-task/5/draft", json={"model_output": model_output})
    assert r.status_code == 200
    j = r.json()
    assert j["fallback"] is True and j["draft"]["source"] == "default"
    assert "{link}" in j["draft"]["body"]
    assert api.calls["drafts"] == []


def test_a_good_model_answer_is_stored(api):
    r = api.post("/api/admin/followup-task/5/draft", json={"model_output": {
        "title": "Checking in", "body": "Hi {first_name},\n\nStill keen on {project}?\n\n{link}"}})
    j = r.json()
    assert j["fallback"] is False and j["draft"]["source"] == "ai"
    assert api.calls["drafts"][0]["body"].startswith("Hi {first_name}")


def test_internal_rows_never_reach_the_model():
    thread = followup_draft.thread_for_prompt([
        {"author_kind": "staff", "msg_type": "system", "body": "Closed-lost: too expensive",
         "meta": {"internal": True, "crm": "status"}},
        {"author_kind": "customer", "msg_type": "text", "body": "Hello", "meta": None},
    ])
    assert [m["text"] for m in thread] == ["Hello"]


# ── the bell's feed and the plan ──────────────────────────────────────────────
def test_the_pending_feed_is_shaped_for_the_bell(api, monkeypatch):
    monkeypatch.setattr(main.db, "list_pending_followup_tasks", lambda: [
        dict(_task(), project_name="Westport", customer_name="Rita Jones",
             customer_email="rita@acme.com", estimator_email="kyle@wetreadwell.com"),
        dict(_task(), id=6, project_name="Other", estimator_email="rj@wetreadwell.com")])
    tasks = api.get("/api/admin/followup-tasks").json()["tasks"]
    assert [t["id"] for t in tasks] == [5, 6]
    t = tasks[0]
    for k in ("id", "proposal_id", "status", "template", "label", "stage", "due_at", "draft",
              "project_name", "customer_name", "estimator_email"):
        assert k in t, k
    mine = api.get("/api/admin/followup-tasks?estimator=RJ@wetreadwell.com").json()["tasks"]
    assert [t["id"] for t in mine] == [6]


def test_the_plan_endpoint_only_moves_the_next_email(api):
    """The drawer's "Reminder schedule" keeps only "Next email on" (2026-10-28): "Then every" and
    "Stop after" are gone, so the route writes next_at and nothing else."""
    day = (NOW + dt.timedelta(days=4)).astimezone(followup_rules.BUSINESS_TZ).date().isoformat()
    r = api.post("/api/admin/proposal/pid-1/followup-plan",
                 json={"next_at": day, "interval_days": 5, "stop_after": 3,
                       "by": "kyle@wetreadwell.com"})
    assert r.status_code == 200, r.text
    (kw,) = api.calls["plan"]
    assert list(kw) == ["next_at"], "a retired schedule field was written: %s" % kw
    assert kw["next_at"].astimezone(followup_rules.BUSINESS_TZ).date().isoformat() == day
    assert "interval_days" not in r.json()["followup_plan"]
    assert "stop_after" not in r.json()["followup_plan"]
    # Only the retired fields: nothing to change, nothing written.
    bad = api.post("/api/admin/proposal/pid-1/followup-plan", json={"interval_days": 5})
    assert bad.status_code == 400 and bad.json()["error"] == "nothing_to_change"
    assert api.post("/api/admin/proposal/pid-1/followup-plan", json={}).status_code == 400
    past = api.post("/api/admin/proposal/pid-1/followup-plan", json={"next_at": "2020-01-01"})
    assert past.status_code == 400 and past.json()["error"] == "date_in_past"
    r = api.post("/api/admin/proposal/pid-1/followup-plan", json={"next_at": None})
    assert r.status_code == 200 and api.calls["plan"][-1] == {"next_at": None}
    assert len(api.calls["plan"]) == 2


# ── the drawer's own "log a call" ─────────────────────────────────────────────
@pytest.mark.parametrize("kind,resolves", [("call", True), ("email", True), ("text", True),
                                           ("note", False)])
def test_logging_contact_from_the_drawer_resolves_the_open_task(api, monkeypatch, kind, resolves):
    """A call logged from the drawer IS the follow-up the open task asks for. Left pending, the
    task would be the one open task and block every later reminder on this proposal. A note is not
    contact with the customer and leaves it alone."""
    monkeypatch.setattr(main.db, "pending_followup_task_id", lambda pid: 5)
    r = api.post("/api/admin/proposal/pid-1/followups",
                 json={"kind": kind, "note": "spoke to Rita", "by": "kyle@wetreadwell.com"})
    assert r.status_code == 200, r.text
    if resolves:
        assert api.calls["decided"] == [(5, "called")]
        assert api.state["task"]["detail"]["via"] == "drawer_log"
        assert api.calls["stamped"]
    else:
        assert api.calls["decided"] == [] and api.state["task"]["detail"]["status"] == "pending"
