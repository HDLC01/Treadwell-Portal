"""POST /api/admin/deposit/{deposit_id}/reveal -- the ONE place the portal will decrypt a
customer's bank numbers back to plaintext, for a staff member looking at one specific deposit in
the CRM drawer. SERVICE_TOKEN-gated like every other /api/admin/* route (asserted here); the tool
side additionally gates this behind its own nav-access check before it ever calls in (see
nav_access.py in the proposal-tool repo -- out of scope for this file).

Every test that reaches the "who/when/which deposit" audit log asserts the numbers are NOT in it --
that assertion is the point of this file existing at all, not a courtesy check.
"""
import logging

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

import bank_crypto
import config
import main


@pytest.fixture
def reveal(monkeypatch):
    monkeypatch.setattr(config, "SERVICE_TOKEN", "svc-tok-xyz")
    monkeypatch.setattr(config, "PORTAL_BANK_KEY", Fernet.generate_key().decode("ascii"))
    tc = TestClient(main.app)
    tc.monkeypatch = monkeypatch
    return tc


def _auth(tc):
    return {"x-service-token": "svc-tok-xyz"}


def test_reveal_requires_service_token(reveal, monkeypatch):
    """No/wrong token -> 401, and the deposit lookup never even runs -- a caller without the
    token learns nothing about whether the id exists."""
    def _boom(deposit_id):
        raise AssertionError("get_deposit_bank_details must not run without the service token")
    monkeypatch.setattr(main.db, "get_deposit_bank_details", _boom)
    r = reveal.post("/api/admin/deposit/dep-1/reveal", json={"staff_email": "kyle@wetreadwell.com"})
    assert r.status_code == 401
    r2 = reveal.post("/api/admin/deposit/dep-1/reveal",
                     json={"staff_email": "kyle@wetreadwell.com"},
                     headers={"x-service-token": "wrong"})
    assert r2.status_code == 401


def test_reveal_requires_staff_email(reveal, monkeypatch):
    monkeypatch.setattr(main.db, "get_deposit_bank_details",
                        lambda deposit_id: (_ for _ in ()).throw(
                            AssertionError("must not reach the DB without staff_email")))
    r = reveal.post("/api/admin/deposit/dep-1/reveal", json={}, headers=_auth(reveal))
    assert r.status_code == 400
    assert "staff_email" in r.json()["error"]


def test_reveal_404_when_deposit_missing(reveal, monkeypatch):
    monkeypatch.setattr(main.db, "get_deposit_bank_details", lambda deposit_id: None)
    r = reveal.post("/api/admin/deposit/dep-1/reveal",
                    json={"staff_email": "kyle@wetreadwell.com"}, headers=_auth(reveal))
    assert r.status_code == 404


def test_reveal_404_for_a_check_deposit(reveal, monkeypatch):
    """There is nothing to decrypt for a check payment -- this must not be a 200 with nulls."""
    monkeypatch.setattr(main.db, "get_deposit_bank_details",
                        lambda deposit_id: {"id": deposit_id, "method": "check",
                                            "routing_number": None, "account_number": None,
                                            "account_type": None})
    r = reveal.post("/api/admin/deposit/dep-1/reveal",
                    json={"staff_email": "kyle@wetreadwell.com"}, headers=_auth(reveal))
    assert r.status_code == 404


def test_reveal_returns_decrypted_numbers_and_audit_logs_without_them(reveal, monkeypatch, caplog):
    routing_ct = bank_crypto.encrypt("021000021")
    account_ct = bank_crypto.encrypt("000123456789")
    monkeypatch.setattr(main.db, "get_deposit_bank_details",
                        lambda deposit_id: {"id": deposit_id, "method": "ach",
                                            "routing_number": routing_ct,
                                            "account_number": account_ct,
                                            "account_type": "checking"})
    with caplog.at_level(logging.INFO, logger="portal"):
        r = reveal.post("/api/admin/deposit/dep-42/reveal",
                        json={"staff_email": "kyle@wetreadwell.com"}, headers=_auth(reveal))
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["routing_number"] == "021000021"
    assert body["account_number"] == "000123456789"
    assert body["account_type"] == "checking"

    # WHO / WHEN / WHICH deposit, never the numbers.
    joined = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "dep-42" in joined
    assert "kyle@wetreadwell.com" in joined
    assert "021000021" not in joined
    assert "000123456789" not in joined


def test_reveal_fails_closed_when_key_is_wrong(reveal, monkeypatch):
    """The deposit was encrypted under one key; the configured key is now a DIFFERENT one (a
    rotation, or a misconfigured environment) -- must refuse, not return garbage or crash the
    drawer with a 500."""
    routing_ct = bank_crypto.encrypt("021000021")
    account_ct = bank_crypto.encrypt("000123456789")
    monkeypatch.setattr(main.db, "get_deposit_bank_details",
                        lambda deposit_id: {"id": deposit_id, "method": "ach",
                                            "routing_number": routing_ct,
                                            "account_number": account_ct,
                                            "account_type": "checking"})
    reveal.monkeypatch.setattr(config, "PORTAL_BANK_KEY", Fernet.generate_key().decode("ascii"))
    r = reveal.post("/api/admin/deposit/dep-1/reveal",
                    json={"staff_email": "kyle@wetreadwell.com"}, headers=_auth(reveal))
    assert r.status_code == 503
    assert r.json()["ok"] is False


def test_reveal_fails_closed_when_key_is_missing(reveal, monkeypatch):
    monkeypatch.setattr(main.db, "get_deposit_bank_details",
                        lambda deposit_id: {"id": deposit_id, "method": "ach",
                                            "routing_number": "enc:v1:whatever",
                                            "account_number": "enc:v1:whatever",
                                            "account_type": "checking"})
    reveal.monkeypatch.setattr(config, "PORTAL_BANK_KEY", "")
    r = reveal.post("/api/admin/deposit/dep-1/reveal",
                    json={"staff_email": "kyle@wetreadwell.com"}, headers=_auth(reveal))
    assert r.status_code == 503
    assert r.json()["ok"] is False
