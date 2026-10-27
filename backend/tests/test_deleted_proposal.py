"""A deleted proposal is gone for the customer: its link shows one sentence and every action refuses.

Hanz, 2026-10-28, after a proposal deleted from the tool still reached a customer as an extra tab:
"make sure that doesn't happen again". The project list already skipped deleted rows
(db.list_proposals_by_email); the proposal's own /p/<token> link still opened the page, served the
PDF and accepted an approval, a deposit and a chat message.

THE GATE IS main._customer_proposal, raised as ProposalGone and answered 410 proposal_gone. Every
customer route that takes a token reaches it, directly or through _require. These tests:

  * sweep EVERY route whose path carries {token} with a deleted row and an authorised session:
    410, the sentence, and not one SQL statement, email or outbound HTTP call (nothing written);
  * sweep the same routes with the same row NOT deleted: the gate lets it through, so a signed-out
    caller gets exactly today's 401 (or the page's authed:false) and a signed-in one never sees 410;
  * refuse a new {token} route that is not in the sweep, and a new caller of the raw token lookup;
  * pin the landing page (no click recorded), feedback (saved without the project), the inbound
    webhook (a reply to a deleted proposal goes to staff, never into its thread or to the customer),
    the bell and the sign-in gate.

The real-Postgres half is test_pg_deleted_proposal.py.
"""
import inspect
import re

import pytest
from fastapi.testclient import TestClient

import config
import email_sender
import main

TOKEN = "tok-del-1"
PID = "pid-del-1"
CUSTOMER = "dana@customer.example"

ROW = {"proposal_id": PID, "token": TOKEN, "customer_email": CUSTOMER,
       "customer_name": "Dana", "project_name": "Acme HQ",
       "proposal_status": "sent", "deposit_status": "pending",
       "contacts_status": "pending", "schedule_status": "pending",
       "deposit_invoice_no": None, "deposit_amount": None, "deposit_required": True,
       "current_revision_no": 2, "approved_at": None, "scope_group": None,
       "followup_paused_until": None, "deleted_at": None}

# Every customer route that resolves a proposal from a token, with a body that would make it act.
# test_every_token_route_is_in_the_sweep keeps this list honest.
ROUTES = [
    ("GET", "/api/portal/{t}", None),
    ("POST", "/api/portal/{t}/questions", {"body": "Can you start Monday?"}),
    ("GET", "/api/portal/{t}/messages?after=0", None),
    ("GET", "/api/portal/{t}/file/f-1", None),
    ("POST", "/api/portal/{t}/viewed", {"revision_no": 2}),
    ("POST", "/api/portal/{t}/approve", {"name": "Dana Customer", "title": "Owner",
                                         "options": ["Base bid"], "consent": True,
                                         "signature": "Dana Customer"}),
    ("POST", "/api/portal/{t}/project-status", {"status": "paused", "months": 3}),
    ("POST", "/api/portal/{t}/deposit", {"method": "check"}),
    ("POST", "/api/portal/{t}/contacts", {"contacts": [{"role": "billing", "name": "Bo",
                                                        "email": "bo@customer.example"}]}),
    ("GET", "/api/portal/{t}/pdf", None),
    ("GET", "/api/portal/{t}/deposit-invoice.pdf", None),
    ("GET", "/api/portal/{t}/signed-contract.pdf", None),
]


@pytest.fixture
def world(monkeypatch):
    """One proposal row, an authorised session, and EVERY side door recorded.

    The database is reached only through q1/qall/execute/pool; replacing those four records every
    statement a route makes, read or write. Email and outbound HTTP are recorded the same way. A
    route that got past the gate on a deleted row would show up in `calls`."""
    calls = {"sql": [], "emails": [], "http": [], "clicks": []}
    state = {"row": dict(ROW), "session": CUSTOMER}

    monkeypatch.setattr(main.db, "get_proposal_by_token",
                        lambda t: dict(state["row"]) if t == TOKEN else None)
    monkeypatch.setattr(main, "_session_email", lambda request: state["session"])

    def rec(kind):
        def f(sql, params=()):
            calls["sql"].append((kind, " ".join(str(sql).split())))
            return [] if kind == "qall" else None
        return f
    monkeypatch.setattr(main.db, "q1", rec("q1"))
    monkeypatch.setattr(main.db, "qall", rec("qall"))
    monkeypatch.setattr(main.db, "execute", rec("execute"))

    def no_pool():
        calls["sql"].append(("pool", "direct connection"))
        raise RuntimeError("no database in this test")
    monkeypatch.setattr(main.db, "pool", no_pool)
    monkeypatch.setattr(main.db, "mark_link_clicked", lambda pid: calls["clicks"].append(pid))

    monkeypatch.setattr(email_sender, "_send",
                        lambda *a, **k: calls["emails"].append(("send", a[:2])) or True)
    monkeypatch.setattr(email_sender, "notify_team",
                        lambda *a, **k: calls["emails"].append(("team", a[:1])) or True)

    def no_http(*a, **k):
        calls["http"].append(a[:1])
        raise RuntimeError("no network in this test")
    monkeypatch.setattr(main.httpx, "get", no_http)
    monkeypatch.setattr(main.httpx, "post", no_http)
    monkeypatch.setattr(config, "PROPOSAL_TOOL_URL", "http://tool.invalid")

    # A route answering 500 against the recorded (empty) database is still a route that ran; the
    # signed-in live sweep only asks that it was never called gone.
    tc = TestClient(main.app, raise_server_exceptions=False)
    tc.calls, tc.state = calls, state
    return tc


def _call(client, method, path, body):
    url = path.format(t=TOKEN)
    return client.post(url, json=body) if method == "POST" else client.get(url)


def _delete(world):
    world.state["row"]["deleted_at"] = "2026-10-28T15:00:00+00:00"


# -- the sweep: deleted -> refused, nothing written ---------------------------
@pytest.mark.parametrize("method,path,body", ROUTES, ids=[f"{m} {p}" for m, p, _ in ROUTES])
def test_a_deleted_proposal_refuses_every_token_route_and_writes_nothing(world, method, path, body):
    _delete(world)
    r = _call(world, method, path, body)
    assert r.status_code == 410, (r.status_code, r.text[:300])
    assert r.json() == {"ok": False, "error": "proposal_gone",
                        "message": main.PROPOSAL_GONE_MESSAGE}
    assert world.calls["sql"] == [], "the route touched the database after the gate"
    assert world.calls["emails"] == [], "the route sent email for a deleted proposal"
    assert world.calls["http"] == [], "the route fetched something for a deleted proposal"


@pytest.mark.parametrize("method,path,body", ROUTES, ids=[f"{m} {p}" for m, p, _ in ROUTES])
def test_a_deleted_proposal_is_refused_signed_out_too(world, method, path, body):
    """The gate runs before the access check, so a signed-out visitor with the link sees the same
    sentence rather than a sign-in form for a proposal that is not there."""
    _delete(world)
    world.state["session"] = None
    r = _call(world, method, path, body)
    assert r.status_code == 410 and r.json()["error"] == "proposal_gone"
    assert world.calls["sql"] == [] and world.calls["emails"] == []


def test_the_sentence_is_the_one_hanz_asked_for():
    assert main.PROPOSAL_GONE_MESSAGE == (
        "This proposal is no longer available. If you have questions, reply to the email or "
        "contact Treadwell.")


def test_the_page_data_carries_nothing_but_the_sentence(world):
    """No document, price, status, approve, deposit, contract or chat: the 410 body is the whole
    answer, so the page has nothing else it could draw."""
    _delete(world)
    body = world.get(f"/api/portal/{TOKEN}").json()
    assert set(body) == {"ok", "error", "message"}
    assert "Acme HQ" not in str(body), "the deleted project's name leaked into the gone state"


# -- the sweep: live -> unchanged ---------------------------------------------
@pytest.mark.parametrize("method,path,body", ROUTES, ids=[f"{m} {p}" for m, p, _ in ROUTES])
def test_a_live_proposal_passes_the_gate_signed_out_exactly_as_before(world, method, path, body):
    """The same row with deleted_at empty: the gate hands it to the access check, which turns a
    signed-out caller away exactly as it always has."""
    world.state["session"] = None
    r = _call(world, method, path, body)
    if path == "/api/portal/{t}":
        assert r.status_code == 200 and r.json()["authed"] is False
        assert r.json()["project_name"] == "Acme HQ"
    else:
        assert r.status_code == 401 and r.json()["error"] == "unauthorized", r.text[:300]


@pytest.mark.parametrize("method,path,body", ROUTES, ids=[f"{m} {p}" for m, p, _ in ROUTES])
def test_a_live_proposal_is_never_called_gone_when_signed_in(world, method, path, body):
    """Signed in, the route runs its own body against the recorded database (whatever that makes
    of an empty result is that route's own business and covered by its own tests). What matters
    here is that it RAN: never a 410, and the route reached past the gate."""
    r = _call(world, method, path, body)
    assert r.status_code != 410, r.text[:300]
    assert "proposal_gone" not in r.text


def test_a_live_proposal_page_still_renders_in_full(world, monkeypatch):
    monkeypatch.setattr(main.db, "get_pinned_draft_data", lambda p: {"project_name": "Acme HQ"})
    monkeypatch.setattr(main.db, "list_questions", lambda pid: [])
    monkeypatch.setattr(main.db, "list_messages", lambda pid, *a, **k: [])
    monkeypatch.setattr(main.db, "list_contacts", lambda pid: [])
    monkeypatch.setattr(main.db, "list_deposits", lambda pid: [])
    monkeypatch.setattr(main.proposals, "build_view_model",
                        lambda p, data: {"project_name": "Acme HQ", "status": {}})
    r = world.get(f"/api/portal/{TOKEN}")
    assert r.status_code == 200, r.text[:300]
    assert r.json()["authed"] is True and "view" in r.json()


# -- nothing new can skip the gate --------------------------------------------
def test_every_token_route_is_in_the_sweep():
    """A new customer route under {token} has to be added to ROUTES, which runs it against a
    deleted row. Without this a route could be added and never be asked."""
    swept = {p.split("?")[0].replace("{t}", "{token}") for _, p, _ in ROUTES}
    swept = {re.sub(r"/f-1$", "/{file_id}", p) for p in swept}
    live = {r.path for r in main.app.routes
            if "{token}" in getattr(r, "path", "") and r.path.startswith("/api/")}
    assert live == swept, f"not swept: {sorted(live - swept)}; gone: {sorted(swept - live)}"


def test_every_token_route_resolves_through_the_gate():
    for r in main.app.routes:
        if "{token}" not in getattr(r, "path", ""):
            continue
        src = inspect.getsource(r.endpoint)
        assert "_require(request, token)" in src or "_customer_proposal(token)" in src, r.path


def test_only_the_gate_and_the_inbound_router_read_the_raw_token_lookup():
    """db.get_proposal_by_token does not filter deleted rows (inbound needs to see them), so a
    route that called it directly would serve a deleted proposal. Two callers, both deliberate."""
    src = inspect.getsource(main)
    calls = re.findall(r"db\.get_proposal_by_token(?:_ci)?\(", src)
    assert len(calls) == 3, calls       # _customer_proposal; inbound's _resolve (exact + _ci)
    gate = inspect.getsource(main._customer_proposal)
    assert "db.get_proposal_by_token(" in gate


# -- the landing page ---------------------------------------------------------
def test_the_landing_page_still_serves_but_records_no_click_for_a_deleted_proposal(world):
    """The page itself has to load (it draws the gone card); the click is not a signal."""
    _delete(world)
    r = world.get(f"/p/{TOKEN}")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert world.calls["clicks"] == []


def test_the_landing_page_records_a_click_on_a_live_proposal(world):
    r = world.get(f"/p/{TOKEN}")
    assert r.status_code == 200
    assert world.calls["clicks"] == [PID]


# -- feedback -----------------------------------------------------------------
def test_feedback_sent_from_a_deleted_proposal_saves_without_the_project(world, monkeypatch):
    _delete(world)
    saved = []
    monkeypatch.setattr(main.db, "add_feedback",
                        lambda email, cat, text, pid: saved.append(pid) or {"id": 1})
    r = world.post("/api/me/feedback", json={"category": "question", "body": "Hi",
                                             "token": TOKEN})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert saved == [None], "feedback was filed against a deleted proposal"


def test_feedback_from_a_live_proposal_still_carries_the_project(world, monkeypatch):
    saved = []
    monkeypatch.setattr(main.db, "add_feedback",
                        lambda email, cat, text, pid: saved.append(pid) or {"id": 1})
    world.post("/api/me/feedback", json={"category": "question", "body": "Hi", "token": TOKEN})
    assert saved == [PID]


# -- the bell and the sign-in gate (SQL; executed for real in test_pg_deleted_proposal.py) ---
def _sql_of(monkeypatch, fn):
    seen = {}
    monkeypatch.setattr(main.db, "qall", lambda sql, params=(): seen.update(sql=sql) or [])
    monkeypatch.setattr(main.db, "q1", lambda sql, params=(): seen.update(sql=sql))
    fn()
    return " ".join(seen["sql"].split())


def test_the_bell_skips_a_deleted_proposals_events(monkeypatch):
    sql = _sql_of(monkeypatch, lambda: main.db.list_customer_events(CUSTOMER))
    assert "(to_jsonb(p) ->> 'deleted_at') is null" in sql


def test_a_deleted_proposal_does_not_earn_a_sign_in_code(monkeypatch):
    sql = _sql_of(monkeypatch, lambda: main.db.email_has_proposal(CUSTOMER))
    assert sql.count("(to_jsonb(p) ->> 'deleted_at') is null") == 2, "both legs, primary and added"
    assert "p.deleted_at" not in sql, "named directly, a database without the column 500s sign-in"


# -- inbound email ------------------------------------------------------------
PRIMARY = "notify.wetreadwell.com"
STAFF = "kyle@wetreadwell.com"


@pytest.fixture
def inbox(monkeypatch):
    import inbound

    calls = {"messages": [], "sends": [], "relays": [], "row": dict(ROW, deleted_at="2026-10-28")}
    monkeypatch.setattr(config, "RESEND_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setattr(config, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(config, "EMAIL_FROM", f"Treadwell <proposals@{PRIMARY}>")
    monkeypatch.setattr(config, "RESEND_INBOUND_DOMAIN", PRIMARY)
    monkeypatch.setattr(config, "RESEND_INBOUND_DOMAINS", [PRIMARY])
    monkeypatch.setattr(config, "INBOUND_SENDER_FALLBACK", True)
    monkeypatch.setattr(config, "INBOUND_STAFF_EMAIL_POSTS", True)
    monkeypatch.setattr(main.inbound, "verify_svix", lambda *a, **k: True)
    monkeypatch.setattr(inbound, "sender_authenticated", lambda headers, frm: True)
    monkeypatch.setattr(main.db, "get_proposal_by_token",
                        lambda t: dict(calls["row"]) if t == TOKEN else None)
    monkeypatch.setattr(main.db, "get_proposal_by_token_ci",
                        lambda t: dict(calls["row"]) if (t or "").lower() == TOKEN.lower() else None)
    monkeypatch.setattr(main.db, "list_proposals_by_email", lambda e: [])
    monkeypatch.setattr(main.db, "has_email_message", lambda pid, eid: False)
    monkeypatch.setattr(main.db, "get_recipients", lambda pid: [CUSTOMER])
    monkeypatch.setattr(main.db, "add_message",
                        lambda *a, **k: calls["messages"].append(a) or {"id": 1})
    monkeypatch.setattr(email_sender, "staff_emails", lambda proposal_id=None: {STAFF})
    monkeypatch.setattr(email_sender, "_resolve_notify", lambda *a, **k: ["bids@wetreadwell.com"])
    monkeypatch.setattr(email_sender, "_send",
                        lambda to, subj, body, *a, **k: calls["sends"].append(
                            {"to": to, "subject": subj, "html": body,
                             "reply_to": k.get("reply_to"), "headers": k.get("headers")}) or True)
    monkeypatch.setattr(email_sender, "send_reply_notification",
                        lambda *a, **k: calls["relays"].append(a) or True)
    calls["text"] = "Is this still on?"
    calls["headers"] = {}

    class R:
        def raise_for_status(self):
            return None

        def json(self):
            return {"text": calls["text"], "headers": calls["headers"]}
    monkeypatch.setattr(main.httpx, "get", lambda *a, **k: R())
    calls["client"] = TestClient(main.app)
    return calls


def _mail(inbox, sender, subject="Re: Your proposal"):
    return inbox["client"].post("/api/inbound/resend", json={
        "type": "email.received",
        "data": {"email_id": "em_1", "to": [f"{TOKEN}@{PRIMARY}"], "from": sender,
                 "subject": subject}})


def test_a_customer_reply_to_a_deleted_proposal_goes_to_staff_not_the_thread(inbox):
    r = _mail(inbox, CUSTOMER)
    assert r.status_code == 200 and r.json() == {"ok": True, "deleted_proposal": True}
    assert inbox["messages"] == [], "the reply was filed in a deleted proposal's thread"
    (fwd,) = inbox["sends"]
    assert fwd["to"] == ["bids@wetreadwell.com"]
    assert "deleted" in fwd["subject"].lower() and "Acme HQ" in fwd["subject"]
    assert "This proposal was deleted" in fwd["html"] and "Is this still on?" in fwd["html"]
    # Reply-To is the SENDER: staff answer from their own inbox, not through a dead thread.
    assert fwd["reply_to"] == CUSTOMER
    assert inbox["relays"] == []


def test_a_staff_email_to_a_deleted_proposal_never_reaches_the_customer(inbox):
    """The staff path would post as Treadwell and email every recipient a link that now says
    "no longer available". For a deleted proposal it is a roster forward like any other."""
    r = _mail(inbox, STAFF)
    assert r.json()["deleted_proposal"] is True
    assert inbox["messages"] == [] and inbox["relays"] == []
    assert len(inbox["sends"]) == 1


def test_an_auto_reply_to_a_deleted_proposal_is_dropped(inbox):
    r = _mail(inbox, CUSTOMER, subject="Automatic reply: Out of office")
    assert r.json() == {"ok": True, "ignored": "auto_reply"}
    assert inbox["messages"] == [] and inbox["sends"] == []


def test_a_reply_to_a_live_proposal_is_filed_exactly_as_before(inbox):
    inbox["row"]["deleted_at"] = None
    r = _mail(inbox, CUSTOMER)
    assert r.json() == {"ok": True, "verified": True}
    assert len(inbox["messages"]) == 1 and inbox["messages"][0][1] == "customer"
    assert inbox["sends"][0]["reply_to"] != CUSTOMER     # the proposal's own Reply-To
