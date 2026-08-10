"""Inbound-email helpers (inbound.py) + reply-to precedence. Pure logic; the
webhook endpoint itself is exercised by the staging end-to-end test, per repo
convention."""
import base64
import hashlib
import hmac as hmac_mod
import time

import config
import email_sender
import inbound

# ── svix signature verification ───────────────────────────────────────────────
KEY = base64.b64encode(b"supersecretkey123").decode()
SECRET = "whsec_" + KEY


def _sign(svix_id: str, ts: str, body: bytes, key_b64: str = KEY) -> str:
    mac = hmac_mod.new(base64.b64decode(key_b64), f"{svix_id}.{ts}.".encode() + body, hashlib.sha256)
    return "v1," + base64.b64encode(mac.digest()).decode()


def test_valid_signature_accepted():
    body, ts = b'{"type":"email.received"}', str(int(time.time()))
    sig = _sign("msg_1", ts, body)
    assert inbound.verify_svix(SECRET, "msg_1", ts, sig, body) is True


def test_bad_signature_rejected():
    body, ts = b"{}", str(int(time.time()))
    assert inbound.verify_svix(SECRET, "msg_1", ts, "v1,AAAA", body) is False


def test_tampered_body_rejected():
    ts = str(int(time.time()))
    sig = _sign("msg_1", ts, b'{"a":1}')
    assert inbound.verify_svix(SECRET, "msg_1", ts, sig, b'{"a":2}') is False


def test_expired_timestamp_rejected():
    old = str(int(time.time()) - 3600)
    sig = _sign("msg_1", old, b"{}")
    assert inbound.verify_svix(SECRET, "msg_1", old, sig, b"{}") is False


def test_multiple_signature_entries_one_valid():
    body, ts = b"{}", str(int(time.time()))
    good = _sign("m", ts, body)
    assert inbound.verify_svix(SECRET, "m", ts, f"v1,BOGUS {good}", body) is True


def test_non_v1_entries_skipped_and_missing_headers_fail():
    body, ts = b"{}", str(int(time.time()))
    assert inbound.verify_svix(SECRET, "m", ts, "v2,whatever", body) is False
    assert inbound.verify_svix(SECRET, "", ts, _sign("m", ts, body), body) is False
    assert inbound.verify_svix(SECRET, "m", "not-a-number", "v1,x", body) is False
    assert inbound.verify_svix("", "m", ts, "v1,x", body) is False


# ── token extraction from recipients ──────────────────────────────────────────
DOM = "piaxenoizh.resend.app"


def test_find_token_plain_and_named():
    assert inbound.find_token(["AbC123@piaxenoizh.resend.app"], DOM) == "AbC123"
    assert inbound.find_token(["Treadwell <tok9@PIAXENOIZH.RESEND.APP>"], DOM) == "tok9"


def test_find_token_scans_past_other_addresses():
    assert inbound.find_token(["someone@gmail.com", "tok@piaxenoizh.resend.app"], DOM) == "tok"


def test_find_token_wrong_domain_or_empty():
    assert inbound.find_token(["tok@other.resend.app"], DOM) is None
    assert inbound.find_token([], DOM) is None
    assert inbound.find_token(["tok@piaxenoizh.resend.app"], "") is None


# ── multi-domain ingest (branded primary + retired legacy) ────────────────────
PRIMARY = "notify.wetreadwell.com"
BOTH = [PRIMARY, DOM]


def test_find_token_accepts_primary_and_legacy():
    """One list, both domains: a reply to a Reply-To we no longer mint still
    resolves, which is what keeps already-sent emails working after the move."""
    assert inbound.find_token([f"tokA@{PRIMARY}"], BOTH) == "tokA"
    assert inbound.find_token([f"tokB@{DOM}"], BOTH) == "tokB"
    assert inbound.find_token([f"Treadwell <tokC@{PRIMARY.upper()}>"], BOTH) == "tokC"
    assert inbound.find_token(["nope@gmail.com"], BOTH) is None
    assert inbound.find_token([f"tok@{PRIMARY}"], []) is None


def test_addressed_to_domain_is_primary_only():
    """The sender-matching fallback keys off this: mail to the branded domain
    qualifies, mail to the legacy domain never does."""
    assert inbound.addressed_to_domain([f"proposals@{PRIMARY}"], PRIMARY) is True
    assert inbound.addressed_to_domain(["a@x.com", f"x@{PRIMARY}"], PRIMARY) is True
    assert inbound.addressed_to_domain([f"whatever@{DOM}"], PRIMARY) is False
    assert inbound.addressed_to_domain(["plain-text-not-an-address"], PRIMARY) is False
    assert inbound.addressed_to_domain([f"x@{PRIMARY}"], "") is False


def test_proposal_reply_to_mints_primary_only(monkeypatch):
    """Legacy domains are accepted on the way IN and never minted on the way out."""
    monkeypatch.setattr(config, "RESEND_INBOUND_DOMAIN", PRIMARY)
    monkeypatch.setattr(config, "RESEND_INBOUND_LEGACY_DOMAINS", [DOM])
    assert email_sender.proposal_reply_to("tok") == f"tok@{PRIMARY}"


# ── loop guard: mail that came from us ───────────────────────────────────────
OWN_FROM = "Treadwell <proposals@notify.wetreadwell.com>"


def test_is_own_address_catches_self_and_receiving_domains():
    assert inbound.is_own_address("proposals@notify.wetreadwell.com", OWN_FROM, BOTH) is True
    assert inbound.is_own_address("Treadwell <PROPOSALS@Notify.Wetreadwell.com>", OWN_FROM, BOTH) is True
    assert inbound.is_own_address(f"sometoken@{DOM}", OWN_FROM, BOTH) is True
    assert inbound.is_own_address("", OWN_FROM, BOTH) is True          # unusable From
    assert inbound.is_own_address("customer@gmail.com", OWN_FROM, BOTH) is False


# ── auto-responder detection ─────────────────────────────────────────────────
def test_is_auto_reply_subjects():
    assert inbound.is_auto_reply("Automatic reply: Proposal question") is True
    assert inbound.is_auto_reply("Out of Office") is True
    assert inbound.is_auto_reply("auto-response: away") is True
    assert inbound.is_auto_reply("Re: your proposal") is False
    assert inbound.is_auto_reply(None) is False
    # "Auto" as an ordinary word must not trip it — Treadwell bids auto dealerships.
    assert inbound.is_auto_reply("Auto dealership floor quote") is False


def test_is_auto_reply_headers_dict_and_list():
    assert inbound.is_auto_reply("Re: x", {"Auto-Submitted": "auto-replied"}) is True
    assert inbound.is_auto_reply("Re: x", {"Auto-Submitted": "no"}) is False
    assert inbound.is_auto_reply("Re: x", {"Precedence": "bulk"}) is True
    assert inbound.is_auto_reply("Re: x", [{"name": "X-Autoreply", "value": "yes"}]) is True
    assert inbound.is_auto_reply("Re: x", [{"name": "Subject", "value": "hi"}]) is False
    # Malformed payloads are ignored, never raised.
    assert inbound.is_auto_reply("Re: x", "not-headers") is False
    assert inbound.is_auto_reply("Re: x", [None, 42]) is False


# ── staff allowlist for inbound classification ───────────────────────────────
def test_staff_emails_enabled_rows_only(monkeypatch):
    import db
    monkeypatch.setattr(db, "list_notify_recipients", lambda: [
        {"email": "Kyle@WeTreadwell.com", "kind": "general", "enabled": True},
        {"email": "muted@wetreadwell.com", "kind": "general", "enabled": False},
        {"email": "kyleene@wetreadwell.com", "kind": "deposit", "enabled": True},
    ])
    assert email_sender.staff_emails() == {"kyle@wetreadwell.com", "kyleene@wetreadwell.com"}


def test_staff_emails_falls_back_to_env_when_db_down(monkeypatch):
    """A momentarily unreachable table must not silently empty the allowlist —
    that would demote every staff reply to 'unverified sender'."""
    import db

    def boom():
        raise RuntimeError("table gone")

    monkeypatch.setattr(db, "list_notify_recipients", boom)
    monkeypatch.setattr(config, "NOTIFY_EMAILS", ["Bids@wetreadwell.com"])
    monkeypatch.setattr(config, "DEPOSIT_NOTIFY_EMAILS", ["kyleene@wetreadwell.com"])
    assert email_sender.staff_emails() == {"bids@wetreadwell.com", "kyleene@wetreadwell.com"}


# ── threading headers: the project rides in a header, not the address ─────────
# Header shapes below are copied from a REAL Resend inbound payload (a Gmail reply
# on 2026-07-31): `headers` is a dict, and `references` comes back as a LIST.
REAL_REFERENCES = ["<treadwell-portal.02c9e3ca878badf6ec1121e7@wetreadwell.com>",
                   "<0100019fa52bdd38-2037843c@email.amazonses.com>"]
REAL_AUTH = ("amazonses.com; spf=pass (spfCheck: domain of wetreadwell.com designates "
             "209.85.218.52 as permitted sender) client-ip=209.85.218.52; "
             "envelope-from=hanz@wetreadwell.com; dkim=pass header.i=@wetreadwell.com")


def test_outbound_stamps_the_proposal_anchor():
    h = email_sender._thread_headers("c@x.com", "TOK123abc")
    assert email_sender.proposal_anchor("TOK123abc") in h["References"]
    # Per-recipient anchor kept too, so the login code still threads with the proposal.
    assert "treadwell-portal." in h["References"]
    # In-Reply-To is the proposal, so a customer with several projects gets a
    # thread per project rather than one merged pile.
    assert h["In-Reply-To"] == email_sender.proposal_anchor("TOK123abc")


def test_outbound_without_a_token_is_unchanged():
    h = email_sender._thread_headers("c@x.com")
    assert h["References"] == h["In-Reply-To"]
    assert "tw-proposal." not in h["References"]


def test_otp_threads_separately_from_the_proposal():
    """Login codes are transient noise — several may be requested while reading one
    proposal. They thread with each other, never into the proposal conversation."""
    otp = email_sender._otp_headers("c@x.com")
    proposal = email_sender._thread_headers("c@x.com", "SOMETOKEN_longenough")
    assert "treadwell-otp." in otp["References"]
    assert "treadwell-portal." not in otp["References"]
    assert "tw-proposal." not in otp["References"]
    assert otp["References"] != proposal["References"]
    assert otp["In-Reply-To"] != proposal["In-Reply-To"]
    # Two codes to the same person share one thread; different people don't.
    assert email_sender._otp_headers("c@x.com") == otp
    assert email_sender._otp_headers("other@x.com") != otp
    # An OTP anchor must never be mistaken for a proposal anchor on the way back in.
    assert inbound.find_thread_token({"references": [otp["References"]]}) is None


def test_find_thread_token_round_trip():
    """The whole point: what we stamp on the way out is recoverable on the way in."""
    tok = "G3Hu5zMKQ6NIu2REaUITyiFmWk_dHr9O"
    sent = email_sender._thread_headers("c@x.com", tok)
    assert inbound.find_thread_token({"References": sent["References"]}) == tok
    assert inbound.find_thread_token({"In-Reply-To": sent["In-Reply-To"]}) == tok


def test_find_thread_token_real_payload_shapes():
    tok = "TOKzz9_realistic_length"
    anchor = email_sender.proposal_anchor(tok)
    # references as a LIST (what Resend actually returns)
    assert inbound.find_thread_token({"references": REAL_REFERENCES + [anchor]}) == tok
    # in-reply-to wins over references — it's the message actually being answered
    assert inbound.find_thread_token({
        "in-reply-to": anchor,
        "references": [email_sender.proposal_anchor("OTHERTOKEN_longenough")],
    }) == tok
    # list-of-{name,value} shape also tolerated
    assert inbound.find_thread_token([{"name": "References", "value": anchor}]) == tok


def test_find_thread_token_ignores_implausibly_short_ids():
    """The floor guards against matching some unrelated address that happens to
    look like ours. Real tokens are 32 urlsafe chars."""
    assert inbound.find_thread_token({"in-reply-to": "<tw-proposal.abc@wetreadwell.com>"}) is None


def test_find_thread_token_absent_or_malformed():
    assert inbound.find_thread_token({"references": REAL_REFERENCES}) is None
    assert inbound.find_thread_token({}) is None
    assert inbound.find_thread_token(None) is None
    assert inbound.find_thread_token("not-headers") is None
    assert inbound.find_thread_token([None, 42]) is None
    # A lookalike that isn't ours must not match.
    assert inbound.find_thread_token({"references": ["<tw-proposal@wetreadwell.com>"]}) is None


def test_reply_to_is_one_clean_address_when_configured(monkeypatch):
    """Will's objection: customers shouldn't be asked to reply to a wall of random
    characters. With INBOUND_REPLY_ADDRESS set, nobody sees a token."""
    monkeypatch.setattr(config, "RESEND_INBOUND_DOMAIN", PRIMARY)
    monkeypatch.setattr(config, "INBOUND_REPLY_ADDRESS", f"proposals@{PRIMARY}")
    rt = email_sender.proposal_reply_to("G3Hu5zMKQ6NIu2REaUITyiFmWk_dHr9O")
    assert rt == f"proposals@{PRIMARY}"
    assert "G3Hu5z" not in rt
    # Unset → falls back to the old token@domain form (still routes, just ugly).
    monkeypatch.setattr(config, "INBOUND_REPLY_ADDRESS", "")
    assert email_sender.proposal_reply_to("TOK") == f"TOK@{PRIMARY}"


# ── SPF/DKIM verdict, used to gate the privileged staff path ─────────────────
def test_sender_authenticated_requires_both_spf_and_dkim():
    assert inbound.sender_authenticated({"authentication-results": REAL_AUTH}) is True
    assert inbound.sender_authenticated({"authentication-results": "spf=pass; dkim=fail"}) is False
    assert inbound.sender_authenticated({"authentication-results": "spf=softfail; dkim=pass"}) is False
    # Absent or unreadable → False. Failing closed only costs a staff reply a trip
    # through the roster forward; failing open would let a forged From post as us.
    assert inbound.sender_authenticated({}) is False
    assert inbound.sender_authenticated(None) is False
    assert inbound.sender_authenticated({"authentication-results": ""}) is False


def test_notify_team_passes_reply_to(monkeypatch):
    """Team notifications carry the proposal's inbound address AND its anchor, so replying
    from a staff inbox reaches the thread instead of the send-only From address.

    The anchor half was missing, and this test asserted the promise ("you can just reply")
    without asserting the mechanism that keeps it — so it passed while every staff reply
    was being dropped. Reply-To gets the mail to us; the anchor is what tells us which
    project it belongs to, since INBOUND_REPLY_ADDRESS leaves no token in the address.
    Full story in test_staff_reply_by_email.py."""
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):  # noqa: A002
        captured.update(json)
        class R:
            def raise_for_status(self):
                return None
        return R()

    monkeypatch.setattr(config, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(config, "EMAIL_REPLY_TO", "")
    monkeypatch.setattr(email_sender.httpx, "post", fake_post)

    email_sender.notify_team("Subj", "<p>x</p>", recipients=["team@x.com"],
                             reply_to=f"tok@{PRIMARY}", token="tokABC123")
    assert captured["reply_to"] == f"tok@{PRIMARY}"
    assert "posts your message to the customer" in captured["html"]
    # The mechanism behind that sentence: the reply comes back naming this project.
    assert inbound.find_thread_token(captured["headers"]) == "tokABC123"

    captured.clear()
    email_sender.notify_team("Subj", "<p>x</p>", recipients=["team@x.com"])
    assert "reply_to" not in captured
    assert "headers" not in captured


# ── quoted-reply stripping ────────────────────────────────────────────────────
def test_strip_gmail_quote():
    txt = "Sounds good, let's proceed.\n\nOn Thu, Jul 16, 2026 at 11:25 PM Treadwell <x@y> wrote:\n> old stuff"
    assert inbound.strip_quoted(txt) == "Sounds good, let's proceed."


def test_strip_gmail_wrapped_attribution():
    # Gmail wraps the "On … <email>" attribution onto its own line with "wrote:"
    # on the next line — the first line ends with <email>, not "wrote:".
    txt = ("Ok, thanks.\n\nOn Fri, Jul 17, 2026 at 1:59 AM Treadwell "
           "<proposals@notify.wetreadwell.com>\nwrote:")
    assert inbound.strip_quoted(txt) == "Ok, thanks."


def test_strip_angle_quotes_and_outlook():
    assert inbound.strip_quoted("Yes.\n> earlier message") == "Yes."
    assert inbound.strip_quoted("Ok!\n-----Original Message-----\nFrom: a@b") == "Ok!"
    assert inbound.strip_quoted("Fine.\nFrom: Treadwell <t@x>\nSent: Thursday") == "Fine."


def test_strip_quoted_empty_falls_back_to_original():
    txt = "> the whole reply was written inside the quote"
    assert inbound.strip_quoted(txt) == txt


# ── per-proposal reply-to ─────────────────────────────────────────────────────
def test_proposal_reply_to_requires_domain(monkeypatch):
    monkeypatch.setattr(config, "RESEND_INBOUND_DOMAIN", "")
    assert email_sender.proposal_reply_to("tok") is None
    monkeypatch.setattr(config, "RESEND_INBOUND_DOMAIN", DOM)
    assert email_sender.proposal_reply_to("tok") == f"tok@{DOM}"
    assert email_sender.proposal_reply_to("") is None


def test_send_reply_to_precedence(monkeypatch):
    """Explicit reply_to wins over the global EMAIL_REPLY_TO; global is the
    fallback; neither → no reply_to key."""
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):  # noqa: A002
        captured.update(json)
        class R:  # minimal ok response
            def raise_for_status(self):
                return None
        return R()

    monkeypatch.setattr(config, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(email_sender.httpx, "post", fake_post)

    monkeypatch.setattr(config, "EMAIL_REPLY_TO", "global@x.com")
    email_sender._send(["a@x.com"], "s", "<p>h</p>", reply_to="tok@dom")
    assert captured["reply_to"] == "tok@dom"

    email_sender._send(["a@x.com"], "s", "<p>h</p>")
    assert captured["reply_to"] == "global@x.com"

    monkeypatch.setattr(config, "EMAIL_REPLY_TO", "")
    captured.clear()
    email_sender._send(["a@x.com"], "s", "<p>h</p>")
    assert "reply_to" not in captured
