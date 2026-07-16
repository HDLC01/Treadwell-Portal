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


# ── quoted-reply stripping ────────────────────────────────────────────────────
def test_strip_gmail_quote():
    txt = "Sounds good, let's proceed.\n\nOn Thu, Jul 16, 2026 at 11:25 PM Treadwell <x@y> wrote:\n> old stuff"
    assert inbound.strip_quoted(txt) == "Sounds good, let's proceed."


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
