"""Team-notification recipient resolution (email_sender.resolve_notify_recipients).
Pure-logic invariants; the DB-backed CRUD is covered by the staging end-to-end smoke,
per the repo convention (see test_customer_auth.py). The roster endpoints' `kind`
CONTRACT is pinned at the bottom of this file anyway — it is pure request validation,
and a typo'd kind stores a row that no alert will ever resolve, which looks like a
working save on screen and is invisible until somebody notices they stopped being
told about deposits."""
import pytest
from fastapi.testclient import TestClient

import email_sender

f = email_sender.resolve_notify_recipients
ENV_G = ["bids@wetreadwell.com"]
ENV_D = ["deposits@wetreadwell.com"]


# ── base resolution (unconfigured → env fallback; configured → rows) ──────────
def test_general_uses_general_rows():
    assert f(["a@x.com"], [], "general", ENV_G, ENV_D) == ["a@x.com"]


def test_general_falls_back_to_env_when_unconfigured():
    # No rows at all → fresh/unconfigured install → env fallback.
    assert f([], [], "general", ENV_G, ENV_D) == ENV_G


def test_deposit_reaches_general_plus_deposit_rows():
    """A deposit alert is MORE people than a general one, not different people: the general
    roster is the floor and deposit-kind rows extend it, general first."""
    assert f(["g@x.com"], ["d@x.com"], "deposit", ENV_G, ENV_D) == ["g@x.com", "d@x.com"]


def test_deposit_uses_general_rows_when_nobody_is_on_the_deposit_list():
    assert f(["g@x.com"], [], "deposit", ENV_G, ENV_D) == ["g@x.com"]


def test_deposit_falls_back_to_env_when_unconfigured():
    assert f([], [], "deposit", ENV_G, ENV_D) == ENV_D


def test_returns_copy_not_env_alias():
    out = f([], [], "general", ENV_G, ENV_D)
    out.append("x@x.com")
    assert ENV_G == ["bids@wetreadwell.com"]   # env list not mutated


# ── the deposit list is ADDITIVE, and only in that one direction ──────────────
HANZ, KYLE, WILL = "hanz@wetreadwell.com", "kyle@wetreadwell.com", "will@wetreadwell.com"
KYLENE = "kylene@wetreadwell.com"


def test_the_first_deposit_row_removes_nobody_from_deposit_alerts():
    """THE REGRESSION THIS FILE'S FIX EXISTS FOR. The resolver read
    `list(deposit_rows or general_rows)`, so a deposit-kind row REPLACED the general roster:
    adding Kylene to hear about the money would have silently stopped hanz/kyle/will hearing
    about it at all — nine general rows on prod, none of them notified, no error anywhere.

    Mutation: restore the `or` and this fails; every other deposit test that only checks
    membership would still pass, because Kylene is in both answers."""
    before = f([HANZ, KYLE, WILL], [], "deposit", ENV_G, ENV_D, configured=True)
    after = f([HANZ, KYLE, WILL], [KYLENE], "deposit", ENV_G, ENV_D, configured=True)
    assert before == [HANZ, KYLE, WILL]
    assert after == [HANZ, KYLE, WILL, KYLENE]      # general first, then the addition
    assert set(before) <= set(after), "adding a deposit recipient dropped somebody"


def test_a_deposit_only_person_gets_no_general_alert():
    """Additive one way only. Kylene is on the roster for the MONEY; a proposal-sent or
    chat-message alert must not start landing in her inbox because of it."""
    assert f([HANZ], [KYLENE], "general", ENV_G, ENV_D, configured=True) == [HANZ]


def test_somebody_on_both_lists_is_emailed_once():
    """Two rows (the table's unique key is (kind, lower(email)), so this is a legal roster),
    one person, one email."""
    assert f([HANZ, KYLE], [KYLE], "deposit", ENV_G, ENV_D, configured=True) == [HANZ, KYLE]


def test_dedupe_across_the_two_lists_is_case_insensitive():
    """`Kylene@` typed into the general list and `kylene@` into the deposit list is one human.
    First-seen casing wins, which is the general row."""
    out = f([HANZ, "Kylene@wetreadwell.com"], [KYLENE], "deposit", ENV_G, ENV_D, configured=True)
    assert out == [HANZ, "Kylene@wetreadwell.com"]


def test_a_mute_beats_a_deposit_kind_row():
    """Mute wins over everything, and the deposit bucket is not an exception to it — somebody
    who silenced one job stays silenced for its deposit alert too."""
    out = f([HANZ], [KYLENE], "deposit", ENV_G, ENV_D, mutes=["KYLENE@wetreadwell.com"],
            configured=True)
    assert out == [HANZ]


def test_an_add_unions_onto_a_deposit_alert():
    """Per-project layering is unchanged by the additive base: roster (general + deposit),
    then this project's adds, in that order."""
    out = f([HANZ], [KYLENE], "deposit", ENV_G, ENV_D, adds=[WILL], configured=True)
    assert out == [HANZ, KYLENE, WILL]


# ── the anti-resurrection fix: CONFIGURED but empty bucket → NOBODY, not env ──
def test_configured_but_general_bucket_empty_is_silent():
    # Roster has rows (only deposit-kind here) → a general alert must NOT fall back
    # to the env inbox just because no general rows are enabled.
    assert f([], ["d@x.com"], "general", ENV_G, ENV_D) == []


def test_configured_all_disabled_is_silent():
    # Everyone toggled off (configured=True, both buckets empty) → send to nobody.
    assert f([], [], "general", ENV_G, ENV_D, configured=True) == []
    assert f([], [], "deposit", ENV_G, ENV_D, configured=True) == []


# ── per-project overrides: union adds, subtract mutes (mute wins) ─────────────
def test_add_extends_base():
    assert f(["a@x.com"], [], "general", ENV_G, ENV_D, adds=["c@x.com"]) == ["a@x.com", "c@x.com"]


def test_mute_removes_from_base():
    assert f(["a@x.com", "b@x.com"], [], "general", ENV_G, ENV_D, mutes=["b@x.com"]) == ["a@x.com"]


def test_mute_wins_over_add():
    assert f(["a@x.com"], [], "general", ENV_G, ENV_D, adds=["b@x.com"], mutes=["b@x.com"]) == ["a@x.com"]


def test_case_insensitive_dedupe():
    # Same address in base + adds (different case) → kept once, first-seen casing.
    assert f(["A@x.com"], [], "general", ENV_G, ENV_D, adds=["a@x.com"]) == ["A@x.com"]


def test_mute_is_case_insensitive():
    assert f(["A@x.com"], [], "general", ENV_G, ENV_D, mutes=["a@x.com"]) == []


def test_add_on_empty_configured_roster():
    # A per-project "add" can notify someone even when the global roster is all-off.
    assert f([], [], "general", ENV_G, ENV_D, adds=["c@x.com"], configured=True) == ["c@x.com"]


# ── the roster endpoints' `kind` contract ─────────────────────────────────────
# Only the kind vocabulary, not the CRUD: the resolver above buckets on exactly two strings,
# so a row stored under any third one is unreachable by every alert. The screen that manages
# the roster lives in the other repo and is about to grow a deposit group, which is what makes
# this the boundary worth pinning here rather than in the staging smoke.
@pytest.fixture
def roster_api(monkeypatch):
    """The admin endpoints with the roster table replaced by a list."""
    import main
    rows, added = [], []
    monkeypatch.setattr(main, "_admin_ok", lambda request: True)
    monkeypatch.setattr(main.db, "list_notify_recipients", lambda: list(rows))

    def add(email, kind, by=None, enabled=True):
        added.append({"email": email, "kind": kind, "enabled": enabled})
    monkeypatch.setattr(main.db, "add_notify_recipient", add)
    monkeypatch.setattr(main.db, "set_notify_recipient_enabled",
                        lambda rid, enabled: added.append({"toggle": rid, "enabled": enabled}))
    return TestClient(main.app), rows, added


def test_post_refuses_an_unknown_kind(roster_api):
    """Stored rather than refused, a typo'd kind is a row that looks saved on screen and that
    no alert ever resolves — silent, and only noticed when somebody asks why they stopped
    hearing about deposits."""
    client, _, added = roster_api
    r = client.post("/api/admin/notify-recipients", json={"email": KYLENE, "kind": "deposits"})
    assert r.status_code == 400, r.text
    assert r.json()["error"] == "invalid_kind"
    assert added == [], "the bad row was written anyway"


def test_post_defaults_to_general(roster_api):
    """An older client that never sends `kind` must keep landing on the general roster."""
    client, _, added = roster_api
    r = client.post("/api/admin/notify-recipients", json={"email": WILL})
    assert r.status_code == 200, r.text
    assert added == [{"email": WILL, "kind": "general", "enabled": False}]


def test_post_accepts_a_deposit_kind(roster_api):
    client, _, added = roster_api
    r = client.post("/api/admin/notify-recipients", json={"email": KYLENE, "kind": "deposit"})
    assert r.status_code == 200, r.text
    assert added == [{"email": KYLENE, "kind": "deposit", "enabled": False}]


def test_get_exposes_each_rows_kind(roster_api):
    """The other repo's page groups by it, so it has to come back down the wire."""
    client, rows, _ = roster_api
    rows += [{"id": 1, "email": HANZ, "kind": "general", "enabled": True, "added_by": None},
             {"id": 2, "email": KYLENE, "kind": "deposit", "enabled": True, "added_by": None}]
    body = client.get("/api/admin/notify-recipients").json()
    assert [(r["email"], r["kind"]) for r in body["recipients"]] == [
        (HANZ, "general"), (KYLENE, "deposit")]


def test_the_toggle_works_on_a_deposit_row(roster_api):
    """Enable/disable is by row id and kind-blind, which is the point: a deposit recipient is
    switched on and off with the same control as anybody else."""
    client, rows, added = roster_api
    rows.append({"id": 2, "email": KYLENE, "kind": "deposit", "enabled": False, "added_by": None})
    r = client.patch("/api/admin/notify-recipients/2", json={"enabled": True})
    assert r.status_code == 200, r.text
    assert added == [{"toggle": 2, "enabled": True}]
