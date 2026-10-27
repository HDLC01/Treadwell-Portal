"""The shared live-member table: tests/scope_member_cases.json, asserted in the PORTAL.

ONE table of scope-group member cases lives in BOTH repos' backend/tests folders, byte-identical:
the proposal tool asserts its drafts.live_scope_members against it, and this file asserts the
portal's own rule against it. The portal's scope_groups.split_members is the authority (the tool's
rule was ported from it), so a case failing HERE means the table or the portal moved; a case failing
only in the tool means the tool's port is wrong. The tool also checks that its copy and this one are
the same file (test_the_portals_copy_of_the_table_is_this_one), so edit both together or neither.

Every place the portal decides membership is run against every case:
  - scope_groups.split_members, THE rule;
  - followup_rules.live_members, the follow-up worker's group pass and the board's follow-up card;
  - scope_groups.send_rows, the rows a combined send speaks for;
  - db.list_scope_group_drafts, the combined send's "every live scope is published" check, executed
    against a real Postgres so the jsonb ->> reads the rule receives are the real ones (skipped
    unless PORTAL_TEST_PG_URL is set; recipe in test_pg_scope_groups.py).
"""
from __future__ import annotations

import copy
import json
import pathlib
import uuid

import pytest

import followup_rules
import pg_fixture
import scope_groups
from pg_fixture import needs_pg

TABLE = pathlib.Path(__file__).resolve().parent / "scope_member_cases.json"
CASES = json.loads(TABLE.read_text(encoding="utf-8"))["cases"]
IDS = [c["name"] for c in CASES]


def _states(case):
    return copy.deepcopy(case["states"])


def _rows(case):
    """One open, published portal row per draft in the case: the rows the wrappers filter."""
    return [{"proposal_id": s["id"], "proposal_status": "sent", "scope_group": "g"}
            for s in case["states"]]


# ── the table itself is not vacuous ──────────────────────────────────────────
def test_the_table_exercises_every_branch_of_the_rule():
    """A green "every case agrees" proves nothing if no case takes a branch. Each case's live and
    parked lists partition its drafts, and between them the cases cover a real group parking a test
    draft, a test group keeping every draft, an archived draft, the jsonb text flags and no drafts."""
    assert len(set(IDS)) == len(IDS), "two cases share a name, so a failure could not name its case"
    for c in CASES:
        ids = [s["id"] for s in c["states"]]
        assert sorted(c["live"] + c["parked"]) == sorted(ids), \
            "case %r: live + parked is not exactly its drafts" % c["name"]
    flags = [s.get(k) for c in CASES for s in c["states"] for k in ("archived", "is_test")]
    assert any(c["parked"] and c["live"] for c in CASES)                  # a real group parks
    assert any(len(c["live"]) > 1 and all(scope_groups.draft_is_test(s) for s in c["states"])
               for c in CASES)                                            # a test group keeps all
    assert any(s.get("archived") in (True, "true") for c in CASES for s in c["states"])
    assert "true" in flags and "false" in flags                           # jsonb ->> text
    assert any(not c["states"] for c in CASES)


# ── THE rule ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_split_members_gives_the_tables_answer(case):
    live, parked = scope_groups.split_members(_states(case))
    assert (live, parked) == (case["live"], case["parked"]), (
        "case %r: split_members gave live=%r parked=%r, the shared table says live=%r parked=%r"
        % (case["name"], live, parked, case["live"], case["parked"]))


def _as_jsonb_text(case):
    """The case's states as db.list_scope_group_draft_states hands them over: `is_test` is
    `data ->> 'is_test'`, so a JSON true/false arrives as the text "true"/"false"."""
    out = _states(case)
    for s in out:
        if isinstance(s.get("is_test"), bool):
            s["is_test"] = "true" if s["is_test"] else "false"
    return out


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_split_members_gives_the_same_answer_from_jsonb_text_flags(case):
    """The table's own jsonb-text case cannot catch a text "false" read as "nobody said": its only
    active draft is live either way. "a real job named like a test" can, once its flag is text, and
    that is the form the portal's drafts read really delivers. Runs without a database."""
    live, parked = scope_groups.split_members(_as_jsonb_text(case))
    assert (live, parked) == (case["live"], case["parked"]), (
        "case %r with its flags as jsonb text: split_members gave live=%r parked=%r, the shared "
        "table says live=%r parked=%r" % (case["name"], live, parked, case["live"], case["parked"]))


# ── the wrappers the follow-up and send paths call ───────────────────────────
@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_the_follow_up_pass_chases_exactly_the_live_members(case):
    got = [r["proposal_id"] for r in followup_rules.live_members(_rows(case), _states(case))]
    assert got == case["live"], (
        "case %r: followup_rules.live_members chased %r, the shared table's live members are %r"
        % (case["name"], got, case["live"]))


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_the_combined_send_speaks_for_exactly_the_live_members(case):
    got = [r["proposal_id"] for r in scope_groups.send_rows(_rows(case), _states(case))]
    assert got == case["live"], (
        "case %r: scope_groups.send_rows emails %r, the shared table's live members are %r"
        % (case["name"], got, case["live"]))


# ── the same table through the real drafts read ──────────────────────────────
if pg_fixture.PG_URL:  # imported only when the tests will run
    from psycopg.types.json import Jsonb

    import db


@pytest.fixture(scope="module")
def pgdb():
    with pg_fixture.scratch_portal_db("members") as pool:
        yield pool


@needs_pg
@pytest.mark.realdb
@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_the_combined_sends_expected_scopes_are_the_live_members(pgdb, case):
    """db.list_scope_group_drafts reads `archived` and `is_test` out of the draft blob with ->>, so
    a JSON true and the text "true" arrive as the same string: the rule must answer the table from
    what Postgres actually hands it. A key the case leaves null is not written at all (nobody said)."""
    db.execute("truncate public.drafts")
    gid = "g-" + uuid.uuid4().hex[:8]
    for s in case["states"]:
        data = {"project_name": s["project_name"], "scope_group": {"id": gid, "label": s["id"]}}
        for key in ("archived", "is_test"):
            if s.get(key) is not None:
                data[key] = s[key]
        db.execute("insert into public.drafts (id, data, owner_email, created_at) "
                   "values (%s, %s, %s, %s)",
                   (s["id"], Jsonb(data), "kyle@estimator.example", s["created_at"]))
    got = db.list_scope_group_drafts(gid)
    assert got == case["live"], (
        "case %r: db.list_scope_group_drafts expects %r, the shared table's live members are %r"
        % (case["name"], got, case["live"]))
