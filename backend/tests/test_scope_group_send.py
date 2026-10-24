"""The combined email's send lifecycle (build spec 2026-10-24, P2).

RJ's "one project, many proposals, one email": every scope is its own proposal, and the customer
hears about them in ONE email. These tests pin what happens AFTER the first one went out -- a
scope added later, a scope revised, a contact whose address bounced, a double click -- plus the
one membership rule, the capability handshake, publish safety on a database without the DDL, and
the scope's name on the customer's money pages.
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import db
import followup_rules
import main
import scope_groups

client = TestClient(main.app)

GID = "grp-acme-0001"
T0 = datetime(2026, 10, 1, 15, 0, tzinfo=timezone.utc)


def _st(pid, archived=False, is_test=None, name="Acme HQ"):
    return {"id": pid, "archived": archived, "is_test": is_test, "project_name": name}


# ══ 7. one membership definition ═════════════════════════════════════════════
def test_an_archived_scope_is_parked_not_live():
    live, parked = scope_groups.split_members([_st("a"), _st("b"), _st("c", archived=True)])
    assert live == ["a", "b"] and parked == ["c"]


def test_a_test_scope_is_never_a_member_of_a_real_group():
    live, parked = scope_groups.split_members([_st("a"), _st("b", is_test=True), _st("c")])
    assert live == ["a", "c"] and parked == ["b"]


def test_a_real_scope_is_never_a_member_of_a_test_group_and_a_test_group_stands():
    # Every active draft is a test: the group is a test project, its scopes are its members.
    assert scope_groups.split_members([_st("a", is_test=True), _st("b", is_test="true")]) == (
        ["a", "b"], [])
    # The stored flag wins in both directions; the name only decides when nobody has said.
    assert scope_groups.draft_is_test(_st("x", is_test=False, name="Test Lab")) is False
    assert scope_groups.draft_is_test(_st("x", is_test=None, name="QA lab")) is True
    assert scope_groups.draft_is_test(_st("x", is_test=None, name="Demolition")) is False


def test_the_db_expected_set_goes_through_the_helper(monkeypatch):
    monkeypatch.setattr(db, "list_scope_group_draft_states",
                        lambda gid: [_st("a"), _st("b", archived=True), _st("c", is_test=True)])
    assert db.list_scope_group_drafts(GID) == ["a"]


def test_the_follow_up_side_uses_the_same_rule():
    rows = [{"proposal_id": p, "proposal_status": "sent"} for p in ("a", "b", "c")]
    live = followup_rules.live_members(rows, [_st("a"), _st("b", is_test=True), _st("c")])
    assert [r["proposal_id"] for r in live] == ["a", "c"]


def test_a_parked_scope_is_still_grouped_so_it_is_never_emailed_alone(monkeypatch):
    monkeypatch.setattr(db, "list_scope_group_draft_states",
                        lambda gid: [_st("a"), _st("b", is_test=True)])
    assert main._active_scope_group({"proposal_id": "b", "scope_group": GID}) == GID
    assert main._active_scope_group({"proposal_id": "a", "scope_group": GID}) == GID
    # Taken out of the group in the tool (no draft carries it): an ordinary proposal again.
    assert main._active_scope_group({"proposal_id": "z", "scope_group": GID}) is None


def test_a_test_scope_is_neither_emailed_nor_blocking_the_send(send):
    go, calls, f = send
    f.drafts = [_st("d-break"), _st("d-lab1"), _st("d-lab2", is_test=True),
                _st("d-lab9", is_test=True)]
    r = go()
    assert r.status_code == 200, r.text
    assert _labels(calls["emails"][0]) == ["Breakroom", "Lab 1"]
    assert r.json()["not_included"] == [], "an unpublished Test scope was reported as missing"


def test_an_archived_unpublished_scope_does_not_block_the_first_send(send):
    """Finding #28/#32: an archived scope nobody can see used to 409 the email forever."""
    go, calls, f = send
    f.drafts = [_st("d-break"), _st("d-lab1"), _st("d-lab2"), _st("d-lab3", archived=True)]
    r = go()
    assert r.status_code == 200, r.text
    assert len(calls["emails"]) == 1


# ══ 1-5. the send lifecycle ══════════════════════════════════════════════════
def _row(pid, label, **over):
    row = {"proposal_id": pid, "token": "tok-" + pid, "customer_email": "cust@acme.com",
           "customer_name": "Dana Reed", "project_name": "Acme HQ", "scope_group": GID,
           "scope_label": label, "proposal_status": "sent", "current_revision_no": 1,
           "assigned_estimator": "kyle@wetreadwell.com", "created_at": T0}
    row.update(over)
    return row


def _labels(email):
    return [s["label"] for s in email["scopes"]]


@pytest.fixture
def send(monkeypatch):
    """The combined send over an in-memory group (scope_send_fake runs db.py's planning code)."""
    from scope_send_fake import FakeSendDB

    f = FakeSendDB([_row("d-break", "Breakroom"),
                    _row("d-lab1", "Lab 1", created_at=T0 + timedelta(seconds=1)),
                    _row("d-lab2", "Lab 2", created_at=T0 + timedelta(seconds=2))],
                   recipients={p: ["cust@acme.com"] for p in ("d-break", "d-lab1", "d-lab2")})
    f.install(monkeypatch, main)
    calls = {"emails": [], "notify": []}
    ok = {"fn": lambda to: True}
    monkeypatch.setattr(main, "_admin_ok", lambda request: True)
    monkeypatch.setattr(main.email_sender, "proposal_reply_to", lambda tok: "rt-" + tok)

    def _send(email, name, project, scopes, **k):
        calls["emails"].append({"to": email, "scopes": list(scopes), **k})
        return ok["fn"](email)

    monkeypatch.setattr(main.email_sender, "send_scope_group_links", _send)
    monkeypatch.setattr(main.email_sender, "notify_team",
                        lambda heading, body, **k: calls["notify"].append(
                            {"heading": heading, "body": body}) or True)
    calls["ok"] = ok

    def go(**body):
        return client.post("/api/admin/scope-group/%s/send" % GID, json=body)

    return go, calls, f


def test_double_click_sends_once_and_the_second_press_says_nothing_to_send(send):
    go, calls, f = send
    assert go().status_code == 200
    r = go()
    assert r.status_code == 409 and r.json()["error"] == "nothing_to_send"
    assert len(calls["emails"]) == 1


def test_a_press_while_the_first_is_still_sending_sends_nothing(send):
    """The claim is taken before the email goes: a scope IN FLIGHT is not pending."""
    go, calls, f = send
    for pid in ("d-break", "d-lab1", "d-lab2"):
        f._add_mark(pid, {"action": "scope_group_sent", "scope_group": GID, "send_no": 1,
                          "revision_no": 1, "kind": "first"})
    r = go()
    assert r.status_code == 409 and r.json()["error"] == "nothing_to_send"
    assert calls["emails"] == []


def test_a_send_that_died_half_way_can_be_sent_again(send):
    """Finding #7c: a claim with no delivery recorded stops counting after INFLIGHT_TTL."""
    go, calls, f = send
    stale = f.now - scope_groups.INFLIGHT_TTL - timedelta(minutes=1)
    for pid in ("d-break", "d-lab1", "d-lab2"):
        f._add_mark(pid, {"action": "scope_group_sent", "scope_group": GID, "send_no": 1,
                          "revision_no": 1, "kind": "first"}, created_at=stale)
    r = go()
    assert r.status_code == 200, r.text
    assert _labels(calls["emails"][0]) == ["Breakroom", "Lab 1", "Lab 2"]
    assert r.json()["send_no"] == 2


def test_a_scope_added_after_the_first_send_is_emailed_alone_as_new(send):
    """Findings #7/#11/#14: the drawer's button used to answer 'already sent' and reach nobody."""
    go, calls, f = send
    assert go().status_code == 200
    f.rows.append(_row("d-lab3", "Lab 3", created_at=T0 + timedelta(seconds=3)))
    f.drafts.append({"id": "d-lab3", "archived": False})
    f.recipients["d-lab3"] = ["cust@acme.com"]
    r = go(by="rj@wetreadwell.com")
    assert r.status_code == 200, r.text
    e = calls["emails"][1]
    assert _labels(e) == ["Lab 3"], "the update email re-sent scopes the customer already has"
    assert [s["change"] for s in e["scopes"]] == ["new"]
    j = r.json()
    assert j["first"] is False and [s["kind"] for s in j["scopes"]] == ["new"]
    # Only Lab 3 is newly announced: enrolled; the others are not enrolled again.
    assert f.calls["enrol"][-1] == ("d-lab3", True)
    assert f.calls["enrol"].count(("d-break", True)) == 1


def test_an_unpublished_scope_blocks_only_the_first_send(send):
    go, calls, f = send
    f.drafts.append({"id": "d-lab3", "archived": False})        # priced but not published
    r = go()
    assert r.status_code == 409 and r.json()["error"] == "scopes_not_published"
    f.drafts.pop()
    assert go().status_code == 200                               # the first email goes out
    f.drafts.append({"id": "d-lab3", "archived": False})
    f.publish_revision("d-lab1", 2)                              # ...and Lab 1 is revised
    r = go()
    assert r.status_code == 200, r.text
    assert r.json()["not_included"] == ["d-lab3"]
    assert _labels(calls["emails"][-1]) == ["Lab 1"]


def test_a_revision_after_the_send_is_pending_and_emailed_as_revised(send):
    """Findings #3/#6: a revised scope used to email nobody and raise no signal."""
    go, calls, f = send
    go()
    f.publish_revision("d-lab1", 2)
    r = go()
    assert r.status_code == 200, r.text
    e = calls["emails"][-1]
    assert _labels(e) == ["Lab 1"] and [s["change"] for s in e["scopes"]] == ["revised"]
    assert r.json()["scopes"][0]["kind"] == "revised"


def test_an_old_mark_without_a_revision_counts_as_announced(send):
    go, calls, f = send
    for pid in ("d-break", "d-lab1", "d-lab2"):
        f._add_mark(pid, {"action": "scope_group_sent", "scope_group": GID, "send_no": 1,
                          "scopes": ["d-break", "d-lab1", "d-lab2"]})
    r = go()
    assert r.status_code == 409 and r.json()["error"] == "nothing_to_send"


def test_a_scope_sent_as_an_ordinary_proposal_joins_without_blocking_or_resending(send):
    """Findings #1/#2: Polish was sent on its own, then 'New scope' made Lab 1. Its portal row has
    no group; it used to 409 the group until somebody republished it (reopening its approval)."""
    go, calls, f = send
    f.rows = [_row("d-polish", "Polish", scope_group=None, scope_label=None,
                   proposal_status="approved", last_sent_at=T0),
              _row("d-lab1", "Lab 1", created_at=T0 + timedelta(seconds=1))]
    f.drafts = [{"id": "d-polish", "archived": False}, {"id": "d-lab1", "archived": False}]
    f.draft_data["d-polish"] = {"scope_group": {"id": GID, "label": "Polish"}}
    f.recipients = {"d-polish": ["cust@acme.com"], "d-lab1": ["cust@acme.com"]}
    r = go()
    assert r.status_code == 200, r.text
    assert f.calls["adopted"] == [("d-polish", GID, "Polish")]
    assert _labels(calls["emails"][0]) == ["Lab 1"], "the approved parent was emailed again"
    assert [s["change"] for s in calls["emails"][0]["scopes"]] == ["new"]


def test_no_delivery_at_all_is_a_502_and_marks_nothing(send):
    go, calls, f = send
    calls["ok"]["fn"] = lambda to: False
    r = go()
    assert r.status_code == 502
    j = r.json()
    assert j["error"] == "delivery_failed" and j["emailed"] == []
    assert j["recipients"] == [{"email": "cust@acme.com", "delivered": False}]
    assert f.marks == [] and f.calls["enrol"] == [] and f.calls["last_sent"] == []


def test_each_recipient_sees_only_their_scopes_in_an_update(send):
    go, calls, f = send
    f.recipients["d-lab2"] = ["cust@acme.com", "pat@acme.com"]
    go()
    f.publish_revision("d-break", 2)
    f.publish_revision("d-lab2", 2)
    calls["emails"].clear()
    r = go()
    assert r.status_code == 200, r.text
    by_to = {e["to"]: _labels(e) for e in calls["emails"]}
    assert by_to == {"cust@acme.com": ["Breakroom", "Lab 2"], "pat@acme.com": ["Lab 2"]}
    assert {x["email"]: x["delivered"] for x in r.json()["recipients"]} == {
        "cust@acme.com": True, "pat@acme.com": True}


def test_retry_failed_mails_only_the_address_that_failed(send):
    """Finding #16: fixing one bounced address used to re-mail everybody and restart every
    cadence. Pat (Lab 2 only) bounces; the retry mails Pat alone, about Lab 2 alone."""
    go, calls, f = send
    f.recipients["d-lab2"] = ["cust@acme.com", "pat@acme.com"]
    calls["ok"]["fn"] = lambda to: to != "pat@acme.com"
    r = go()
    assert r.status_code == 200, r.text                  # somebody got it: announced
    assert {x["email"]: x["delivered"] for x in r.json()["recipients"]} == {
        "cust@acme.com": True, "pat@acme.com": False}
    enrolled = list(f.calls["enrol"])
    calls["emails"].clear()
    calls["ok"]["fn"] = lambda to: True
    r = go(retry_failed=True)
    assert r.status_code == 200, r.text
    assert [(e["to"], _labels(e)) for e in calls["emails"]] == [("pat@acme.com", ["Lab 2"])]
    assert f.calls["enrol"] == enrolled, "a retry restarted the follow-up cadence"
    lab2 = [m for m in f.marks if m["proposal_id"] == "d-lab2"][-1]["detail"]["delivery"]
    assert lab2 == {"delivered": ["cust@acme.com", "pat@acme.com"], "failed": []}
    # Nothing left to retry.
    r = go(retry_failed=True)
    assert r.status_code == 409 and r.json()["error"] == "nothing_to_send"


def test_a_retry_that_fails_again_is_a_502_and_can_be_retried(send):
    go, calls, f = send
    f.recipients["d-lab2"] = ["cust@acme.com", "pat@acme.com"]
    calls["ok"]["fn"] = lambda to: to != "pat@acme.com"
    go()
    r = go(retry_failed=True)
    assert r.status_code == 502 and r.json()["error"] == "delivery_failed"
    calls["ok"]["fn"] = lambda to: True
    assert go(retry_failed=True).json()["emailed"] == ["pat@acme.com"]


def test_a_resend_never_wipes_a_customer_pause(send):
    go, calls, f = send
    go()
    f.row("d-lab1")["followup_paused_until"] = T0 + timedelta(days=60)
    f.publish_revision("d-lab1", 2)
    go()
    assert f.row("d-lab1")["followup_paused_until"] == T0 + timedelta(days=60)
    assert ("d-lab1", True) in f.calls["enrol"]


def test_the_note_and_attachments_ride_along(send):
    """Finding #17: they were silently dropped for a scope."""
    import base64
    go, calls, f = send
    pdf = b"%PDF-1.4\n%fake\n"
    r = go(message="Includes the revised drawings", attachments=[
        {"name": "drawings.pdf", "mime": "application/pdf",
         "b64": base64.b64encode(pdf).decode()}])
    assert r.status_code == 200, r.text
    e = calls["emails"][0]
    assert e["note"] == "Includes the revised drawings"
    assert e["attachments"] == [("drawings.pdf", pdf)]


def test_an_unreadable_attachment_is_refused_before_anything_is_claimed(send):
    go, calls, f = send
    r = go(attachments=[{"name": "x.pdf", "mime": "application/pdf", "b64": "!!not base64!!"}])
    assert r.status_code == 400
    assert f.calls["claims"] == [] and calls["emails"] == []


def test_the_email_renderer_names_new_and_revised_and_attaches(monkeypatch):
    import email_sender
    sent = []
    monkeypatch.setattr(email_sender, "_send",
                        lambda to, subject, html, headers=None, **k: sent.append(
                            {"html": html, **k}) or True)
    ok = email_sender.send_scope_group_links(
        "cust@acme.com", "Dana Reed", "Acme HQ",
        [{"label": "Lab 3", "url": "https://p/x/p/tok-c", "change": "new"},
         {"label": "Lab 1", "url": "https://p/x/p/tok-a", "change": "revised"}],
        note="Call me Monday", token="tok-c", attachments=[("a.pdf", b"%PDF")])
    assert ok
    h = sent[0]["html"]
    assert "Lab 3 (new)" in h and "Lab 1 (revised)" in h
    assert "1 new proposal and 1 revised proposal" in h
    assert "Your proposals have been updated" in h
    assert "Call me Monday" in h
    assert sent[0]["attachments"] == [("a.pdf", b"%PDF")]
    # The first email of a project keeps its wording.
    sent.clear()
    email_sender.send_scope_group_links("cust@acme.com", "Dana", "Acme HQ",
                                        [{"label": "Lab 1", "url": "https://p/x/p/a"},
                                         {"label": "Lab 2", "url": "https://p/x/p/b"}])
    assert "Your proposals are ready" in sent[0]["html"] and "(new)" not in sent[0]["html"]


# ══ 6. the board says honestly what has been emailed ═════════════════════════
def test_the_pipeline_exposes_scope_send_state(monkeypatch):
    rows = [_row("d-break", "Breakroom", last_sent_at=T0),
            _row("d-lab1", "Lab 1", current_revision_no=2, last_sent_at=T0,
                 created_at=T0 + timedelta(seconds=1)),
            {"proposal_id": "d-solo", "token": "tok-solo", "customer_email": "x@y.com",
             "project_name": "Warehouse", "proposal_status": "sent", "deposit_status": "pending",
             "schedule_status": "pending", "created_at": T0}]
    marks = [{"id": i, "proposal_id": p, "created_at": T0,
              "detail": {"action": "scope_group_sent", "scope_group": GID, "send_no": 1,
                         "revision_no": 1, "kind": "first",
                         "delivery": {"delivered": ["cust@acme.com"], "failed": []}}}
             for i, p in enumerate(("d-break", "d-lab1"), 1)]
    monkeypatch.setattr(main, "_admin_ok", lambda request: True)
    monkeypatch.setattr(main.db, "unread_counts", lambda: {})
    monkeypatch.setattr(main.db, "get_settings", lambda key: None)
    monkeypatch.setattr(main.db, "list_all_portal_proposals", lambda: [dict(r) for r in rows])
    monkeypatch.setattr(main.db, "list_scope_group_draft_states",
                        lambda gid: [_st("d-break"), _st("d-lab1"), _st("d-lab2")])
    monkeypatch.setattr(main.db, "scope_group_resumed_at", lambda gid: None)
    monkeypatch.setattr(main.db, "list_scope_group_marks", lambda gids: marks)
    for r in rows:
        r.setdefault("deposit_status", "pending")
        r.setdefault("schedule_status", "pending")
    out = {r["proposal_id"]: r for r in client.get("/api/admin/pipeline").json()["proposals"]}
    assert out["d-break"]["scope_send_state"] == {
        "announced": True, "pending_scope_ids": ["d-lab1"], "not_included": ["d-lab2"]}
    # Lab 1 went out at revision 1 and is now at 2: not announced, and pending.
    assert out["d-lab1"]["scope_send_state"]["announced"] is False
    assert "scope_send_state" not in out["d-solo"], "an ungrouped row changed shape"


# ══ 8. publish safety on a database without the DDL ══════════════════════════
@pytest.fixture
def publish(monkeypatch):
    calls = {"messages": [], "emails": [], "updated": [], "recipients": []}
    state = {"existing": None,
             "draft": {"contact_email": "cust@acme.com", "contact_name": "Dana Reed",
                       "project_name": "Acme HQ", "scope_group": {"id": GID, "label": "Lab 1"}}}

    def missing_column(*a, **k):
        raise RuntimeError('column "scope_group" of relation "portal_proposals" does not exist')

    def create(*a, **k):
        # A database without the DDL: only a write that NAMES the scope columns fails.
        if k.get("scope_group"):
            missing_column()

    monkeypatch.setattr(main, "_admin_ok", lambda request: True)
    monkeypatch.setattr(main.db, "get_draft_data", lambda pid: dict(state["draft"]))
    monkeypatch.setattr(main.db, "get_proposal", lambda pid: state["existing"])
    monkeypatch.setattr(main.db, "create_portal_proposal", create)
    monkeypatch.setattr(main.db, "set_scope_group", missing_column)
    monkeypatch.setattr(main.db, "adopt_into_scope_group", missing_column)
    monkeypatch.setattr(main.db, "update_portal_proposal",
                        lambda *a, **k: calls["updated"].append(a[0]))
    monkeypatch.setattr(main.db, "reset_for_revision", lambda *a, **k: False)
    monkeypatch.setattr(main.db, "reopen_if_closed", lambda pid: False)
    monkeypatch.setattr(main.db, "add_message",
                        lambda pid, kind, who, body, **k: calls["messages"].append(body))
    monkeypatch.setattr(main.db, "set_recipients",
                        lambda *a, **k: calls["recipients"].append(a[0]))
    monkeypatch.setattr(main.email_sender, "send_portal_link",
                        lambda to, *a, **k: calls["emails"].append(to) or True)

    def go():
        return client.post("/api/admin/publish", json={"draft_id": "d-lab1", "revision_no": 1})

    return go, calls, state


def test_a_new_scope_on_a_database_without_the_ddl_leaves_no_row(publish):
    """Findings #24/#40: the row and the 'your proposal is ready' card used to be written first,
    then the scope columns failed, leaving a visible, ungrouped, un-emailed proposal."""
    go, calls, _ = publish
    r = go()
    assert r.status_code == 503 and r.json()["error"] == "scope_groups_not_ready"
    assert calls["messages"] == [] and calls["emails"] == [] and calls["recipients"] == []


def test_an_existing_scope_on_a_database_without_the_ddl_is_refused_before_any_write(publish):
    go, calls, state = publish
    state["existing"] = {"token": "tok-1", "customer_email": "cust@acme.com",
                         "proposal_status": "sent"}
    r = go()
    assert r.status_code == 503
    assert calls["updated"] == [] and calls["messages"] == [] and calls["emails"] == []


# ══ 9. capability handshake ══════════════════════════════════════════════════
def test_the_capability_route(monkeypatch):
    assert client.get("/api/admin/capabilities").status_code == 401
    monkeypatch.setattr(main, "_admin_ok", lambda request: True)
    monkeypatch.setattr(main.db, "scope_group_columns_ready", lambda: True)
    assert client.get("/api/admin/capabilities").json() == {
        "ok": True, "scope_groups": True, "group_followups": True, "version": 1}
    # Code without the DDL is not ready for grouped publishes, and says so.
    monkeypatch.setattr(main.db, "scope_group_columns_ready", lambda: False)
    assert client.get("/api/admin/capabilities").json()["scope_groups"] is False


# ══ 10. the scope's name where money changes hands ═══════════════════════════
SCOPED = {"proposal_id": "d-lab1", "project_name": "Acme HQ", "scope_group": GID,
          "scope_label": "Lab 1", "customer_name": "Dana", "approved_total": 10000}
PLAIN = {"proposal_id": "d-solo", "project_name": "Warehouse", "scope_group": None,
         "scope_label": None, "customer_name": "Dana", "approved_total": 10000}


def test_display_project_name():
    assert scope_groups.display_project_name(SCOPED) == "Acme HQ - Lab 1"
    assert scope_groups.display_project_name(PLAIN) == "Warehouse"
    # A stale label without a group is an ordinary proposal.
    assert scope_groups.display_project_name(dict(PLAIN, scope_label="Lab 9")) == "Warehouse"


def test_the_contract_sentence_and_file_name_name_the_scope():
    import signing
    name = signing.signing_project_name(SCOPED, {"project_name": "Acme HQ"})
    assert name == "Acme HQ - Lab 1"
    assert "Acme HQ - Lab 1" in signing.consent_text(name)
    assert signing.contract_filename(name) == "Acme HQ - Lab 1 - Signed Contract.pdf"
    assert signing.signing_project_name(PLAIN, {"project_name": "Warehouse"}) == "Warehouse"


def test_the_invoice_job_name_names_the_scope():
    import invoice
    assert invoice.invoice_payload(SCOPED, 2500.0)["job_name"] == "Acme HQ - Lab 1"
    assert invoice.invoice_payload(PLAIN, 2500.0)["job_name"] == "Warehouse"


def test_the_customer_page_carries_the_payment_reference(monkeypatch):
    row = dict(SCOPED, token="tok-d-lab1", customer_email="cust@acme.com",
               proposal_status="approved", deposit_status="pending", schedule_status="pending")
    monkeypatch.setattr(main, "_session_email", lambda request: "cust@acme.com")
    monkeypatch.setattr(main.db, "get_proposal_by_token", lambda tok: dict(row))
    monkeypatch.setattr(main.db, "list_proposals_by_email", lambda e: [dict(row)])
    monkeypatch.setattr(main.db, "get_pinned_draft_data", lambda p: {})
    monkeypatch.setattr(main.proposals, "build_view_model",
                        lambda p, data: {"project_name": p.get("project_name"),
                                         "status": {"proposal": p.get("proposal_status")}})
    for name in ("list_questions", "list_contacts", "list_deposits"):
        monkeypatch.setattr(main.db, name, lambda pid: [])
    monkeypatch.setattr(main.db, "list_messages", lambda pid, *a, **k: [])
    vm = client.get("/api/portal/tok-d-lab1").json()["view"]
    assert vm["payment_reference"] == "Acme HQ - Lab 1"


def test_the_deposit_step_shows_the_scope_reference():
    """Executed through the real app.js setupDeposit (tests/js/deposit-reference-harness.js)."""
    import json
    import pathlib
    import shutil
    import subprocess
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    here = pathlib.Path(__file__).resolve()
    proc = subprocess.run(["node", str(here.parent / "js" / "deposit-reference-harness.js"),
                           str(here.parents[2] / "frontend")],
                          capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["scoped"]["check"] == "Acme HQ - Lab 1"
    assert out["scoped"]["line"].endswith("Reference: Acme HQ - Lab 1")
    assert out["plain"]["check"] == "Warehouse"
    assert out["older"]["check"] == "Old portal payload"


def test_the_approval_emails_name_the_scope(monkeypatch, tmp_path):
    calls = {"team": [], "customer": []}
    p = _row("d-lab1", "Lab 1", proposal_status="viewed", approved_at=None,
             current_revision_no=None, deposit_required=True)
    monkeypatch.setattr(main.config, "UPLOAD_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(main, "_require", lambda request, token: dict(p))
    monkeypatch.setattr(main, "_session_email", lambda request: "cust@acme.com")
    monkeypatch.setattr(main.db, "get_pinned_draft_data",
                        lambda p: {"project_name": "Acme HQ", "work_type": "epoxy",
                                   "proposal_lump_sum": 10000.0})
    monkeypatch.setattr(main.signing, "signing_block",
                        lambda p, data: {"required": False, "blocked_reason": None})
    monkeypatch.setattr(main.db, "add_approval", lambda *a, **k: 1)
    monkeypatch.setattr(main.db, "set_approved", lambda *a, **k: None)
    monkeypatch.setattr(main.db, "add_message", lambda *a, **k: {"id": 1})
    monkeypatch.setattr(main.db, "get_recipients", lambda pid: ["cust@acme.com"])
    monkeypatch.setattr(main.email_sender, "proposal_reply_to", lambda t: None)
    monkeypatch.setattr(main.email_sender, "notify_team",
                        lambda subject, body, **k: calls["team"].append((subject, body, k)))
    monkeypatch.setattr(main, "_notify_customer",
                        lambda p, heading, body, **k: calls["customer"].append((body, k)))
    monkeypatch.setattr(main, "_notice_scope_siblings", lambda p: "")
    monkeypatch.setattr(main, "_raise_scope_decision", lambda p: None)
    seen = {}
    monkeypatch.setattr(main.automations, "run_on_approval",
                        lambda p, project: seen.update(project=project))
    r = client.post("/api/portal/tok-d-lab1/approve", json={"name": "Dana Reed"})
    assert r.status_code == 200, r.text
    subject, body, k = calls["team"][-1]
    assert subject == "Proposal APPROVED — Acme HQ - Lab 1"
    assert "Project: Acme HQ - Lab 1." in body
    assert k["project"] == "Acme HQ", "the staff thread key moved off the project"
    cbody, ck = calls["customer"][-1]
    assert "approval of <strong>Acme HQ - Lab 1</strong>" in cbody
    assert "approved <strong>Acme HQ - Lab 1</strong>" in ck["peer_body_html"]
    # The approval automations (Dropbox folder, deposit request) keep the project's own name.
    assert seen["project"] == "Acme HQ"
