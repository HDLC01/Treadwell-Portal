"""scripts/encrypt_bank_numbers.py -- the one-off migration that encrypts whatever ACH
routing/account numbers are still sitting in plaintext from before bank_crypto.py shipped.

NO REAL DATABASE, same as the rest of this suite: db.pool() is stubbed with a fake connection that
records every statement it is handed, and the script's own SQL text + Python logic run for real
against canned rows -- executed, not grepped, so a rewrite of the WHERE clause or the UPDATE would
fail one of these even if nobody re-reads the source.
"""
import sys

import pytest
from cryptography.fernet import Fernet

import bank_crypto
import config
import db
from scripts import encrypt_bank_numbers as mig


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows
        self.executed = []   # list of (flattened sql, params)

    def execute(self, sql, params=()):
        self.executed.append((" ".join(sql.split()), params))
        if sql.strip().lower().startswith("select"):
            return _Result(self._rows)
        return _Result([])

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        return self._conn


@pytest.fixture
def key(monkeypatch):
    k = Fernet.generate_key().decode("ascii")
    monkeypatch.setattr(config, "PORTAL_BANK_KEY", k)
    return k


def _updates(conn):
    return [e for e in conn.executed if e[0].lower().startswith("update")]


def _selects(conn):
    return [e for e in conn.executed if e[0].lower().startswith("select")]


def test_dry_run_reports_the_count_and_changes_nothing(monkeypatch, key):
    rows = [
        {"id": "d1", "routing_number": "021000021", "account_number": "000123456789",
         "masked_ref": None, "routing_masked": None},
        {"id": "d2", "routing_number": "111000025", "account_number": "222233334444",
         "masked_ref": "••••4444", "routing_masked": None},
    ]
    conn = _FakeConn(rows)
    monkeypatch.setattr(db, "pool", lambda: _FakePool(conn))
    result = mig.run(apply=False)
    assert result == {"candidates": 2, "updated": 0}
    assert _updates(conn) == [], "dry run must issue no UPDATE at all"


def test_apply_encrypts_the_candidate_in_one_transaction(monkeypatch, key):
    rows = [
        {"id": "d1", "routing_number": "021000021", "account_number": "000123456789",
         "masked_ref": None, "routing_masked": None},
    ]
    conn = _FakeConn(rows)
    monkeypatch.setattr(db, "pool", lambda: _FakePool(conn))
    result = mig.run(apply=True)
    assert result == {"candidates": 1, "updated": 1}
    updates = _updates(conn)
    assert len(updates) == 1
    _, params = updates[0]
    new_routing, new_account, new_routing_masked, new_masked_ref, dep_id = params
    assert new_routing.startswith(bank_crypto.ENC_PREFIX)
    assert new_account.startswith(bank_crypto.ENC_PREFIX)
    assert bank_crypto.decrypt(new_routing) == "021000021"
    assert bank_crypto.decrypt(new_account) == "000123456789"
    assert new_routing_masked == "••••0021"
    assert new_masked_ref == "••••6789"
    assert dep_id == "d1"
    # ONE connection acquired for the whole run -- select and update share it (the transaction).
    assert len(_selects(conn)) == 1


def test_apply_does_not_overwrite_an_existing_mask(monkeypatch, key):
    rows = [
        {"id": "d1", "routing_number": "021000021", "account_number": "000123456789",
         "masked_ref": "••••OLD1", "routing_masked": "••••OLD2"},
    ]
    conn = _FakeConn(rows)
    monkeypatch.setattr(db, "pool", lambda: _FakePool(conn))
    mig.run(apply=True)
    _, params = _updates(conn)[0]
    _, _, routing_masked, masked_ref, _ = params
    assert routing_masked == "••••OLD2"
    assert masked_ref == "••••OLD1"


def test_the_candidate_query_excludes_already_encrypted_rows():
    """THE guard that makes --apply idempotent: once a row is enc:v1:-prefixed, a second run's
    WHERE clause must not select it again."""
    sql = mig._CANDIDATES_SQL.lower()
    assert "not like 'enc:v1:%'" in sql
    assert "method = 'ach'" in sql


def test_second_apply_is_a_no_op_once_everything_is_encrypted(monkeypatch, key):
    """Simulates re-running --apply after a first run already migrated everything: the (fake) DB
    now hands back zero candidate rows, exactly as the real WHERE clause would once nothing is
    left in plaintext, and the script must report zero/zero rather than erroring."""
    conn = _FakeConn([])
    monkeypatch.setattr(db, "pool", lambda: _FakePool(conn))
    result = mig.run(apply=True)
    assert result == {"candidates": 0, "updated": 0}
    assert _updates(conn) == []


def test_plan_update_does_not_double_encrypt_an_already_encrypted_field(monkeypatch, key):
    """Defensive-only: a row left mid-migration by an interrupted previous run has ONE field
    already encrypted. It must be left alone, not re-encrypted (Fernet cannot decrypt a
    ciphertext-of-ciphertext back to the original digits in a single decrypt() call)."""
    already = bank_crypto.encrypt("021000021")
    row = {"id": "d1", "routing_number": already, "account_number": "000123456789",
           "masked_ref": None, "routing_masked": None}
    plan = mig.plan_update(row)
    assert plan["routing_number"] == already
    assert plan["account_number"].startswith(bank_crypto.ENC_PREFIX)
    assert bank_crypto.decrypt(plan["account_number"]) == "000123456789"


def test_main_refuses_before_touching_the_database_when_key_is_unusable(monkeypatch):
    monkeypatch.setattr(config, "PORTAL_BANK_KEY", "")

    def _boom():
        raise AssertionError("must not touch the database when PORTAL_BANK_KEY is unusable")

    monkeypatch.setattr(db, "pool", _boom)
    monkeypatch.setattr(sys, "argv", ["encrypt_bank_numbers.py"])
    with pytest.raises(SystemExit):
        mig.main()
