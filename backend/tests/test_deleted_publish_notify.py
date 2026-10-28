"""Staff and service calls never email a customer about a DELETED proposal.

Review finding on the deleted-link change (2026-10-28): the customer routes refuse a deleted
proposal, but admin_publish onto an EXISTING portal row and POST /api/notify still emailed the
customer a link that answers "This proposal is no longer available" -- and the publish quietly
rewrote the deleted row. Both now refuse with 409 `proposal_deleted` before anything is written or
sent. The tool's own flow is restore (which clears deleted_at), then publish, and that still works.

Every db call is recorded (a stand-in for main.db), and email is recorded at email_sender._send,
the one function that calls Resend. The real-Postgres half is in test_pg_deleted_proposal.py.
"""
import pytest
from fastapi.testclient import TestClient

import config
import email_sender
import main

PID = "pid-del-pub"
TOKEN = "tok-del-pub"
CUSTOMER = "dana@customer.example"
SERVICE = "svc-test-token"

ROW = {"proposal_id": PID, "token": TOKEN, "customer_email": CUSTOMER, "customer_name": "Dana",
       "project_name": "Acme HQ", "proposal_status": "sent", "scope_group": None,
       "scope_label": None, "current_revision_no": 1, "deleted_at": "2026-10-28T15:00:00+00:00"}
DRAFT = {"contact_email": CUSTOMER, "contact_name": "Dana Reed", "project_name": "Acme HQ"}

READS = {"get_draft_data", "get_proposal"}


class RecordingDB:
    """main.db for these tests: every call recorded, a few answered from `row`."""

    def __init__(self, row):
        self.row = row
        self.calls = []

    def __getattr__(self, name):
        def f(*a, **k):
            self.calls.append(name)
            if name == "get_draft_data":
                return dict(DRAFT)
            if name == "get_proposal":
                return dict(self.row) if self.row and a and a[0] == PID else None
            if name == "get_recipients":
                return [CUSTOMER]
            if name == "restore_proposal":
                if not self.row:
                    return False
                self.row["deleted_at"] = None
                return True
            if name in ("list_notify_overrides", "list_scope_group_draft_states"):
                return []
            if name == "reset_for_revision":
                return False
            return None
        return f

    def wrote(self):
        return [c for c in self.calls if c not in READS]


@pytest.fixture
def world(monkeypatch):
    fake = RecordingDB(dict(ROW))
    monkeypatch.setattr(main, "db", fake)
    monkeypatch.setattr(config, "SERVICE_TOKEN", SERVICE)
    sent = []

    def fake_send(to, subject, html, headers=None, reply_to=None, attachments=None):
        sent.append({"to": list(to), "subject": subject, "html": html})
        return True

    def no_network(*a, **k):
        raise AssertionError("a test reached for the network")
    monkeypatch.setattr(email_sender, "_send", fake_send)
    monkeypatch.setattr(email_sender.httpx, "post", no_network)
    monkeypatch.setattr(main.httpx, "post", no_network)
    monkeypatch.setattr(main.httpx, "get", no_network)
    monkeypatch.setattr(email_sender, "_thread_subject_template", lambda: "{project} proposal")
    monkeypatch.setattr(email_sender, "_sent_template", lambda: None)
    monkeypatch.setattr(email_sender, "notify_team",
                        lambda heading, *a, **k: sent.append({"team": heading}) or True)
    monkeypatch.setattr(main, "_pdf_cache_drop", lambda pid: None)
    client = TestClient(main.app, headers={"x-service-token": SERVICE})
    client.db, client.sent = fake, sent
    return client


def _customer_mail(sent):
    return [m for m in sent if "to" in m]


def _publish(client, **extra):
    body = {"draft_id": PID, "by": "kyle@wetreadwell.com", "revision_no": 2}
    body.update(extra)
    return client.post("/api/admin/publish", json=body)


# -- publish ------------------------------------------------------------------
def test_publishing_onto_a_deleted_row_is_refused_and_sends_and_writes_nothing(world):
    r = _publish(world, message="Here is the new number", emails=["pat@customer.example"])
    assert r.status_code == 409, r.text
    assert r.json() == {"ok": False, "error": "proposal_deleted",
                        "message": main.DELETED_REFUSAL_MESSAGE}
    assert world.sent == [], "an email went out about a deleted proposal"
    assert world.db.wrote() == [], "the deleted row was written: %s" % world.db.wrote()


def test_a_deleted_scope_of_a_group_is_refused_before_it_rejoins_the_group(world, monkeypatch):
    """adopt_into_scope_group used to run first, writing the deleted row back into its group."""
    monkeypatch.setattr(RecordingDB, "__getattr__", _with_group(RecordingDB.__getattr__))
    r = _publish(world)
    assert r.status_code == 409 and r.json()["error"] == "proposal_deleted"
    assert "adopt_into_scope_group" not in world.db.calls
    assert world.db.wrote() == [] and world.sent == []


def _with_group(orig):
    def getattr_(self, name):
        if name == "get_draft_data":
            return lambda *a, **k: (self.calls.append(name),
                                    dict(DRAFT, scope_group={"id": "grp-1", "label": "Epoxy"}))[1]
        return orig(self, name)
    return getattr_


def test_the_same_publish_on_a_live_row_goes_through(world):
    """The control: without deleted_at the identical request publishes and emails the customer, so
    the refusal above is the guard and nothing else."""
    world.db.row["deleted_at"] = None
    r = _publish(world)
    assert r.status_code == 200, r.text
    assert [m["to"] for m in _customer_mail(world.sent)] == [[CUSTOMER]]
    assert "update_portal_proposal" in world.db.calls


def test_restore_then_publish_is_the_tools_flow_and_it_works(world):
    """The tool's restore (api_restore_draft) calls the portal's restore first, which clears
    deleted_at; the publish after it is an ordinary re-send."""
    assert _publish(world).status_code == 409
    r = world.post(f"/api/admin/proposal/{PID}/restore", json={"by": "kyle@wetreadwell.com"})
    assert r.status_code == 200 and r.json()["was_deleted"] is True, r.text
    assert world.db.row["deleted_at"] is None
    r = _publish(world)
    assert r.status_code == 200, r.text
    assert [m["to"] for m in _customer_mail(world.sent)] == [[CUSTOMER]]


def test_a_first_publish_with_no_row_is_unaffected(world):
    world.db.row = None
    r = _publish(world, revision_no=1)
    assert r.status_code == 200, r.text
    assert "create_portal_proposal" in world.db.calls
    assert [m["to"] for m in _customer_mail(world.sent)] == [[CUSTOMER]]


# -- /api/notify --------------------------------------------------------------
@pytest.mark.parametrize("kind", ["published", "reply", "anything"])
def test_notify_about_a_deleted_proposal_is_refused_and_sends_nothing(world, kind):
    r = world.post("/api/notify", json={"proposal_id": PID, "type": kind,
                                        "message": "We replied to your question"})
    assert r.status_code == 409, r.text
    assert r.json()["error"] == "proposal_deleted"
    assert world.sent == []
    assert world.db.wrote() == []


@pytest.mark.parametrize("kind", ["published", "reply"])
def test_notify_about_a_live_proposal_still_emails(world, kind):
    world.db.row["deleted_at"] = None
    r = world.post("/api/notify", json={"proposal_id": PID, "type": kind,
                                        "message": "We replied to your question"})
    assert r.status_code == 200, r.text
    assert [m["to"] for m in _customer_mail(world.sent)] == [[CUSTOMER]]


def test_notify_still_needs_the_service_token_first(world):
    r = TestClient(main.app).post("/api/notify", json={"proposal_id": PID, "type": "published"})
    assert r.status_code == 401
    assert world.db.calls == []
