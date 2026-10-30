"""Approving several proposals of one project at once, with ONE signature (2026-10-31).

Hanz: "We must have the ability to also select and approve multiple proposals." On a project priced
as several proposals (a scope group) the customer ticks several, chooses each one's options, and
signs once. What must stay PER PROPOSAL, exactly as a single approval makes it: the approval row
(with the same signing metadata on every row), the signed contract and its certificate, the deposit
invoice, the card status. What becomes ONE: the signature, the staff email, each contact's email.

THE NON-NEGOTIABLES THESE PIN:
  * every gate the single approve card enforces holds for EVERY selected proposal (an option, the
    document opened at this revision, the consent sentence ticked), checked by the SERVER;
  * all or nothing: any refusal refuses the whole batch and writes nothing;
  * only proposals this signed-in customer may open, of this project, not approved/closed/deleted;
  * a proposal sent to somebody else never appears, not even by name in a refusal.

THESE RUN THE REAL ROUTES through the real gate (_require -> _customer_proposal -> _can_access). Only
the seams that leave the process are replaced: the database functions (by an in-memory world whose
approve_batch keeps the real function's all-or-nothing contract; the SQL itself runs against a real
Postgres in test_pg_approve_batch.py), Resend, and the proposal tool.

THE PAPERWORK RUNS AFTER THE ANSWER (2026-10-31): the route commits, assigns the invoice numbers and
hands the batch to a background job. Here the job's thread is recorded instead of started, and
_commit runs it in place (`finish=True`), so the side effects these tests read are the background
job's. The job's own guarantees -- answer first, resume after a restart, no duplicates, renders
retried then reported -- are the section at the end of this file.
"""
import copy
import json
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import automations
import config
import db
import email_sender
import followup_worker
import invoice
import main
import signing

SESSION = "dana@acme.com"
G = "g-acme"


def _draft(label, work_type="epoxy", rooms=None):
    return {"project_name": "Acme HQ", "work_type": work_type,
            "scope_group": {"id": G, "label": label},
            "rooms": rooms or [{"name": label, "is_base": True, "bid": {"total": 12000.0}},
                               {"name": "Sealer", "bid": {"total": 1000.0}}]}


def _row(pid, label, rev, **over):
    r = {"proposal_id": pid, "token": "tok-" + pid, "customer_email": SESSION,
         "customer_name": "Dana Reed", "project_name": "Acme HQ", "proposal_status": "viewed",
         "approved_at": None, "deposit_required": True, "current_revision_no": rev,
         "deposit_amount": None, "deposit_invoice_no": None, "deposit_requested_at": None,
         "deposit_status": "pending", "scope_group": G, "scope_label": label,
         "deleted_at": None, "created_at": "2026-10-20T10:00:00+00:00"}
    r.update(over)
    return r


def _world_rows():
    return {
        "a": _row("a", "Lab 1", 2),
        "b": _row("b", "Lab 2", 3, deposit_required=True),
        "c": _row("c", "Breakroom", 1),
        # Same project, sent to SOMEBODY ELSE. Must never be approvable, or even named, for Dana.
        "d": _row("d", "Lab 3", 1, customer_email="pat@other.example"),
        # Dana's, but ANOTHER project.
        "e": _row("e", "Main Floor", 1, scope_group="g-other", project_name="Other Job"),
    }


def _world_drafts():
    return {"a": _draft("Lab 1",
                        rooms=[{"name": "Lab 1", "is_base": True, "bid": {"total": 12000.0}},
                               {"name": "Sealer", "bid": {"total": 1000.0}}]),
            "b": _draft("Lab 2",
                        rooms=[{"name": "Lab 2", "is_base": True, "bid": {"total": 8000.0}}]),
            # Gyp has no signature block: approved without an e-signature, as single approve does.
            "c": _draft("Breakroom", work_type="gyp",
                        rooms=[{"name": "Breakroom", "is_base": True, "bid": {"total": 5000.0}}]),
            "d": _draft("Lab 3"), "e": _draft("Main Floor")}


@pytest.fixture
def world(monkeypatch, tmp_path):
    rows, drafts = _world_rows(), _world_drafts()
    st = {"rows": rows, "drafts": drafts, "session": SESSION, "opens": set(),
          "approvals": [], "batch_calls": [], "messages": [], "team": [], "customer": [],
          "deposit_emails": [], "contracts": [], "certs": [], "invoices": [], "tasks": [],
          "staff_task_emails": [], "batch_error": None, "next_id": 100,
          "clock": datetime(2026, 10, 31, 15, 0, tzinfo=timezone.utc), "spawned": [],
          "saves": 0}
    by_tok = lambda t: next((r for r in rows.values() if r["token"] == t), None)  # noqa: E731

    monkeypatch.setattr(main, "_session_email", lambda request: st["session"])
    monkeypatch.setattr(main.db, "get_proposal_by_token",
                        lambda t: copy.deepcopy(by_tok(t)) if by_tok(t) else None)
    monkeypatch.setattr(main.db, "email_can_access", lambda pid, e: False)
    monkeypatch.setattr(main.db, "get_proposal", lambda pid: copy.deepcopy(rows.get(pid)))
    monkeypatch.setattr(main.db, "list_proposals_by_email", lambda e: [
        copy.deepcopy(r) for r in rows.values()
        if r["customer_email"] == e.lower() and not r.get("deleted_at")])
    monkeypatch.setattr(main.db, "list_scope_group_draft_states", lambda g: [])
    monkeypatch.setattr(main.db, "list_scope_group",
                        lambda g: [copy.deepcopy(r) for r in rows.values()
                                   if r["scope_group"] == g])
    monkeypatch.setattr(main.db, "get_pinned_draft_data",
                        lambda p: copy.deepcopy(drafts.get(p["proposal_id"])))
    monkeypatch.setattr(main.db, "approve_several_ready", lambda: True)
    monkeypatch.setattr(main.ratelimit, "allow_ip", lambda *a, **k: True)
    monkeypatch.setattr(main.db, "record_document_open",
                        lambda pid, e, rev: st["opens"].add((pid, e.lower(), int(rev or 0))))
    monkeypatch.setattr(main.db, "document_opened",
                        lambda pid, e, rev: (pid, (e or "").lower(), int(rev or 0)) in st["opens"])

    def approve_batch(items):
        """The real function's contract: re-check under the lock, all or nothing."""
        st["batch_calls"].append(copy.deepcopy(items))
        if st["batch_error"] is not None:
            raise st["batch_error"]
        reasons = []
        for it in items:
            r = rows.get(it["proposal_id"])
            if r is None or r.get("deleted_at"):
                reasons.append({"proposal_id": it["proposal_id"], "code": "gone"})
            elif r["proposal_status"] == "approved" or r.get("approved_at"):
                reasons.append({"proposal_id": it["proposal_id"], "code": "already_approved"})
            elif r["proposal_status"] == "closed_lost":
                reasons.append({"proposal_id": it["proposal_id"], "code": "closed"})
            elif (r["current_revision_no"] or None) != (it["expected_revision_no"] or None):
                reasons.append({"proposal_id": it["proposal_id"], "code": "revised"})
        if reasons:
            raise db.BatchRefused(reasons)
        out = {}
        for it in items:
            st["next_id"] += 1
            out[it["proposal_id"]] = st["next_id"]
            # signed_at is the column default, now(): ONE value for every row of the transaction.
            st["approvals"].append(dict(copy.deepcopy(it["approval"]),
                                        proposal_id=it["proposal_id"], id=st["next_id"],
                                        signed_at=st["clock"].isoformat()))
            pr = it["proposal"]
            rows[it["proposal_id"]].update(
                proposal_status="approved", approved_at="2026-10-31T15:00:00+00:00",
                approved_total=pr["total"], approved_option=pr["option_label"],
                approved_options=pr["options"], deposit_amount=pr["deposit_amount"],
                approved_name=pr["name"], approved_title=pr["title"])
        return out
    monkeypatch.setattr(main.db, "approve_batch", approve_batch)
    monkeypatch.setattr(main.db, "latest_approval_record", lambda pid: next(
        (copy.deepcopy(a) for a in reversed(st["approvals"]) if a["proposal_id"] == pid), None))

    monkeypatch.setattr(main.db, "add_message", lambda pid, kind, who, body, **k: st[
        "messages"].append({"pid": pid, "body": body, "type": k.get("msg_type"),
                            "meta": k.get("meta")}) or {"id": len(st["messages"])})
    monkeypatch.setattr(main.db, "upsert_signed_contract", lambda aid, pid, **k: st[
        "contracts"].append({"approval_id": aid, "proposal_id": pid, **k}))

    def get_signed_contract(pid):
        """The real ordering: a built one first, the newest of those."""
        mine = [c for c in st["contracts"] if c["proposal_id"] == pid]
        built = [c for c in mine if c.get("pdf")]
        return dict((built or mine)[-1]) if (built or mine) else None
    monkeypatch.setattr(main.db, "get_signed_contract", get_signed_contract)
    monkeypatch.setattr(main.db, "built_contract_ids", lambda ids: {
        c["approval_id"] for c in st["contracts"] if c.get("pdf") and c["approval_id"] in ids})

    # The background job's record, kept where the real one is: `paperwork` in the first approval
    # row's batch. Claim and save keep the real SQL's lease rules (db.batch_paperwork_claim/_save,
    # run against Postgres in test_pg_approve_batch.py), on this world's clock.
    lease_lock = threading.Lock()
    monkeypatch.setattr(main, "_paperwork_now", lambda: st["clock"])
    monkeypatch.setattr(main, "_paperwork_spawn",
                        lambda aid, pdfs: st["spawned"].append((aid, pdfs)))

    def by_id(aid):
        return next((a for a in st["approvals"] if a["id"] == aid), None)

    def claim(aid, owner, lease_sec):
        with lease_lock:
            a = by_id(aid)
            pw = ((a or {}).get("batch") or {}).get("paperwork")
            if not pw or pw.get("state") != "pending":
                return None
            held = pw.get("lease") or {}
            # Held while `until` is still ahead. A lease handed back (0 s) ends "now", and the
            # real now() has always moved on by the next statement, so it is free at once.
            if held.get("owner") not in (None, owner) and held["until"] > st["clock"]:
                return None
            pw["lease"] = {"owner": owner, "until": st["clock"] + timedelta(seconds=lease_sec)}
            return copy.deepcopy(a["batch"])

    def save(aid, owner, paperwork, lease_sec):
        with lease_lock:
            a = by_id(aid)
            cur = (a["batch"].get("paperwork") or {}).get("lease") or {}
            if cur.get("owner") != owner:
                return False
            # Through JSON, as jsonb stores it: a value that cannot be stored fails here too.
            new = json.loads(json.dumps({k: v for k, v in paperwork.items() if k != "lease"}))
            new["lease"] = {"owner": owner,
                            "until": st["clock"] + timedelta(seconds=lease_sec)}
            a["batch"]["paperwork"] = new
            st["saves"] += 1
            return True
    monkeypatch.setattr(main.db, "batch_paperwork_claim", claim)
    monkeypatch.setattr(main.db, "batch_paperwork_save", save)
    monkeypatch.setattr(main.db, "batch_paperwork_pending", lambda limit=20: [
        a["id"] for a in st["approvals"]
        if ((a.get("batch") or {}).get("paperwork") or {}).get("state") == "pending"][:limit])
    monkeypatch.setattr(main.db, "batch_approval_records", lambda bid: [
        copy.deepcopy(a) for a in st["approvals"] if (a.get("batch") or {}).get("batch_id") == bid])
    monkeypatch.setattr(main.db, "get_recipients", lambda pid: [rows[pid]["customer_email"]] + (
        ["ap@acme.com"] if pid == "b" else []))
    monkeypatch.setattr(main.config, "UPLOAD_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(main.config, "PROPOSAL_TOOL_URL", "http://tool.invalid", raising=False)
    monkeypatch.setattr(main.config, "SERVICE_TOKEN", "", raising=False)

    def no_http(*a, **k):
        raise AssertionError("no network in this test")
    monkeypatch.setattr(main.httpx, "get", no_http)
    main._PDF_CACHE.clear()
    main._pdf_cache_put("a", 2, b"%PDF proposal a")
    main._pdf_cache_put("b", 3, b"%PDF proposal b")

    def render(pdf, cert, **k):
        st["certs"].append(cert)
        return b"%PDF-1.7 signed " + cert["proposal_id"].encode() + b"\n%%EOF"
    monkeypatch.setattr(main.signing, "render_signed_contract", render)

    # The deposit invoice runs REAL automations.request_deposit -> issue_deposit_invoice: only its
    # database writes, its renderer and Resend are replaced.
    monkeypatch.setattr(db, "set_deposit_amount",
                        lambda pid, amt: rows[pid].update(deposit_amount=amt))

    def assign(pid):
        """The real function's contract: minted ONCE, the stored number returned after that."""
        if not rows[pid].get("deposit_invoice_no"):
            rows[pid]["deposit_invoice_no"] = "INV-" + pid.upper()
            st["invoices"].append(pid)
        return rows[pid]["deposit_invoice_no"]
    monkeypatch.setattr(db, "assign_invoice_no", assign)
    monkeypatch.setattr(db, "set_deposit_requested",
                        lambda pid: rows[pid].update(deposit_requested_at="now"))
    monkeypatch.setattr(invoice, "invoice_payload", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(invoice, "render_invoice_pdf", lambda payload: b"%PDF invoice")
    monkeypatch.setattr(email_sender, "send_deposit_request",
                        lambda e, *a, **k: st["deposit_emails"].append(e) or True)
    monkeypatch.setattr(email_sender, "proposal_reply_to", lambda t: None)
    monkeypatch.setattr(email_sender, "notify_team", lambda subject, body, **k: st[
        "team"].append({"subject": subject, "body": body, **k}) or [])
    monkeypatch.setattr(email_sender, "send_customer_update",
                        lambda e, url, project, heading, body, **k: st["customer"].append(
                            {"to": e, "heading": heading, "body": body, **k}) or True)
    monkeypatch.setattr(automations, "create_dropbox_folder", lambda *a: None)

    # The scope decision task (one per batch, not one per approval).
    monkeypatch.setattr(followup_worker, "group_members", lambda g, strict=False: [
        copy.deepcopy(r) for r in rows.values() if r["scope_group"] == g])
    monkeypatch.setattr(main.db, "list_open_group_tasks", lambda g: [])
    monkeypatch.setattr(main.db, "reserve_followup_task",
                        lambda pid, key, detail: st["tasks"].append((pid, key, detail)) or 7)
    monkeypatch.setattr(followup_worker, "_send_staff",
                        lambda p, due: st["staff_task_emails"].append(p["proposal_id"]))
    monkeypatch.setattr(followup_worker, "_echo_to_thread", lambda *a, **k: None)

    tc = TestClient(main.app)
    tc.st = st
    yield tc
    main._PDF_CACHE.clear()


def _open(client, pid, rev=None):
    r = client.st["rows"][pid]
    return client.post("/api/portal/%s/document-opened" % r["token"],
                       json={"revision_no": r["current_revision_no"] if rev is None else rev})


def _items(client, spec):
    """spec: {pid: [labels]} -> the body items, with the revision the page holds."""
    return [{"token": client.st["rows"][pid]["token"],
             "revision_no": client.st["rows"][pid]["current_revision_no"],
             "option_labels": labels} for pid, labels in spec.items()]


def _preview(client, spec, anchor="a"):
    return client.post("/api/portal/tok-%s/approve-several/preview" % anchor,
                       json={"items": [{"token": i["token"], "option_labels": i["option_labels"]}
                                       for i in _items(client, spec)]})


def _commit(client, spec, anchor="a", items=None, finish=True, **over):
    """POST the combined approval. `finish`: then run the background job it handed off, in place
    of the thread the route would have started, and return the route's own answer."""
    pv = _preview(client, spec, anchor).json()
    body = {"batch_id": "batch-0000-0001", "items": items or _items(client, spec),
            "name": "Dana Reed", "title": "Owner", "date": "2026-10-31", "consent": True,
            "consent_digest": pv.get("consent_digest")}
    body.update(over)
    for k in [k for k, v in body.items() if v is None]:
        body.pop(k)
    r = client.post("/api/portal/tok-%s/approve-several" % anchor, json=body,
                    headers={"user-agent": "Mozilla/5.0 (Test)",
                             "x-forwarded-for": "203.0.113.9"})
    if finish:
        _run_spawned(client)
    return r


def _run_spawned(client):
    """What the route's thread would have done: one pass over each batch it handed off."""
    while client.st["spawned"]:
        aid, pdfs = client.st["spawned"].pop(0)
        main._run_batch_paperwork(aid, pdfs)


def _nothing_written(st):
    assert st["approvals"] == [], "an approval row was written"
    assert not any(r.get("approved_total") for r in st["rows"].values()), "a row was approved"
    assert st["contracts"] == [] and st["certs"] == [], "a contract was built"
    assert st["messages"] == [], "a chat line was posted"
    assert st["team"] == [] and st["customer"] == [], "an approval email went out"
    assert st["invoices"] == [] and st["deposit_emails"] == [], "an invoice was issued"
    assert st["tasks"] == [], "a scope decision task was raised"


# ── what can be selected ─────────────────────────────────────────────────────
def test_the_list_is_this_customers_proposals_of_this_project_and_nothing_else(world):
    r = world.get("/api/portal/tok-a/approve-several")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True and body["max"] == main.MAX_BATCH
    toks = [p["token"] for p in body["proposals"]]
    assert sorted(toks) == ["tok-a", "tok-b", "tok-c"]
    text = r.text
    # Lab 3 was sent to somebody else; Main Floor is another project. Neither appears AT ALL.
    assert "Lab 3" not in text and "tok-d" not in text and "Main Floor" not in text
    # No proposal id ever reaches the page: tokens are its only identifier.
    assert '"proposal_id"' not in text
    rows = {p["token"]: p for p in body["proposals"]}
    assert rows["tok-a"]["approvable"] is True
    assert rows["tok-a"]["document_required"] is True and rows["tok-a"]["document_opened"] is False
    assert rows["tok-c"]["signing"]["required"] is False
    assert rows["tok-c"]["document_required"] is False, "nothing to sign, nothing to open first"


def test_an_ungrouped_proposal_offers_nothing(world):
    world.st["rows"]["e"]["scope_group"] = None
    r = world.get("/api/portal/tok-e/approve-several").json()
    assert r == {"ok": True, "available": False, "proposals": []}


def test_a_database_without_the_ddl_offers_nothing(world, monkeypatch):
    monkeypatch.setattr(main.db, "approve_several_ready", lambda: False)
    assert world.get("/api/portal/tok-a/approve-several").json()["available"] is False
    r = _commit(world, {"a": ["Lab 1"], "b": ["Lab 2"]})
    assert r.status_code == 503
    _nothing_written(world.st)


# ── the opened-document record ───────────────────────────────────────────────
def test_opening_a_document_is_recorded_for_the_session_at_the_current_revision(world):
    assert _open(world, "a").json() == {"ok": True, "marked": True}
    assert ("a", SESSION, 2) in world.st["opens"]
    # A tab still showing the previous revision vouches for nothing.
    assert _open(world, "b", rev=2).json() == {"ok": True, "marked": False}
    assert not any(o[0] == "b" for o in world.st["opens"])


def test_another_customers_proposal_cannot_be_marked_opened(world):
    r = world.post("/api/portal/tok-d/document-opened", json={"revision_no": 1})
    assert r.status_code == 401
    assert world.st["opens"] == set()


# ── the whole flow ───────────────────────────────────────────────────────────
def test_approving_two_of_three_with_one_signature(world):
    st = world.st
    _open(world, "a")
    _open(world, "b")
    r = _commit(world, {"a": ["Lab 1", "Sealer"], "b": ["Lab 2"]})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["ok"] is True and out["batch_id"] == "batch-0000-0001"
    got = {a["token"]: a for a in out["approved"]}
    # Each proposal's OWN total from its OWN selection, and its own deposit.
    assert got["tok-a"]["total"] == 13000.0 and got["tok-a"]["deposit_amount"] == 3250.0
    assert got["tok-b"]["total"] == 8000.0 and got["tok-b"]["deposit_amount"] == 2000.0
    assert got["tok-a"]["invoice_no"] == "INV-A" and got["tok-b"]["invoice_no"] == "INV-B"
    # The answer came before the contracts: they are reported by /status once stored.
    assert out["paperwork"] == "pending"
    assert got["tok-a"]["contract_ready"] is False and got["tok-b"]["contract_ready"] is False
    status = world.get("/api/portal/tok-a/approve-several/status",
                       params={"batch_id": "batch-0000-0001"}).json()
    assert status["paperwork"] == "done"
    assert {s["token"]: s["contract_ready"] for s in status["approved"]} == {"tok-a": True,
                                                                             "tok-b": True}

    # TWO approval rows, the SAME signing event on both.
    assert [a["proposal_id"] for a in st["approvals"]] == ["a", "b"]
    a, b = st["approvals"]
    for k in ("name", "title", "ip", "user_agent", "approver_email", "consent_version"):
        assert a[k] == b[k], k
    # The same batch record on both; the first one alone also carries the job's own record.
    assert {k: v for k, v in a["batch"].items() if k != "paperwork"} == b["batch"]
    assert a["batch"]["paperwork"]["state"] == "done" and "paperwork" not in b["batch"]
    assert a["ip"] == "203.0.113.9" and a["user_agent"] == "Mozilla/5.0 (Test)"
    assert a["consent_version"] == signing.COMBINED_CONSENT_VERSION
    assert a["revision_no"] == 2 and b["revision_no"] == 3
    assert a["contract_sha256"] == signing.sha256_hex(b"%PDF proposal a")
    assert b["contract_sha256"] == signing.sha256_hex(b"%PDF proposal b")
    assert a["options"] == ["Lab 1", "Sealer"] and b["options"] == ["Lab 2"]
    assert a["batch"]["count"] == 2
    assert [p["proposal_id"] for p in a["batch"]["proposals"]] == ["a", "b"]
    assert st["rows"]["c"]["proposal_status"] == "viewed", "the third proposal stays open"

    # TWO contracts, each standalone: its own proposal, price, options and fingerprint, and the
    # combined sentence naming both.
    certs = {c["proposal_id"]: c for c in st["certs"]}
    assert set(certs) == {"a", "b"}
    assert certs["a"]["total"] == 13000.0 and certs["b"]["total"] == 8000.0
    assert certs["a"]["options_summary"] == "Lab 1, Sealer"
    assert certs["a"]["deposit_amount"] == 3250.0
    assert certs["a"]["proposal_pdf_sha256"] == signing.sha256_hex(b"%PDF proposal a")
    for c in certs.values():
        assert c["consent_version"] == signing.COMBINED_CONSENT_VERSION
        assert "Acme HQ - Lab 1 ($13,000.00)" in c["consent_text"]
        assert "Acme HQ - Lab 2 ($8,000.00)" in c["consent_text"]
        assert c["combined_approval"]["count"] == 2
        assert c["combined_approval"]["proposals"] == ["Acme HQ - Lab 1", "Acme HQ - Lab 2"]
    assert {c["proposal_id"] for c in st["contracts"]} == {"a", "b"}
    assert {c["approval_id"] for c in st["contracts"]} == {a["id"], b["id"]}
    # ...and each contract in its OWN thread, where both sides go back to it.
    posted = {m["pid"]: m for m in st["messages"]
              if m["type"] == "text" and (m["meta"] or {}).get("attachments")}
    assert set(posted) == {"a", "b"}
    assert posted["a"]["meta"]["attachments"][0]["name"] == "Acme HQ - Lab 1 - Signed Contract.pdf"

    # TWO invoices, issued exactly as a single approval issues them, and their emails folded into
    # the one confirmation instead of going out on their own.
    assert sorted(st["invoices"]) == ["a", "b"]
    assert st["deposit_emails"] == [], "each invoice went out in an email of its own"
    cards = [m for m in st["messages"] if m["type"] == "deposit_request"]
    assert sorted(m["pid"] for m in cards) == ["a", "b"]
    assert st["rows"]["a"]["deposit_requested_at"] and st["rows"]["b"]["deposit_requested_at"]

    # ONE staff email, listing both, carrying both contracts.
    assert len(st["team"]) == 1
    t = st["team"][0]
    assert t["kind"] == "approved"
    assert "Acme HQ - Lab 1" in t["body"] and "Acme HQ - Lab 2" in t["body"]
    assert len(t["attachments"]) == 2

    # ONE email per contact, listing only the proposals that contact is on.
    by_to = {}
    for m in st["customer"]:
        by_to.setdefault(m["to"], []).append(m)
    assert sorted(by_to) == ["ap@acme.com", SESSION]
    assert all(len(v) == 1 for v in by_to.values()), "a contact got more than one email"
    dana = by_to[SESSION][0]
    assert dana["heading"] == "Thank you — your proposals are approved"
    assert "INV-A" in dana["body"] and "INV-B" in dana["body"]
    assert len(dana["attachments"]) == 4, "both contracts and both invoices"
    ap = by_to["ap@acme.com"][0]
    assert "Lab 2" in ap["body"] and "Lab 1" not in ap["body"], (
        "a contact on one proposal heard about another one")
    assert len(ap["attachments"]) == 2
    assert ap["heading"] == "This proposal has been approved"

    # Each thread records its own approval. It names the other proposal only where everybody on
    # the thread was sent that one too: ap@ reads b's thread and was never sent Lab 1.
    lines = {m["pid"]: m["body"] for m in st["messages"]
             if m["type"] == "system" and m["body"].startswith("Approved by")}
    assert "Total $13,000.00" in lines["a"] and "together with Lab 2." in lines["a"]
    assert "Total $8,000.00" in lines["b"] and "Lab 1" not in lines["b"]
    # The full list is still on b, for staff only.
    internal = [m for m in st["messages"] if m["pid"] == "b" and (m["meta"] or {}).get("internal")
                and "combined approval" in m["body"]]
    assert len(internal) == 1 and "Lab 1, Lab 2" in internal[0]["body"]
    assert not any((m["meta"] or {}).get("internal") and "combined approval" in m["body"]
                   for m in st["messages"] if m["pid"] == "a"), "a names everything already"
    # ONE decision task for the open rest of the project, not one per approval.
    assert len(st["tasks"]) == 1
    assert [s["proposal_id"] for s in st["tasks"][0][2]["scopes"]] == ["c", "d"]


def test_a_mixed_batch_signs_only_what_can_be_signed(world):
    """Gyp has no signature block: in a batch it is approved without one, exactly as on its own.
    The sentence names only the signable proposal and no contract is built for the other."""
    st = world.st
    _open(world, "a")
    pv = _preview(world, {"a": ["Lab 1"], "c": ["Breakroom"]}).json()
    assert "Lab 1" in pv["consent_text"] and "Breakroom" not in pv["consent_text"]
    r = _commit(world, {"a": ["Lab 1"], "c": ["Breakroom"]})
    assert r.status_code == 200, r.text
    rec = {x["proposal_id"]: x for x in st["approvals"]}
    assert rec["a"]["consent_version"] == signing.COMBINED_CONSENT_VERSION
    assert rec["c"]["consent_version"] is None and rec["c"]["contract_sha256"] is None
    assert [c["proposal_id"] for c in st["certs"]] == ["a"]
    assert {x["token"]: x["signed"] for x in r.json()["approved"]} == {"tok-a": True,
                                                                         "tok-c": False}


def test_an_unsignable_only_batch_needs_no_consent(world):
    world.st["drafts"]["b"]["work_type"] = "gyp"
    r = _commit(world, {"b": ["Lab 2"], "c": ["Breakroom"]}, consent=None, consent_digest=None)
    assert r.status_code == 200, r.text
    assert world.st["certs"] == []


def test_a_proposal_without_deposit_gets_no_invoice_and_the_contacts_prompt(world):
    st = world.st
    st["rows"]["b"]["deposit_required"] = False
    _open(world, "a")
    _open(world, "b")
    r = _commit(world, {"a": ["Lab 1"], "b": ["Lab 2"]})
    assert r.status_code == 200, r.text
    got = {a["token"]: a for a in r.json()["approved"]}
    assert got["tok-b"]["deposit_amount"] is None and got["tok-b"]["invoice_no"] is None
    assert st["invoices"] == ["a"]
    assert any(m["pid"] == "b" and "project contacts" in m["body"] for m in st["messages"])


def test_a_double_submit_returns_the_first_result_and_writes_nothing_more(world):
    _open(world, "a")
    _open(world, "b")
    first = _commit(world, {"a": ["Lab 1"], "b": ["Lab 2"]})
    assert first.status_code == 200
    n = (len(world.st["approvals"]), len(world.st["team"]), len(world.st["customer"]))
    again = world.post("/api/portal/tok-a/approve-several", json={
        "batch_id": "batch-0000-0001", "items": _items(world, {"a": ["Lab 1"], "b": ["Lab 2"]}),
        "name": "Dana Reed", "consent": True, "consent_digest": "x"})
    assert again.status_code == 200 and again.json()["replayed"] is True
    assert (len(world.st["approvals"]), len(world.st["team"]), len(world.st["customer"])) == n
    # A DIFFERENT batch over the same proposals is a refusal: they are already approved.
    other = world.post("/api/portal/tok-a/approve-several", json={
        "batch_id": "batch-0000-0002", "items": _items(world, {"a": ["Lab 1"], "b": ["Lab 2"]}),
        "name": "Dana Reed", "consent": True, "consent_digest": "x"})
    assert other.status_code == 409


# ── every gate refuses the WHOLE batch and writes nothing ────────────────────
def _setup_not_opened(w):
    _open(w, "a")                       # b never opened


def _setup_approved(w):
    _open(w, "a"); _open(w, "b")
    w.st["rows"]["b"].update(proposal_status="approved", approved_at="2026-10-30T10:00:00+00:00")


def _setup_signed_then_lost(w):
    _open(w, "a"); _open(w, "b")
    w.st["rows"]["b"].update(proposal_status="closed_lost", approved_at="2026-10-30T10:00:00+00:00")


def _setup_closed(w):
    _open(w, "a"); _open(w, "b")
    w.st["rows"]["b"]["proposal_status"] = "closed_lost"


def _setup_deleted(w):
    _open(w, "a"); _open(w, "b")
    w.st["rows"]["b"]["deleted_at"] = "2026-10-30T10:00:00+00:00"


def _setup_revised_since_opened(w):
    _open(w, "a"); _open(w, "b")
    w.st["rows"]["b"]["current_revision_no"] = 4      # a re-send after the open


def _setup_no_pricing(w):
    _open(w, "a"); _open(w, "b")
    w.st["drafts"]["b"]["rooms"] = []


@pytest.mark.parametrize("setup,code,status", [
    (_setup_not_opened, "not_opened", 409),
    (_setup_approved, "already_approved", 409),
    (_setup_signed_then_lost, "already_approved", 409),
    (_setup_closed, "closed", 409),
    (_setup_deleted, "unavailable", 409),
    (_setup_revised_since_opened, "revised", 409),
    (_setup_no_pricing, "no_pricing", 400),
])
def test_a_proposal_failing_a_gate_refuses_the_whole_batch(world, setup, code, status):
    items = _items(world, {"a": ["Lab 1"], "b": ["Lab 2"]})   # the page's view, taken BEFORE
    setup(world)
    r = _commit(world, {"a": ["Lab 1"], "b": ["Lab 2"]}, items=items)
    assert r.status_code == status, r.text
    body = r.json()
    assert body["error"] == "batch_refused"
    assert code in [x["code"] for x in body["reasons"]], body
    assert world.st["batch_calls"] == [], "the commit was attempted"
    _nothing_written(world.st)


def test_a_revision_the_page_never_held_is_refused_even_when_opened(world):
    """The open record is for the CURRENT revision; the page must also be submitting that one."""
    _open(world, "a"); _open(world, "b")
    items = _items(world, {"a": ["Lab 1"], "b": ["Lab 2"]})
    items[1]["revision_no"] = 2
    r = _commit(world, {"a": ["Lab 1"], "b": ["Lab 2"]}, items=items)
    assert r.status_code == 409 and "revised" in [x["code"] for x in r.json()["reasons"]]
    _nothing_written(world.st)


@pytest.mark.parametrize("pid", ["d", "e"])
def test_a_proposal_from_outside_this_customers_project_refuses_without_naming_it(world, pid):
    """d was sent to somebody else; e is another project. Either refuses the batch, and the
    refusal never says what the proposal is."""
    _open(world, "a")
    world.st["opens"].add((pid, SESSION, 1))
    items = _items(world, {"a": ["Lab 1"], pid: ["x"]})
    r = _commit(world, {"a": ["Lab 1"]}, items=items)
    assert r.status_code == 409
    assert world.st["rows"][pid]["scope_label"] not in r.text
    assert "unavailable" in [x["code"] for x in r.json()["reasons"]]
    _nothing_written(world.st)


@pytest.mark.parametrize("over,code", [
    ({"consent": None}, "consent"),
    ({"consent": "true"}, "consent"),
    ({"consent": 1}, "consent"),
    ({"consent_digest": "0" * 64}, "consent_changed"),
    ({"name": ""}, "name"),
])
def test_the_signature_itself_is_checked_before_anything_is_written(world, over, code):
    _open(world, "a"); _open(world, "b")
    r = _commit(world, {"a": ["Lab 1"], "b": ["Lab 2"]}, **over)
    assert r.status_code == 400, r.text
    assert code in [x["code"] for x in r.json()["reasons"]]
    _nothing_written(world.st)


def test_a_changed_selection_changes_the_sentence_and_the_old_tick_does_not_carry(world):
    _open(world, "a"); _open(world, "b")
    old = _preview(world, {"a": ["Lab 1"], "b": ["Lab 2"]}).json()["consent_digest"]
    r = world.post("/api/portal/tok-a/approve-several", json={
        "batch_id": "batch-0000-0003", "name": "Dana Reed", "consent": True,
        "consent_digest": old, "items": _items(world, {"a": ["Lab 1", "Sealer"], "b": ["Lab 2"]})})
    assert r.status_code == 400
    assert "consent_changed" in [x["code"] for x in r.json()["reasons"]]
    _nothing_written(world.st)


@pytest.mark.parametrize("items_spec,error", [
    ({"a": ["Lab 1"]}, "batch_size"),
    ({"a": ["Lab 1"], "b": ["Nope"]}, "batch_refused"),
    ({"a": [], "b": ["Lab 2"]}, "batch_refused"),
])
def test_bad_selections_are_refused(world, items_spec, error):
    _open(world, "a"); _open(world, "b")
    r = _commit(world, items_spec)
    assert r.status_code == 400 and r.json()["error"] == error, r.text
    _nothing_written(world.st)


def test_the_same_proposal_twice_is_refused(world):
    _open(world, "a")
    items = _items(world, {"a": ["Lab 1"]}) * 2
    r = _commit(world, {"a": ["Lab 1"]}, items=items)
    assert r.status_code == 400 and r.json()["error"] == "duplicate_proposal"
    _nothing_written(world.st)


def test_more_than_the_cap_is_refused(world, monkeypatch):
    monkeypatch.setattr(main, "MAX_BATCH", 2)
    _open(world, "a"); _open(world, "b")
    r = _commit(world, {"a": ["Lab 1"], "b": ["Lab 2"], "c": ["Breakroom"]})
    assert r.status_code == 400 and r.json()["error"] == "batch_size"
    _nothing_written(world.st)


def test_an_unreadable_project_refuses_the_batch(world, monkeypatch):
    def boom(g):
        raise RuntimeError("drafts unreadable")
    _open(world, "a"); _open(world, "b")
    pv = _preview(world, {"a": ["Lab 1"], "b": ["Lab 2"]}).json()
    monkeypatch.setattr(main.db, "list_scope_group_draft_states", boom)
    r = world.post("/api/portal/tok-a/approve-several", json={
        "batch_id": "batch-0000-0004", "name": "Dana Reed", "consent": True,
        "consent_digest": pv["consent_digest"],
        "items": _items(world, {"a": ["Lab 1"], "b": ["Lab 2"]})})
    assert r.status_code == 503
    _nothing_written(world.st)


# ── the commit point ─────────────────────────────────────────────────────────
def test_a_state_change_under_the_lock_refuses_everything(world, monkeypatch):
    """Between the check and the commit somebody approved Lab 2 in another tab. The lock re-check
    catches it and the whole batch is refused: Lab 1 is not approved on its own."""
    _open(world, "a"); _open(world, "b")
    world.st["batch_error"] = db.BatchRefused([{"proposal_id": "b", "code": "already_approved"}])
    r = _commit(world, {"a": ["Lab 1"], "b": ["Lab 2"]})
    assert r.status_code == 409
    reasons = r.json()["reasons"]
    assert reasons == [{"token": "tok-b", "label": "Lab 2", "code": "already_approved",
                        "message": "Lab 2 is already approved."}]
    assert len(world.st["batch_calls"]) == 1
    _nothing_written(world.st)


def test_a_failure_inside_the_commit_writes_nothing(world):
    _open(world, "a"); _open(world, "b")
    world.st["batch_error"] = RuntimeError("column \"batch\" does not exist")
    r = _commit(world, {"a": ["Lab 1"], "b": ["Lab 2"]})
    assert r.status_code == 503
    assert "Nothing was approved" in r.json()["message"]
    _nothing_written(world.st)


def test_a_contract_that_cannot_be_built_costs_the_document_not_the_approval(world, monkeypatch):
    def refuse(pdf, cert, **k):
        raise signing.ContractUnavailable("proposal tool returned 502")
    monkeypatch.setattr(main.signing, "render_signed_contract", refuse)
    _open(world, "a"); _open(world, "b")
    r = _commit(world, {"a": ["Lab 1"], "b": ["Lab 2"]})
    assert r.status_code == 200
    assert all(a["contract_ready"] is False for a in r.json()["approved"])
    assert len(world.st["approvals"]) == 2
    # Retried by the sweeps that follow, then given up on and the emails sent without them.
    for _ in range(main._PAPERWORK_MAX_ATTEMPTS):
        main._paperwork_sweep()
    assert "could not be built yet" in world.st["team"][0]["body"]


# ── the signed contract stays rebuildable on its own ─────────────────────────
def test_a_combined_contract_rebuilds_with_the_sentence_it_was_signed_under(world, monkeypatch):
    _open(world, "a"); _open(world, "b")
    assert _commit(world, {"a": ["Lab 1"], "b": ["Lab 2"]}).status_code == 200
    signed_text = {c["proposal_id"]: c["consent_text"] for c in world.st["certs"]}
    world.st["certs"].clear()
    rec = world.st["approvals"][0]
    rec["signed_at"] = "2026-10-31T15:00:00+00:00"
    monkeypatch.setattr(main.db, "latest_approval_record", lambda pid: dict(rec))
    monkeypatch.setattr(main.db, "get_signed_contract", lambda pid: None)   # never stored
    pdf, err, status = main._signed_contract_pdf(dict(world.st["rows"]["a"]))
    assert status == 200, err
    assert world.st["certs"][0]["consent_text"] == signed_text["a"]
    assert world.st["certs"][0]["combined_approval"]["count"] == 2


def test_the_combined_sentence_cannot_be_certified_without_its_list():
    with pytest.raises(signing.UnknownConsentVersion):
        signing.consent_text("Acme HQ", signing.COMBINED_CONSENT_VERSION)
    with pytest.raises(signing.UnknownConsentVersion):
        signing.build_certificate(
            project_name="Acme HQ", proposal_id="a", revision_no=1, signer_name="Dana",
            signer_title=None, signer_email="d@x", signed_at=main._now_utc(), ip_address="1",
            user_agent="u", options_summary="x", total=1.0, deposit_amount=None,
            proposal_pdf_sha256="", revision_sha="",
            consent_version=signing.COMBINED_CONSENT_VERSION, combined={"signed": []})


def test_single_approve_is_unchanged(world, monkeypatch):
    """The one-proposal path keeps its own writes, its own consent version and its own emails."""
    calls = {"add": [], "batch": []}
    monkeypatch.setattr(main.db, "add_approval",
                        lambda pid, *a, **k: calls["add"].append((pid, k)) or 55)
    monkeypatch.setattr(main.db, "set_approved", lambda pid, total, *a, **k: world.st["rows"][
        pid].update(proposal_status="approved", deposit_amount=k.get("deposit_amount")))
    r = world.post("/api/portal/tok-a/approve", json={
        "name": "Dana Reed", "option_labels": ["Lab 1"], "consent": True})
    assert r.status_code == 200
    assert world.st["batch_calls"] == []
    assert calls["add"][0][1]["consent_version"] == signing.CONSENT_VERSION
    assert world.st["certs"][0]["consent_version"] == signing.CONSENT_VERSION
    assert "combined_approval" not in world.st["certs"][0]
    # Its invoice still goes out in its own email.
    assert world.st["deposit_emails"] == [SESSION]
    # ...and all of it inside the request, as before: nothing handed to the background.
    assert world.st["spawned"] == []
    assert [c["proposal_id"] for c in world.st["contracts"]] == ["a"]
    assert world.st["team"] and world.st["team"][0]["kind"] == "approved"


# ── review fixes (2026-10-31) ────────────────────────────────────────────────
def _batch_commits_lab1_meanwhile(world):
    """What a combined approval of Lab 1 + Sealer and Lab 2 leaves behind when it commits."""
    st = world.st
    for pid, total, opts in (("a", 13000.0, ["Lab 1", "Sealer"]), ("b", 8000.0, ["Lab 2"])):
        st["next_id"] += 1
        st["approvals"].append({"proposal_id": pid, "id": st["next_id"], "total": total,
                                "options": opts, "batch": {"batch_id": "batch-0000-0001"}})
        st["rows"][pid].update(proposal_status="approved", approved_at="2026-10-31T15:00:00+00:00",
                               approved_total=total, approved_options=opts,
                               deposit_amount=round(total * 0.25, 2))


def test_a_single_approval_that_loses_to_a_batch_writes_nothing(world, monkeypatch):
    """Review finding: single approve checked approved_at, then spent up to 30 s fetching the PDF,
    then wrote with no lock. A batch committing Lab 1 inside that window was followed by a second
    approval row at another price and an overwritten approved_total. The writes now go through
    db.approve_single, which re-checks under the row lock (its SQL: test_pg_approve_batch.py)."""
    st = world.st
    monkeypatch.setattr(main.db, "add_approval", lambda pid, *a, **k: st["approvals"].append(
        {"proposal_id": pid, "total": a[3], "single": True}) or 99)
    monkeypatch.setattr(main.db, "set_approved", lambda pid, total, *a, **k: st["rows"][
        pid].update(proposal_status="approved", approved_total=total,
                    deposit_amount=k.get("deposit_amount")))

    def approve_single(pid, approval, approved):
        """The real function's contract: re-check under the lock, write only if still open."""
        r = st["rows"][pid]
        if r["proposal_status"] == "approved" or r.get("approved_at"):
            return False, None
        return True, db.write_single_approval(pid, approval, approved)
    monkeypatch.setattr(main.db, "approve_single", approve_single, raising=False)

    def slow_pdf(p, timeout=None):
        _batch_commits_lab1_meanwhile(world)      # the batch commits during the cold render
        return b"%PDF proposal a"
    monkeypatch.setattr(main, "_proposal_pdf_bytes", slow_pdf)

    r = world.post("/api/portal/tok-a/approve", json={
        "name": "Ann Colleague", "option_labels": ["Lab 1"], "consent": True})
    assert r.status_code == 200 and r.json() == {"ok": True, "already_approved": True}
    assert [a for a in st["approvals"] if a["proposal_id"] == "a" and a.get("single")] == [], (
        "a second approval row was written for Lab 1")
    assert st["rows"]["a"]["approved_total"] == 13000.0, "the batch's price was overwritten"
    assert st["certs"] == [] and st["contracts"] == [], "a second contract was built"
    assert st["team"] == [] and st["customer"] == [] and st["invoices"] == []


@pytest.mark.parametrize("same_batch", [True, False])
def test_a_copy_refused_under_the_lock_reports_what_the_first_copy_did(world, monkeypatch,
                                                                       same_batch):
    """Review finding: a duplicate of batch X that passed phase 0 while the first copy was
    committing reached the lock, met already_approved on every item and answered "Nothing was
    approved" over an approval that went through. It now answers with the first result. A
    DIFFERENT batch over the same proposals is still a refusal."""
    st = world.st
    _open(world, "a"); _open(world, "b")
    fake = main.db.approve_batch

    def first_copy_commits_first(items):
        first = copy.deepcopy(items)
        if not same_batch:
            for it in first:
                it["approval"]["batch"] = dict(it["approval"]["batch"], batch_id="batch-other-01")
        fake(first)
        return fake(items)
    monkeypatch.setattr(main.db, "approve_batch", first_copy_commits_first)
    r = _commit(world, {"a": ["Lab 1", "Sealer"], "b": ["Lab 2"]})
    if same_batch:
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["replayed"] is True and body["batch_id"] == "batch-0000-0001"
        assert {x["token"]: x["total"] for x in body["approved"]} == {"tok-a": 13000.0,
                                                                       "tok-b": 8000.0}
    else:
        assert r.status_code == 409
        assert r.json()["message"] == main.BATCH_REFUSED_MESSAGE
    assert len(st["approvals"]) == 2
    assert st["team"] == [] and st["customer"] == [] and st["certs"] == []


def test_each_proposals_own_roster_decides_who_hears_about_it(world, monkeypatch):
    """Review finding: the one staff email was addressed from the FIRST proposal's per-project
    overrides only. Kyle, added on Lab 2 alone, heard nothing of Lab 2; Will, muted on Lab 1,
    missed Lab 2 too. Now each proposal's roster is resolved with its own overrides, and each
    person gets ONE email listing only the proposals they would have heard about."""
    st = world.st
    monkeypatch.setattr(db, "list_notify_recipients", lambda: [
        {"email": "hanz@wetreadwell.com", "kind": "general", "enabled": True},
        {"email": "will@wetreadwell.com", "kind": "general", "enabled": True}])
    overrides = {"a": [{"email": "will@wetreadwell.com", "mode": "mute"}],
                 "b": [{"email": "kyle@wetreadwell.com", "mode": "add"}]}
    monkeypatch.setattr(db, "list_notify_overrides", lambda pid: overrides.get(pid, []))
    _open(world, "a"); _open(world, "b")
    assert _commit(world, {"a": ["Lab 1"], "b": ["Lab 2"]}).status_code == 200
    got = {}
    for t in st["team"]:
        assert t["kind"] == "approved"
        for addr in t.get("recipients") or []:
            got.setdefault(addr, []).append(t)
    assert sorted(got) == ["hanz@wetreadwell.com", "kyle@wetreadwell.com",
                           "will@wetreadwell.com"]
    assert all(len(v) == 1 for v in got.values()), "somebody got more than one email"
    hanz = got["hanz@wetreadwell.com"][0]
    assert "Acme HQ - Lab 1" in hanz["body"] and "Acme HQ - Lab 2" in hanz["body"]
    assert len(hanz["attachments"]) == 2
    for who in ("kyle@wetreadwell.com", "will@wetreadwell.com"):
        t = got[who][0]
        assert "Acme HQ - Lab 2" in t["body"] and "Acme HQ - Lab 1" not in t["body"], who
        assert [n for n, _ in t["attachments"]] == ["Acme HQ - Lab 2 - Signed Contract.pdf"]
    assert len(st["team"]) == 2, "kyle and will hear about the same proposal: one send"


def test_one_email_never_carries_more_than_the_attachment_cap(world, monkeypatch):
    """Review finding: every contract (and for the customer every invoice) rode ONE email, with no
    size check; past Resend's 40 MB the one send failed and took every proposal's paperwork with
    it. Past the cap a proposal's documents are linked instead of attached."""
    st = world.st
    monkeypatch.setattr(main, "_COMBINED_ATT_TOTAL", 30, raising=False)  # one contract fits
    _open(world, "a"); _open(world, "b")
    assert _commit(world, {"a": ["Lab 1"], "b": ["Lab 2"]}).status_code == 200
    staff = st["team"][0]
    assert [n for n, _ in staff["attachments"]] == ["Acme HQ - Lab 1 - Signed Contract.pdf"]
    assert "Acme HQ - Lab 2</strong> is too large to attach" in staff["body"]
    dana = next(m for m in st["customer"] if m["to"] == SESSION)
    assert len(dana["attachments"]) == 2, "Lab 1's contract and invoice only"
    assert "/p/tok-b" in dana["body"] and "download them from its page" in dana["body"]
    lab2 = next(li for li in dana["body"].split("<li>") if "Acme HQ - Lab 2" in li)
    assert "INV-B" in lab2 and "(attached)" not in lab2, "says attached, is not"
    # ap@ is on Lab 2 alone: its documents fit their own email.
    ap = next(m for m in st["customer"] if m["to"] == "ap@acme.com")
    assert len(ap["attachments"]) == 2 and "download them from its page" not in ap["body"]


def test_the_decision_task_names_every_proposal_the_batch_approved(world, monkeypatch):
    """Review finding: the one scope-decision task and its email named only the first approved
    proposal, so staff read "Lab 1 was approved" and could think Lab 2 was still waiting."""
    st = world.st
    seen = []
    monkeypatch.setattr(followup_worker, "_send_staff", lambda p, due: seen.append(p))
    _open(world, "a"); _open(world, "b")
    assert _commit(world, {"a": ["Lab 1"], "b": ["Lab 2"]}).status_code == 200
    (_pid, _key, detail), = st["tasks"]
    assert detail["approved_scope"]["label"] == "Lab 1, Lab 2"
    assert [a["label"] for a in detail["approved_scopes"]] == ["Lab 1", "Lab 2"]
    assert [s["proposal_id"] for s in detail["scopes"]] == ["c", "d"]
    dec = seen[0]["_decision"]
    assert dec["approved_label"] == "Lab 1, Lab 2" and dec["approved_count"] == 2


def test_the_decision_email_says_scopes_when_several_were_approved(monkeypatch):
    sent = []
    monkeypatch.setattr(followup_worker.email_sender, "notify_team",
                        lambda subject, body, **k: sent.append(body) or ["x"])
    monkeypatch.setattr(followup_worker.email_sender, "proposal_reply_to", lambda t: None)
    due = followup_worker.rules.Due("k", "staff", "staff_scope_decision")
    base = {"proposal_id": "a", "assigned_estimator": "kyle@wetreadwell.com",
            "project_name": "Acme HQ", "customer_name": "Dana Reed"}
    followup_worker._send_staff(dict(base, _decision={
        "approved_label": "Lab 1, Lab 2", "approved_count": 2, "scopes": []}), due)
    followup_worker._send_staff(dict(base, _decision={
        "approved_label": "Lab 1", "scopes": []}), due)
    assert "<strong>Lab 1, Lab 2</strong> scopes of" in sent[0]
    assert "<strong>Lab 1</strong> scope of" in sent[1]


def test_two_signable_proposals_with_one_name_are_refused(world):
    """Review finding: one consent sentence naming "Acme HQ - Lab 1" twice cannot say which
    document was signed at which price. Refused, nothing written."""
    world.st["rows"]["b"]["scope_label"] = "Lab 1"
    _open(world, "a"); _open(world, "b")
    r = _commit(world, {"a": ["Lab 1"], "b": ["Lab 2"]})
    assert r.status_code == 400, r.text
    assert "same_name" in [x["code"] for x in r.json()["reasons"]]
    _nothing_written(world.st)


def test_the_chat_line_tells_proposals_apart_by_id_not_by_label(world):
    """Review finding: `others` filtered by label text, so two proposals sharing a label each read
    "together with ." with an empty list."""
    world.st["rows"]["c"]["scope_label"] = "Lab 1"     # Gyp: unsigned, so no same_name refusal
    _open(world, "a")
    assert _commit(world, {"a": ["Lab 1"], "c": ["Breakroom"]}).status_code == 200, "commit"
    lines = {m["pid"]: m["body"] for m in world.st["messages"]
             if m["type"] == "system" and m["body"].startswith("Approved by")}
    assert lines["a"].endswith("together with Lab 1.") and lines["c"].endswith(
        "together with Lab 1."), lines
    assert "together with ." not in " ".join(lines.values())


# ── the paperwork in the background (2026-10-31) ─────────────────────────────
# Hanz: lift the 6-proposal limit by building the paperwork in the background. The request answers
# once the approvals commit; the contracts, invoices, chat lines and emails follow, durably: a
# restart resumes the job, a second worker cannot double it, and a failed render is retried, then
# reported, without holding up the rest.
class _ProcessDied(BaseException):
    """The process going down mid-render: not an Exception, so nothing on the way out catches it
    and the job is left exactly as a killed container leaves it."""


def _many(world, n):
    """n more of Dana's proposals in the same project, each signable, each opened, its PDF cached.
    Returns the spec for _commit."""
    st = world.st
    spec = {}
    for i in range(1, n + 1):
        pid, label = "p%02d" % i, "Room %02d" % i
        st["rows"][pid] = _row(pid, label, 1)
        st["drafts"][pid] = _draft(label, rooms=[{"name": label, "is_base": True,
                                                  "bid": {"total": 1000.0 * i}}])
        main._pdf_cache_put(pid, 1, b"%PDF proposal " + pid.encode())
        _open(world, pid)
        spec[pid] = [label]
    return spec


def _built(st):
    """approval id -> how many times a BUILT contract was stored for it."""
    out = {}
    for c in st["contracts"]:
        if c.get("pdf"):
            out[c["approval_id"]] = out.get(c["approval_id"], 0) + 1
    return out


def _one_of_everything(st, pids, emails=(SESSION,)):
    """The finished batch: one contract per approval, one invoice, one deposit card, one approval
    line and one thread copy per proposal, ONE staff email, ONE email per contact."""
    ids = {a["proposal_id"]: a["id"] for a in st["approvals"]}
    assert set(_built(st)) == {ids[p] for p in pids}, "a contract is missing"
    assert all(n == 1 for n in _built(st).values()), ("a contract was built twice", _built(st))
    assert sorted(st["invoices"]) == sorted(pids), "an invoice number was minted twice"
    for kind, pick in (("deposit card", lambda m: m["type"] == "deposit_request"),
                       ("approval line", lambda m: m["type"] == "system"
                        and m["body"].startswith("Approved by")),
                       ("thread copy", lambda m: m["type"] == "text"
                        and (m["meta"] or {}).get("system_doc"))):
        got = sorted(m["pid"] for m in st["messages"] if pick(m))
        assert got == sorted(pids), (kind, got)
    assert len(st["team"]) == 1, "staff got more than one email"
    assert sorted(m["to"] for m in st["customer"]) == sorted(emails), "a contact got two emails"
    assert st["deposit_emails"] == [], "an invoice went out in an email of its own"


def _anchor(st):
    return st["approvals"][0]


def _signed_unless(pid_failing, real, exc=None):
    """A renderer that refuses one proposal's contract and builds every other one."""
    def render(pdf, cert, **k):
        if cert["proposal_id"] == pid_failing:
            raise exc or signing.ContractUnavailable("LibreOffice timed out")
        return real(pdf, cert, **k)
    return render


def test_twelve_proposals_answer_before_any_render_and_finish_in_the_background(world,
                                                                                 monkeypatch):
    """The 12-proposal batch the old cap refused. The renders are slow (as the tool's are); the
    answer does not wait for one of them, and the real background thread then does all of it,
    never more than two renders at once."""
    st = world.st
    spec = _many(world, 12)
    gate = {"now": 0, "max": 0}
    lock = threading.Lock()

    def slow_render(pdf, cert, **k):
        with lock:
            gate["now"] += 1
            gate["max"] = max(gate["max"], gate["now"])
        time.sleep(0.15)
        with lock:
            gate["now"] -= 1
        st["certs"].append(cert)
        return b"%PDF-1.7 signed " + cert["proposal_id"].encode() + b"\n%%EOF"
    monkeypatch.setattr(main.signing, "render_signed_contract", slow_render)
    started = []

    def spawn(aid, pdfs):
        t = threading.Thread(target=main._paperwork_thread, args=(aid, pdfs), daemon=True)
        started.append(t)
        t.start()
    monkeypatch.setattr(main, "_paperwork_spawn", spawn)

    t0 = time.monotonic()
    r = _commit(world, spec, anchor="p01", finish=False)
    took = time.monotonic() - t0
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["paperwork"] == "pending"
    assert len(body["approved"]) == 12
    # Every deposit's number is there at once, so every Pay link works now.
    for a in body["approved"]:
        assert a["invoice_no"] == "INV-" + a["token"][4:].upper(), a
        assert a["deposit_amount"] and a["deposit_required"] is True
        assert a["signed"] is True and a["contract_ready"] is False
    # 12 renders at 0.15 s, two at a time, is 0.9 s at the least: the answer did not wait.
    assert took < 0.9, "the request waited for the renders (%.2fs)" % took

    assert len(started) == 1
    started[0].join(30)
    assert not started[0].is_alive(), "the background job did not finish"
    # The proposal tool renders two at once (LibreOffice); the background never asks for more.
    assert 1 <= gate["max"] <= 2, "%d renders at once" % gate["max"]
    assert _anchor(st)["batch"]["paperwork"]["state"] == "done"
    _one_of_everything(st, list(spec))
    staff = st["team"][0]
    assert staff["subject"].startswith("12 proposals APPROVED")
    assert len(staff["attachments"]) == 12
    dana = st["customer"][0]
    assert len(dana["attachments"]) == 24, "12 contracts and 12 invoices"
    status = world.get("/api/portal/tok-p01/approve-several/status",
                       params={"batch_id": "batch-0000-0001"}).json()
    assert status["paperwork"] == "done"
    assert all(s["contract_ready"] for s in status["approved"]) and len(status["approved"]) == 12


def test_an_invoice_can_be_downloaded_the_moment_the_approval_answers(world):
    """Pay works at once: the invoice number and amount are stored at commit, and the invoice
    document is rendered from exactly those."""
    _open(world, "a"); _open(world, "b")
    r = _commit(world, {"a": ["Lab 1"], "b": ["Lab 2"]}, finish=False)
    assert r.status_code == 200
    assert world.st["contracts"] == [] and world.st["certs"] == [], "a render ran in the request"
    assert world.st["team"] == [] and world.st["customer"] == []
    pdf = world.get("/api/portal/tok-b/deposit-invoice.pdf")
    assert pdf.status_code == 200 and pdf.content == b"%PDF invoice"


def test_a_restart_in_the_middle_resumes_and_repeats_nothing(world, monkeypatch):
    """The process dies during the fifth contract render (a deploy, a crash). The job is left
    pending with its lease held; once the lease runs out the next sweep takes it over and finishes
    it. Nothing done before the crash is done again."""
    st = world.st
    spec = _many(world, 12)
    calls = {"n": 0}
    real = main.signing.render_signed_contract

    def dies_once(pdf, cert, **k):
        calls["n"] += 1
        if calls["n"] == 5:
            raise _ProcessDied()
        return real(pdf, cert, **k)
    monkeypatch.setattr(main.signing, "render_signed_contract", dies_once)
    assert _commit(world, spec, anchor="p01", finish=False).status_code == 200
    (aid, pdfs), = st["spawned"]
    with pytest.raises(_ProcessDied):
        main._run_batch_paperwork(aid, pdfs)
    pw = _anchor(st)["batch"]["paperwork"]
    assert pw["state"] == "pending" and pw["lease"]["owner"]
    assert st["team"] == [] and st["customer"] == [], "emailed before the paperwork was done"
    built_before = dict(_built(st))
    assert 0 < len(built_before) < 12

    # Another container, straight away: the dead worker's lease still holds it off.
    assert main._paperwork_sweep() == {aid: "busy"}
    st["clock"] += timedelta(seconds=main._PAPERWORK_LEASE_SEC + 1)
    assert main._paperwork_sweep() == {aid: "done"}
    _one_of_everything(st, list(spec))
    # The contracts stored before the crash were not rendered again.
    assert {k: v for k, v in _built(st).items() if k in built_before} == built_before
    assert main._paperwork_sweep() == {}, "a finished batch was picked up again"


def test_a_crash_after_a_render_before_its_mark_does_not_render_it_again(world, monkeypatch):
    """The narrowest gap: the contract was STORED and the process died before the job recorded it.
    The resumed run finds it in the database and does not build it a second time."""
    st = world.st
    _open(world, "a"); _open(world, "b")
    assert _commit(world, {"a": ["Lab 1"], "b": ["Lab 2"]}, finish=False).status_code == 200
    (aid, pdfs), = st["spawned"]
    real_mark = main._Paperwork.mark

    def dies_on_the_first_contract_mark(self, step, key=None):
        if step == "contracts":
            raise _ProcessDied()
        return real_mark(self, step, key)
    monkeypatch.setattr(main._Paperwork, "mark", dies_on_the_first_contract_mark)
    with pytest.raises(_ProcessDied):
        main._run_batch_paperwork(aid, pdfs)
    monkeypatch.setattr(main._Paperwork, "mark", real_mark)
    renders_before = len(st["certs"])
    assert renders_before >= 1
    st["clock"] += timedelta(seconds=main._PAPERWORK_LEASE_SEC + 1)
    assert main._paperwork_sweep() == {aid: "done"}
    _one_of_everything(st, ["a", "b"], emails=(SESSION, "ap@acme.com"))
    assert len(st["certs"]) == 2, "a stored contract was rendered again"


def test_a_crash_after_an_invoice_was_issued_before_its_mark_does_not_issue_it_again(world,
                                                                                    monkeypatch):
    """The invoice's chat card and requested stamp were written and the process died before the
    job recorded it. deposit_requested_at says it was issued: the resumed run does not post a
    second card, and the one email still carries the invoice."""
    st = world.st
    _open(world, "a"); _open(world, "b")
    assert _commit(world, {"a": ["Lab 1"], "b": ["Lab 2"]}, finish=False).status_code == 200
    (aid, pdfs), = st["spawned"]
    real_mark = main._Paperwork.mark

    def dies_on_the_first_invoice_mark(self, step, key=None):
        if step == "invoices":
            raise _ProcessDied()
        return real_mark(self, step, key)
    monkeypatch.setattr(main._Paperwork, "mark", dies_on_the_first_invoice_mark)
    with pytest.raises(_ProcessDied):
        main._run_batch_paperwork(aid, pdfs)
    monkeypatch.setattr(main._Paperwork, "mark", real_mark)
    assert [m["pid"] for m in st["messages"] if m["type"] == "deposit_request"]
    st["clock"] += timedelta(seconds=main._PAPERWORK_LEASE_SEC + 1)
    assert main._paperwork_sweep() == {aid: "done"}
    _one_of_everything(st, ["a", "b"], emails=(SESSION, "ap@acme.com"))
    dana = next(m for m in st["customer"] if m["to"] == SESSION)
    assert len(dana["attachments"]) == 4, "both contracts and both invoices"


def test_a_crash_half_way_through_the_emails_sends_the_rest_once(world, monkeypatch):
    """The staff email and Dana's went out; the process died sending ap@'s. The resumed run sends
    ap@'s alone: nobody hears twice."""
    st = world.st
    _open(world, "a"); _open(world, "b")
    assert _commit(world, {"a": ["Lab 1"], "b": ["Lab 2"]}, finish=False).status_code == 200
    (aid, pdfs), = st["spawned"]
    real_send = email_sender.send_customer_update
    calls = {"n": 0}

    def dies_on_the_second(e, *a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise _ProcessDied()
        return real_send(e, *a, **k)
    monkeypatch.setattr(email_sender, "send_customer_update", dies_on_the_second)
    with pytest.raises(_ProcessDied):
        main._run_batch_paperwork(aid, pdfs)
    assert len(st["team"]) == 1 and len(st["customer"]) == 1
    st["clock"] += timedelta(seconds=main._PAPERWORK_LEASE_SEC + 1)
    assert main._paperwork_sweep() == {aid: "done"}
    _one_of_everything(st, ["a", "b"], emails=(SESSION, "ap@acme.com"))


def test_two_workers_at_once_do_the_work_once(world, monkeypatch):
    """Two containers during a deploy (or the request's thread and the sweeper) start the same batch
    at the same moment. One holds the lease; the other stands aside."""
    st = world.st
    spec = _many(world, 6)

    def render(pdf, cert, **k):
        time.sleep(0.05)
        st["certs"].append(cert)
        return b"%PDF-1.7 signed " + cert["proposal_id"].encode() + b"\n%%EOF"
    monkeypatch.setattr(main.signing, "render_signed_contract", render)
    assert _commit(world, spec, anchor="p01", finish=False).status_code == 200
    (aid, pdfs), = st["spawned"]
    results, go = [], threading.Barrier(2)

    def worker():
        go.wait()
        results.append(main._run_batch_paperwork(aid, pdfs))
    ts = [threading.Thread(target=worker) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    assert sorted(results) == ["busy", "done"]
    _one_of_everything(st, list(spec))
    assert len(st["certs"]) == 6


def test_a_worker_that_lost_its_lease_stops_without_writing(world, monkeypatch):
    """A worker stalled past its lease, which another one then took over: its next save is refused
    and it stops, rather than carrying on beside the new owner."""
    st = world.st
    _open(world, "a"); _open(world, "b")
    assert _commit(world, {"a": ["Lab 1"], "b": ["Lab 2"]}, finish=False).status_code == 200
    (aid, pdfs), = st["spawned"]
    real_recipients = main.db.get_recipients

    def taken_over_meanwhile(pid):
        _anchor(st)["batch"]["paperwork"]["lease"] = {
            "owner": "someone-else", "until": st["clock"] + timedelta(seconds=300)}
        return real_recipients(pid)
    monkeypatch.setattr(main.db, "get_recipients", taken_over_meanwhile)
    assert main._run_batch_paperwork(aid, pdfs) == "busy"
    # The first approval line went out before its mark was refused; nothing after it did.
    assert [m["pid"] for m in st["messages"]] == ["a"]
    assert st["contracts"] == [] and st["team"] == [] and st["customer"] == []


def test_a_render_that_keeps_failing_is_retried_then_reported_and_holds_nothing_up(world,
                                                                                   monkeypatch):
    st = world.st
    spec = _many(world, 4)
    monkeypatch.setattr(main.signing, "render_signed_contract",
                        _signed_unless("p03", main.signing.render_signed_contract))
    assert _commit(world, spec, anchor="p01").status_code == 200
    # The other three are done; the emails wait for the fourth.
    assert len(_built(st)) == 3 and st["team"] == []
    pw = _anchor(st)["batch"]["paperwork"]
    assert pw["state"] == "pending" and pw["attempts"] == {"contract:p03": 1}
    assert main._paperwork_sweep() == {_anchor(st)["id"]: "pending"}      # attempt 2
    assert st["team"] == []
    assert main._paperwork_sweep() == {_anchor(st)["id"]: "done"}         # attempt 3: given up
    assert main._paperwork_sweep() == {}
    # REPORTED: staff read it on the proposal's thread (internal), and in the one email.
    note = [m for m in st["messages"] if m["pid"] == "p03" and (m["meta"] or {}).get("internal")
            and "could not be built after 3 tries" in m["body"]]
    assert len(note) == 1 and "LibreOffice timed out" in note[0]["body"]
    assert len(st["team"]) == 1 and len(st["customer"]) == 1
    assert "could not be built yet" in st["team"][0]["body"]
    assert len(st["team"][0]["attachments"]) == 3
    assert len(st["customer"][0]["attachments"]) == 3 + 4, "3 contracts and every invoice"
    assert sorted(st["invoices"]) == sorted(spec)


def test_the_emails_wait_no_longer_than_the_time_limit(world, monkeypatch):
    """A render still being retried does not hold the emails past the wait: they go with what is
    ready, the rest linked, and the job carries on retrying without emailing again."""
    st = world.st
    spec = _many(world, 3)
    real = main.signing.render_signed_contract
    monkeypatch.setattr(main.signing, "render_signed_contract",
                        _signed_unless("p02", real, signing.ContractUnavailable("busy")))
    assert _commit(world, spec, anchor="p01").status_code == 200
    assert st["team"] == []
    st["clock"] += timedelta(seconds=main._PAPERWORK_EMAIL_WAIT_SEC)
    assert main._paperwork_sweep() == {_anchor(st)["id"]: "pending"}   # attempt 2, still retrying
    assert len(st["team"]) == 1 and len(st["customer"]) == 1
    assert "could not be built yet" in st["team"][0]["body"]
    assert len(st["team"][0]["attachments"]) == 2
    lab2 = next(li for li in st["customer"][0]["body"].split("<li>") if "Room 02" in li)
    assert "INV-P02" in lab2 and "Pay this deposit" in lab2
    # It comes good on the next pass: stored and posted to its thread, no second email.
    monkeypatch.setattr(main.signing, "render_signed_contract", real)
    assert main._paperwork_sweep() == {_anchor(st)["id"]: "done"}
    assert len(st["team"]) == 1 and len(st["customer"]) == 1
    _one_of_everything(st, list(spec))


@pytest.mark.parametrize("n,ok", [(20, True), (21, False)])
def test_the_cap_is_twenty(world, n, ok):
    assert main.MAX_BATCH == 20
    spec = _many(world, n)
    r = _commit(world, spec, anchor="p01")
    if ok:
        assert r.status_code == 200, r.text
        assert len(r.json()["approved"]) == 20
        _one_of_everything(world.st, list(spec))
    else:
        assert r.status_code == 400 and r.json()["error"] == "batch_size"
        assert "between 2 and 20" in r.json()["message"]
        _nothing_written(world.st)
    pv = world.post("/api/portal/tok-p01/approve-several/preview", json={"items": [
        {"token": world.st["rows"][p]["token"], "option_labels": spec[p]} for p in spec]})
    assert pv.status_code == (200 if ok else 400)


def test_the_status_lists_only_what_this_session_may_open(world):
    """d is in the same project but was sent to somebody else: never listed, whatever batch_id the
    page sends."""
    _open(world, "a"); _open(world, "b")
    _commit(world, {"a": ["Lab 1"], "b": ["Lab 2"]})
    world.st["approvals"].append({"id": 999, "proposal_id": "d", "consent_version": "x",
                                  "batch": {"batch_id": "batch-0000-0001"}})
    s = world.get("/api/portal/tok-a/approve-several/status",
                  params={"batch_id": "batch-0000-0001"})
    assert s.status_code == 200
    assert sorted(x["token"] for x in s.json()["approved"]) == ["tok-a", "tok-b"]
    assert "Lab 3" not in s.text and "tok-d" not in s.text
    assert world.get("/api/portal/tok-a/approve-several/status",
                     params={"batch_id": "x"}).status_code == 400
    world.st["session"] = "pat@other.example"
    assert world.get("/api/portal/tok-a/approve-several/status",
                     params={"batch_id": "batch-0000-0001"}).status_code == 401


def test_the_job_record_rides_the_commit(world):
    """The approvals and the record of the work they owe are written by the same transaction: the
    first row's batch carries it, pending, and nothing else does."""
    _open(world, "a"); _open(world, "b")
    _commit(world, {"a": ["Lab 1"], "b": ["Lab 2"]}, finish=False)
    (call,) = world.st["batch_calls"]
    first, second = call
    assert first["approval"]["batch"]["paperwork"]["state"] == "pending"
    assert "paperwork" not in second["approval"]["batch"]
    assert world.st["spawned"][0][0] == world.st["approvals"][0]["id"]


# ── review fixes (2026-10-31) ────────────────────────────────────────────────
def test_a_worker_whose_lease_ran_out_in_the_email_phase_does_not_send_a_second_email(world,
                                                                                    monkeypatch):
    """The tool is slow: every invoice rendered at issue time failed (the cards went out without a
    document), so the email phase renders each one again for the attachment. During the first of
    those renders the lease runs out and the sweeper -- a separate thread of the same process --
    takes the batch over and finishes it, emails included. The first worker must stop there, not
    send the staff email a second time."""
    st = world.st
    spec = _many(world, 4)
    calls = {"n": 0, "nested": None}

    def render_invoice(payload):
        calls["n"] += 1
        if calls["n"] <= 4:                       # issue time: the tool is down
            raise invoice.InvoiceUnavailable("the tool timed out")
        if calls["nested"] is None:               # the email phase's first re-render: too slow
            calls["nested"] = "running"
            st["clock"] += timedelta(seconds=main._PAPERWORK_LEASE_SEC + 1)
            calls["nested"] = main._paperwork_sweep()
        return b"%PDF invoice"
    monkeypatch.setattr(invoice, "render_invoice_pdf", render_invoice)
    assert _commit(world, spec, anchor="p01", finish=False).status_code == 200
    (aid, pdfs), = st["spawned"]
    assert main._run_batch_paperwork(aid, pdfs) == "busy", "it carried on beside the new owner"
    assert calls["nested"] == {aid: "done"}
    assert len(st["team"]) == 1, "the staff email went out twice"
    assert [m["to"] for m in st["customer"]] == [SESSION]
    _one_of_everything(st, list(spec))


def test_a_lease_lost_while_waiting_for_a_render_slot_issues_no_invoice(world, monkeypatch):
    """The render slots are shared with every other batch, so a worker can wait for one past its
    lease. Issuing an invoice posts the customer's deposit card: a worker that no longer holds the
    batch must not issue it."""
    st = world.st
    _open(world, "a"); _open(world, "b")
    assert _commit(world, {"a": ["Lab 1"], "b": ["Lab 2"]}, finish=False).status_code == 200
    (aid, pdfs), = st["spawned"]
    real = main._RENDER_SLOTS

    class SlowSlots:
        def __enter__(self):
            real.acquire()
            # Meanwhile the lease ran out and another container took the batch.
            _anchor(st)["batch"]["paperwork"]["lease"] = {
                "owner": "someone-else", "until": st["clock"] + timedelta(seconds=300)}
            return self

        def __exit__(self, *exc):
            real.release()
    monkeypatch.setattr(main, "_RENDER_SLOTS", SlowSlots())
    assert main._run_batch_paperwork(aid, pdfs) == "busy"
    assert [m for m in st["messages"] if m["type"] == "deposit_request"] == [], (
        "a deposit card was posted by a worker that had lost the batch")
    assert st["contracts"] == [] and st["team"] == [] and st["customer"] == []


def test_the_email_phase_renders_earlier_invoices_only_within_its_budget(world, monkeypatch):
    """A resumed pass holds none of the invoice documents an earlier pass issued. Rendering them
    all again, one by one, could keep the emails waiting on the tool for minutes: past the budget
    the email carries the number and the Pay link instead of the attachment."""
    st = world.st
    _open(world, "a"); _open(world, "b")
    assert _commit(world, {"a": ["Lab 1"], "b": ["Lab 2"]}, finish=False).status_code == 200
    (aid, pdfs), = st["spawned"]

    def dies(*a, **k):                  # the process goes down as the first email is sent
        raise _ProcessDied()
    monkeypatch.setattr(email_sender, "notify_team", dies)
    with pytest.raises(_ProcessDied):
        main._run_batch_paperwork(aid, pdfs)
    monkeypatch.setattr(email_sender, "notify_team", lambda subject, body, **k: st[
        "team"].append({"subject": subject, "body": body, **k}) or [])
    renders = []
    monkeypatch.setattr(invoice, "render_invoice_pdf",
                        lambda payload: renders.append(1) or b"%PDF invoice")
    monkeypatch.setattr(main, "_PAPERWORK_EMAIL_RENDER_SEC", 0, raising=False)
    st["clock"] += timedelta(seconds=main._PAPERWORK_LEASE_SEC + 1)
    assert main._paperwork_sweep() == {aid: "done"}
    assert renders == [], "the invoices were rendered again past the budget"
    dana = next(m for m in st["customer"] if m["to"] == SESSION)
    assert len(dana["attachments"]) == 2, "the two contracts, the invoices linked"
    assert dana["body"].count("Pay this deposit") == 2 and "(attached)" not in dana["body"]
    assert "INV-A" in dana["body"] and "INV-B" in dana["body"]


def test_a_cold_document_does_not_hold_the_answer_and_is_hashed_by_the_background(world,
                                                                                 monkeypatch):
    """The documents being signed are fetched before the commit. A customer who read the proposals
    more than ten minutes ago (or a restart) means cold renders on the tool, and twenty of those
    would outlast nginx's 60 s: the page says it failed while the server commits. The fetch has
    one deadline for all of them; what misses it is signed without a stored hash and the
    background hashes the document it builds the contract from, as the download rebuild does."""
    st = world.st
    spec = _many(world, 3)
    main._PDF_CACHE.clear()
    main._pdf_cache_put("p01", 1, b"%PDF proposal p01")       # warm; p02 and p03 are cold
    release = threading.Event()
    real_fetch = main._proposal_pdf_bytes

    def fetch(p, *, timeout=90.0):
        if p["proposal_id"] != "p01" and not release.is_set():
            release.wait(5)                     # a cold LibreOffice render
            return b"%PDF proposal cold " + p["proposal_id"].encode()
        if p["proposal_id"] != "p01":
            return b"%PDF proposal cold " + p["proposal_id"].encode()
        return real_fetch(p, timeout=timeout)
    monkeypatch.setattr(main, "_proposal_pdf_bytes", fetch)
    monkeypatch.setattr(main, "_BATCH_PDF_DEADLINE_SEC", 0.3, raising=False)
    t0 = time.monotonic()
    try:
        r = _commit(world, spec, anchor="p01", finish=False)
        took = time.monotonic() - t0
    finally:
        release.set()
    assert r.status_code == 200, r.text
    assert took < 2.5, "the answer waited for the cold renders (%.2fs)" % took
    sha = {a["proposal_id"]: a["contract_sha256"] for a in st["approvals"]}
    assert sha["p01"] == signing.sha256_hex(b"%PDF proposal p01")
    assert sha["p02"] is None and sha["p03"] is None
    _run_spawned(world)
    assert _anchor(st)["batch"]["paperwork"]["state"] == "done"
    _one_of_everything(st, list(spec))
    certs = {c["proposal_id"]: c for c in st["certs"]}
    assert certs["p02"]["proposal_pdf_sha256"] == signing.sha256_hex(b"%PDF proposal cold p02")
    assert certs["p03"]["proposal_pdf_sha256"] == signing.sha256_hex(b"%PDF proposal cold p03")
    assert certs["p01"]["proposal_pdf_sha256"] == sha["p01"]


def test_the_email_wait_counts_from_the_end_of_the_first_render_pass(world, monkeypatch):
    """A big batch's first pass can take longer than the email wait on its own. Counted from the
    commit, one render failing once in that pass would have the emails go on the very next pass,
    after one retry -- and the contract that then succeeds would never be emailed, although the
    page said it would be. Counted from the end of the renders, it gets its retries first."""
    st = world.st
    spec = _many(world, 4)
    real = main.signing.render_signed_contract
    tries = {"p03": 0}

    def slow(pdf, cert, **k):
        st["clock"] += timedelta(minutes=3)     # four of these: 12 minutes for the first pass
        if cert["proposal_id"] == "p03":
            tries["p03"] += 1
            if tries["p03"] < 3:
                raise signing.ContractUnavailable("LibreOffice timed out")
        return real(pdf, cert, **k)
    monkeypatch.setattr(main.signing, "render_signed_contract", slow)
    assert _commit(world, spec, anchor="p01").status_code == 200
    assert st["team"] == [] and len(_built(st)) == 3
    st["clock"] += timedelta(seconds=60)
    assert main._paperwork_sweep() == {_anchor(st)["id"]: "pending"}     # attempt 2 fails
    assert st["team"] == [] and st["customer"] == [], "emailed after a single retry"
    st["clock"] += timedelta(seconds=60)
    assert main._paperwork_sweep() == {_anchor(st)["id"]: "done"}        # attempt 3 builds it
    _one_of_everything(st, list(spec))
    assert len(st["team"][0]["attachments"]) == 4, "the late contract was not emailed"
    assert len(st["customer"][0]["attachments"]) == 4 + 4
