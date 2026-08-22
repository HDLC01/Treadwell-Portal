"""Every notify_team() call names its CRM step, and the next one that forgets fails here.

THE REGRESSION THIS FILE EXISTS FOR. `notify_team(subject, body, kind="general", ...)` has a
DEFAULT, and nine call sites grew up around it: two named `kind="deposit"` and the other seven
took the default in silence. So the roster had exactly two buckets, and every other CRM moment —
sent, opened, question, approved, contacts, portal feedback, the customer's own status answer —
shared one list that could only be set to everything or nothing. Nothing anywhere reported that.

Hanz, 2026-08-21: "is there a way like a UI/UX that to implement toggle on and off who gets
automatically toggled on for the notif sending for each step of the CRM?" — and the honest answer
to his premise was that Kylene is not tied to approval at all. She is on the deposit bucket, and
approval only LOOKS connected because it leads to a deposit request.

WHY A SYNTAX-TREE WALK AND NOT A LIST OF EXPECTED STEPS. A hand-kept list is exactly the thing
that failed: somebody adds a tenth notification, does not think about the roster, and the list in
this file stays green because nobody edited it either. This walks the AST of the real module and
DISCOVERS the calls, so a new one is caught the moment it exists, without anybody remembering.

Both halves matter:
  * a call must pass `kind` (or `recipients`, which means the caller resolved its own list and no
    roster bucket is involved — followup_worker does that);
  * the value must be a plain string literal the resolver recognises. `kind=some_variable` would
    pass a "does it pass kind" check and could still be a step nothing on the roster can hold.
"""
import ast
import pathlib

import email_sender

BACKEND = pathlib.Path(__file__).resolve().parents[1]
# Every module that may notify the team. Discovered, not listed: a new file that imports
# email_sender and calls notify_team is picked up without editing this test.
SOURCES = sorted(p for p in BACKEND.glob("*.py") if "notify_team" in p.read_text(encoding="utf-8"))


def _calls(path: pathlib.Path) -> list[ast.Call]:
    """Every notify_team(...) call in one module, however it is spelled — `notify_team(...)` or
    `email_sender.notify_team(...)`."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
        if name == "notify_team":
            out.append(node)
    return out


def _kw(call: ast.Call, name: str):
    for k in call.keywords:
        if k.arg == name:
            return k.value
    return None


def _all_calls() -> list[tuple[pathlib.Path, ast.Call]]:
    return [(p, c) for p in SOURCES for c in _calls(p)]


def test_the_sources_and_the_calls_were_actually_found():
    """A walk that finds nothing passes every assertion below, so pin the count. Nine in the
    portal's main.py plus followup_worker's own explicit-recipients send."""
    found = _all_calls()
    assert [p.name for p in SOURCES] == ["email_sender.py", "followup_worker.py", "main.py"]
    by_file = {}
    for path, _ in found:
        by_file[path.name] = by_file.get(path.name, 0) + 1
    assert by_file == {"followup_worker.py": 1, "main.py": 9}, by_file


def test_every_call_names_a_step_the_resolver_recognises():
    """THE POINT OF THE FILE. Mutation: drop `kind="viewed"` from the proposal-opened call and
    this fails naming that line."""
    known = set(email_sender.NOTIFY_STEP_IDS) | {email_sender.GENERAL_KIND}
    bad = []
    for path, call in _all_calls():
        if _kw(call, "recipients") is not None:
            continue                     # caller resolved its own list; no bucket involved
        kind = _kw(call, "kind")
        if kind is None:
            bad.append(f"{path.name}:{call.lineno} passes no kind= at all")
        elif not isinstance(kind, ast.Constant) or not isinstance(kind.value, str):
            bad.append(f"{path.name}:{call.lineno} passes a non-literal kind=")
        elif kind.value not in known:
            bad.append(f"{path.name}:{call.lineno} passes kind={kind.value!r}, not a known step")
    assert not bad, "notify_team calls with no usable step:\n  " + "\n  ".join(bad)


def test_no_call_still_takes_the_general_default():
    """The floor is a RESOLUTION rule, not a step, so nothing should be SENT as 'general'. A call
    that names it is a call that has not been thought about — which is the state all seven were in.

    Mutation: change any call to kind="general" and this fails."""
    named = [f"{p.name}:{c.lineno}" for p, c in _all_calls()
             if _kw(c, "recipients") is None
             and isinstance(_kw(c, "kind"), ast.Constant)
             and _kw(c, "kind").value == email_sender.GENERAL_KIND]
    assert named == [], "these send as the floor rather than as a step: " + ", ".join(named)


def test_the_nine_moments_are_each_covered_exactly_once():
    """Nine call sites, nine steps, one each — so the vocabulary is not carrying a step nothing
    fires, and no two moments share a bucket by accident.

    This is the one assertion that would need editing when a tenth notification is added, and it
    is deliberately the LOOSE one: it compares SETS, so it says "the steps and the sends agree"
    rather than naming lines. The strict per-call rule above is the one that catches a forgotten
    step, and it needs no maintenance at all."""
    used = [_kw(c, "kind").value for p, c in _all_calls()
            if _kw(c, "recipients") is None and isinstance(_kw(c, "kind"), ast.Constant)]
    assert sorted(used) == sorted(email_sender.NOTIFY_STEP_IDS)
    assert len(set(used)) == len(used), "two call sites share one step: " + repr(used)


def test_each_step_id_is_a_slug_that_fits_the_column_and_the_url():
    """Stored in a text column, validated by a CHECK constraint, and sent as JSON from the matrix.
    Nothing exotic: lowercase, underscores, short."""
    for sid, label, hint in email_sender.NOTIFY_STEPS:
        assert sid and sid.replace("_", "").isalnum() and sid.islower(), sid
        assert len(sid) <= 32, sid
        assert label and hint, sid
        # The label is what a column header says; the hint is the sentence under it. A label long
        # enough to be a sentence breaks the grid, and a hint short enough to be a label explains
        # nothing.
        assert len(label) <= 24, (sid, label)
        assert len(hint) > 24, (sid, hint)


def test_the_two_money_steps_are_the_ones_that_inherit_the_deposit_env_list():
    assert email_sender.DEPOSIT_STEPS == {"deposit_submitted", "deposit_received"}
    assert email_sender.DEPOSIT_STEPS <= set(email_sender.NOTIFY_STEP_IDS)


def test_the_steps_payload_is_the_vocabulary_in_order():
    """The UI renders its columns from this and keeps no list of its own, so the order here is the
    order on screen — the order the work happens in."""
    payload = email_sender.steps_payload()
    assert [s["id"] for s in payload] == list(email_sender.NOTIFY_STEP_IDS)
    assert [s["id"] for s in payload][:3] == ["sent", "viewed", "question"]
    # `required` joined the shape when the delivery-failure column was made unsilenceable:
    # the page has to be able to SAY a column cannot be emptied before somebody tries.
    assert all(set(s) == {"id", "label", "hint", "required"} for s in payload)
    assert [s["id"] for s in payload if s["required"]] == ["sent"]
