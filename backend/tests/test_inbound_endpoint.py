"""POST /api/inbound/resend — the routing decisions, end to end through the
endpoint with the DB and Resend stubbed out.

The pure helpers live in test_inbound.py; what's here is the part that decides
what an arriving email BECOMES: a customer message, a staff reply relayed to the
customer, a forward to the roster, or nothing. Signature verification is stubbed
(it has its own tests) so each case is only about routing.
"""
import pytest
from fastapi.testclient import TestClient

import config

PRIMARY = "notify.wetreadwell.com"
LEGACY = "piaxenoizh.resend.app"
TOKEN = "tokABC123"
PID = "pid-0001"
CUSTOMER = "customer@example.com"
STAFF = "kyle@wetreadwell.com"

PROPOSAL = {"proposal_id": PID, "token": TOKEN, "project_name": "Test Project",
            "customer_email": CUSTOMER}


@pytest.fixture
def env(monkeypatch):
    """Stub the webhook's whole outside world and record what it tried to do."""
    import main
    import email_sender
    import inbound

    calls = {"messages": [], "sends": [], "reply_notifications": []}

    monkeypatch.setattr(config, "RESEND_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setattr(config, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(config, "EMAIL_FROM", f"Treadwell <proposals@{PRIMARY}>")
    monkeypatch.setattr(config, "RESEND_INBOUND_DOMAIN", PRIMARY)
    monkeypatch.setattr(config, "RESEND_INBOUND_LEGACY_DOMAINS", [LEGACY])
    monkeypatch.setattr(config, "RESEND_INBOUND_DOMAINS", [PRIMARY, LEGACY])
    monkeypatch.setattr(config, "INBOUND_SENDER_FALLBACK", True)
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://portal.example.com")

    monkeypatch.setattr(main.inbound, "verify_svix", lambda *a, **k: True)
    monkeypatch.setattr(main.db, "get_proposal_by_token",
                        lambda t: dict(PROPOSAL) if t == TOKEN else None)
    monkeypatch.setattr(main.db, "get_proposal_by_token_ci",
                        lambda t: dict(PROPOSAL) if (t or "").lower() == TOKEN.lower() else None)
    monkeypatch.setattr(main.db, "list_proposals_by_email", lambda e: [])
    monkeypatch.setattr(main.db, "has_email_message", lambda pid, eid: False)
    monkeypatch.setattr(main.db, "get_recipients", lambda pid: [CUSTOMER])
    monkeypatch.setattr(main.db, "add_message",
                        lambda *a, **k: calls["messages"].append({"args": a, "kwargs": k}))
    # Optional proposal_id, matching the real signature: the endpoint re-checks the roster
    # once it knows the project, so this project's per-project additions count too.
    monkeypatch.setattr(email_sender, "staff_emails", lambda proposal_id=None: {STAFF})
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

    # A real inbound payload carries an SPF/DKIM verdict from the receiving MTA;
    # the staff path is gated on it, so the default fixture supplies a passing one.
    # Tests that care about spoofing override calls["headers"].
    calls["headers"] = {"authentication-results":
                        "amazonses.com; spf=pass; dkim=pass header.i=@wetreadwell.com"}

    def fake_get(url, headers=None, timeout=None):
        class R:
            def raise_for_status(self):
                return None

            def json(self):
                return {"text": calls.get("body", "Sounds good, go ahead."),
                        "headers": calls.get("headers")}
        return R()

    monkeypatch.setattr(main.httpx, "get", fake_get)
    calls["client"] = TestClient(main.app)
    calls["inbound"] = inbound
    return calls


def post(env, to, sender, subject="Re: Your proposal"):
    return env["client"].post("/api/inbound/resend", json={
        "type": "email.received",
        "data": {"email_id": "em_test_1", "to": to, "from": sender, "subject": subject},
    })


# ── the customer replies ─────────────────────────────────────────────────────
def test_customer_reply_files_to_thread_and_forwards_with_proposal_reply_to(env):
    r = post(env, [f"{TOKEN}@{PRIMARY}"], CUSTOMER)
    assert r.status_code == 200 and r.json()["verified"] is True
    (msg,) = env["messages"]
    assert msg["args"][1] == "customer" and msg["args"][2] == CUSTOMER
    assert msg["kwargs"]["meta"]["email_id"] == "em_test_1"
    # The forward's Reply-To is the PROPOSAL, not the customer: a staff reply from
    # their own inbox comes back through the webhook instead of going direct.
    (fwd,) = env["sends"]
    assert fwd["reply_to"] == f"{TOKEN}@{PRIMARY}"


def test_reply_to_a_retired_receiving_domain_still_lands(env):
    """The whole point of the legacy list — Reply-To addresses already sitting in
    customers' inboxes keep working after the branded domain takes over."""
    r = post(env, [f"{TOKEN}@{LEGACY}"], CUSTOMER)
    assert r.json()["verified"] is True
    assert env["messages"][0]["args"][1] == "customer"


def test_unverified_sender_is_forwarded_but_never_posted(env):
    r = post(env, [f"{TOKEN}@{PRIMARY}"], "stranger@elsewhere.com")
    assert r.json()["verified"] is False
    assert env["messages"] == []
    (fwd,) = env["sends"]
    assert "UNVERIFIED SENDER" in fwd["html"]
    assert fwd["reply_to"] == "stranger@elsewhere.com"


# ── staff replies by email (the new path) ────────────────────────────────────
def test_staff_reply_posts_as_staff_and_notifies_the_customer(env):
    r = post(env, [f"{TOKEN}@{PRIMARY}"], f"Kyle Loseke <{STAFF}>")
    assert r.json()["staff"] is True
    (msg,) = env["messages"]
    assert msg["args"][1] == "staff" and msg["args"][2] == STAFF
    # Customer is notified exactly as if the reply had been typed in the portal.
    (note,) = env["reply_notifications"]
    assert note["to"] == CUSTOMER and note["reply_to"] == f"{TOKEN}@{PRIMARY}"
    assert "Sounds good" in note["message"]
    # And the roster is NOT re-emailed — it's where the reply came from.
    assert env["sends"] == []


def test_staff_membership_is_case_insensitive_via_roster(env):
    r = post(env, [f"{TOKEN}@{PRIMARY}"], "KYLE@WeTreadwell.com")
    assert r.json()["staff"] is True
    assert env["messages"][0]["args"][1] == "staff"


def test_forged_staff_from_cannot_speak_as_treadwell(env):
    """Roster membership alone must not grant the privileged path — a From header is
    forgeable and svix only proves the webhook came from Resend. Without a passing
    SPF/DKIM verdict the message is demoted, never posted as Treadwell."""
    env["headers"] = {"authentication-results": "amazonses.com; spf=fail; dkim=fail"}
    r = post(env, [f"{TOKEN}@{PRIMARY}"], STAFF)
    assert "staff" not in r.json()
    # Not posted as Treadwell, and the customer was never emailed on its behalf.
    assert env["reply_notifications"] == []
    assert [m for m in env["messages"] if m["args"][1] == "staff"] == []
    # Staff still see it — it goes out as an unverified-sender forward.
    (fwd,) = env["sends"]
    assert "UNVERIFIED SENDER" in fwd["html"]


def test_missing_auth_verdict_also_demotes_staff(env):
    """Absent header → fail closed. The cost is a roster forward; the alternative
    is letting a forged From post into a customer's thread."""
    env["headers"] = None
    r = post(env, [f"{TOKEN}@{PRIMARY}"], STAFF)
    assert "staff" not in r.json()
    assert env["reply_notifications"] == []


# ── routing by threading header (the clean-address mechanism) ─────────────────
def test_reply_to_the_clean_address_routes_by_thread_header(env):
    """The address carries no token at all — the proposal comes from the Message-ID
    we stamped on the outbound email and Gmail echoed back in References."""
    import email_sender
    env["headers"] = {
        "authentication-results": "spf=pass; dkim=pass",
        "references": ["<treadwell-portal.02c9e3ca878badf6ec1121e7@wetreadwell.com>",
                       email_sender.proposal_anchor(TOKEN)],
    }
    r = post(env, [f"proposals@{PRIMARY}"], CUSTOMER)
    assert r.json()["verified"] is True
    (msg,) = env["messages"]
    assert msg["args"][0] == PID and msg["args"][1] == "customer"


def test_thread_header_wins_over_sender_matching(env, monkeypatch):
    """Header routing is exact; sender matching is a guess. The exact one must win
    even when the sender maps cleanly to a different proposal."""
    import main
    import email_sender
    monkeypatch.setattr(main.db, "list_proposals_by_email",
                        lambda e: [dict(PROPOSAL, proposal_id="WRONG-PID")])
    env["headers"] = {"authentication-results": "spf=pass; dkim=pass",
                      "in-reply-to": email_sender.proposal_anchor(TOKEN)}
    post(env, [f"proposals@{PRIMARY}"], CUSTOMER)
    assert env["messages"][0]["args"][0] == PID


def test_clean_address_with_no_thread_header_falls_back_to_sender(env, monkeypatch):
    """A freshly composed email to proposals@ has no thread to follow."""
    import main
    monkeypatch.setattr(main.db, "list_proposals_by_email",
                        lambda e: [dict(PROPOSAL)] if e == CUSTOMER else [])
    env["headers"] = {"authentication-results": "spf=pass; dkim=pass"}
    r = post(env, [f"proposals@{PRIMARY}"], CUSTOMER)
    assert r.json()["verified"] is True
    assert env["messages"][0]["args"][1] == "customer"


# ── loop and auto-responder guards ───────────────────────────────────────────
def test_mail_from_ourselves_is_ignored(env):
    r = post(env, [f"{TOKEN}@{PRIMARY}"], f"proposals@{PRIMARY}")
    assert r.json()["ignored"] == "own_address"
    assert env["messages"] == [] and env["sends"] == []


def test_customer_auto_reply_is_recorded_but_not_forwarded(env):
    r = post(env, [f"{TOKEN}@{PRIMARY}"], CUSTOMER, subject="Automatic reply: away")
    assert r.json()["ignored"] == "auto_reply"
    assert len(env["messages"]) == 1        # the record is kept
    assert env["sends"] == []               # nobody is paged for an out-of-office


def test_staff_auto_reply_never_reaches_the_customer(env):
    r = post(env, [f"{TOKEN}@{PRIMARY}"], STAFF, subject="Out of Office")
    assert r.json()["ignored"] == "auto_reply"
    assert env["messages"] == [] and env["reply_notifications"] == []


# ── mail with no token in the address ────────────────────────────────────────
def test_mail_to_the_branded_address_is_matched_by_sender(env, monkeypatch):
    """Someone replies to proposals@ (or a reply loses its token): match the
    sender to exactly one proposal and file it there."""
    import main
    monkeypatch.setattr(main.db, "list_proposals_by_email",
                        lambda e: [dict(PROPOSAL)] if e == CUSTOMER else [])
    r = post(env, [f"proposals@{PRIMARY}"], CUSTOMER)
    assert r.json()["verified"] is True
    assert env["messages"][0]["args"][1] == "customer"


def test_ambiguous_sender_is_forwarded_not_guessed(env, monkeypatch):
    import main
    monkeypatch.setattr(main.db, "list_proposals_by_email",
                        lambda e: [dict(PROPOSAL), dict(PROPOSAL, proposal_id="pid-0002")])
    r = post(env, [f"proposals@{PRIMARY}"], CUSTOMER)
    assert r.json()["unmatched"] is True
    assert env["messages"] == []
    (fwd,) = env["sends"]
    assert "UNMATCHED" in fwd["html"] and "no single proposal" in fwd["html"]
    assert "Sounds good" in fwd["html"]          # staff can read it and route it


def test_unknown_sender_to_branded_address_is_forwarded(env):
    r = post(env, [f"hello@{PRIMARY}"], "someone@random.com")
    assert r.json()["unmatched"] is True
    (fwd,) = env["sends"]
    assert "UNMATCHED" in fwd["html"]
    assert fwd["reply_to"] == "someone@random.com"


def test_staff_emailing_the_branded_address_goes_to_a_human(env, monkeypatch):
    """Sender-matching must never speak for staff: a staff member's address is on
    proposals as a notify recipient, not as the customer."""
    import main
    monkeypatch.setattr(main.db, "list_proposals_by_email", lambda e: [dict(PROPOSAL)])
    r = post(env, [f"proposals@{PRIMARY}"], STAFF)
    assert r.json()["unmatched"] is True
    assert env["messages"] == [] and env["reply_notifications"] == []


def test_fallback_off_drops_untokened_mail(env, monkeypatch):
    """Staging's posture: it shares the Resend account with production, so with the
    fallback off it must neither file nor forward mail it can't match by token."""
    monkeypatch.setattr(config, "INBOUND_SENDER_FALLBACK", False)
    r = post(env, [f"proposals@{PRIMARY}"], CUSTOMER)
    assert r.json()["ignored"] == "no_match"
    assert env["messages"] == [] and env["sends"] == []


def test_legacy_domain_never_uses_sender_matching(env, monkeypatch):
    """Prod ignores non-token mail on the shared domain — that's what stops a
    staging tester's reply being matched into a production proposal."""
    import main
    monkeypatch.setattr(main.db, "list_proposals_by_email", lambda e: [dict(PROPOSAL)])
    r = post(env, [f"some-staging-token@{LEGACY}"], CUSTOMER)
    assert r.json()["ignored"] == "no_match"
    assert env["messages"] == [] and env["sends"] == []


# ── plumbing that must keep working ──────────────────────────────────────────
def test_duplicate_delivery_is_dropped(env, monkeypatch):
    import main
    monkeypatch.setattr(main.db, "has_email_message", lambda pid, eid: True)
    r = post(env, [f"{TOKEN}@{PRIMARY}"], CUSTOMER)
    assert r.json()["ignored"] == "duplicate"
    assert env["messages"] == []


def test_body_fetch_failure_returns_500_so_svix_retries(env, monkeypatch):
    import main

    def boom(url, headers=None, timeout=None):
        raise RuntimeError("resend down")

    monkeypatch.setattr(main.httpx, "get", boom)
    r = post(env, [f"{TOKEN}@{PRIMARY}"], CUSTOMER)
    assert r.status_code == 500
    assert env["messages"] == []


def test_quoted_history_is_stripped_before_it_reaches_the_thread(env):
    env["body"] = "Yes, proceed.\n\nOn Thu, Jul 30 Treadwell <x@y> wrote:\n> old thread"
    post(env, [f"{TOKEN}@{PRIMARY}"], CUSTOMER)
    assert env["messages"][0]["args"][3] == "Yes, proceed."


def test_not_configured_without_a_signing_secret(env, monkeypatch):
    monkeypatch.setattr(config, "RESEND_WEBHOOK_SECRET", "")
    r = post(env, [f"{TOKEN}@{PRIMARY}"], CUSTOMER)
    assert r.status_code == 503
