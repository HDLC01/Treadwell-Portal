"""Four STAFF-side CRM actions now appear in the project's thread, staff-only.

Hanz: "every actiion of customer and staff should appear in the chatbox whether it be email,
messages etc". Almost all of it already did — the send and each revision, questions and replies in
both directions and by email, the approval with who and the total, the invoice, the deposit, every
follow-up email the worker sends, the customer's own delayed / not-going-ahead / ready-again, and
the customer opening it. The gap was everything STAFF did: who the job was handed to, that somebody
RANG the customer, that it had been filed as lost, that the chasing had been switched off. A reader
opening a quiet project could not tell "nobody has touched this" from "Kyle called them on Tuesday
and they asked for a fortnight" — and the logged call is the one this whole file is really for,
because it was the only one of the four that left no mark anywhere a person reads.

WHY EVERY CARD IS INTERNAL. The thread is SHARED: the same rows render in the customer's portal and
in the staff drawer. These four are the CRM's own bookkeeping and some of them are sentences a
customer must never see — "Closed–Lost. Reason: Selected another contractor", "reassigned from Kyle
to RJ". `meta.internal` is what enforces that: db.list_messages excludes it by default and only the
staff drawer opts out. So `internal` is asserted on EVERY card individually rather than once
generically, because the flag is per-row and a card that forgets it is a leak that cannot be taken
back.

AND THE BELL IS A SECOND CUSTOMER SURFACE. db.list_customer_events selects exactly the shape these
cards have (author_kind 'staff', msg_type 'system') and filtered only `followup` and `view`, so
filing a job as lost would have rung the customer's own notification bell to tell them. It now
excludes `internal` too — asserted below, because that predicate is the only thing between a
closed-lost reason and the customer's notification feed.

WHY NO-OPS WRITE NOTHING. Three of the four endpoints can be called with nothing changing: the
estimator picker re-sends the estimator a project already has, the automation switch posts the
state it is already in, and a closed job can be closed again. A thread full of "reassigned from
Kyle to Kyle" is worse than no thread — it is more to read past, and it pushes the lines that mean
something off the screen. The logged follow-up is the exception and has no no-op to detect: every
call records a NEW event, and a second logged call after the first is the normal way to use it.

WHY EVERY WRITE IS GUARDED. By the time a card is written the endpoint has already done the thing
the estimator pressed the button for. Unguarded, a blip on a courtesy row returns a 500 from an
endpoint that already assigned the estimator or already closed the job: the drawer says "couldn't
save", the rep believes it, and they either give up or do it twice. Same posture and the same
reason as the contacts prompts at admin_deposit_received and api_approve, and each guard test below
asserts BOTH halves — the normal success response, and that the state change stands.
"""
import datetime as dt

import pytest
from fastapi.testclient import TestClient

import db
import followup_rules
import main


ROW = {
    "proposal_id": "pid-1", "token": "tok-1", "project_name": "Printing Co Test Hanz",
    "proposal_status": "sent", "assigned_estimator": None,
    "customer_email": "kevin.stucky@printingco.com", "customer_name": "Kevin Stucky",
    "current_revision_no": 1,
    # Enrolled and running, which is what the drawer's automation switch reads as "on".
    "followup_enrolled_at": dt.datetime(2026, 8, 1, 12, 0), "followup_disabled_at": None,
    "followup_paused_until": None, "closed_lost_reason": None, "closed_at": None,
}


@pytest.fixture
def crm(monkeypatch):
    """The four admin endpoints with the database stubbed and every write recorded.

    `row` is mutable so a test can put the project into the state that makes a call a NO-OP —
    already assigned to this estimator, already paused to this date, already closed for this
    reason. That is the whole point of the fixture: "posts nothing when nothing changed" is a
    statement about the row as it stood, and a stub that always returns a fresh project could not
    express it.

    get_proposal hands back a COPY so the endpoint cannot mutate the row a test set up, which would
    make the second half of a two-call test read the first call's writes."""
    calls = {"cards": [], "assigned": [], "automation": [], "followups": [],
             "paused": [], "resumed": [], "closed": [], "reopened": []}
    row = dict(ROW)

    monkeypatch.setattr(main, "_admin_ok", lambda request: True)
    monkeypatch.setattr(main.db, "get_proposal", lambda pid: dict(row))
    monkeypatch.setattr(main.db, "set_assigned_estimator",
                        lambda pid, email: calls["assigned"].append((pid, email)))
    monkeypatch.setattr(main.db, "set_followup_enabled",
                        lambda pid, on: calls["automation"].append((pid, on)))
    monkeypatch.setattr(main.db, "pause_followups",
                        lambda pid, until: calls["paused"].append((pid, until)))
    monkeypatch.setattr(main.db, "resume_followups",
                        lambda pid: calls["resumed"].append(pid))
    monkeypatch.setattr(main.db, "close_lost",
                        lambda pid, reason: calls["closed"].append((pid, reason)) or True)
    monkeypatch.setattr(main.db, "reopen_if_closed",
                        lambda pid: calls["reopened"].append(pid) or True)

    def add_followup(pid, kind, detail=None, created_by=None):
        calls["followups"].append({"pid": pid, "kind": kind, "detail": detail or {},
                                   "by": created_by})
        return {"kind": kind, "detail": detail or {}, "created_by": created_by,
                "created_at": dt.datetime(2026, 8, 19, 9, 0)}

    monkeypatch.setattr(main.db, "add_followup", add_followup)

    def add_message(pid, kind, who, body, msg_type="text", meta=None):
        calls["cards"].append({"pid": pid, "kind": kind, "author": who, "body": body,
                               "msg_type": msg_type, "meta": meta})
        return {"id": len(calls["cards"])}

    monkeypatch.setattr(main.db, "add_message", add_message)

    tc = TestClient(main.app)
    tc.calls = calls
    tc.row = row
    tc.monkeypatch = monkeypatch
    return tc


def _assign(client, estimator="kyle@wetreadwell.com", by="hanz@wetreadwell.com"):
    payload = {"estimator_email": estimator}
    if by is not None:
        payload["by"] = by
    return client.post("/api/admin/proposal/pid-1/assign", json=payload)


def _automation(client, enabled, by="hanz@wetreadwell.com"):
    payload = {"enabled": enabled}
    if by is not None:
        payload["by"] = by
    return client.post("/api/admin/proposal/pid-1/followup-automation", json=payload)


def _log(client, kind="call", note=None, by="hanz@wetreadwell.com"):
    payload = {"kind": kind}
    if note is not None:
        payload["note"] = note
    if by is not None:
        payload["by"] = by
    return client.post("/api/admin/proposal/pid-1/followups", json=payload)


def _status(client, by="hanz@wetreadwell.com", **fields):
    payload = dict(fields)
    if by is not None:
        payload["by"] = by
    return client.post("/api/admin/proposal/pid-1/status", json=payload)


def _one(client):
    assert len(client.calls["cards"]) == 1, (
        "expected exactly one thread card, got %r" % (client.calls["cards"],))
    return client.calls["cards"][0]


def _until(months):
    """The pause date the endpoint will compute, worked out the same way it does.

    Hardcoding a date would pin the test to the day it was written, and re-deriving it here is
    what makes the assertion about the SENTENCE rather than about the calendar."""
    return followup_rules.add_months(followup_rules.business_today(main._now_utc()), months)


# ── 1. the estimator was assigned, or changed ─────────────────────────────────
def test_assigning_an_estimator_is_written_to_the_thread(crm):
    r = _assign(crm)
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "assigned_estimator": "kyle@wetreadwell.com"}
    c = _one(crm)
    assert c["body"] == "Hanz assigned Kyle as the estimator."
    assert c["msg_type"] == "system", "a new msg_type would need a migration and renders nowhere"
    assert c["kind"] == "staff" and c["author"] is None


def test_the_assign_card_is_staff_only(crm):
    """WITHOUT meta.internal the customer reads "Hanz assigned Kyle as the estimator" in their own
    conversation — an internal staffing decision, in front of the client, in the thread they use to
    ask questions. db.list_messages excludes internal rows by default; the flag is per row, so it
    is asserted per card."""
    _assign(crm)
    assert _one(crm)["meta"] == {"crm": "assign", "internal": True}


def test_a_handover_names_both_estimators(crm):
    """The point of the card. "Kyle is on it" is worth little to somebody who needs to know it
    LEFT Troy — that is the fact that explains why the customer heard nothing for a week."""
    crm.row["assigned_estimator"] = "troy@wetreadwell.com"
    _assign(crm)
    assert _one(crm)["body"] == "Hanz reassigned this from Troy to Kyle."


def test_reassigning_to_the_same_estimator_writes_no_card(crm):
    """THE NO-OP. The drawer posts its form, so re-saving a project — or picking the estimator it
    already has — arrives here as a real request. "Reassigned from Kyle to Kyle" is noise that
    pushes the lines that mean something off the screen.

    The state write is deliberately still asserted: skipping the CARD must not turn into skipping
    the endpoint's work."""
    crm.row["assigned_estimator"] = "kyle@wetreadwell.com"
    r = _assign(crm)
    assert r.status_code == 200
    assert crm.calls["cards"] == []
    assert crm.calls["assigned"] == [("pid-1", "kyle@wetreadwell.com")]


def test_the_same_estimator_in_different_case_is_still_the_same_person(crm):
    """Recipient lists and pickers are typed by hand, and Kyle@ is not a second Kyle."""
    crm.row["assigned_estimator"] = "Kyle@WeTreadwell.com"
    _assign(crm)
    assert crm.calls["cards"] == []


@pytest.mark.parametrize("by", [None, "", "   "])
def test_an_unnamed_assigner_still_gets_a_sentence(crm, by):
    """`by` is stamped by the staff tool's proxy from the signed-in user, so it can arrive empty
    from a script or an older client. "None assigned Kyle" is the version of this bug that ships."""
    _assign(crm, by=by)
    assert _one(crm)["body"] == "Kyle assigned as the estimator."


def test_a_typed_name_is_not_mangled_into_an_address(crm):
    """`by` is an address today, which reads badly mid-sentence and is shortened to a first name.
    A value that is already a NAME must pass through: the address helper capitalises each word,
    which turned "Kyle Smith" into "Kyle smith"."""
    _assign(crm, by="Kyle Smith")
    assert _one(crm)["body"] == "Kyle Smith assigned Kyle as the estimator."


def test_a_failed_assign_card_still_assigns_the_estimator(crm):
    """The estimator is already assigned by the time the card is written. A 500 here would tell the
    rep their click failed when it had not — and they would either give up or do it twice."""
    def boom(*a, **k):
        raise RuntimeError("relation \"portal_questions\" does not exist")
    crm.monkeypatch.setattr(main.db, "add_message", boom)
    r = _assign(crm)
    assert r.status_code == 200
    assert r.json() == {"ok": True, "assigned_estimator": "kyle@wetreadwell.com"}
    assert crm.calls["assigned"] == [("pid-1", "kyle@wetreadwell.com")], (
        "the assignment was undone by a failed thread write")


# ── 2. somebody actually chased the customer ──────────────────────────────────
def test_a_logged_call_reaches_the_thread_with_its_note(crm):
    """THE MOST VALUABLE OF THE FOUR. portal_followups already recorded this — the digest reads it
    and stops recommending a chase — but nothing rendered it, so the estimator who rang on Tuesday
    and the estimator who forgot looked identical in the thread, and the next person rang the
    customer again.

    The note is the content: "waiting on the GC's schedule" is the entire value of the call."""
    r = _log(crm, "call", "Left a voicemail, trying again Thursday.")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True and r.json()["followup"]["kind"] == "staff_call"
    c = _one(crm)
    assert c["body"] == 'Hanz logged a call — "Left a voicemail, trying again Thursday."'
    assert c["msg_type"] == "system" and c["kind"] == "staff" and c["author"] is None


def test_the_followup_card_is_staff_only(crm):
    """A logged call is an internal record of OUR work. Shown to the customer it reads as being
    told what we did about them, in a sentence written for a colleague."""
    _log(crm, "call", "Chased on price.")
    assert _one(crm)["meta"] == {"crm": "followup", "internal": True}


@pytest.mark.parametrize("kind,expected", [
    ("call", "Hanz logged a call."),
    ("email", "Hanz logged an email."),
    ("text", "Hanz logged a text."),
    ("note", "Hanz logged a note."),
    # The long form the API also accepts, so the card cannot depend on which client sent it.
    ("staff_call", "Hanz logged a call."),
])
def test_each_kind_reads_as_english(crm, kind, expected):
    """"logged a email" is the version of this that ships to a colleague."""
    _log(crm, kind)
    assert _one(crm)["body"] == expected


@pytest.mark.parametrize("by", [None, "", "   "])
def test_an_unnamed_chase_still_records_that_it_happened(crm, by):
    """Who rang matters less than that somebody did — the card must survive a missing actor."""
    _log(crm, "call", "Spoke to the PM.", by=by)
    assert _one(crm)["body"] == 'A call was logged — "Spoke to the PM."'


def test_a_second_logged_call_is_not_a_no_op(crm):
    """Unlike the other three, this endpoint has nothing to compare against and should not try:
    every call records a NEW event, and chasing twice is the normal way to use it. A "don't repeat
    yourself" guard here would hide the second call, which is the one that shows persistence."""
    _log(crm, "call", "No answer.")
    _log(crm, "call", "No answer again.")
    assert [c["body"] for c in crm.calls["cards"]] == [
        'Hanz logged a call — "No answer."',
        'Hanz logged a call — "No answer again."']


def test_an_invalid_kind_records_nothing(crm):
    r = _log(crm, "carrier_pigeon")
    assert r.status_code == 400
    assert crm.calls["cards"] == [] and crm.calls["followups"] == []


def test_a_failed_followup_card_still_logs_the_followup(crm):
    """The digest's suppression hangs off the portal_followups row, so losing the endpoint over a
    thread write would mean the customer gets chased again tomorrow by the morning email."""
    def boom(*a, **k):
        raise RuntimeError("meta column missing")
    crm.monkeypatch.setattr(main.db, "add_message", boom)
    r = _log(crm, "call", "Talked to the GC.")
    assert r.status_code == 200
    assert r.json()["followup"]["kind"] == "staff_call"
    assert len(crm.calls["followups"]) == 1, "the logged call went down with the card"


# ── 3. automation off, or back on ─────────────────────────────────────────────
def test_switching_automation_off_is_written_to_the_thread(crm):
    """Silence after this point is a DECISION, not a lapse. Without the card the next reader sees a
    project nobody has chased for three weeks and no reason why."""
    r = _automation(crm, False)
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True and "followup_state" in r.json()
    c = _one(crm)
    assert c["body"] == "Hanz turned follow-up automation off."
    assert c["msg_type"] == "system" and c["kind"] == "staff" and c["author"] is None


def test_the_automation_card_is_staff_only(crm):
    """The customer must not read that we stopped chasing them — it is an invitation to stop
    answering, and it is a decision about our own workflow, not about their project."""
    _automation(crm, False)
    assert _one(crm)["meta"] == {"crm": "automation", "internal": True}


def test_switching_automation_back_on_is_written_too(crm):
    crm.row["followup_disabled_at"] = dt.datetime(2026, 8, 10, 9, 0)
    _automation(crm, True)
    assert _one(crm)["body"] == "Hanz turned follow-up automation back on."


@pytest.mark.parametrize("by", [None, "", "   "])
def test_an_unnamed_toggle_still_records_the_change(crm, by):
    _automation(crm, False, by=by)
    assert _one(crm)["body"] == "Follow-up automation was turned off."


def test_turning_automation_on_when_it_is_already_on_writes_no_card(crm):
    """THE NO-OP. A switch invites a second click, a second tab, a drawer re-save. Two identical
    "turned automation back on" lines an hour apart describe a change that never happened."""
    r = _automation(crm, True)
    assert r.status_code == 200
    assert crm.calls["cards"] == []
    assert crm.calls["automation"] == [("pid-1", True)], "the toggle itself stopped happening"


def test_turning_automation_off_when_it_is_already_off_writes_no_card(crm):
    crm.row["followup_disabled_at"] = dt.datetime(2026, 8, 10, 9, 0)
    _automation(crm, False)
    assert crm.calls["cards"] == []


def test_switching_off_a_project_nothing_was_chasing_writes_no_card(crm):
    """A legacy proposal published before automation existed has no followup_enrolled_at, so the
    drawer's switch already reads off. Turning it "off" does stamp a followup_disabled_at, but
    nothing was chasing the project, so a line announcing that the chasing stopped would be a
    change the reader cannot see and did not get."""
    crm.row["followup_enrolled_at"] = None
    _automation(crm, False)
    assert crm.calls["cards"] == []


def test_a_failed_automation_card_still_flips_the_switch(crm):
    def boom(*a, **k):
        raise RuntimeError("pool timeout")
    crm.monkeypatch.setattr(main.db, "add_message", boom)
    r = _automation(crm, False)
    assert r.status_code == 200 and r.json()["ok"] is True
    assert crm.calls["automation"] == [("pid-1", False)], (
        "the automation toggle was undone by a failed thread write")


# ── 3b. turning automation on also LIFTS A PAUSE ──────────────────────────────
# Added 2026-08-21. This route called set_followup_enabled and nothing else, and on a paused project
# that made the switch a lie: followup_disabled_at and followup_paused_until BOTH suppress a send, so
# clearing one of two stops is not the "Start the follow-up cadence from today" the drawer's own
# button title promises. It also mattered more than it looks, because this is the control an
# estimator reaches for when they cannot find a way out of a hold — which is the state every held
# SENT bid was in until the drawer grew a bring-back on the same day. The workaround failed as
# silently as the missing button.
def test_turning_automation_on_lifts_the_pause_as_well_as_the_flag(crm):
    """The flag and the date are two separate stops. Asserted as resume_followups being CALLED
    rather than as a response field, because that call is the whole of the fix: the endpoint could
    return a perfectly correct-looking followup_state (it re-reads the row) while nothing had
    unpaused."""
    crm.row["followup_disabled_at"] = dt.datetime(2026, 8, 10, 9, 0)
    crm.row["followup_paused_until"] = "2026-12-21"
    r = _automation(crm, True)
    assert r.status_code == 200, r.text
    assert crm.calls["automation"] == [("pid-1", True)]
    assert crm.calls["resumed"] == ["pid-1"], (
        "automation was switched on and the pause was left in place, so the button says the "
        "cadence is running and nothing is sent")


def test_the_lifted_pause_gets_its_own_line_in_the_thread(crm):
    """Two changes, two sentences. The switch moving is a workflow decision; the pause coming off
    changes what the CUSTOMER receives, and "why did they get a reminder in September?" is answered
    by this line and by nothing else."""
    crm.row["followup_disabled_at"] = dt.datetime(2026, 8, 10, 9, 0)
    crm.row["followup_paused_until"] = "2026-12-21"
    _automation(crm, True)
    bodies = [c["body"] for c in crm.calls["cards"]]
    assert len(bodies) == 2, bodies
    assert bodies[0] == "Hanz turned follow-up automation back on."
    assert bodies[1] == "Hanz lifted the follow-up pause, so the reminders start again."
    assert crm.calls["cards"][1]["meta"] == {"crm": "automation_resumed", "internal": True}, (
        "the pause card is not staff-only, so the customer reads our own bookkeeping")


def test_the_pause_lift_is_recorded_even_when_the_switch_did_not_move(crm):
    """Outside the switch-moved guard on purpose. A project that is enrolled, enabled AND paused —
    which is exactly what a hold leaves behind — reports "on" already, so the no-op rule would
    swallow the one part of this request that changed anything."""
    crm.row["followup_paused_until"] = "2026-12-21"
    _automation(crm, True)
    assert crm.calls["resumed"] == ["pid-1"]
    c = _one(crm)
    assert c["body"] == "Hanz lifted the follow-up pause, so the reminders start again."


def test_turning_automation_off_leaves_the_pause_exactly_where_it_is(crm):
    """Only on the way ON. A paused project that somebody also takes off automation carries two
    separate decisions, and resuming while switching off would be this route inventing a third."""
    crm.row["followup_paused_until"] = "2026-12-21"
    _automation(crm, False)
    assert crm.calls["resumed"] == [], "switching automation off unpaused the project"


def test_turning_it_on_when_nothing_is_paused_resumes_nothing(crm):
    """A pointless write is not harmless here: resume_followups stamps updated_at, and the board
    dates and sorts on the row."""
    crm.row["followup_disabled_at"] = dt.datetime(2026, 8, 10, 9, 0)
    _automation(crm, True)
    assert crm.calls["resumed"] == []
    assert _one(crm)["body"] == "Hanz turned follow-up automation back on."


# ── 4. staff moved the project's status ───────────────────────────────────────
def test_staff_marking_it_delayed_is_written_with_the_window_and_the_date(crm):
    """The CUSTOMER saying this already posted a card; staff saying it posted nothing, so a project
    paused by the estimator after a phone call went quiet for two months with no explanation in the
    thread. Both the window and the resume date are on the card because they answer different
    questions: how long the customer asked for, and when the chasing comes back."""
    r = _status(crm, status="delayed", months=2)
    assert r.status_code == 200, r.text
    c = _one(crm)
    assert c["body"] == ("Hanz marked this delayed by about 2 months — follow-ups paused until %s."
                         % _until(2).isoformat())
    assert c["msg_type"] == "system" and c["kind"] == "staff" and c["author"] is None


def test_the_delayed_card_is_staff_only(crm):
    _status(crm, status="delayed", months=2)
    assert _one(crm)["meta"] == {"crm": "status_delayed", "internal": True}


def test_the_top_of_the_range_reads_open_ended(crm):
    """The picker's 4 means "four or more", so naming "4 months" would promise a date nobody
    chose. Shared with the customer's own delay card so one pause cannot be described two ways in
    the same thread."""
    _status(crm, status="delayed", months=4)
    assert "delayed by about 4+ months" in _one(crm)["body"]


def test_pausing_to_the_date_it_is_already_paused_to_writes_no_card(crm):
    """THE NO-OP, and the same one the customer path guards — a second click from an older email,
    or a rep confirming what a colleague already set."""
    crm.row["followup_paused_until"] = _until(2).isoformat()
    r = _status(crm, status="delayed", months=2)
    assert r.status_code == 200
    assert crm.calls["cards"] == []
    assert crm.calls["paused"], "the pause itself stopped happening"


#: The comment staff have had to write since 2026-08-20. One sentence, which is all it asks for.
NOTE = "12% over Wilson on the pour."


def test_staff_closing_it_lost_is_written_with_the_reason(crm):
    """The reason is the card. "Closed" tells the next reader nothing they can act on; "selected
    another contractor" is what they need before they ring the customer about the next job.

    AND THE COMMENT, since 2026-08-20 (Hanz). A reason alone is eight identical cards by the end of
    a quarter; the sentence is the part the sales meeting is actually held to read. It has no column
    and does not need one — this card and portal_followups.detail are both jsonb, which is why the
    whole feature needed no DDL."""
    _status(crm, status="closed_lost", reason="another_contractor", note=NOTE)
    c = _one(crm)
    assert c["body"] == ('Hanz marked this Closed–Lost. Reason: Selected another contractor. "%s"'
                         % NOTE)
    assert c["msg_type"] == "system" and c["kind"] == "staff" and c["author"] is None


def test_the_closed_lost_card_is_staff_only(crm):
    """THE SHARPEST CASE IN THIS FILE, and sharper since the comment went on it. Unfiltered, the
    customer reads "Closed–Lost. Reason: Selected another contractor" AND whatever the estimator
    wrote about why we lost, in their own conversation, in a thread they can reply to."""
    _status(crm, status="closed_lost", reason="another_contractor", note=NOTE)
    assert _one(crm)["meta"] == {"crm": "status_closed_lost", "internal": True}


def test_closing_it_without_a_reason_or_a_comment_is_refused(crm):
    """WHO AND WHEN: Hanz, 2026-08-20. This test was
    `test_closing_it_without_a_reason_still_records_the_close` and pinned the opposite: a bare close
    wrote "Hanz marked this Closed–Lost." and saved. That was the looser of two ends of the same
    act — the proposal tool's own draft route 422s a missing reason and a missing comment for a bid
    nobody has sent — so the same decision, made from two drawers, filed one bid under a reason and
    the other under "Not recorded".

    The CUSTOMER's own form (api_project_status) stays lenient and must: it asks "if you don't mind
    saying", and a customer who does mind still has to be able to tell us they are out."""
    for fields in ({}, {"reason": "another_contractor"}, {"note": NOTE},
                   {"reason": "vibes", "note": NOTE},
                   {"reason": "another_contractor", "note": "   "},
                   {"reason": "another_contractor", "note": "\n\t"}):
        crm.calls["cards"].clear()
        crm.calls["closed"].clear()
        r = _status(crm, status="closed_lost", **fields)
        assert r.status_code == 400, "%r got through" % fields
        assert crm.calls["closed"] == [], "%r closed the job anyway" % fields
        assert crm.calls["cards"] == []


def test_closing_a_job_that_is_already_closed_for_that_reason_still_records_the_comment(crm):
    """WHO AND WHEN: Hanz, 2026-08-20. This was the NO-OP — "the board's Lost tab can be dragged
    onto the same column twice" — and it stops being one now that a comment is required, because the
    comment is always news. Two people can close the same bid under "Not Low Bid" and only one of
    them wrote down that we were 12% over Wilson; dropping the second card because the reason matched
    would drop the only part worth reading.

    So the card CHANGES rather than disappearing: it says a note was added on the close, not that the
    job was closed again. The reason-changed rule below is untouched, and still covers the bare
    re-click it was written for."""
    crm.row["proposal_status"] = "closed_lost"
    crm.row["closed_lost_reason"] = "another_contractor"
    _status(crm, status="closed_lost", reason="another_contractor", note=NOTE)
    c = _one(crm)
    assert c["body"] == 'Hanz added a note on the close: "%s"' % NOTE
    assert c["meta"] == {"crm": "status_closed_lost", "internal": True}
    assert "Closed–Lost" not in c["body"], (
        "it narrates the close a second time, so the thread reads as though the job died twice")
    assert crm.calls["closed"], "the close itself stopped happening"


def test_correcting_the_close_reason_IS_news(crm):
    """"We lost on price" and "they went with somebody else" are different stories about the same
    job, and the correction is exactly what a reader needs. Treating any re-close as a no-op would
    have swallowed it."""
    crm.row["proposal_status"] = "closed_lost"
    crm.row["closed_lost_reason"] = "price"
    _status(crm, status="closed_lost", reason="another_contractor", note=NOTE)
    assert _one(crm)["body"] == (
        'Hanz marked this Closed–Lost. Reason: Selected another contractor. "%s"' % NOTE)


# ── the two answers that pause a bid instead of killing it ────────────────────
def test_a_hold_pauses_the_chase_and_says_why_in_the_thread(crm):
    """Hanz, 2026-08-20: "Project on Hold" and "Small Bid <$25k - Pending" leave the card on the
    Active board with the reminders paused. Routed through THIS status rather than a mechanism of
    their own, so one bid cannot carry two pause dates able to disagree — and the reason and comment
    ride along, or the pause reads as an unexplained gap in the chasing."""
    r = _status(crm, status="delayed", months=4, reason="on_hold", note="GC has gone quiet.")
    assert r.status_code == 200, r.text
    assert crm.calls["paused"], "the pause itself did not happen"
    bodies = [c["body"] for c in crm.calls["cards"]]
    assert len(bodies) == 2, bodies
    assert "delayed by about 4+ months" in bodies[0]
    assert bodies[1] == 'On hold: Project on Hold — "GC has gone quiet."'
    assert crm.calls["cards"][1]["meta"] == {"crm": "status_on_hold", "internal": True}


def test_a_hold_gets_its_card_even_when_the_pause_date_is_unchanged(crm):
    """The no-op rule above was about a pause carrying nothing but a date, where a second press says
    nothing new. A hold carries somebody's sentence about why, and that sentence is the whole content
    of the act — swallowing it because a colleague paused to the same day last week would lose the
    only part anyone reads."""
    crm.row["followup_paused_until"] = _until(4).isoformat()
    _status(crm, status="delayed", months=4, reason="on_hold", note="Still quiet.")
    bodies = [c["body"] for c in crm.calls["cards"]]
    assert any('Still quiet.' in b for b in bodies), bodies


def test_a_hold_is_refused_without_a_comment_or_with_a_lost_reason(crm):
    """The same strictness as the close, and for the same reason: a hold with no sentence on it is a
    bid that has gone quiet for reasons nobody wrote down. A close-lost reason arriving here would
    pause a bid somebody meant to kill."""
    for fields in ({"reason": "on_hold"}, {"reason": "on_hold", "note": "  "},
                   {"reason": "another_contractor", "note": NOTE},
                   {"reason": "vibes", "note": NOTE}):
        crm.calls["cards"].clear()
        crm.calls["paused"].clear()
        r = _status(crm, status="delayed", months=4, **fields)
        assert r.status_code == 400, "%r got through" % fields
        assert crm.calls["paused"] == [], "%r paused the chase anyway" % fields
        assert crm.calls["cards"] == []


def test_reopening_a_lost_job_is_written_to_the_thread(crm):
    crm.row["proposal_status"] = "closed_lost"
    _status(crm, status="active")
    c = _one(crm)
    assert c["body"] == "Hanz moved this back to Active."
    assert c["meta"] == {"crm": "status_active", "internal": True}


def test_unpausing_a_delayed_job_is_written_too(crm):
    """Not only a reopen: taking the pause off is the moment the chasing restarts, and the customer
    is about to start hearing from us again."""
    crm.row["followup_paused_until"] = "2026-10-19"
    _status(crm, status="active")
    assert _one(crm)["body"] == "Hanz moved this back to Active."


def test_bringing_back_a_HELD_bid_clears_the_pause_and_closes_nothing(crm):
    """THE WAY OUT OF A HOLD, on a project the customer has. The drawer's bring-back posts
    `bring_back` to the proposal tool, which clears its own marks and forwards `active` here — and
    `active` is where resume_followups lives, so this call is the whole of "the hold is off".

    Asserted as the CALL, not as a status: a hold never set proposal_status (that is the point of
    routing it through `delayed`), so a held bid is still 'sent' and nothing about the response body
    would change whether or not the pause came off. reopen_if_closed must NOT run either — there is
    nothing closed to reopen, and calling it would rewrite the status of a live bid."""
    crm.row["followup_paused_until"] = "2026-12-21"
    r = _status(crm, status="active")
    assert r.status_code == 200, r.text
    assert crm.calls["resumed"] == ["pid-1"], "the hold survived the bring-back"
    assert crm.calls["reopened"] == [], (
        "a bid nobody closed was put through reopen_if_closed, which rewrites its status")
    assert _one(crm)["body"] == "Hanz moved this back to Active."


def test_setting_an_already_active_project_active_writes_no_card(crm):
    """THE NO-OP. Active and unpaused is the button's resting state, so the board re-saving a live
    project must not narrate it."""
    r = _status(crm, status="active")
    assert r.status_code == 200
    assert crm.calls["cards"] == []
    assert crm.calls["resumed"] == ["pid-1"], "the resume itself stopped happening"


def test_an_invalid_status_records_nothing(crm):
    r = _status(crm, status="on_fire")
    assert r.status_code == 400
    assert crm.calls["cards"] == []


def test_a_failed_status_card_still_closes_the_job(crm):
    """close_lost has already run. A 500 here says "couldn't save" about a job that IS closed, and
    the rep's next move is to try again or to leave it looking live on the board."""
    def boom(*a, **k):
        raise RuntimeError("relation missing")
    crm.monkeypatch.setattr(main.db, "add_message", boom)
    r = _status(crm, status="closed_lost", reason="price", note=NOTE)
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True and "proposal_status" in j and "followup_state" in j
    assert crm.calls["closed"] == [("pid-1", "price")], (
        "the close was undone by a failed thread write")


# ── every card, without exception ─────────────────────────────────────────────
def test_no_crm_card_can_be_written_without_internal(crm):
    """The per-card assertions above are the real guard; this is the net under the NEXT card
    somebody adds here. One row that forgets the flag is one internal note in front of a customer,
    and unlike a missing card that is not recoverable."""
    crm.row["assigned_estimator"] = "troy@wetreadwell.com"
    _assign(crm)
    _log(crm, "call", "Rang them.")
    _automation(crm, False)
    _status(crm, status="delayed", months=1)
    crm.row["proposal_status"] = "closed_lost"
    _status(crm, status="active")
    assert len(crm.calls["cards"]) == 5, "an endpoint stopped writing its card"
    for c in crm.calls["cards"]:
        assert c["meta"] and c["meta"].get("internal") is True, (
            "a CRM card is visible to the customer: %r" % (c,))
        assert c["meta"].get("crm"), "the card is not identifiable as a CRM row: %r" % (c,)
        assert c["msg_type"] == "system" and c["kind"] == "staff"


# ── the two customer surfaces the cards must stay off ─────────────────────────
def test_the_thread_hides_the_cards_from_the_customer_by_default(monkeypatch):
    """The thread is SHARED — the same rows render in the customer's portal and in the staff
    drawer. Asserted on the query because that is where the exclusion lives: a row that reaches
    this SELECT reaches the customer."""
    seen = {}
    monkeypatch.setattr(db, "qall", lambda sql, params=(): seen.update(sql=sql) or [])
    db.list_messages("pid-1")
    sql = " ".join(seen["sql"].split())
    assert "coalesce((meta->>'internal')::boolean, false) = false" in sql, (
        "list_messages does not exclude internal rows by default — the customer reads the CRM's "
        "own bookkeeping, including who we lost the job to")


def test_the_cards_are_there_for_STAFF(monkeypatch):
    """The other half. Without it the exclusion above is indistinguishable from never writing the
    cards at all, and the thread is the whole point — it is where staff read a project's history."""
    seen = {}
    monkeypatch.setattr(db, "qall", lambda sql, params=(): seen.update(sql=sql) or [])
    db.list_messages("pid-1", after_id=0, include_internal=True)
    sql = " ".join(seen["sql"].split())
    assert "meta->>'internal'" not in sql, (
        "the staff drawer filters internal rows, so the CRM cards have nowhere to land")


def test_the_cards_are_kept_out_of_the_customers_notification_bell(monkeypatch):
    """A SECOND CUSTOMER SURFACE, and the one that is easy to miss. list_customer_events selects
    exactly the shape these cards have — author_kind 'staff', msg_type 'system' — and filtered only
    `followup` and `view`. Without the internal predicate, filing a job as lost rang the customer's
    own bell to tell them the reason we lost it."""
    seen = {}
    monkeypatch.setattr(db, "qall", lambda sql, params=(): seen.update(sql=sql) or [])
    db.list_customer_events("dana@acme.com")
    sql = " ".join(seen["sql"].split())
    assert "coalesce((q.meta->>'internal')::boolean, false) = false" in sql, (
        "the customer's notification bell does not exclude internal rows — closing a job lost "
        "notifies the customer of the reason")
    # The two narrower rules it already had. `view` is not the same claim as `internal` (a view
    # card belongs in the customer's thread but not in their bell), so neither replaces the other.
    assert "coalesce((q.meta->>'view')::boolean, false) = false" in sql
    assert "coalesce((q.meta->>'followup')::boolean, false) = false" in sql


# ── the customer's own status card is unchanged ───────────────────────────────
@pytest.fixture
def portal(monkeypatch):
    """The CUSTOMER-initiated status path, which already posted its own cards and must keep posting
    exactly the same ones. The staff card shares `_delay_window` with it, and a refactor that
    reworded the customer's card would be a change to what a customer reads, delivered as a
    side-effect of a staff feature."""
    calls = {"cards": []}
    row = dict(ROW)
    monkeypatch.setattr(main, "_require", lambda request, token: dict(row))
    monkeypatch.setattr(main, "_session_email", lambda request: "kevin.stucky@printingco.com")
    monkeypatch.setattr(main.ratelimit, "allow_ip", lambda ip, n, w: True)
    monkeypatch.setattr(main.db, "pause_followups", lambda pid, until: None)
    monkeypatch.setattr(main.db, "resume_followups", lambda pid: None)
    monkeypatch.setattr(main.db, "close_lost", lambda pid, reason: True)
    monkeypatch.setattr(main.db, "add_followup",
                        lambda pid, kind, detail=None, created_by=None: {"kind": kind})
    monkeypatch.setattr(main.db, "add_message",
                        lambda pid, kind, who, body, msg_type="text", meta=None:
                        calls["cards"].append({"kind": kind, "author": who, "body": body,
                                               "msg_type": msg_type, "meta": meta})
                        or {"id": 1})
    monkeypatch.setattr(main, "_notify_staff_status", lambda p, subject, body: None)
    tc = TestClient(main.app)
    tc.calls = calls
    tc.row = row
    return tc


@pytest.mark.parametrize("months,window", [(1, "1 month"), (2, "2 months"), (4, "4+ months")])
def test_the_customers_own_delay_card_is_unchanged(portal, months, window):
    """Pins the wording on BOTH sides of the shared helper. The customer's card is customer-facing
    copy and carries no `internal` — it is theirs, they wrote it."""
    r = portal.post("/api/portal/tok-1/project-status",
                    json={"status": "delayed", "months": months})
    assert r.status_code == 200, r.text
    c = portal.calls["cards"][0]
    assert c["body"] == "Project delayed — revisiting in about %s." % window
    assert c["msg_type"] == "status_update" and c["kind"] == "customer"
    assert c["meta"]["status"] == "delayed" and c["meta"]["months"] == months
    assert "internal" not in c["meta"], (
        "the customer's own status card was hidden from them by the staff-card work")


def test_the_customer_saying_not_moving_forward_still_posts_its_own_card(portal):
    r = portal.post("/api/portal/tok-1/project-status",
                    json={"status": "not_moving_forward", "reason": "another_contractor"})
    assert r.status_code == 200, r.text
    c = portal.calls["cards"][0]
    assert c["body"] == ("Not moving forward with this project. "
                         "Reason: Selected another contractor.")
    assert c["kind"] == "customer" and "internal" not in c["meta"]


def test_the_customer_saying_ready_again_still_posts_its_own_card(portal):
    r = portal.post("/api/portal/tok-1/project-status", json={"status": "resume"})
    assert r.status_code == 200, r.text
    c = portal.calls["cards"][0]
    assert c["body"] == "Ready to move forward again."
    assert c["kind"] == "customer" and "internal" not in c["meta"]
