"""Estimate revisions: the customer sees the version they were SENT.

Two problems these cover. First, the portal used to render the proposal page and
its PDF live from `drafts.data`, so an estimator saving a change silently rewrote a
proposal that had already gone out — including, after approval, the numbers the
customer agreed to. Second, a revised estimate had nowhere to live: staff had to
create a whole new project, losing the thread and the history.

A send now snapshots the draft, and `portal_proposals.current_revision_no` pins the
customer to that snapshot.
"""
import pytest

import db


# ── pinning ──────────────────────────────────────────────────────────────────
def _pin(monkeypatch, *, pinned, snapshots, live):
    """Wire get_revision_data/get_draft_data and return get_pinned_draft_data(p)."""
    monkeypatch.setattr(db, "get_revision_data",
                        lambda pid, no: snapshots.get(no))
    monkeypatch.setattr(db, "get_draft_data", lambda pid: live)
    return db.get_pinned_draft_data({"proposal_id": "p1", "current_revision_no": pinned})


def test_pinned_proposal_reads_the_snapshot_not_the_live_draft(monkeypatch):
    """The whole point: editing the draft after sending must not change what the
    customer sees."""
    out = _pin(monkeypatch, pinned=2,
               snapshots={1: {"total": "old"}, 2: {"total": "sent"}},
               live={"total": "estimator is mid-edit"})
    assert out == {"total": "sent"}


def test_legacy_proposal_falls_back_to_live_data(monkeypatch):
    """Rows published before revisions existed have no pin. They must keep working
    exactly as before rather than showing a blank proposal — they self-heal on the
    project's next send."""
    out = _pin(monkeypatch, pinned=None, snapshots={}, live={"total": "live"})
    assert out == {"total": "live"}


def test_missing_snapshot_falls_back_rather_than_breaking(monkeypatch):
    """Should never happen (the row is only written after the snapshot exists), but
    a customer seeing their proposal matters more than strictness here."""
    out = _pin(monkeypatch, pinned=7, snapshots={}, live={"total": "live"})
    assert out == {"total": "live"}


# ── revision publish semantics ───────────────────────────────────────────────
@pytest.fixture
def publish(monkeypatch):
    """POST /api/admin/publish with the DB + email seams captured."""
    from fastapi.testclient import TestClient
    import main

    calls = {"messages": [], "superseded": [], "reset": [], "updated": [], "created": [],
             "emails": []}

    monkeypatch.setattr(main, "_admin_ok", lambda request: True)
    monkeypatch.setattr(main.db, "get_draft_data",
                        lambda pid: {"contact_email": "c@x.com", "contact_name": "Cust",
                                     "project_name": "Westport"})
    monkeypatch.setattr(main.db, "add_message",
                        lambda pid, kind, who, body, **k: calls["messages"].append(
                            {"body": body, "type": k.get("msg_type"), "meta": k.get("meta")}))
    monkeypatch.setattr(main.db, "supersede_proposal_cards",
                        lambda pid, rev: calls["superseded"].append(rev))
    monkeypatch.setattr(main.db, "update_portal_proposal",
                        lambda *a, **k: calls["updated"].append(k))
    monkeypatch.setattr(main.db, "create_portal_proposal",
                        lambda *a, **k: calls["created"].append(k))
    monkeypatch.setattr(main.db, "add_recipient", lambda *a, **k: None)
    monkeypatch.setattr(main.db, "set_recipients", lambda *a, **k: None)
    monkeypatch.setattr(main.db, "remove_recipient", lambda *a, **k: None)
    monkeypatch.setattr(main.db, "get_recipients", lambda pid: ["c@x.com"])
    # Publishing also enrols the proposal in follow-up automation. Left unstubbed
    # these reach for a real connection and each call burns the pool's full timeout,
    # which turned this suite from seconds into minutes.
    monkeypatch.setattr(main.db, "enroll_followup",
                        lambda pid: calls.setdefault("enrolled", []).append(pid))
    monkeypatch.setattr(main.db, "set_assigned_estimator",
                        lambda pid, e: calls.setdefault("assigned", []).append(e))
    monkeypatch.setattr(main.db, "reopen_if_closed", lambda pid: False)
    monkeypatch.setattr(main.db, "mark_last_sent",
                        lambda pid: calls.setdefault("last_sent", []).append(pid))
    monkeypatch.setattr(main, "_pdf_cache_drop", lambda pid: None)
    monkeypatch.setattr(main.email_sender, "proposal_reply_to", lambda t: None)
    monkeypatch.setattr(main.email_sender, "send_portal_link",
                        lambda e, n, url, proj, **k: calls["emails"].append(
                            {"to": e, "revised": k.get("revised")}) or True)

    def _run(existing, body, *, was_approved=False):
        monkeypatch.setattr(main.db, "get_proposal", lambda pid: existing)
        monkeypatch.setattr(main.db, "reset_for_revision",
                            lambda pid, rev: (calls["reset"].append(rev), was_approved)[1])
        r = TestClient(main.app).post("/api/admin/publish", json=body)
        assert r.status_code == 200, r.text
        return r.json(), calls

    return _run


_EXISTING = {"proposal_id": "p1", "token": "tok", "customer_email": "c@x.com",
             "proposal_status": "approved"}


def test_first_publish_pins_revision_one_and_sends_the_normal_email(publish):
    out, calls = publish(None, {"draft_id": "p1", "revision_no": 1})
    assert calls["created"][0]["revision_no"] == 1
    assert calls["emails"][0]["revised"] is False
    assert out["revised"] is False
    # No supersede on a first send — there is nothing to retire.
    assert calls["superseded"] == []


def test_every_publish_stamps_when_this_send_went_out(publish):
    """`created_at` records the FIRST send and never moves, so a re-sent proposal used to re-enter
    the board's Sent column showing a date from weeks earlier — staff could see it had gone back to
    Sent but not that it had just been sent again. Hanz, 2026-08-13: "I have resent the proposal
    again but it didnt move back to sent?"

    Asserted on the publish itself, not on the pipeline: a mutation deleting this call left every
    other test green, because the read side coalesces to created_at and a column that is never
    written simply stays null forever."""
    _, calls = publish(None, {"draft_id": "p1", "revision_no": 1})
    assert calls.get("last_sent") == ["p1"], "a first send did not record when it went out"

    _, calls2 = publish(_EXISTING, {"draft_id": "p1", "revision_no": 2})
    assert calls2.get("last_sent") == ["p1", "p1"], "a re-send did not move the sent date"


def test_the_send_is_stamped_even_when_nobody_was_emailed(publish):
    """Same rule as created_at and the follow-up enrolment beside it: they record the SEND, not
    its delivery. A publish with no recipients still moved the proposal to the customer's portal."""
    _, calls = publish(None, {"draft_id": "p1", "revision_no": 1, "emails": []})
    assert calls.get("last_sent") == ["p1"]


def test_revision_supersedes_the_old_card_and_posts_a_new_one(publish):
    out, calls = publish(_EXISTING, {"draft_id": "p1", "revision_no": 2})
    assert calls["superseded"] == [2]
    cards = [m for m in calls["messages"] if m["type"] == "proposal_card"]
    assert len(cards) == 1
    assert "Revision 2" in cards[0]["body"]
    assert cards[0]["meta"] == {"revision_no": 2}
    assert out["revised"] is True


def test_revision_reopens_the_proposal_for_approval(publish):
    _, calls = publish(_EXISTING, {"draft_id": "p1", "revision_no": 3})
    assert calls["reset"] == [3]
    assert calls["updated"][0]["revision_no"] == 3


def test_revision_of_an_approved_proposal_says_so_in_the_thread(publish):
    """A customer whose approval has just been invalidated must be told, not left
    assuming the price they signed still stands."""
    _, calls = publish(_EXISTING, {"draft_id": "p1", "revision_no": 2}, was_approved=True)
    systems = [m for m in calls["messages"] if m["type"] == "system"]
    assert len(systems) == 1
    assert "needs a new approval" in systems[0]["body"]
    assert "recorded for reference" in systems[0]["body"]


def test_no_audit_line_when_it_was_not_yet_approved(publish):
    _, calls = publish(_EXISTING, {"draft_id": "p1", "revision_no": 2}, was_approved=False)
    assert [m for m in calls["messages"] if m["type"] == "system"] == []


def test_revised_email_wording_only_from_revision_two(publish):
    _, calls = publish(_EXISTING, {"draft_id": "p1", "revision_no": 1})
    # A re-send of the SAME version (e.g. adding a recipient) is not a revision.
    assert calls["emails"][0]["revised"] is False
    assert calls["superseded"] == []


def test_publish_without_revision_no_behaves_exactly_as_before(publish):
    """An older proposal tool sends no revision_no. Nothing may be superseded, no
    status reset, no new card — the pre-revisions path, untouched."""
    out, calls = publish(_EXISTING, {"draft_id": "p1"})
    assert calls["superseded"] == [] and calls["reset"] == []
    assert [m for m in calls["messages"] if m["type"] == "proposal_card"] == []
    assert calls["updated"][0]["revision_no"] is None
    assert out["revised"] is False


def test_garbage_revision_no_is_ignored_not_crashed(publish):
    out, calls = publish(_EXISTING, {"draft_id": "p1", "revision_no": "not-a-number"})
    assert out["revision_no"] is None and calls["reset"] == []
