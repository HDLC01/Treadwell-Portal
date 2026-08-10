"""A Treadwell employee replying from their own inbox must reach the customer.

Hanz, 2026-08-11: "When a Treadwell employee replies through email it doesn't get captured by the
Proposal CRM and doesn't get sent out to the customer."

WHY IT BROKE, AND WHY THE EXISTING TESTS PASSED ANYWAY.

The webhook routes an inbound reply to a project by finding a token, in two places:

  1. the recipient address        — proposals+<token>@... / <token>@notify.wetreadwell.com
  2. the threading headers        — our `tw-proposal.<token>@` anchor, quoted back on reply

Production sets INBOUND_REPLY_ADDRESS, so every proposal advertises ONE clean shared address
with no token in it. That deletes route 1 for real traffic and leaves route 2 as the only one.
Customer mail was fine: send_reply_notification has always passed `token`, so the anchor rides
along. Staff notifications were the one kind of mail that never passed it — notify_team called
_send with no `headers` at all. So a staff reply arrived carrying nothing that named a project,
matched nothing, and took the "unmatched" branch: forwarded back to the roster, never filed as
staff, never relayed to the customer. Silent, and exactly what Hanz saw.

Every test in test_inbound_endpoint.py replies to `<TOKEN>@<domain>`, which is route 1 — a
configuration production has not used since INBOUND_REPLY_ADDRESS went in. That is why a green
suite sat on top of a broken workflow. So the fixture here is deliberately the PRODUCTION shape:
one shared reply address, and the project carried only in the headers.
"""
import pytest
from fastapi.testclient import TestClient

import config

DOMAIN = "notify.wetreadwell.com"
SHARED = f"proposals@{DOMAIN}"          # what INBOUND_REPLY_ADDRESS actually is on prod
TOKEN = "tokABC123"
PID = "pid-0001"
CUSTOMER = "customer@example.com"
SECOND = "partner@example.com"
STAFF = "kyle@wetreadwell.com"
ADDED = "rj@wetreadwell.com"            # on THIS project only, via a notify override
PASSING_AUTH = "amazonses.com; spf=pass; dkim=pass header.i=@wetreadwell.com"

PROPOSAL = {"proposal_id": PID, "token": TOKEN, "project_name": "Test Project",
            "customer_email": CUSTOMER}


@pytest.fixture
def env(monkeypatch):
    import main
    import email_sender
    import inbound

    calls = {"messages": [], "sends": [], "reply_notifications": [],
             "headers": {"authentication-results": PASSING_AUTH}}

    monkeypatch.setattr(config, "RESEND_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setattr(config, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(config, "EMAIL_FROM", f"Treadwell <{SHARED}>")
    monkeypatch.setattr(config, "RESEND_INBOUND_DOMAIN", DOMAIN)
    monkeypatch.setattr(config, "RESEND_INBOUND_DOMAINS", [DOMAIN])
    monkeypatch.setattr(config, "INBOUND_REPLY_ADDRESS", SHARED)
    # Off, as on prod. With it on, an unmatched staff email would be forwarded to the
    # roster — which is the wrong outcome dressed up as activity, and is what was
    # happening. Off, the same email is dropped outright.
    monkeypatch.setattr(config, "INBOUND_SENDER_FALLBACK", False)
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://portal.example.com")

    monkeypatch.setattr(main.inbound, "verify_svix", lambda *a, **k: True)
    monkeypatch.setattr(main.db, "get_proposal_by_token",
                        lambda t: dict(PROPOSAL) if t == TOKEN else None)
    monkeypatch.setattr(main.db, "get_proposal_by_token_ci",
                        lambda t: dict(PROPOSAL) if (t or "").lower() == TOKEN.lower() else None)
    monkeypatch.setattr(main.db, "list_proposals_by_email", lambda e: [])
    monkeypatch.setattr(main.db, "has_email_message", lambda pid, eid: False)
    monkeypatch.setattr(main.db, "get_recipients", lambda pid: [CUSTOMER, SECOND])
    monkeypatch.setattr(main.db, "add_message",
                        lambda *a, **k: calls["messages"].append({"args": a, "kwargs": k}))
    monkeypatch.setattr(main.db, "list_notify_overrides",
                        lambda pid: calls.get("overrides", []))
    monkeypatch.setattr(main.db, "list_notify_recipients", lambda: [{"email": STAFF,
                                                                    "enabled": True}])
    monkeypatch.setattr(email_sender, "_resolve_notify", lambda *a, **k: ["bids@wetreadwell.com"])
    monkeypatch.setattr(email_sender, "_send",
                        lambda to, subj, html_body, *a, **k:
                        calls["sends"].append({"to": to, "subject": subj, "html": html_body,
                                               "reply_to": k.get("reply_to"),
                                               "headers": k.get("headers")}) or True)
    monkeypatch.setattr(
        email_sender, "send_reply_notification",
        lambda email, url, project, reply_to=None, message=None, token=None:
        calls["reply_notifications"].append({"to": email, "reply_to": reply_to,
                                             "message": message, "token": token}) or True)

    def fake_get(url, headers=None, timeout=None):
        class R:
            def raise_for_status(self):
                return None

            def json(self):
                return {"text": calls.get("body", "Confirmed, we can start Monday."),
                        "headers": calls["headers"]}
        return R()

    monkeypatch.setattr(main.httpx, "get", fake_get)
    calls["client"] = TestClient(main.app)
    calls["email_sender"] = email_sender
    calls["inbound"] = inbound
    return calls


def post(env, sender, anchored=True, subject="Re: Test Project"):
    """A reply to the ONE shared address. `anchored` controls whether the sender's mail
    client quoted our proposal anchor back — the only thing that names the project."""
    if anchored:
        env["headers"] = dict(env["headers"],
                              **{"in-reply-to": f"<tw-proposal.{TOKEN}@wetreadwell.com>"})
    return env["client"].post("/api/inbound/resend", json={
        "type": "email.received",
        "data": {"email_id": "em_test_1", "to": [SHARED], "from": sender, "subject": subject},
    })


# ── the reported bug ─────────────────────────────────────────────────────────
def test_staff_reply_to_the_shared_address_reaches_the_customer(env):
    """THE regression test. One shared reply address, project only in the headers."""
    r = post(env, f"Kyle Loseke <{STAFF}>")
    assert r.status_code == 200 and r.json().get("staff") is True
    (msg,) = env["messages"]
    assert msg["args"][1] == "staff" and msg["args"][2] == STAFF
    # Both recipients hear about it, exactly as if it had been typed in the portal.
    assert sorted(n["to"] for n in env["reply_notifications"]) == sorted([CUSTOMER, SECOND])
    assert "start Monday" in env["reply_notifications"][0]["message"]


def test_the_reply_is_filed_in_the_crm_thread_as_staff(env):
    """"doesn't get captured by the Proposal CRM" — the thread insert is what the CRM
    drawer and the board's last-activity read."""
    post(env, STAFF)
    (msg,) = env["messages"]
    assert msg["args"][0] == PID
    assert msg["kwargs"]["meta"]["source"] == "email"
    assert msg["kwargs"]["meta"]["from"] == STAFF


def test_without_the_anchor_the_reply_is_lost(env):
    """The failure mode itself, pinned. Nothing in the message names a project and the sender
    fallback skips staff by design, so with forwarding off the email simply vanishes. This is
    what every staff reply hit before notify_team stamped the anchor, and it is why the fix
    belongs on the SENDING side rather than here."""
    r = post(env, STAFF, anchored=False)
    assert r.json() == {"ok": True, "ignored": "no_match"}
    assert env["messages"] == [] and env["reply_notifications"] == []


def test_the_prod_shape_of_the_bug_bounced_the_reply_back_at_the_team(env, monkeypatch):
    """What Hanz actually saw. Prod runs INBOUND_SENDER_FALLBACK=true, so an unanchored staff
    reply was not merely dropped: it came back to the roster as "Unmatched email", which reads
    like activity while the customer is still waiting. Verified against the live container env
    on 2026-08-11 (prod true, staging false).

    Kept alongside the anchored test so both environments' versions of the failure are on
    record — the fix is the same either way, because it is on the sending side."""
    monkeypatch.setattr(config, "INBOUND_SENDER_FALLBACK", True)
    r = post(env, STAFF, anchored=False)
    assert r.json() == {"ok": True, "unmatched": True}
    (fwd,) = env["sends"]
    assert "UNMATCHED" in fwd["html"] and "sent by staff" in fwd["html"]
    # The two things that were wrong, stated as assertions.
    assert env["messages"] == [], "nothing was filed in the CRM thread"
    assert env["reply_notifications"] == [], "the customer was never told"


def test_with_the_anchor_the_prod_config_relays_it_properly(env, monkeypatch):
    """Same prod config, anchor present: the fallback never gets consulted because route 2
    matched first. This is the before/after pair for the line above."""
    monkeypatch.setattr(config, "INBOUND_SENDER_FALLBACK", True)
    assert post(env, STAFF).json().get("staff") is True
    assert len(env["reply_notifications"]) == 2


# ── the sending side: the anchor has to be on the mail staff reply TO ────────
def test_notify_team_stamps_the_proposal_anchor(env):
    es = env["email_sender"]
    es.notify_team("New proposal question — Test Project", "<p>hi</p>",
                   recipients=[STAFF], reply_to=SHARED, token=TOKEN)
    (sent,) = env["sends"]
    assert sent["headers"] == {"References": f"<tw-proposal.{TOKEN}@wetreadwell.com>",
                               "In-Reply-To": f"<tw-proposal.{TOKEN}@wetreadwell.com>"}


def test_the_anchor_notify_team_sends_is_the_one_the_webhook_reads_back(env):
    """The two halves have to agree on the format, so assert the round trip rather than
    the string twice. A silent rename on either side would put this bug straight back."""
    es, ib = env["email_sender"], env["inbound"]
    es.notify_team("s", "<p>b</p>", recipients=[STAFF], reply_to=SHARED, token=TOKEN)
    assert ib.find_thread_token(env["sends"][0]["headers"]) == TOKEN


def test_notify_team_without_a_token_promises_nothing_it_cannot_do(env):
    """A team email with no project (there is one: the unmatched-email forward) must not
    tell staff that replying posts to a thread, because it cannot be routed."""
    es = env["email_sender"]
    es.notify_team("s", "<p>b</p>", recipients=[STAFF], reply_to=SHARED)
    (sent,) = env["sends"]
    assert sent["headers"] is None
    assert "posts your message to the customer" not in sent["html"]


def test_a_project_email_does_say_you_can_just_reply(env):
    es = env["email_sender"]
    es.notify_team("s", "<p>b</p>", recipients=[STAFF], reply_to=SHARED, token=TOKEN)
    assert "posts your message to the customer" in env["sends"][0]["html"]


def test_the_customer_reply_forward_carries_the_anchor(env):
    """This is the email staff actually press Reply on — "Customer replied by email".
    Verified customer, so Reply-To is us and the anchor must be there to route it."""
    post(env, CUSTOMER)
    (fwd,) = env["sends"]
    assert fwd["reply_to"] == SHARED
    assert env["inbound"].find_thread_token(fwd["headers"]) == TOKEN


def test_an_unverified_forward_carries_no_anchor(env):
    """Reply-To points at the stranger, not at us, so there is no reply for us to route
    — and stamping a project on it would invite staff to answer into a thread the
    sender was never verified against."""
    post(env, "stranger@elsewhere.com")
    (fwd,) = env["sends"]
    assert fwd["headers"] is None
    assert fwd["reply_to"] == "stranger@elsewhere.com"


def test_every_notify_team_that_sets_a_proposal_reply_to_also_sets_the_token():
    """The omission this whole bug is made of, pinned across the file rather than per call site.

    Reply-To and token are two halves of one thing: the address gets the reply to us, the token
    tells us which project it belongs to. A site with only the first advertises reply-by-email
    and then loses the reply — which is exactly what all of them did. There were SEVEN when this
    was written, and the one that got missed on the first pass was in an endpoint a parallel
    branch had deleted, so it did not exist in the tree the fix was applied to.

    Textual on purpose: a new notify_team call is written by copying a neighbour, and this fails
    the moment somebody copies one and drops the token.
    """
    import pathlib
    import re
    src = pathlib.Path(__file__).resolve().parents[1] / "main.py"
    body = src.read_text(encoding="utf-8")
    calls = [m for m in re.finditer(r"email_sender\.notify_team\(", body)]
    assert len(calls) >= 6, "notify_team call sites moved; this test needs rewriting"
    missing = []
    for m in calls:
        # The argument list, brace/paren-counted from the opening paren.
        depth, i = 0, m.end() - 1
        while i < len(body):
            if body[i] == "(":
                depth += 1
            elif body[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        args = body[m.end():i]
        if "proposal_reply_to" in args and "token=" not in args:
            missing.append(body[:m.start()].count("\n") + 1)
    assert not missing, (
        "notify_team at line(s) %s sets a proposal Reply-To but no token, so a staff reply to "
        "that email cannot be routed to its project" % missing)


# ── who counts as staff ──────────────────────────────────────────────────────
def test_someone_added_to_just_this_project_can_reply_to_it(env):
    """A per-project notify override is how somebody gets one project's emails without
    being on the global roster. They were mailed, so they must be able to answer."""
    env["overrides"] = [{"email": ADDED, "mode": "add"}]
    r = post(env, ADDED)
    assert r.json().get("staff") is True
    assert env["messages"][0]["args"][1] == "staff"


def test_an_override_on_another_project_grants_nothing(env):
    """The widened check is scoped to the matched project. list_notify_overrides is
    called with a pid, so an add filed elsewhere must not appear here."""
    import main
    env["overrides"] = []
    r = post(env, ADDED)
    assert r.json().get("staff") is not True
    assert env["messages"] == []


def test_a_muted_staff_member_is_still_staff(env):
    """Muting stops this project's mail reaching them. It does not turn them into a
    customer, and they may still be answering from a forwarded copy."""
    env["overrides"] = [{"email": STAFF, "mode": "mute"}]
    assert post(env, STAFF).json().get("staff") is True


def test_a_recipient_of_this_proposal_is_never_promoted_to_staff(env):
    """The hole the widened check would otherwise open. An override is typed by hand, so
    a customer's address can land in one; staff is the privileged path that relays a
    message to every recipient AS Treadwell. Being on the proposal has to win."""
    env["overrides"] = [{"email": CUSTOMER, "mode": "add"}]
    r = post(env, CUSTOMER)
    assert r.json().get("staff") is not True
    assert r.json()["verified"] is True
    assert env["messages"][0]["args"][1] == "customer"


def test_a_forged_from_still_cannot_speak_as_treadwell(env):
    """The SPF/DKIM gate is unchanged by any of this, and is re-asserted here because
    the anchor is now attacker-visible: it ships in every staff notification, so a
    leaked one plus a guessed roster address is precisely the attempt this blocks."""
    env["headers"] = {"authentication-results": "amazonses.com; spf=fail; dkim=fail"}
    r = post(env, STAFF)
    assert r.json().get("staff") is not True
    assert env["messages"] == []


def test_a_staff_out_of_office_is_not_relayed_to_the_customer(env):
    env["headers"] = dict(env["headers"], **{"auto-submitted": "auto-replied"})
    r = post(env, STAFF, subject="Automatic reply: Test Project")
    assert r.json()["ignored"] == "auto_reply"
    assert env["reply_notifications"] == []
