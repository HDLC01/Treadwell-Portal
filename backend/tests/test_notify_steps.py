"""Per-step notification resolution: the floor, the opt-in, the suppression, and the cell API.

WHAT THIS IS. `portal_notify_recipients.kind` used to hold exactly ('general','deposit'), so the
roster had two buckets and seven of the nine CRM moments shared one of them. It now holds a CRM
STEP, and resolution is:

    recipients(step) = (enabled general rows + enabled rows FOR THIS STEP)
                       - rows for this step that are switched OFF
                       + this project's adds
                       - this project's mutes            (mute wins over everything)

THE TWO CLAIMS THAT CARRY THE RISK, and the two this file spends most of its length on:

  1. A CONFIGURED roster whose step bucket is EMPTY reaches the GENERAL FLOOR. Not nobody, and
     not the env default inbox either. Those are three different answers and only one of them is
     right: a moment nobody has thought about must still reach the team, and a configured roster
     must never resurrect the shipped env address.

  2. A step row that is switched OFF is a SUPPRESSION, and it beats the floor. This is a
     deliberate choice against the alternative (make a per-step off a no-op and let the floor
     win), and the reason is on the screen this drives: on the matrix every green cell receives
     and every grey cell does not. If an off cell could not actually stop an email, the grid would
     be showing one thing while the resolver did another, which is the single failure mode this
     feature is most likely to ship with. It also means a per-step off is a knob rather than a
     cliff: the only alternative way to stop somebody hearing about ONE moment is to take them off
     the team, which stops the other eight.

     The floor's real job survives intact, because it applies exactly when nothing has been SAID
     about this person and this step.
"""
import pytest
from fastapi.testclient import TestClient

import email_sender

f = email_sender.resolve_notify_recipients
ENV_G = ["bids@wetreadwell.com"]
ENV_D = ["deposits@wetreadwell.com"]

HANZ, KYLE, WILL = "hanz@wetreadwell.com", "kyle@wetreadwell.com", "will@wetreadwell.com"
KYLENE = "kylene@wetreadwell.com"
TEAM = [HANZ, KYLE, WILL]


# ── claim 1: the floor ────────────────────────────────────────────────────────
@pytest.mark.parametrize("step", email_sender.NOTIFY_STEP_IDS)
def test_every_step_reaches_the_general_floor_when_its_own_bucket_is_empty(step):
    """Nobody has configured this step. The team still hears about it.

    Mutation: make the step branch `base = list(step_rows)` and this fails on all nine."""
    assert f(TEAM, [], step, ENV_G, ENV_D, configured=True) == TEAM


@pytest.mark.parametrize("step", email_sender.NOTIFY_STEP_IDS)
def test_a_configured_roster_never_falls_back_to_the_env_inbox(step):
    """Somebody has set the roster up and switched everyone off. That means nobody — it must not
    quietly re-add the shipped bids@ address, which is the whole point of the `configured` flag.

    Mutation: drop the `configured` argument from _resolve_notify's call and this fails."""
    assert f([], [], step, ENV_G, ENV_D, configured=True) == []


@pytest.mark.parametrize("step", email_sender.NOTIFY_STEP_IDS)
def test_an_unconfigured_roster_still_falls_back_to_env(step):
    """Fresh install, no rows at all. The env list is all there is, and the two money steps
    inherit the DEPOSIT env list because that is what the single old 'deposit' kind did."""
    expected = ENV_D if step in email_sender.DEPOSIT_STEPS else ENV_G
    assert f([], [], step, ENV_G, ENV_D) == expected


def test_a_step_opt_in_is_added_to_the_floor_not_swapped_for_it():
    """Kylene is on the roster for the deposit. The team keeps hearing about it.

    Mutation: `base = list(step_rows or general_rows)` — the exact shape of the bug fixed on
    2026-08-20, one step wider. Fails here."""
    assert f(TEAM, [KYLENE], "deposit_received", ENV_G, ENV_D, configured=True) == TEAM + [KYLENE]


def test_a_step_opt_in_does_not_leak_into_the_other_steps():
    """Additive one way only. Kylene is on for the money; a question or a portal-feedback note
    must not start landing in her inbox because of it."""
    for step in ("question", "sent", "viewed", "approved", "contacts", "feedback"):
        assert f(TEAM, [], step, ENV_G, ENV_D, configured=True) == TEAM


def test_the_floor_is_what_an_unnamed_caller_still_gets():
    """`general` is not a step; it is the resolution rule. A caller that names no step lands on
    the floor alone — exactly where the seven un-named call sites used to land, so this widening
    changed nothing for anybody until a step row was written."""
    assert f(TEAM, [KYLENE], "general", ENV_G, ENV_D, configured=True) == TEAM
    assert f(TEAM, [KYLENE], "not_a_step_at_all", ENV_G, ENV_D, configured=True) == TEAM


# ── claim 2: an explicit OFF suppresses that one step ─────────────────────────
def test_a_suppressed_person_drops_out_of_that_step_only():
    """Kyle does not want to hear every time a proposal is opened. He still hears about the other
    eight moments, which is the difference between a knob and a cliff.

    Mutation: drop `suppressed` from the mute set and this fails."""
    assert f(TEAM, [], "viewed", ENV_G, ENV_D, configured=True, suppressed=[KYLE]) == [HANZ, WILL]
    assert f(TEAM, [], "approved", ENV_G, ENV_D, configured=True) == TEAM


def test_a_suppression_beats_the_floor_and_a_step_opt_in_alike():
    """Both directions, because a suppression that only outranked one of them would be a rule
    nobody could predict from the grid."""
    assert f([KYLE], [], "sent", ENV_G, ENV_D, configured=True, suppressed=[KYLE]) == []
    assert f([], [KYLE], "sent", ENV_G, ENV_D, configured=True, suppressed=[KYLE]) == []


def test_a_suppression_is_case_insensitive():
    assert f([HANZ.upper()], [], "sent", ENV_G, ENV_D, configured=True, suppressed=[HANZ]) == []


def test_suppressing_everybody_means_nobody():
    """The honest consequence of claim 2, pinned so it cannot be a surprise: switch the whole team
    off for one step and that step reaches nobody. The UI carries an explicit warning for exactly
    this state — the column header says "nobody is told" — rather than the resolver quietly
    overruling the grid. That one step is affected and the other eight are not is asserted
    through the real bucketing, in test_resolve_notify_buckets_the_whole_roster."""
    assert f(TEAM, [], "viewed", ENV_G, ENV_D, configured=True, suppressed=TEAM) == []


def test_a_suppression_does_not_resurrect_the_env_list():
    """Emptying a step by suppression must not read as "unconfigured"."""
    assert f(TEAM, [], "contacts", ENV_G, ENV_D, configured=True, suppressed=TEAM) == []


# ── per-project overrides still resolve last, and mute still wins ─────────────
def test_a_per_project_add_reaches_somebody_suppressed_at_the_step_level():
    """Narrower beats wider: an org-wide "not this moment" is overruled by "yes, on THIS job".

    This is not a loophole, it is consistency. Being the assigned estimator has ALWAYS reached
    somebody who is not on the roster at all, and a step row saying "not this moment" is a weaker
    statement than being absent altogether — so if absence loses to an add, a suppression must too.
    The narrow way to keep one job quiet is the per-project mute, which still wins (below).

    Mutation: subtract the suppressions after the adds instead of from the base, and this fails."""
    out = f(TEAM, [], "viewed", ENV_G, ENV_D, adds=[KYLE], mutes=[], configured=True,
            suppressed=[KYLE])
    assert out == [HANZ, WILL, KYLE]
    # And the mute still closes it: suppressed at the step, added by the project, muted on it.
    assert f(TEAM, [], "viewed", ENV_G, ENV_D, adds=[KYLE], mutes=[KYLE], configured=True,
             suppressed=[KYLE]) == [HANZ, WILL]


def test_mute_still_beats_add():
    """Unchanged and load-bearing: an explicit "not me, not this job" outranks being added to it,
    including being added by owning the job.

    Mutation: apply adds after mutes and this fails."""
    assert f(TEAM, [], "approved", ENV_G, ENV_D, adds=[KYLENE], mutes=[KYLENE],
             configured=True) == TEAM
    assert f(TEAM, [], "approved", ENV_G, ENV_D, adds=[KYLE], mutes=[KYLE], configured=True) == [
        HANZ, WILL]


def test_mute_beats_a_step_opt_in_too():
    assert f([], [KYLENE], "deposit_submitted", ENV_G, ENV_D, mutes=[KYLENE],
             configured=True) == []


# ── the legacy 'deposit' kind keeps working ───────────────────────────────────
def test_a_legacy_deposit_row_covers_both_money_steps():
    """kylene@ is live on prod as `kind='deposit'`, from when that was the only non-general value.
    The schema change migrates those rows; this is what keeps her notified in the window before it
    is applied, and after any hand-written one.

    Mutation: return `(kind,)` unconditionally from steps_for_kind and this fails."""
    assert email_sender.steps_for_kind("deposit") == ("deposit_submitted", "deposit_received")
    for step in ("deposit_submitted", "deposit_received"):
        assert step in email_sender.steps_for_kind("deposit")


def test_the_floor_is_not_a_step_bucket():
    assert email_sender.steps_for_kind("general") == ()
    assert email_sender.steps_for_kind("wat") == ()
    assert email_sender.steps_for_kind("sent") == ("sent",)


def test_the_legacy_kind_still_resolves_like_a_deposit_step():
    assert f(TEAM, [KYLENE], "deposit", ENV_G, ENV_D, configured=True) == TEAM + [KYLENE]
    assert f([], [], "deposit", ENV_G, ENV_D) == ENV_D


# ── the whole roster, bucketed by the real _resolve_notify ────────────────────
@pytest.fixture
def roster(monkeypatch):
    """The real _resolve_notify over a stubbed db, because the bucketing — which rows are the
    floor, which are this step's opt-ins, which are its suppressions — is where a widened `kind`
    is most likely to go wrong, and the pure function above never sees a `kind` at all."""
    rows: list[dict] = []
    overrides: list[dict] = []
    import db
    monkeypatch.setattr(db, "list_notify_recipients", lambda: list(rows))
    monkeypatch.setattr(db, "list_notify_overrides", lambda pid: list(overrides))
    return rows, overrides


PROD = [
    {"id": 1, "email": HANZ, "kind": "general", "enabled": True},
    {"id": 2, "email": KYLE, "kind": "general", "enabled": True},
    {"id": 3, "email": WILL, "kind": "general", "enabled": False},   # on the roster, switched off
    {"id": 4, "email": KYLENE, "kind": "deposit_received", "enabled": True},
    {"id": 5, "email": KYLE, "kind": "viewed", "enabled": False},    # suppression
]


def test_resolve_notify_buckets_the_whole_roster(roster):
    rows, _ = roster
    rows.extend(PROD)
    r = email_sender._resolve_notify
    assert r("sent") == [HANZ, KYLE]                      # the floor; Will is off
    assert r("viewed") == [HANZ]                          # Kyle suppressed for this one moment
    assert r("deposit_received") == [HANZ, KYLE, KYLENE]   # floor plus the money opt-in
    assert r("deposit_submitted") == [HANZ, KYLE]          # her row is for the OTHER money step
    assert r("approved") == [HANZ, KYLE]


def test_a_disabled_general_row_is_not_a_suppression(roster):
    """Will's general row is off. That means he is not on the floor — it must NOT mean he is
    suppressed for every step, or a step opt-in of his own could never reach him.

    Mutation: treat any disabled row as a suppression and this fails."""
    rows, _ = roster
    rows.extend(PROD)
    rows.append({"id": 6, "email": WILL, "kind": "approved", "enabled": True})
    assert email_sender._resolve_notify("approved") == [HANZ, KYLE, WILL]
    assert email_sender._resolve_notify("sent") == [HANZ, KYLE]


def test_a_legacy_deposit_row_is_bucketed_into_both_money_steps(roster):
    rows, _ = roster
    rows.extend([{"id": 1, "email": HANZ, "kind": "general", "enabled": True},
                 {"id": 2, "email": KYLENE, "kind": "deposit", "enabled": True}])
    assert email_sender._resolve_notify("deposit_submitted") == [HANZ, KYLENE]
    assert email_sender._resolve_notify("deposit_received") == [HANZ, KYLENE]
    assert email_sender._resolve_notify("approved") == [HANZ]


def test_a_row_whose_kind_nothing_recognises_is_ignored_but_still_counts_as_configured(roster):
    """A value from the future must not resolve as anything — and must not drag the whole roster
    back to the env inbox either, which would mail the shipped address on every notification."""
    rows, _ = roster
    rows.append({"id": 1, "email": KYLENE, "kind": "invoice_issued", "enabled": True})
    assert email_sender._resolve_notify("sent") == []
    assert email_sender._resolve_notify("invoice_issued") == []


def test_the_estimator_is_folded_in_as_an_add_and_a_mute_still_wins(roster):
    rows, ov = roster
    rows.extend(PROD)
    # Kyle is suppressed for `viewed` org-wide AND is this job's estimator, so he is added back:
    # owning the job is the narrower statement. A per-project mute then closes it for good.
    assert email_sender._resolve_notify("viewed", "p1", KYLE) == [HANZ, KYLE]
    ov.append({"email": KYLE, "mode": "mute"})
    assert email_sender._resolve_notify("viewed", "p1", KYLE) == [HANZ]


def test_a_db_failure_falls_back_to_env_per_step(monkeypatch):
    import db
    def boom():
        raise RuntimeError("table is gone")
    monkeypatch.setattr(db, "list_notify_recipients", boom)
    monkeypatch.setattr(email_sender.config, "NOTIFY_EMAILS", ENV_G)
    monkeypatch.setattr(email_sender.config, "DEPOSIT_NOTIFY_EMAILS", ENV_D)
    assert email_sender._resolve_notify("sent") == ENV_G
    assert email_sender._resolve_notify("deposit_received") == ENV_D


# ── the cell endpoint ─────────────────────────────────────────────────────────
@pytest.fixture
def cell_api(monkeypatch):
    import main
    calls: list[dict] = []
    rows: list[dict] = []
    monkeypatch.setattr(main, "_admin_ok", lambda request: True)
    monkeypatch.setattr(main.db, "list_notify_recipients", lambda: list(rows))
    monkeypatch.setattr(main.db, "set_notify_step",
                        lambda email, kind, enabled, added_by=None:
                            calls.append({"set": email, "step": kind, "enabled": enabled}))
    monkeypatch.setattr(main.db, "clear_notify_step",
                        lambda email, kind: calls.append({"clear": email, "step": kind}))
    return TestClient(main.app), rows, calls


def test_the_three_cell_states_write_the_three_different_things(cell_api):
    """on / off / inherit are three states because two would be a lie: without a stored OFF row
    there is no way to tell a cell somebody switched off from one nobody has touched, and the two
    resolve differently."""
    client, _, calls = cell_api
    for state, expected in (
        ("on", {"set": KYLE, "step": "viewed", "enabled": True}),
        ("off", {"set": KYLE, "step": "viewed", "enabled": False}),
        ("inherit", {"clear": KYLE, "step": "viewed"}),
    ):
        calls.clear()
        r = client.put("/api/admin/notify-recipients/step",
                       json={"email": KYLE, "step": "viewed", "state": state})
        assert r.status_code == 200, r.text
        assert calls == [expected]


def test_a_cell_refuses_a_step_the_resolver_does_not_know(cell_api):
    """A stored row with a typo'd step is a toggle that looks saved and never resolves — the same
    silent failure the roster's `kind` check has always guarded."""
    client, _, calls = cell_api
    for step in ("deposits", "general", "", "approve", "viewed ok"):
        r = client.put("/api/admin/notify-recipients/step",
                       json={"email": KYLE, "step": step, "state": "on"})
        assert r.status_code == 400, (step, r.text)
    assert calls == []
    # Case and stray whitespace are NORMALISED, not refused: a step id is a slug we chose, and
    # rejecting " Approved" would be rejecting the right answer typed slightly differently.
    r = client.put("/api/admin/notify-recipients/step",
                   json={"email": KYLE.upper(), "step": " APPROVED ", "state": "ON"})
    assert r.status_code == 200, r.text
    assert calls == [{"set": KYLE, "step": "approved", "enabled": True}]


def test_the_floor_is_not_settable_as_a_cell(cell_api):
    """`general` is set by the person's own on/off chip. Two controls for one state is how they
    come to disagree."""
    client, _, calls = cell_api
    r = client.put("/api/admin/notify-recipients/step",
                   json={"email": KYLE, "step": "general", "state": "on"})
    assert r.status_code == 400 and r.json()["error"] == "invalid_step"
    assert calls == []


def test_a_cell_refuses_a_state_it_does_not_have(cell_api):
    client, _, calls = cell_api
    r = client.put("/api/admin/notify-recipients/step",
                   json={"email": KYLE, "step": "viewed", "state": "maybe"})
    assert r.status_code == 400 and r.json()["error"] == "invalid_state"
    assert calls == []


def test_a_cell_refuses_a_non_address(cell_api):
    client, _, calls = cell_api
    r = client.put("/api/admin/notify-recipients/step",
                   json={"email": "nope", "step": "viewed", "state": "on"})
    assert r.status_code == 400 and r.json()["error"] == "invalid_email"
    assert calls == []


def test_a_cell_needs_admin(monkeypatch):
    import main
    monkeypatch.setattr(main, "_admin_ok", lambda request: False)
    r = TestClient(main.app).put("/api/admin/notify-recipients/step",
                                 json={"email": KYLE, "step": "viewed", "state": "on"})
    assert r.status_code == 401


def test_a_new_person_answers_to_the_roster_cap_but_an_existing_one_does_not(cell_api):
    """A cell is the ONLY way to add a deposit-only person (kylene@ is exactly that), so it can
    grow the roster and has to be capped. But the cap counts PEOPLE: a full roster must not stop
    the thirteen people already on it from using their own toggles."""
    client, rows, calls = cell_api
    import main
    rows.extend({"id": i, "email": f"p{i}@wetreadwell.com", "kind": "general", "enabled": True}
                for i in range(main._MAX_NOTIFY_RECIPIENTS))
    r = client.put("/api/admin/notify-recipients/step",
                   json={"email": "one.too.many@wetreadwell.com", "step": "viewed", "state": "on"})
    assert r.status_code == 400 and r.json()["error"] == "too_many"
    assert calls == []
    r = client.put("/api/admin/notify-recipients/step",
                   json={"email": "P0@WeTreadwell.com", "step": "viewed", "state": "on"})
    assert r.status_code == 200, r.text
    assert calls == [{"set": "p0@wetreadwell.com", "step": "viewed", "enabled": True}]


def test_clearing_a_cell_is_never_capped(cell_api):
    """Undo must work on a full roster, or the only way out of the cap is SQL."""
    client, rows, calls = cell_api
    import main
    rows.extend({"id": i, "email": f"p{i}@wetreadwell.com", "kind": "general", "enabled": True}
                for i in range(main._MAX_NOTIFY_RECIPIENTS))
    r = client.put("/api/admin/notify-recipients/step",
                   json={"email": "gone@wetreadwell.com", "step": "viewed", "state": "inherit"})
    assert r.status_code == 200, r.text
    assert calls == [{"clear": "gone@wetreadwell.com", "step": "viewed"}]


# ── the GET carries the vocabulary the UI renders ────────────────────────────
def test_the_roster_get_serves_the_step_list(monkeypatch):
    """The page lives in the other repo and renders its columns from this. A hardcoded copy over
    there is a copy that drifts, and a column the resolver does not recognise is a toggle that
    silently does nothing."""
    import main
    monkeypatch.setattr(main, "_admin_ok", lambda request: True)
    monkeypatch.setattr(main.db, "list_notify_recipients", lambda: [
        {"id": 1, "email": HANZ, "kind": "general", "enabled": True, "added_by": None},
        {"id": 2, "email": KYLENE, "kind": "deposit_received", "enabled": True, "added_by": None}])
    body = TestClient(main.app).get("/api/admin/notify-recipients").json()
    assert [s["id"] for s in body["steps"]] == list(email_sender.NOTIFY_STEP_IDS)
    assert [(r["email"], r["kind"]) for r in body["recipients"]] == [
        (HANZ, "general"), (KYLENE, "deposit_received")]


def test_the_post_refuses_a_step_kind_and_still_refuses_a_typo(monkeypatch):
    """The add field adds PEOPLE TO THE TEAM. It may not mint a step row.

    THIS TEST USED TO ASSERT THE OPPOSITE, and that is why it is worth reading. It pinned 200 for
    all nine steps, on the reasoning that the widened column should accept the widened vocabulary
    everywhere. But this route creates the row switched OFF, and under the step vocabulary a
    disabled step row means SUPPRESS - so the route minted exactly what the unsilenceable-step
    guard exists to prevent, and this test locked that in. Review executed it: two POSTs of
    {kind: "sent"} both returned 200 and took that alert's reach from ['hanz','will'] to nobody,
    with the other eight steps untouched. One alert silenced, nothing on any screen.

    So the route is back to the two kinds it accepted before the matrix existed, and step rows go
    through PUT .../step, which is guarded. Nothing sends anything else: the tool's proxy allows
    the same two and the page's add field has a single kind. This removes capability rather than
    adding a check, which is the cheaper kind of fix.
    """
    import main
    added: list[dict] = []
    monkeypatch.setattr(main, "_admin_ok", lambda request: True)
    monkeypatch.setattr(main.db, "list_notify_recipients", lambda: [])
    monkeypatch.setattr(main.db, "add_notify_recipient",
                        lambda email, kind, by=None, enabled=True:
                            added.append({"email": email, "kind": kind}))
    client = TestClient(main.app)

    for step in email_sender.NOTIFY_STEP_IDS:
        r = client.post("/api/admin/notify-recipients", json={"email": KYLENE, "kind": step})
        assert r.status_code == 400, (step, r.status_code)
        assert r.json()["error"] == "invalid_kind", (step, r.json())
    assert added == [], "a step row was created through the add field: %r" % (added,)

    # The floor and the legacy deposit kind still work - this route is how a colleague is added.
    for kind in (email_sender.GENERAL_KIND, email_sender.LEGACY_DEPOSIT_KIND):
        assert client.post("/api/admin/notify-recipients",
                           json={"email": KYLENE, "kind": kind}).status_code == 200, kind
    assert [a["kind"] for a in added] == [email_sender.GENERAL_KIND,
                                          email_sender.LEGACY_DEPOSIT_KIND]

    added.clear()
    assert client.post("/api/admin/notify-recipients",
                       json={"email": KYLENE, "kind": "deposits"}).status_code == 400
    assert added == []


def test_a_crafted_patch_cannot_silence_the_delivery_failure_alert(monkeypatch):
    """PATCH takes an id, not an (email, step) pair, which is how it slipped the guard.

    Nothing in that request says which step is being silenced, so the first version of the guard
    never ran for it. Review reached this from the tool's own proxy, which forwards any row id on
    an admin session alone while the roster GET hands step-row ids to the browser. Executed then:
    two PATCHes of {enabled: false} on `sent` rows returned 200 and took the reach to nobody.
    """
    import main
    rows = [
        {"id": 1, "email": HANZ, "kind": "general", "enabled": True},
        {"id": 2, "email": WILL, "kind": "general", "enabled": True},
        {"id": 3, "email": HANZ, "kind": "sent", "enabled": True},
        {"id": 4, "email": WILL, "kind": "sent", "enabled": True},
    ]
    wrote: list[tuple] = []
    monkeypatch.setattr(main, "_admin_ok", lambda request: True)
    monkeypatch.setattr(main.db, "list_notify_recipients", lambda: [dict(r) for r in rows])
    monkeypatch.setattr(main.db, "set_notify_recipient_enabled",
                        lambda rid, enabled: wrote.append((rid, enabled)))
    client = TestClient(main.app)

    # Switching the first of the two off is fine - somebody is still told.
    assert client.patch("/api/admin/notify-recipients/3", json={"enabled": False}).status_code == 200
    assert wrote == [(3, False)]
    wrote.clear()

    # With row 3 already off, taking row 4 off would leave nobody. Refused.
    rows[2]["enabled"] = False
    r = client.patch("/api/admin/notify-recipients/4", json={"enabled": False})
    assert r.status_code == 400, r.status_code
    assert r.json()["error"] == "would_silence_step", r.json()
    assert wrote == [], "the write went through despite the refusal: %r" % (wrote,)

    # Turning somebody back ON is never refused, whatever the state.
    assert client.patch("/api/admin/notify-recipients/4", json={"enabled": True}).status_code == 200

    # And a step nobody depends on can still be emptied - the guard is narrow.
    rows.append({"id": 5, "email": HANZ, "kind": "viewed", "enabled": True})
    assert client.patch("/api/admin/notify-recipients/5", json={"enabled": False}).status_code == 200

def test_a_disabled_legacy_deposit_row_is_not_a_suppression(roster):
    """THE ONE THAT REACHES LIVE DATA ON DEPLOY, and the mirror of the general-row test above.

    A row written under the OLD vocabulary cannot have meant "suppress", because the concept did
    not exist: `kind` held exactly ('general','deposit') and an off row could only mean "an address
    somebody typed into the Deposit-alerts card and never turned green". Adding a recipient has
    ALWAYS created the row switched OFF, so every address ever typed there and left grey is exactly
    this shape.

    Reading it as a suppression is worse than for any other kind, because the legacy value fans out
    to BOTH money steps, so one dormant row would silence two emails. Measured before the fix, on
    exactly these rows: HEAD resolved `deposit` to ['hanz', 'will']; the step code resolved
    deposit_submitted AND deposit_received to ['will'] alone.

    Mutation: drop the `row_kind == LEGACY_DEPOSIT_KIND` skip in bucket_notify_rows and this
    fails on both money steps."""
    rows, _ = roster
    rows.extend([{"id": 1, "email": HANZ, "kind": "general", "enabled": True},
                 {"id": 2, "email": WILL, "kind": "general", "enabled": True},
                 {"id": 3, "email": HANZ, "kind": "deposit", "enabled": False}])
    for step in ("deposit_submitted", "deposit_received"):
        assert email_sender._resolve_notify(step) == [HANZ, WILL], step
    # And the pre-widening call still resolves the way it did before any of this.
    assert email_sender._resolve_notify("deposit") == [HANZ, WILL]
    # The exemption is for the LEGACY kind only. A real step row that is off is still a
    # suppression, which is the whole feature.
    rows.append({"id": 4, "email": HANZ, "kind": "deposit_received", "enabled": False})
    assert email_sender._resolve_notify("deposit_received") == [WILL]
    assert email_sender._resolve_notify("deposit_submitted") == [HANZ, WILL]


def test_the_buckets_put_a_dormant_legacy_row_in_none_of_them():
    """Straight at bucket_notify_rows, because `_resolve_notify` could hide a wrong bucket behind
    the floor: hanz is on the floor anyway, so only the buckets show that his dormant row was not
    read as a suppression rather than merely being cancelled out."""
    rows = [{"email": HANZ, "kind": "general", "enabled": True},
            {"email": KYLENE, "kind": "deposit", "enabled": False},
            {"email": KYLE, "kind": "deposit", "enabled": True}]
    floor, opt_ins, suppressed = email_sender.bucket_notify_rows(rows, "deposit_received")
    assert floor == [HANZ]
    assert opt_ins == [KYLE], "an enabled legacy row still opts in to both money steps"
    assert suppressed == [], "a dormant legacy row was read as an instruction nobody gave"


def test_step_reach_is_the_floor_plus_opt_ins_minus_suppressions():
    """The guard below is only as good as this. Deliberately no per-project adds or mutes: a step
    that reaches somebody only because one job carries an override is still a step nobody set
    up."""
    rows = [{"email": HANZ, "kind": "general", "enabled": True},
            {"email": WILL, "kind": "general", "enabled": False},
            {"email": KYLENE, "kind": "sent", "enabled": True},
            {"email": KYLE, "kind": "general", "enabled": True},
            {"email": KYLE, "kind": "sent", "enabled": False}]
    assert email_sender.step_reach(rows, "sent") == [HANZ, KYLENE]
    assert email_sender.step_reach(rows, "approved") == [HANZ, KYLE]
    assert email_sender.step_reach([], "sent") == []


# THE ONE STEP THAT MAY NOT BE LEFT REACHING NOBODY.
#
# WHY REFUSE RATHER THAN WARN. `sent` is the only step whose email is also a WARNING: admin_publish
# sends it on a delivery FAILURE too ("That customer has not received the proposal"). Every other
# step reports something that happened; this one also reports something that did not, and it fires
# exactly when nobody is watching the project. A column badge is a report that appears only AFTER
# the last person is gone, and it lives on one page; the refusal is enforced on the server, so it
# holds for a stale tab and a curl as well.
#
# It is narrow on purpose: one step, one direction (somebody -> nobody). Undo, the other eight
# steps, and moving the alert from one person to another all still work.
@pytest.fixture
def store_api(monkeypatch):
    """cell_api plus a STORE, so a sequence of clicks sees its own earlier writes. Needed because
    the interesting claim is a two-click one: turn somebody else on first, THEN switch yourself
    off, and that has to be allowed."""
    import main
    rows: list[dict] = []
    monkeypatch.setattr(main, "_admin_ok", lambda request: True)
    monkeypatch.setattr(main.db, "list_notify_recipients", lambda: [dict(r) for r in rows])

    def _set(email, kind, enabled, added_by=None):
        for r in rows:
            if r["kind"] == kind and r["email"].lower() == email.lower():
                r["enabled"] = enabled
                return
        rows.append({"id": 900 + len(rows), "email": email, "kind": kind, "enabled": enabled})

    def _clear(email, kind):
        rows[:] = [r for r in rows
                   if not (r["kind"] == kind and r["email"].lower() == email.lower())]

    monkeypatch.setattr(main.db, "set_notify_step", _set)
    monkeypatch.setattr(main.db, "clear_notify_step", _clear)
    client = TestClient(main.app)

    def put(email, step, state):
        return client.put("/api/admin/notify-recipients/step",
                          json={"email": email, "step": step, "state": state})

    return put, rows


def test_switching_the_last_person_off_proposal_sent_is_refused(store_api):
    """The worst thing the new suppression power can do, and the write never lands.

    Mutation: delete the UNSILENCEABLE_STEPS block in admin_notify_step_set and this fails."""
    put, rows = store_api
    rows.append({"id": 1, "email": HANZ, "kind": "general", "enabled": True})
    r = put(HANZ, "sent", "off")
    assert r.status_code == 400, r.text
    assert r.json()["error"] == "would_silence_step" and r.json()["step"] == "sent"
    assert rows == [{"id": 1, "email": HANZ, "kind": "general", "enabled": True}], (
        "the refused write was stored anyway")
    assert email_sender.step_reach(rows, "sent") == [HANZ]


def test_the_same_click_on_every_other_step_is_allowed(store_api):
    """A knob, not a lock. Only `sent` carries the delivery-failure alert, so only `sent` is
    protected; suppressing the last person on the other eight is a real thing somebody may want."""
    put, rows = store_api
    rows.append({"id": 1, "email": HANZ, "kind": "general", "enabled": True})
    for step in [s for s in email_sender.NOTIFY_STEP_IDS if s != "sent"]:
        assert put(HANZ, step, "off").status_code == 200, step
        assert email_sender.step_reach(rows, step) == [], step


def test_clearing_the_last_explicit_on_for_proposal_sent_is_refused_too(store_api):
    """`inherit` empties a column just as thoroughly when the person is not on the floor. Guarding
    only "off" would leave the same hole one click to the left.

    Mutation: run the guard only when state != 'inherit' and this fails."""
    put, rows = store_api
    rows.append({"id": 1, "email": KYLENE, "kind": "sent", "enabled": True})
    r = put(KYLENE, "sent", "inherit")
    assert r.status_code == 400 and r.json()["error"] == "would_silence_step"
    assert rows[0]["enabled"] is True


def test_the_alert_can_be_moved_from_one_person_to_another(store_api):
    """The refusal must not trap the column on whoever holds it. Turn the new person on, then the
    old one off: two clicks, both allowed, and the alert has moved."""
    put, rows = store_api
    rows.append({"id": 1, "email": HANZ, "kind": "general", "enabled": True})
    assert put(WILL, "sent", "on").status_code == 200
    assert put(HANZ, "sent", "off").status_code == 200
    assert email_sender.step_reach(rows, "sent") == [WILL]


def test_a_step_that_already_reaches_nobody_can_still_be_edited(store_api):
    """The guard fires on somebody -> nobody, not on nobody -> nobody. Otherwise a roster with the
    whole team switched off could not be tidied up, and undo would be the one thing that failed."""
    put, rows = store_api
    rows.append({"id": 1, "email": HANZ, "kind": "general", "enabled": False})
    assert email_sender.step_reach(rows, "sent") == []
    assert put(HANZ, "sent", "off").status_code == 200
    assert put(HANZ, "sent", "inherit").status_code == 200
    assert rows == [{"id": 1, "email": HANZ, "kind": "general", "enabled": False}]


def test_only_proposal_sent_is_marked_required_in_the_step_list(monkeypatch):
    """The flag the column reads to say so BEFORE somebody tries. It is the explanation; the
    refusal above is the check, and the two must name the same step."""
    import main
    monkeypatch.setattr(main, "_admin_ok", lambda request: True)
    monkeypatch.setattr(main.db, "list_notify_recipients", lambda: [])
    body = TestClient(main.app).get("/api/admin/notify-recipients").json()
    assert [s["id"] for s in body["steps"] if s["required"]] == ["sent"]
    assert email_sender.UNSILENCEABLE_STEPS == {"sent"}
    assert "delivery failed" in [s for s in body["steps"] if s["id"] == "sent"][0]["hint"]
