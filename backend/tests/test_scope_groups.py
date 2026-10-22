"""Scope groups: one project, several separately priced scopes, each its own proposal.

RJ (Operations Manager) asked for projects with no single base bid. A client gets a few scopes --
"Breakroom" polish, "Lab 1" epoxy, "Lab 2" epoxy -- under one project name, and each scope is its
OWN proposal document that the customer approves on its own. Confirmed with RJ:

  1. three separate proposal documents, each approved independently;
  2. ONE combined email carrying every scope's link, not three emails;
  3. approving one scope leaves the others open, and staff hear about it;
  4. approving two of three is two separate jobs -- which separate proposals give for free.

So every scope is a completely normal proposal row going through the existing single-proposal
pipeline, and the only new concept is the shared `scope_group` link. These tests pin the places
the link changes behaviour (publish, the combined send, the customer list, approve, the follow-up
worker) and, just as hard, that a proposal WITHOUT a group behaves exactly as it did.

THE DATA SHAPE, which the proposal tool has to match: the draft blob carries
    data["scope_group"] = {"id": "<group id>", "label": "<this scope's name>"}
and the portal reads it straight out of the shared drafts table at publish time.
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import followup_worker as fw
import main

client = TestClient(main.app)

GID = "grp-acme-0001"
T0 = datetime(2026, 10, 1, 15, 0, tzinfo=timezone.utc)


def _sib(pid, label, **over):
    row = {"proposal_id": pid, "token": "tok-" + pid, "customer_email": "cust@acme.com",
           "customer_name": "Dana Reed", "project_name": "Acme HQ", "scope_group": GID,
           "scope_label": label, "proposal_status": "sent", "deposit_status": "pending",
           "schedule_status": "pending", "contacts_status": "pending",
           "deposit_required": True, "assigned_estimator": "kyle@wetreadwell.com",
           "created_at": T0}
    row.update(over)
    return row


# ══ B. publish ═══════════════════════════════════════════════════════════════
@pytest.fixture
def publish(monkeypatch):
    """admin_publish with every seam that leaves the process stubbed and recorded."""
    calls = {"scope": [], "emails": [], "enrol": [], "notify": [], "created": [],
             "last_sent": [], "messages": []}
    state = {"draft": {"contact_email": "cust@acme.com", "contact_name": "Dana Reed",
                       "project_name": "Acme HQ"},
             "existing": {"token": "tok-1", "customer_email": "cust@acme.com",
                          "proposal_status": "sent", "assigned_estimator": None}}

    monkeypatch.setattr(main, "_admin_ok", lambda request: True)
    monkeypatch.setattr(main.db, "get_draft_data", lambda pid: dict(state["draft"]))
    monkeypatch.setattr(main.db, "get_proposal",
                        lambda pid: dict(state["existing"]) if state["existing"] else None)
    monkeypatch.setattr(main.db, "create_portal_proposal",
                        lambda pid, *a, **k: calls["created"].append(pid))
    monkeypatch.setattr(main.db, "update_portal_proposal", lambda *a, **k: None)
    monkeypatch.setattr(main.db, "reset_for_revision", lambda *a, **k: False)
    monkeypatch.setattr(main.db, "supersede_proposal_cards", lambda *a, **k: None)
    monkeypatch.setattr(main.db, "add_message",
                        lambda pid, kind, who, body, **k: calls["messages"].append(
                            (pid, body, k.get("msg_type"))) or {"id": 1})
    monkeypatch.setattr(main.db, "set_recipients", lambda *a, **k: None)
    monkeypatch.setattr(main.db, "add_recipient", lambda *a, **k: None)
    monkeypatch.setattr(main.db, "remove_recipient", lambda *a, **k: None)
    monkeypatch.setattr(main.db, "get_recipients", lambda pid: ["cust@acme.com"])
    monkeypatch.setattr(main.db, "set_assigned_estimator", lambda *a, **k: None)
    monkeypatch.setattr(main.db, "enroll_followup", lambda pid: calls["enrol"].append(pid))
    monkeypatch.setattr(main.db, "mark_last_sent", lambda pid: calls["last_sent"].append(pid))
    monkeypatch.setattr(main.db, "add_notify_override_if_absent", lambda *a, **k: None)
    monkeypatch.setattr(main.db, "set_scope_group",
                        lambda pid, gid, label: calls["scope"].append((pid, gid, label)),
                        raising=False)
    monkeypatch.setattr(main, "_pdf_cache_drop", lambda pid: None)
    monkeypatch.setattr(main.email_sender, "proposal_reply_to", lambda tok: None)
    monkeypatch.setattr(main.email_sender, "send_portal_link",
                        lambda to, *a, **k: calls["emails"].append(to) or True)
    monkeypatch.setattr(main.email_sender, "notify_team",
                        lambda heading, body, **k: calls["notify"].append(heading) or True)

    def go(**extra):
        body = {"draft_id": "d-lab1", "by": "rj@wetreadwell.com", "revision_no": 1}
        body.update(extra)
        return client.post("/api/admin/publish", json=body)

    return go, calls, state


def test_publish_stores_the_scope_group_and_label(publish):
    go, calls, state = publish
    state["draft"]["scope_group"] = {"id": GID, "label": "Lab 1"}
    r = go()
    assert r.status_code == 200, r.text
    assert calls["scope"] == [("d-lab1", GID, "Lab 1")]
    j = r.json()
    assert j["scope_group"] == GID and j["scope_label"] == "Lab 1"
    assert j["awaiting_group_send"] is True


def test_a_first_publish_of_a_sibling_stores_it_too(publish):
    """The create path, not just the re-publish one: the first send is the one that matters."""
    go, calls, state = publish
    state["existing"] = None
    state["draft"]["scope_group"] = {"id": GID, "label": "Breakroom"}
    r = go(draft_id="d-break")
    assert r.status_code == 200, r.text
    assert calls["created"] == ["d-break"]
    assert calls["scope"] == [("d-break", GID, "Breakroom")]


def test_a_legacy_publish_leaves_the_stored_scope_untouched(publish):
    """No scope_group in the draft is every proposal in the system today. Nothing is written --
    not even a NULL -- so a row's stored values survive exactly as they were."""
    go, calls, _ = publish
    r = go()
    assert r.status_code == 200, r.text
    assert calls["scope"] == [], "a legacy publish wrote the scope columns"
    j = r.json()
    assert "awaiting_group_send" not in j or j["awaiting_group_send"] is False


def test_a_legacy_publish_still_emails_the_customer_and_enrols(publish):
    """The control for the test below: the ungrouped path is unchanged."""
    go, calls, _ = publish
    go()
    assert calls["emails"] == ["cust@acme.com"]
    assert calls["enrol"] == ["d-lab1"]
    assert calls["last_sent"] == ["d-lab1"]
    assert calls["notify"] == ["Proposal sent — Acme HQ"]


def test_a_sibling_publish_sends_no_customer_email_of_its_own(publish):
    """Only the combined email reaches the customer. A per-scope "your proposal is ready" would
    be three emails for one project, which is the thing RJ asked us not to do."""
    go, calls, state = publish
    state["draft"]["scope_group"] = {"id": GID, "label": "Lab 1"}
    r = go()
    assert r.status_code == 200, r.text
    assert calls["emails"] == [], "the sibling emailed the customer on its own"
    assert r.json()["emailed"] == []
    # Nothing has reached the customer yet, so nothing is being chased and nothing is "sent".
    assert calls["enrol"] == [], "a sibling nobody has been emailed about was enrolled"
    assert calls["last_sent"] == []
    # And the staff confirmation does not cry "did NOT send" over a send that was never meant
    # to happen -- the combined send confirms instead.
    assert calls["notify"] == []


def test_a_malformed_scope_group_is_refused_before_anything_is_written(publish):
    """Ignoring it would quietly fall back to a normal publish, emailing this scope alone."""
    go, calls, state = publish
    state["draft"]["scope_group"] = {"id": "bad id; drop table", "label": "Lab 1"}
    r = go()
    assert r.status_code == 400 and r.json()["error"] == "invalid_scope_group"
    assert calls["scope"] == [] and calls["emails"] == [] and calls["messages"] == []


def test_scope_group_parsing_shape():
    f = main._scope_of
    assert f({}) == (None, None, None)
    assert f({"scope_group": None}) == (None, None, None)
    assert f({"scope_group": {"id": GID, "label": " Lab 1 "}}) == (GID, "Lab 1", None)
    # A label is optional; the publish still groups.
    assert f({"scope_group": {"id": GID}}) == (GID, None, None)
    assert f({"scope_group": "grp"})[2] == "invalid_scope_group"
    assert f({"scope_group": {"id": ""}})[2] == "invalid_scope_group"
    assert f({"scope_group": {"id": "x" * 200}})[2] == "invalid_scope_group"
    assert f({"scope_group": {"id": GID, "label": 7}})[2] == "invalid_scope_group"


# ══ C. the combined send ═════════════════════════════════════════════════════
@pytest.fixture
def group(monkeypatch):
    calls = {"emails": [], "claims": [], "released": [], "enrol": [], "last_sent": [],
             "notify": [], "messages": []}
    state = {
        "rows": [_sib("d-break", "Breakroom"),
                 _sib("d-lab1", "Lab 1", created_at=T0 + timedelta(seconds=1)),
                 _sib("d-lab2", "Lab 2", created_at=T0 + timedelta(seconds=2))],
        "drafts": ["d-break", "d-lab1", "d-lab2"],
        "recipients": {},
        "sent_no": 0,
        "send_ok": lambda to: True,
    }

    monkeypatch.setattr(main, "_admin_ok", lambda request: True)
    monkeypatch.setattr(main.db, "list_scope_group", lambda gid: [dict(r) for r in state["rows"]],
                        raising=False)
    monkeypatch.setattr(main.db, "list_scope_group_drafts", lambda gid: list(state["drafts"]),
                        raising=False)

    def _claim(gid, pids, by=None, resend=False):
        # The real one is a row-locked transaction; this models what it promises.
        calls["claims"].append({"gid": gid, "pids": list(pids), "resend": resend})
        if state["sent_no"] and not resend:
            return None
        state["sent_no"] += 1
        return state["sent_no"]

    def _release(gid, n):
        calls["released"].append((gid, n))
        state["sent_no"] -= 1

    monkeypatch.setattr(main.db, "claim_scope_group_send", _claim, raising=False)
    monkeypatch.setattr(main.db, "release_scope_group_send", _release, raising=False)
    monkeypatch.setattr(main.db, "get_recipients",
                        lambda pid: state["recipients"].get(pid, ["cust@acme.com"]))
    monkeypatch.setattr(main.db, "enroll_followup", lambda pid: calls["enrol"].append(pid))
    monkeypatch.setattr(main.db, "mark_last_sent", lambda pid: calls["last_sent"].append(pid))
    monkeypatch.setattr(main.db, "add_message",
                        lambda pid, kind, who, body, **k: calls["messages"].append(
                            {"pid": pid, "body": body, "meta": k.get("meta")}) or {"id": 1})
    monkeypatch.setattr(main.email_sender, "proposal_reply_to", lambda tok: None)

    def _send(email, name, project, scopes, **k):
        calls["emails"].append({"to": email, "name": name, "project": project,
                                "scopes": list(scopes), **k})
        return state["send_ok"](email)

    monkeypatch.setattr(main.email_sender, "send_scope_group_links", _send, raising=False)
    monkeypatch.setattr(main.email_sender, "notify_team",
                        lambda heading, body, **k: calls["notify"].append(
                            {"heading": heading, "body": body, **k}) or True)

    def go(gid=GID, **body):
        return client.post("/api/admin/scope-group/%s/send" % gid, json=body)

    return go, calls, state


def test_one_email_with_a_distinct_link_per_scope(group):
    go, calls, _ = group
    r = go(by="rj@wetreadwell.com")
    assert r.status_code == 200, r.text
    assert len(calls["emails"]) == 1, "the customer got %d emails" % len(calls["emails"])
    e = calls["emails"][0]
    assert e["to"] == "cust@acme.com" and e["project"] == "Acme HQ"
    urls = [s["url"] for s in e["scopes"]]
    assert len(set(urls)) == 3
    assert urls == ["%s/p/tok-%s" % (main.config.PUBLIC_BASE_URL, p)
                    for p in ("d-break", "d-lab1", "d-lab2")]
    assert [s["label"] for s in e["scopes"]] == ["Breakroom", "Lab 1", "Lab 2"]
    j = r.json()
    assert j["ok"] is True and j["send_no"] == 1 and j["emailed"] == ["cust@acme.com"]


def test_the_send_marks_every_scope_sent_and_starts_the_chase(group):
    """Recorded like a normal publish: last_sent stamped, the cadence enrolled, one staff
    confirmation for the group, and a staff-only card on each scope's thread."""
    go, calls, _ = group
    go(by="rj@wetreadwell.com")
    assert sorted(calls["last_sent"]) == ["d-break", "d-lab1", "d-lab2"]
    assert sorted(calls["enrol"]) == ["d-break", "d-lab1", "d-lab2"]
    assert len(calls["notify"]) == 1
    n = calls["notify"][0]
    assert n["heading"] == "Proposals sent — Acme HQ"
    for label in ("Breakroom", "Lab 1", "Lab 2"):
        assert label in n["body"]
    assert "cust@acme.com" not in n["body"], "the success email names the customer's address"
    assert sorted(m["pid"] for m in calls["messages"]) == ["d-break", "d-lab1", "d-lab2"]
    assert all(m["meta"].get("internal") is True for m in calls["messages"])


def test_refuses_and_reports_when_a_scope_is_not_published_yet(group):
    go, calls, state = group
    state["rows"] = state["rows"][:2]                 # Lab 2 is still a draft
    r = go()
    assert r.status_code == 409
    j = r.json()
    assert j["error"] == "scopes_not_published" and j["missing"] == ["d-lab2"]
    assert calls["emails"] == [] and calls["claims"] == []


def test_a_second_call_does_not_send_twice(group):
    go, calls, _ = group
    assert go().json()["send_no"] == 1
    r = go()
    assert r.status_code == 200
    assert r.json()["already_sent"] is True
    assert len(calls["emails"]) == 1, "the second call emailed the customer again"
    assert len(calls["notify"]) == 1


def test_an_explicit_resend_does_send_again(group):
    """After a revision, say. Opt-in, so a double click can never be one."""
    go, calls, _ = group
    go()
    r = go(resend=True)
    assert r.status_code == 200 and r.json()["send_no"] == 2
    assert len(calls["emails"]) == 2
    assert calls["claims"][-1]["resend"] is True


def test_a_send_that_reached_nobody_releases_the_claim(group):
    """At-most-once, like the follow-up worker: nothing went out, so a retry must be possible."""
    go, calls, state = group
    state["send_ok"] = lambda to: False
    r = go()
    assert r.status_code == 200 and r.json()["emailed"] == []
    assert calls["released"] == [(GID, 1)]
    assert calls["enrol"] == [] and calls["last_sent"] == []
    assert "did NOT send" in calls["notify"][0]["heading"]
    # And the retry really can go.
    state["send_ok"] = lambda to: True
    assert go().json()["emailed"] == ["cust@acme.com"]


def test_each_contact_gets_only_the_scopes_they_are_on(group):
    """A contact on Lab 2 only must not be handed links to Breakroom and Lab 1."""
    go, calls, state = group
    state["recipients"] = {"d-break": ["cust@acme.com"], "d-lab1": ["cust@acme.com"],
                           "d-lab2": ["cust@acme.com", "lab2@acme.com"]}
    go()
    by_to = {e["to"]: [s["label"] for s in e["scopes"]] for e in calls["emails"]}
    assert by_to == {"cust@acme.com": ["Breakroom", "Lab 1", "Lab 2"], "lab2@acme.com": ["Lab 2"]}


def test_a_closed_scope_is_left_out_of_the_email(group):
    go, calls, state = group
    state["rows"][2]["proposal_status"] = "closed_lost"
    go()
    assert [s["label"] for s in calls["emails"][0]["scopes"]] == ["Breakroom", "Lab 1"]
    assert "d-lab2" not in calls["enrol"]


def test_unknown_or_malformed_group(group):
    go, _, state = group
    assert go(gid="bad%20id").status_code == 400
    state["rows"], state["drafts"] = [], []
    assert go().status_code == 404


def test_the_combined_email_has_one_labelled_button_per_scope(monkeypatch):
    """The real renderer, with only the network call stubbed."""
    import email_sender
    sent = []
    monkeypatch.setattr(email_sender, "_send",
                        lambda to, subject, html, headers=None, **k: sent.append(
                            {"to": to, "subject": subject, "html": html,
                             "headers": headers}) or True)
    ok = email_sender.send_scope_group_links(
        "cust@acme.com", "Dana Reed", "Acme HQ",
        [{"label": "Breakroom", "url": "https://p/x/p/tok-a"},
         {"label": "Lab <1>", "url": "https://p/x/p/tok-b"},
         {"label": "Lab 2", "url": "https://p/x/p/tok-c", "approved": True}],
        note="See you Tuesday", token="tok-a")
    assert ok and len(sent) == 1
    h = sent[0]["html"]
    for tok in ("tok-a", "tok-b", "tok-c"):
        assert h.count('href="https://p/x/p/%s"' % tok) == 1
    assert "Breakroom — view &amp; approve" in h
    assert "Lab &lt;1&gt; — view &amp; approve" in h, "a scope label went out unescaped"
    assert "Lab 2 — view<" in h, "an approved scope still says approve"
    assert "Hi Dana" in h and "See you Tuesday" in h
    assert sent[0]["to"] == ["cust@acme.com"]
    assert sent[0]["subject"] == email_sender.customer_thread_subject("Acme HQ")
    assert sent[0]["headers"] == email_sender._thread_headers("cust@acme.com", "tok-a")


def test_the_combined_send_needs_the_service_token():
    r = client.post("/api/admin/scope-group/%s/send" % GID, json={})
    assert r.status_code == 401


# ══ D. the customer's list ═══════════════════════════════════════════════════
# A fake portal_proposals + recipients table, and a list_proposals_by_email that applies the
# SAME primary-OR-recipient rule the real SQL does. The leak tests stub `list_scope_group` to
# return EVERY sibling as a trap: if the customer path ever read the unscoped group, the
# other customer's scope would show up.
TABLE = [
    _sib("d-break", "Breakroom"),
    _sib("d-lab1", "Lab 1", created_at=T0 + timedelta(seconds=1)),
    # Same group, different customer, and this session is not a recipient on it.
    _sib("d-lab2", "Lab 2", customer_email="someone-else@other.com",
         created_at=T0 + timedelta(seconds=2)),
    {"proposal_id": "d-solo", "token": "tok-solo", "customer_email": "cust@acme.com",
     "project_name": "Warehouse", "proposal_status": "viewed", "deposit_status": "pending",
     "schedule_status": "pending", "created_at": T0 + timedelta(days=2)},
    {"proposal_id": "d-old", "token": "tok-old", "customer_email": "cust@acme.com",
     "project_name": "Old job", "proposal_status": "approved", "deposit_status": "received",
     "schedule_status": "pending", "created_at": T0 - timedelta(days=30)},
]
RECIPIENTS = {}


def _by_email(email):
    e = email.lower()
    rows = [r for r in TABLE if (r["customer_email"] or "").lower() == e
            or e in [x.lower() for x in RECIPIENTS.get(r["proposal_id"], [])]]
    return sorted(rows, key=lambda r: r["created_at"], reverse=True)


@pytest.fixture
def customer(monkeypatch):
    monkeypatch.setattr(main, "_session_email", lambda request: "cust@acme.com")
    monkeypatch.setattr(main.db, "list_proposals_by_email", _by_email)
    monkeypatch.setattr(main.db, "list_scope_group",
                        lambda gid: [r for r in TABLE if r.get("scope_group") == gid],
                        raising=False)


def test_siblings_are_grouped_and_labelled(customer):
    cards = client.get("/api/me/proposals").json()["proposals"]
    assert [c["display_name"] for c in cards] == [
        "Warehouse", "Acme HQ — Breakroom", "Acme HQ — Lab 1", "Old job"]
    lab1 = cards[2]
    assert lab1["scope_group"] == GID and lab1["scope_label"] == "Lab 1"
    assert lab1["project_name"] == "Acme HQ"        # the raw name is unchanged
    solo = cards[0]
    assert solo["scope_group"] is None and solo["scope_label"] is None


def test_LEAK_a_sibling_sent_to_another_email_never_appears_in_the_list(customer):
    cards = client.get("/api/me/proposals").json()["proposals"]
    assert "tok-d-lab2" not in [c["token"] for c in cards]
    assert all("Lab 2" not in (c["display_name"] or "") for c in cards)


@pytest.fixture
def portal_view(customer, monkeypatch):
    monkeypatch.setattr(main.db, "get_proposal_by_token",
                        lambda tok: next((dict(r) for r in TABLE if r["token"] == tok), None))
    monkeypatch.setattr(main.db, "get_pinned_draft_data", lambda p: {})
    monkeypatch.setattr(main.proposals, "build_view_model",
                        lambda p, data: {"project_name": p.get("project_name"),
                                         "status": {"proposal": p.get("proposal_status")}})
    monkeypatch.setattr(main.db, "list_questions", lambda pid: [])
    monkeypatch.setattr(main.db, "list_messages", lambda pid, *a, **k: [])
    monkeypatch.setattr(main.db, "list_contacts", lambda pid: [])
    monkeypatch.setattr(main.db, "list_deposits", lambda pid: [])


def test_the_proposal_page_carries_its_scope_and_siblings(portal_view):
    vm = client.get("/api/portal/tok-d-lab1").json()["view"]
    assert vm["scope"]["label"] == "Lab 1" and vm["scope"]["group"] == GID
    sibs = vm["scope"]["siblings"]
    assert [s["scope_label"] for s in sibs] == ["Breakroom", "Lab 1"]
    assert [s["current"] for s in sibs] == [False, True]


def test_LEAK_the_page_sibling_list_is_scoped_to_the_session(portal_view):
    vm = client.get("/api/portal/tok-d-lab1").json()["view"]
    assert "tok-d-lab2" not in [s["token"] for s in vm["scope"]["siblings"]]


def test_LEAK_an_added_recipient_does_see_the_sibling_they_are_on(portal_view):
    """The scoping is the real one -- primary OR added recipient -- not "primary only"."""
    RECIPIENTS["d-lab2"] = ["cust@acme.com"]
    try:
        vm = client.get("/api/portal/tok-d-lab1").json()["view"]
        assert [s["scope_label"] for s in vm["scope"]["siblings"]] == ["Breakroom", "Lab 1",
                                                                      "Lab 2"]
    finally:
        RECIPIENTS.clear()


def test_an_ungrouped_proposal_page_has_no_scope(portal_view):
    vm = client.get("/api/portal/tok-solo").json()["view"]
    assert vm["scope"] is None


# ══ E. the staff board ═══════════════════════════════════════════════════════
def test_the_pipeline_carries_scope_group_and_label(monkeypatch):
    monkeypatch.setattr(main, "_admin_ok", lambda request: True)
    monkeypatch.setattr(main.db, "unread_counts", lambda: {})
    monkeypatch.setattr(main.db, "get_settings", lambda key: None)
    monkeypatch.setattr(main.db, "list_all_portal_proposals",
                        lambda: [_sib("d-lab1", "Lab 1"), dict(TABLE[3])])
    rows = client.get("/api/admin/pipeline").json()["proposals"]
    assert (rows[0]["scope_group"], rows[0]["scope_label"]) == (GID, "Lab 1")
    assert (rows[1]["scope_group"], rows[1]["scope_label"]) == (None, None)


def test_the_pipeline_query_reads_the_columns_ddl_safely(monkeypatch):
    """Through to_jsonb, like every column prod may not have yet: a missing column named
    directly would take the board down with UndefinedColumn."""
    seen = {}
    monkeypatch.setattr(main.db, "qall", lambda sql, params=(): seen.update(sql=sql) or [])
    main.db.list_all_portal_proposals()
    sql = " ".join(seen["sql"].split())
    assert "(to_jsonb(p) ->> 'scope_group') as scope_group" in sql
    assert "(to_jsonb(p) ->> 'scope_label') as scope_label" in sql


# ══ F. approve ═══════════════════════════════════════════════════════════════
@pytest.fixture
def approve(monkeypatch, tmp_path):
    calls = {"approved": [], "messages": [], "team": [], "scope_reads": []}
    state = {"proposal": _sib("d-lab1", "Lab 1", proposal_status="viewed", approved_at=None,
                              current_revision_no=None),
             "group": [_sib("d-break", "Breakroom"),
                       _sib("d-lab1", "Lab 1", proposal_status="viewed"),
                       _sib("d-lab2", "Lab 2", proposal_status="viewed"),
                       _sib("d-lab3", "Lab 3", proposal_status="approved")]}
    monkeypatch.setattr(main.config, "UPLOAD_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(main, "_require", lambda request, token: dict(state["proposal"]))
    monkeypatch.setattr(main, "_session_email", lambda request: "cust@acme.com")
    monkeypatch.setattr(main.db, "get_pinned_draft_data",
                        lambda p: {"project_name": "Acme HQ", "work_type": "epoxy",
                                   "proposal_lump_sum": 10000.0})
    # Budget-style: no signing, so the test stays about the group rather than the contract.
    monkeypatch.setattr(main.signing, "signing_block",
                        lambda p, data: {"required": False, "blocked_reason": None})
    monkeypatch.setattr(main.db, "add_approval", lambda *a, **k: 1)
    monkeypatch.setattr(main.db, "set_approved",
                        lambda pid, *a, **k: calls["approved"].append(pid))
    monkeypatch.setattr(main.db, "add_message",
                        lambda pid, kind, who, body, **k: calls["messages"].append(
                            {"pid": pid, "body": body, "type": k.get("msg_type"),
                             "meta": k.get("meta")}) or {"id": 1})
    monkeypatch.setattr(main.db, "get_recipients", lambda pid: ["cust@acme.com"])
    monkeypatch.setattr(main.email_sender, "proposal_reply_to", lambda t: None)
    monkeypatch.setattr(main.email_sender, "notify_team",
                        lambda subject, body, **k: calls["team"].append(
                            {"subject": subject, "body": body}))
    monkeypatch.setattr(main.email_sender, "send_customer_update", lambda *a, **k: True)
    monkeypatch.setattr(main.automations, "run_on_approval", lambda p, project: None)

    def _group(gid):
        calls["scope_reads"].append(gid)
        return [dict(r) for r in state["group"]]

    monkeypatch.setattr(main.db, "list_scope_group", _group, raising=False)

    def go():
        return client.post("/api/portal/tok-d-lab1/approve",
                           json={"name": "Dana Reed", "date": "2026-10-02"})

    return go, calls, state


def test_approving_one_scope_posts_a_card_on_each_other_open_scope(approve):
    go, calls, _ = approve
    r = go()
    assert r.status_code == 200, r.text
    notes = [m for m in calls["messages"] if "still open" in m["body"]]
    assert sorted(m["pid"] for m in notes) == ["d-break", "d-lab2"], (
        "only the OTHER scopes that are still open get the card")
    for m in notes:
        assert m["body"] == "Lab 1 was approved; this scope is still open."
        assert m["type"] == "system" and m["meta"]["internal"] is True


def test_approving_one_scope_never_touches_the_others_status(approve):
    go, calls, _ = approve
    go()
    assert calls["approved"] == ["d-lab1"], "another scope's status was written"


def test_the_staff_approval_email_says_which_scopes_are_still_open(approve):
    go, calls, _ = approve
    go()
    body = calls["team"][0]["body"]
    assert "Lab 1" in body and "Breakroom" in body and "Lab 2" in body
    assert "still open" in body


def test_an_ungrouped_approval_is_unchanged(approve):
    go, calls, state = approve
    state["proposal"] = dict(state["proposal"], scope_group=None, scope_label=None)
    go()
    assert calls["scope_reads"] == [], "an ungrouped approval read a scope group"
    assert not any("still open" in m["body"] for m in calls["messages"])
    assert "still open" not in calls["team"][0]["body"]


def test_a_failing_group_read_never_fails_the_approval(approve, monkeypatch):
    go, calls, _ = approve
    monkeypatch.setattr(main.db, "list_scope_group",
                        lambda gid: (_ for _ in ()).throw(RuntimeError("db blip")))
    r = go()
    assert r.status_code == 200 and r.json()["ok"] is True
    assert calls["approved"] == ["d-lab1"]


# ══ G. follow-up dedup ═══════════════════════════════════════════════════════
NOW = datetime(2026, 8, 4, 15, 0, tzinfo=timezone.utc)     # 10am Chicago, inside the window
ENROLLED = NOW - timedelta(hours=30)                       # the first nudge is due


def _fp(pid, **over):
    p = {"proposal_id": pid, "token": "tok-" + pid, "customer_email": "c@x.com",
         "customer_name": "Cust", "project_name": "Acme HQ", "proposal_status": "sent",
         "followup_enrolled_at": ENROLLED, "followup_disabled_at": None,
         "followup_paused_until": None, "cycle_viewed_at": None, "deposit_required": True,
         "assigned_estimator": "kyle@wetreadwell.com", "approved_total": 1000.0}
    p.update(over)
    return p


def _tick(monkeypatch, rows):
    calls = {"customer": [], "staff": [], "reserved": []}
    by_id = {r["proposal_id"]: r for r in rows}
    monkeypatch.setenv("FOLLOWUP_AUTOMATION_ENABLED", "true")
    monkeypatch.setattr(fw.db, "get_settings", lambda key: None)
    monkeypatch.setattr(fw.db, "list_followup_candidates", lambda: [dict(r) for r in rows])
    monkeypatch.setattr(fw.db, "get_proposal", lambda pid: dict(by_id[pid]))
    monkeypatch.setattr(fw.db, "get_recipients", lambda pid: ["c@x.com"])
    monkeypatch.setattr(fw.db, "get_followup_recipients", lambda pid: ["c@x.com"])
    monkeypatch.setattr(fw.db, "reserve_followup",
                        lambda pid, key, detail: calls["reserved"].append((pid, key)) or 1)
    monkeypatch.setattr(fw.db, "delete_followup", lambda rid: None)
    monkeypatch.setattr(fw.db, "add_message", lambda *a, **k: {"id": 1})
    monkeypatch.setattr(fw.email_sender, "proposal_reply_to", lambda t: None)
    monkeypatch.setattr(fw.email_sender, "_resolve_notify", lambda kind, pid=None: [])
    monkeypatch.setattr(fw.email_sender, "send_followup",
                        lambda addr, url, proj, tmpl, **k: calls["customer"].append(
                            {"url": url, "template": tmpl}) or True)
    monkeypatch.setattr(fw.email_sender, "notify_team",
                        lambda subject, body, **k: calls["staff"].append(
                            {"subject": subject, "pid": k.get("proposal_id")}) or True)
    fw._tick(NOW)
    return calls


def _chased(calls):
    return sorted({c["url"].rsplit("/p/", 1)[1].split("#")[0] for c in calls["customer"]})


def test_two_due_siblings_send_ONE_customer_email_per_tick(monkeypatch):
    rows = [_fp("a", scope_group=GID, scope_label="Lab 1"),
            _fp("b", scope_group=GID, scope_label="Lab 2")]
    calls = _tick(monkeypatch, rows)
    assert len(calls["customer"]) == 1, (
        "the group was chased %d times in one tick" % len(calls["customer"]))


def test_the_earliest_enrolled_sibling_leads(monkeypatch):
    rows = [_fp("b", scope_group=GID, followup_enrolled_at=ENROLLED + timedelta(minutes=1)),
            _fp("a", scope_group=GID)]
    calls = _tick(monkeypatch, rows)
    assert _chased(calls) == ["tok-a"]


def test_a_follower_does_not_burn_its_rule_while_it_waits(monkeypatch):
    """No reservation is taken for a skipped customer send, so if the lead drops out (approved
    and paid, closed) the next sibling picks the chase up rather than finding its step spent."""
    rows = [_fp("a", scope_group=GID), _fp("b", scope_group=GID)]
    calls = _tick(monkeypatch, rows)
    b_keys = [k for pid, k in calls["reserved"] if pid == "b"]
    assert b_keys and all("staff" in k for k in b_keys), b_keys


def test_staff_notes_are_still_per_scope(monkeypatch):
    rows = [_fp("a", scope_group=GID), _fp("b", scope_group=GID)]
    calls = _tick(monkeypatch, rows)
    assert sorted(s["pid"] for s in calls["staff"]) == ["a", "b"]


def test_when_the_lead_is_no_longer_chased_the_next_sibling_leads(monkeypatch):
    """Approved with the deposit already on its way: the customer side of the lead has stopped,
    so the open scope is the one that gets the customer email."""
    rows = [_fp("a", scope_group=GID, proposal_status="approved", approved_at=ENROLLED,
                deposit_status="submitted", cycle_viewed_at=ENROLLED),
            _fp("b", scope_group=GID, followup_enrolled_at=ENROLLED + timedelta(minutes=1))]
    calls = _tick(monkeypatch, rows)
    assert _chased(calls) == ["tok-b"]


def test_ungrouped_proposals_are_unaffected(monkeypatch):
    rows = [_fp("a"), _fp("b")]
    calls = _tick(monkeypatch, rows)
    assert _chased(calls) == ["tok-a", "tok-b"]


def test_two_groups_each_get_their_own_one(monkeypatch):
    rows = [_fp("a", scope_group=GID), _fp("b", scope_group=GID),
            _fp("c", scope_group="grp-other"), _fp("d", scope_group="grp-other"),
            _fp("e")]
    calls = _tick(monkeypatch, rows)
    assert _chased(calls) == ["tok-a", "tok-c", "tok-e"]
