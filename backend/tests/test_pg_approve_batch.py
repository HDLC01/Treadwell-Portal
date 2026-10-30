"""Approving several proposals at once, EXECUTED against a real Postgres (2026-10-31).

test_approve_several.py proves the route's gates and side effects with the database replaced; this
file runs the SQL: the combined-approval DDL from schema.sql, the open records, and db.approve_batch's
lock, re-check and single transaction -- all of it, or none of it -- then the whole route through
main.app with only email and the network stubbed.

HOW TO RUN: see test_pg_scope_groups.py (a throwaway local Postgres named by PORTAL_TEST_PG_URL).
Skipped without it.
"""
from __future__ import annotations

import pytest

import pg_fixture
from pg_fixture import needs_pg

pytestmark = [needs_pg, pytest.mark.realdb, pytest.mark.realwording]

if pg_fixture.PG_URL:  # imported only when the tests will run
    from datetime import date

    from fastapi.testclient import TestClient
    from psycopg.types.json import Jsonb

    import config
    import customer_auth as ca
    import db
    import email_sender
    import main

DANA = "dana@customer.example"
G = "g-acme"


@pytest.fixture(scope="module")
def pgdb():
    with pg_fixture.scratch_portal_db("batch") as pool:
        yield pool


def _draft(label, total, work_type="gyp"):
    # Gyp by default: approved without an e-signature, so the route needs no proposal tool. The
    # signable path is covered with the database replaced (test_approve_several.py).
    return {"project_name": "Acme HQ", "work_type": work_type,
            "scope_group": {"id": G, "label": label},
            "rooms": [{"name": label, "is_base": True, "bid": {"total": total}}]}


@pytest.fixture
def world(pgdb, monkeypatch):
    db.execute("truncate public.portal_proposals, public.portal_sessions, public.drafts, "
               "public.draft_revisions restart identity cascade")
    db._APPROVE_SEVERAL_CACHE.update(at=None, value=False)
    for pid, label, total in (("a", "Lab 1", 12000.0), ("b", "Lab 2", 8000.0),
                              ("c", "Breakroom", 5000.0)):
        db.execute("insert into public.drafts (id, data) values (%s, %s)",
                   (pid, Jsonb(_draft(label, total))))
        db.create_portal_proposal(pid, "tok-" + pid, DANA, "Dana", "Acme HQ", None,
                                  "kyle@wetreadwell.com", True, None, G, label)
        db.execute("update public.portal_proposals set proposal_status = 'viewed' "
                   "where proposal_id = %s", (pid,))
    sent = []
    monkeypatch.setattr(email_sender, "_send", lambda *a, **k: sent.append(a[:2]) or True)
    monkeypatch.setattr(email_sender, "notify_team", lambda *a, **k: sent.append(a[:1]) or [])
    monkeypatch.setattr(config, "PROPOSAL_TOOL_URL", "")
    return sent


def _item(pid, total, rev=None, **approval):
    a = {"name": "Dana Reed", "title": "Owner", "approved_date": date(2026, 10, 31),
         "total": total, "option_label": "Base", "ip": "203.0.113.9", "approver_email": DANA,
         "options": ["Base"], "consent_version": None, "user_agent": "UA", "revision_no": rev,
         "revision_sha256": None, "contract_sha256": None,
         "batch": {"batch_id": "b-1", "count": 2, "proposals": [], "signed": []}}
    a.update(approval)
    return {"proposal_id": pid, "expected_revision_no": rev, "approval": a,
            "proposal": {"total": total, "option_label": "Base", "options": ["Base"],
                         "deposit_amount": round(total * 0.25, 2), "name": "Dana Reed",
                         "title": "Owner", "approved_date": date(2026, 10, 31)}}


def _counts():
    return (db.q1("select count(*) as n from public.portal_approvals")["n"],
            db.q1("select count(*) as n from public.portal_proposals "
                  "where proposal_status = 'approved'")["n"])


def test_the_ddl_is_there_and_readable(world):
    assert db.approve_several_ready() is True


def test_both_are_written_in_one_go(world):
    ids = db.approve_batch([_item("a", 12000.0), _item("b", 8000.0)])
    assert set(ids) == {"a", "b"} and all(isinstance(v, int) for v in ids.values())
    rows = {r["proposal_id"]: r for r in db.qall(
        "select proposal_id, proposal_status, approved_at, approved_total, deposit_amount, "
        "approved_options from public.portal_proposals")}
    assert rows["a"]["proposal_status"] == "approved" and rows["a"]["approved_at"] is not None
    assert float(rows["a"]["deposit_amount"]) == 3000.0 and rows["a"]["approved_options"] == ["Base"]
    assert rows["c"]["proposal_status"] == "viewed"
    rec = db.latest_approval_record("b")
    assert rec["batch"]["batch_id"] == "b-1" and rec["ip"] == "203.0.113.9"


def test_one_already_approved_refuses_all_and_writes_nothing(world):
    db.execute("update public.portal_proposals set proposal_status='approved', approved_at=now() "
               "where proposal_id='b'")
    before = _counts()
    with pytest.raises(db.BatchRefused) as e:
        db.approve_batch([_item("a", 12000.0), _item("b", 8000.0)])
    assert e.value.reasons == [{"proposal_id": "b", "code": "already_approved"}]
    assert _counts() == before
    assert db.q1("select proposal_status from public.portal_proposals "
                 "where proposal_id='a'")["proposal_status"] == "viewed"


@pytest.mark.parametrize("change,code", [
    ("update public.portal_proposals set proposal_status='closed_lost' where proposal_id='b'",
     "closed"),
    ("update public.portal_proposals set current_revision_no=5 where proposal_id='b'", "revised"),
    ("update public.portal_proposals set deleted_at=now() where proposal_id='b'", "gone"),
])
def test_every_state_change_under_the_lock_refuses_all(world, change, code):
    db.execute(change)
    before = _counts()
    with pytest.raises(db.BatchRefused) as e:
        db.approve_batch([_item("a", 12000.0), _item("b", 8000.0)])
    assert [r["code"] for r in e.value.reasons] == [code]
    assert _counts() == before


def test_a_write_that_fails_half_way_rolls_back_the_first(world):
    """The first proposal's two writes have run when the second one's insert fails: the
    transaction takes the first back with it."""
    before = _counts()
    with pytest.raises(Exception):
        db.approve_batch([_item("a", 12000.0), _item("b", 8000.0, revision_no="not a number")])
    assert _counts() == before
    assert db.q1("select approved_at from public.portal_proposals "
                 "where proposal_id='a'")["approved_at"] is None


def test_document_opens_are_per_person_per_revision(world):
    assert db.document_opened("a", DANA, 2) is False
    db.record_document_open("a", DANA, 2)
    db.record_document_open("a", "DANA@customer.example", 2)
    assert db.document_opened("a", "Dana@Customer.Example", 2) is True
    assert db.document_opened("a", DANA, 3) is False, "a new revision has to be opened again"
    assert db.document_opened("a", "pat@other.example", 2) is False
    row = db.q1("select open_count from public.portal_document_opens where proposal_id='a'")
    assert row["open_count"] == 2
    db.record_document_open("b", DANA, None)
    assert db.document_opened("b", DANA, None) is True


def test_the_whole_route_against_the_real_database(world):
    sent = world
    client = TestClient(main.app)
    client.cookies.set(config.SESSION_COOKIE, ca.start_session(DANA))
    listing = client.get("/api/portal/tok-a/approve-several").json()
    assert listing["available"] is True
    assert sorted(p["token"] for p in listing["proposals"]) == ["tok-a", "tok-b", "tok-c"]
    items = [{"token": "tok-a", "revision_no": None, "option_labels": ["Lab 1"]},
             {"token": "tok-b", "revision_no": None, "option_labels": ["Lab 2"]}]
    r = client.post("/api/portal/tok-a/approve-several", json={
        "batch_id": "batch-pg-0001", "items": items, "name": "Dana Reed", "title": "Owner",
        "date": "2026-10-31"})
    assert r.status_code == 200, r.text
    assert {a["token"]: a["total"] for a in r.json()["approved"]} == {"tok-a": 12000.0,
                                                                       "tok-b": 8000.0}
    recs = db.qall("select proposal_id, name, ip, user_agent, batch, consent_version "
                   "from public.portal_approvals order by proposal_id")
    assert [x["proposal_id"] for x in recs] == ["a", "b"]
    assert recs[0]["batch"] == recs[1]["batch"] and recs[0]["batch"]["count"] == 2
    assert recs[0]["consent_version"] is None, "Gyp is approved without an e-signature"
    inv = {x["proposal_id"]: x["deposit_invoice_no"] for x in db.qall(
        "select proposal_id, deposit_invoice_no from public.portal_proposals")}
    assert inv["a"] and inv["b"] and inv["a"] != inv["b"] and inv["c"] is None
    # Each thread got its own approval line.
    lines = db.qall("select proposal_id from public.portal_questions "
                    "where body like %s order by proposal_id", ("Approved by Dana Reed%",))
    assert [x["proposal_id"] for x in lines] == ["a", "b"]
    # Doing it again is answered with the first result and writes nothing more.
    again = client.post("/api/portal/tok-a/approve-several", json={
        "batch_id": "batch-pg-0001", "items": items, "name": "Dana Reed"})
    assert again.status_code == 200 and again.json().get("replayed") is True
    assert db.q1("select count(*) as n from public.portal_approvals")["n"] == 2
    assert sent, "no email at all went out"
