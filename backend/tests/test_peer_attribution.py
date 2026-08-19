"""Two contacts on one proposal: whose message is whose.

Hanz, 2026-08-11: "2 or more customers receive the Proposal and one customer opens or does an
action then it should be considered as one action in the CRM... It should then highlight in the
CRM who viewed it as well and who replied. Then there is one chatbox and one status for the
customer side portal."

THE SHARED HALF WAS ALREADY TRUE and needed nothing: one portal_proposals row per draft, one
token, one thread, one status, one deposit. Checked against live data — 2 of 9 sent proposals have
two contacts and they already share all of it.

WHAT WAS WRONG WAS THE PEOPLE LAYER, and one part of it was a live bug on production:

    const mine = m.author_kind === "customer";        // frontend/app.js

True for EVERY customer message regardless of who wrote it. So the second contact on a proposal
saw the first contact's reply sitting on their own side of the thread, in their own colour, as if
they had written it. `_msg` never shipped `author_email`, so the browser had nothing better to go
on — the information existed in the database and was thrown away by the serializer.

`mine` is now decided server-side against the session that is asking. Everything a peer sees is
derived there too: a FIRST NAME, never an address (Hanz: first name in the portal, full address
staff-side), and `meta` whitelisted — an inbound-email row keeps the sender's address in
`meta.from`, so passing meta through would have leaked exactly what the first names avoid.

Staff-side, the drawer names which contact wrote a message ONLY when there is more than one.
That is deliberate: the blanket TREADWELL / CUSTOMER label came off the same day, because with a
single contact it restated what the side and colour of the bubble already said.
"""
import pathlib
import re

import pytest
from fastapi.testclient import TestClient

import main

PID = "pid-0001"
TOKEN = "tok-abc-123"
A = "dana.reed@acme.com"          # first contact
B = "ap@acme.com"                 # second contact
# Enough of a row for the endpoints under test to read; the status fields are not the subject
# here but /messages returns them alongside the thread.
PROP = {"proposal_id": PID, "token": TOKEN, "project_name": "Westport", "customer_email": A,
        "proposal_status": "sent", "deposit_status": "pending", "contacts_status": "pending",
        "schedule_status": "pending", "deposit_amount": None, "deposit_required": True}


def _row(id_, kind, email, body, meta=None, msg_type="text"):
    import datetime
    return {"id": id_, "author_kind": kind, "author_email": email, "body": body,
            "msg_type": msg_type, "meta": meta or {},
            "created_at": datetime.datetime(2026, 8, 11, 12, 0, 0)}


THREAD = [
    _row(1, "staff", None, "Your proposal is ready.", msg_type="proposal_card"),
    _row(2, "customer", A, "Can you start the 14th?"),
    _row(3, "customer", B, "Accounting will handle the deposit.",
         meta={"source": "email", "from": B, "email_id": "em_1"}),
    _row(4, "staff", "kyle@wetreadwell.com", "Yes, the 14th works."),
    _row(5, "customer", None, "Legacy row with no author recorded."),
]


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(main.db, "get_proposal_by_token", lambda t: dict(PROP) if t == TOKEN else None)
    monkeypatch.setattr(main.db, "get_proposal", lambda pid: dict(PROP))
    monkeypatch.setattr(main.db, "list_messages", lambda pid, after=0: list(THREAD))
    monkeypatch.setattr(main.db, "list_questions", lambda pid: [])
    monkeypatch.setattr(main.db, "get_recipients", lambda pid: [A, B])
    return TestClient(main.app)


def _as(viewer):
    """One thread, serialised as `viewer` sees it."""
    return [main._customer_msg(r, viewer) for r in THREAD]


# ── the live bug ─────────────────────────────────────────────────────────────
def test_the_second_contact_does_not_see_the_firsts_reply_as_their_own():
    """THE regression test. Contact B looking at the thread."""
    msgs = _as(B)
    a_msg = next(m for m in msgs if m["id"] == 2)
    assert a_msg["mine"] is False, (
        "contact B sees contact A's message as their own, which is what shipped")
    assert a_msg["author_first_name"] == "Dana"


def test_each_contact_sees_their_own_message_as_theirs():
    assert next(m for m in _as(A) if m["id"] == 2)["mine"] is True
    assert next(m for m in _as(B) if m["id"] == 3)["mine"] is True


def test_the_same_row_reads_differently_to_the_two_of_them():
    """The whole point: one stored thread, two correct views of it."""
    row2 = (next(m for m in _as(A) if m["id"] == 2), next(m for m in _as(B) if m["id"] == 2))
    assert row2[0]["mine"] is True and row2[1]["mine"] is False


def test_matching_ignores_case():
    assert next(m for m in _as("DANA.REED@Acme.com") if m["id"] == 2)["mine"] is True


def test_a_staff_message_is_never_mine():
    for viewer in (A, B):
        assert next(m for m in _as(viewer) if m["id"] == 4)["mine"] is False
        assert next(m for m in _as(viewer) if m["id"] == 1)["mine"] is False


def test_staff_messages_carry_no_customer_first_name():
    """It would render as a peer and put a customer's name on our words."""
    assert next(m for m in _as(A) if m["id"] == 4)["author_first_name"] == ""


def test_a_legacy_row_with_no_author_still_reads_as_the_viewers_own():
    """Most proposals have one contact and years of rows predate author_email. Guessing the
    other way would relabel a customer's own history as somebody else's."""
    for viewer in (A, B):
        assert next(m for m in _as(viewer) if m["id"] == 5)["mine"] is True


def test_an_unauthenticated_viewer_owns_nothing_rather_than_everything():
    """A None session must not make every attributed message read as "yours"."""
    msgs = _as(None)
    assert next(m for m in msgs if m["id"] == 2)["mine"] is False
    assert next(m for m in msgs if m["id"] == 3)["mine"] is False


# ── nothing about a peer leaks ────────────────────────────────────────────────
def test_a_peers_address_never_reaches_the_other_contact():
    """First name in the portal, full address staff-side. Asserted over the whole payload rather
    than field by field, because the leak that nearly happened was in `meta`, not in a field
    anybody would think to check."""
    import json
    blob = json.dumps(_as(B))
    assert A not in blob, "contact A's address is in what we send contact B"
    assert "acme.com" not in blob.replace("Accounting", ""), blob


def test_the_meta_whitelist_drops_the_inbound_senders_address():
    """An emailed reply stores the sender in meta.from. Passing meta through untouched is how a
    first-names-only rule quietly becomes an addresses-everywhere rule."""
    m = next(x for x in _as(A) if x["id"] == 3)
    assert m["meta"] == {"source": "email"}, m["meta"]
    assert "from" not in m["meta"] and "email_id" not in m["meta"]


def test_the_meta_the_ui_actually_needs_survives():
    """The whitelist has to be a whitelist, not a deletion: the invoice card reads amount and
    invoice_no, and a superseded proposal card reads its revision."""
    row = _row(9, "staff", None, "Deposit invoice",
               meta={"amount": 10312.5, "invoice_no": "TW-1042", "reference": "TW-ABC",
                     "revision_no": 2, "superseded": True, "superseded_by": 3, "from": "x@y.com"},
               msg_type="deposit_request")
    m = main._customer_msg(row, A)
    for k in ("amount", "invoice_no", "reference", "revision_no", "superseded", "superseded_by"):
        assert k in m["meta"], k
    assert "from" not in m["meta"]


def test_no_customer_payload_carries_author_email():
    for viewer in (A, B, None):
        for m in _as(viewer):
            assert "author_email" not in m


@pytest.mark.parametrize("email,expect", [
    ("dana.reed@acme.com", "Dana"),
    ("ap@acme.com", "Ap"),
    ("will_buchanan@x.com", "Will"),
    ("mary-jane@x.com", "Mary"),
    ("dana+bids@acme.com", "Dana"),
    ("DANA@acme.com", "DANA"),
    (None, ""),
    ("", ""),
])
def test_the_first_name_comes_off_the_local_part(email, expect):
    assert main._first_name_of(email) == expect


# ── through the endpoints ────────────────────────────────────────────────────
def test_the_polling_endpoint_uses_the_same_shape_as_the_page_load(wired, monkeypatch):
    """Two paths serialise this thread and a third appends to it. If they disagree, a message
    changes sides the moment the poll replaces it."""
    monkeypatch.setattr(main, "_session_email", lambda request: B)
    monkeypatch.setattr(main, "_can_access", lambda request, p: True)
    r = wired.get("/api/portal/%s/messages" % TOKEN)
    assert r.status_code == 200
    got = {m["id"]: m for m in r.json()["messages"]}
    assert got[2]["mine"] is False and got[3]["mine"] is True
    assert "author_email" not in got[2]


def test_the_staff_serializer_still_carries_the_full_address():
    """Staff need to know which contact spoke, and they already see every recipient's address in
    the drawer. This is the asymmetry the whole design rests on."""
    m = main._msg(THREAD[1])
    assert m["author_email"] == A
    assert "mine" not in m, "the staff shape has no viewer to be relative to"


def test_the_drawer_payload_lists_the_recipients_primary_first(wired):
    """The staff bubbles only name anyone when there is more than one, so the count has to be
    in the payload."""
    got = main._recipients_or_empty(PID, dict(PROP))
    assert got[0] == A and B in got and len(got) == 2


def test_the_recipient_list_deduplicates_the_primary():
    """get_recipients usually includes the primary contact, so a naive concat shows them twice
    and a two-contact proposal looks like three."""
    import main as m
    m_orig = m.db.get_recipients
    try:
        m.db.get_recipients = lambda pid: [A, A.upper(), B]
        assert m._recipients_or_empty(PID, dict(PROP)) == [A, B]
    finally:
        m.db.get_recipients = m_orig


def test_an_unreadable_recipient_list_costs_the_names_not_the_drawer():
    import main as m
    m_orig = m.db.get_recipients
    try:
        m.db.get_recipients = lambda pid: (_ for _ in ()).throw(RuntimeError("down"))
        assert m._recipients_or_empty(PID, dict(PROP)) == [A], (
            "a failed read should still fall back to the primary contact")
    finally:
        m.db.get_recipients = m_orig


def test_every_customer_facing_path_uses_the_customer_serializer():
    """The gap a mutation found. Only the POLL endpoint was covered, so reverting the PAGE LOAD
    to the staff serializer survived the whole suite — and the page load is what a customer sees
    first, before any poll runs.

    A sweep rather than an endpoint test: api_get_portal reads a dozen tables and stubbing all of
    them would test the stubs. What can actually go wrong here is one call site using the wrong
    serializer name, and that is exactly what this catches. Same shape as the notify_team/token
    sweep, written after the same kind of omission.
    """
    src = (pathlib.Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))

    # Where the customer's own thread is built. All three must be the customer shape.
    assert 'vm["messages"] = [_customer_msg(' in code, (
        "the page load serialises the thread with the staff shape, so the second contact sees "
        "the first contact's reply as their own")
    assert "_customer_msg(m, _session_email(request))" in code, "the poll uses the staff shape"
    assert '"message": _customer_msg(row,' in code, (
        "the row echoed back from a POST does not match the shape the client polls")

    # And the bare staff serializer is used ONLY where staff are the audience.
    # `(?<!def )` so the definition itself is not counted as a call; `(?<![_\w])` so
    # _customer_msg( and _recent_msg( do not match.
    bare = [code[:m.start()].count("\n") + 1
            for m in re.finditer(r"(?<!def )(?<![_\w])_msg\(", code)]
    # Located by PATTERN, not by the exact call text. This used to index on the literal
    # `[_msg(m) for m in db.list_messages(proposal_id)]`, and adding the `include_internal=True`
    # argument to that same call (2026-08-19, so the staff drawer keeps seeing internal cards)
    # broke it with a bare ValueError — a test that fails on an argument it has no opinion about.
    _admin = re.search(r'"messages": \[_msg\(m\) for m in db\.list_messages\(', code)
    assert _admin, ("the staff drawer no longer builds its thread with _msg() — rewrite this test "
                    "rather than deleting it; the claim below is what it protects")
    admin_line = code[:_admin.start()].count("\n") + 1
    assert bare == [admin_line], (
        "_msg() is used at line(s) %s; the only customer-visible payload it may build is the "
        "staff drawer's at %s" % (bare, admin_line))


def test_all_three_customer_paths_agree_on_the_same_row():
    """Stated as one assertion so the three cannot drift apart quietly: page load, poll, and the
    row echoed back from a POST all describe row 2 identically for a given viewer."""
    row = THREAD[1]
    shapes = [main._customer_msg(row, B) for _ in range(3)]
    assert shapes[0] == shapes[1] == shapes[2]
    assert set(shapes[0]) == {"id", "author_kind", "body", "msg_type", "mine",
                              "author_first_name", "meta", "created_at"}, (
        "the customer message shape changed; check every path that builds one")


# ── telling the OTHER contact what was done ──────────────────────────────────
# Hanz: "For example one contact sent the deposit it should update on the 2nd contact as well.
# But, we need to inform the other contact of what has been done."
#
# Every recipient used to get the same second-person copy, so "we've recorded YOUR check" landed
# on the contact who had not paid — which reads as either a mistake or a second charge. The actor
# keeps the receipt; everyone else gets a third-person heads-up naming them.
def _capture(monkeypatch):
    sent = []
    monkeypatch.setattr(main.email_sender, "send_customer_update",
                        lambda e, url, proj, heading, body, **k:
                        sent.append({"to": e, "heading": heading, "body": body}) or True)
    monkeypatch.setattr(main.email_sender, "proposal_reply_to", lambda t: None)
    monkeypatch.setattr(main.db, "get_recipients", lambda pid: [A, B])
    monkeypatch.setattr(main.config, "PUBLIC_BASE_URL", "https://portal.example.com")
    return sent


def test_the_actor_gets_the_receipt_and_the_peer_gets_the_heads_up(monkeypatch):
    sent = _capture(monkeypatch)
    main._notify_customer(dict(PROP), "We've received your deposit details",
                          "<p>we've recorded your check</p>",
                          actor_email=A,
                          peer_heading="The deposit has been sent",
                          peer_body_html="<p>Dana sent the check</p>")
    by = {m["to"]: m for m in sent}
    assert by[A]["body"] == "<p>we've recorded your check</p>"
    assert by[B]["body"] == "<p>Dana sent the check</p>"
    assert by[B]["heading"] == "The deposit has been sent"


def test_the_receipt_never_lands_on_somebody_who_did_nothing(monkeypatch):
    """The whole point. Asserted as an absence, because that is the failure mode."""
    sent = _capture(monkeypatch)
    main._notify_customer(dict(PROP), "h", "<p>we've recorded your check</p>",
                          actor_email=A, peer_body_html="<p>Dana sent the check</p>")
    peer = next(m for m in sent if m["to"] == B)
    assert "your check" not in peer["body"]


def test_an_unknown_actor_gives_everyone_the_third_person_version(monkeypatch):
    """A receipt on somebody who did nothing is the harmful direction; "Dana approved this" sent
    to Dana is merely redundant. So an unknown actor fails toward redundant."""
    sent = _capture(monkeypatch)
    main._notify_customer(dict(PROP), "h", "<p>your check</p>",
                          actor_email=None, peer_body_html="<p>Dana sent the check</p>")
    assert all("your check" not in m["body"] for m in sent)
    assert len(sent) == 2


def test_matching_the_actor_ignores_case(monkeypatch):
    sent = _capture(monkeypatch)
    main._notify_customer(dict(PROP), "h", "<p>receipt</p>", actor_email=A.upper(),
                          peer_body_html="<p>peer</p>")
    assert next(m for m in sent if m["to"] == A)["body"] == "<p>receipt</p>"


def test_a_milestone_with_no_peer_copy_is_unchanged(monkeypatch):
    """Staff milestones (deposit received) have no customer actor, so everybody still gets one
    identical email. The new arguments are optional and this proves it."""
    sent = _capture(monkeypatch)
    main._notify_customer(dict(PROP), "Deposit received", "<p>we have your deposit</p>")
    assert [m["body"] for m in sent] == ["<p>we have your deposit</p>"] * 2


def test_a_single_contact_proposal_just_gets_its_receipt(monkeypatch):
    """The common case must not start reading in the third person about itself."""
    sent = _capture(monkeypatch)
    monkeypatch.setattr(main.db, "get_recipients", lambda pid: [A])
    main._notify_customer(dict(PROP), "h", "<p>your check</p>", actor_email=A,
                          peer_body_html="<p>Dana sent the check</p>")
    assert len(sent) == 1 and sent[0]["body"] == "<p>your check</p>"


def test_the_peer_copy_carries_no_email_address(monkeypatch):
    """First names only, same rule as the thread. A milestone email is the other place a peer's
    address could reach a customer."""
    sent = _capture(monkeypatch)
    main._notify_customer(dict(PROP), "h", "<p>receipt</p>", actor_email=A,
                          peer_body_html="<p><strong>%s</strong> sent the check</p>"
                                         % main._first_name_of(A))
    peer = next(m for m in sent if m["to"] == B)
    assert A not in peer["body"] and "@" not in peer["body"]
    assert "Dana" in peer["body"]


def test_all_three_customer_milestones_pass_an_actor():
    """approve, deposit-submitted and contacts are the three a CUSTOMER performs. A site that
    forgets actor_email silently reverts to "your deposit" for everybody — invisible unless a
    proposal has two contacts, which most do not."""
    src = (pathlib.Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    calls = [m.start() for m in re.finditer(r"_notify_customer\(\n", code)]
    assert len(calls) == 4, "the milestone list changed; check which are customer-driven"
    withactor = 0
    for i in calls:
        depth, j = 0, code.index("(", i)
        k = j
        while k < len(code):
            if code[k] == "(":
                depth += 1
            elif code[k] == ")":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        if "actor_email=" in code[j:k]:
            withactor += 1
    assert withactor == 3, (
        "%d of 4 _notify_customer calls pass an actor; the three customer-driven milestones "
        "(approve, deposit submitted, contacts) must, and staff deposit-received must not"
        % withactor)
