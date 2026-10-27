"""A login code lives 5 minutes at most (Hanz, 2026-10-28).

config.OTP_TTL_MINUTES defaults to 5 and is CAPPED at 5: PORTAL_OTP_TTL_MINUTES can shorten it but
never lengthen it, a value below 1 is raised to 1, and anything that is not a whole number falls
back to 5 instead of stopping the app from starting. The code email prints the same number, and a
code already issued keeps the expiry stored with it.
"""
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import config
import customer_auth as ca
import email_sender

BACKEND = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("raw,want", [
    ("5", 5), ("3", 3), ("1", 1),
    ("10", 5), ("6", 5), ("1440", 5),         # above the cap: clamped
    ("0", 1), ("-4", 1),                      # below 1: clamped
    ("", 5), ("abc", 5), ("4.5", 5), (None, 5),   # not a whole number: the default
    (" 2 ", 2),
])
def test_the_ttl_is_clamped_to_one_to_five_minutes(raw, want):
    assert config.otp_ttl_minutes(raw) == want


def _ttl_with_env(value):
    """config.OTP_TTL_MINUTES as a fresh interpreter computes it at import with this env."""
    env = dict(os.environ)
    env.pop("PORTAL_OTP_TTL_MINUTES", None)
    if value is not None:
        env["PORTAL_OTP_TTL_MINUTES"] = value
    out = subprocess.run([sys.executable, "-c", "import config; print(config.OTP_TTL_MINUTES)"],
                         cwd=str(BACKEND), env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return int(out.stdout.strip())


def test_the_setting_at_import_defaults_to_five_and_never_exceeds_it():
    assert _ttl_with_env(None) == 5, "the default is no longer 5 minutes"
    assert _ttl_with_env("10") == 5, "the old 10-minute value in a .env still lengthens the code"
    assert _ttl_with_env("3") == 3
    assert _ttl_with_env("ten") == 5


def test_a_new_code_expires_in_the_configured_minutes(monkeypatch):
    stored = {}
    monkeypatch.setattr(ca.db, "upsert_login_code",
                        lambda email, h, expires: stored.update(expires=expires))
    monkeypatch.setattr(config, "OTP_TTL_MINUTES", config.otp_ttl_minutes("10"))
    before = datetime.now(timezone.utc)
    ca.issue_code("dana@customer.example")
    assert before + timedelta(minutes=5) <= stored["expires"] <= (
        datetime.now(timezone.utc) + timedelta(minutes=5))


def test_the_code_email_says_five_minutes(monkeypatch):
    sent = []
    monkeypatch.setattr(email_sender, "_send", lambda to, subject, html, *a, **k:
                        sent.append(html) or True)
    monkeypatch.setattr(config, "OTP_TTL_MINUTES", config.otp_ttl_minutes(None))
    email_sender.send_otp("dana@customer.example", "123456", "Acme HQ")
    assert "This code expires in 5 minutes." in sent[0]


def test_a_code_already_issued_keeps_its_stored_expiry(monkeypatch):
    """A code issued under the old 10 minutes is judged by the expiry stored with it, not by the
    new setting: 8 minutes in, it still works."""
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(ca.db, "get_login_code", lambda email: {
        "expires_at": now + timedelta(minutes=2), "attempts": 0,
        "code_hash": ca.hash_code("123456"), "created_at": now - timedelta(minutes=8)})
    assert ca.verify_code("dana@customer.example", "123456") == (True, "")
