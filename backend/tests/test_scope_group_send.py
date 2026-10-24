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
