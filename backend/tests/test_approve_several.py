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
"""
import copy

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
          "staff_task_emails": [], "batch_error": None, "next_id": 100}
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
            st["approvals"].append(dict(it["approval"], proposal_id=it["proposal_id"],
                                        id=st["next_id"]))
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
    monkeypatch.setattr(main.db, "get_signed_contract", lambda pid: None)
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


def _commit(client, spec, anchor="a", items=None, **over):
    pv = _preview(client, spec, anchor).json()
    body = {"batch_id": "batch-0000-0001", "items": items or _items(client, spec),
            "name": "Dana Reed", "title": "Owner", "date": "2026-10-31", "consent": True,
            "consent_digest": pv.get("consent_digest")}
    body.update(over)
    for k in [k for k, v in body.items() if v is None]:
        body.pop(k)
    return client.post("/api/portal/tok-%s/approve-several" % anchor, json=body,
                       headers={"user-agent": "Mozilla/5.0 (Test)",
                                "x-forwarded-for": "203.0.113.9"})


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
    assert got["tok-a"]["contract_ready"] is True and got["tok-b"]["contract_ready"] is True

    # TWO approval rows, the SAME signing event on both.
    assert [a["proposal_id"] for a in st["approvals"]] == ["a", "b"]
    a, b = st["approvals"]
    for k in ("name", "title", "ip", "user_agent", "approver_email", "consent_version", "batch"):
        assert a[k] == b[k], k
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
