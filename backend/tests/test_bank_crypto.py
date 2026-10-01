"""bank_crypto.py in isolation -- no app, no DB, no TestClient. These are the tests that pin the
actual cryptographic contract test_deposit.py's endpoint tests lean on: round trip, fail-closed on
a missing/broken key, and the enc:v1: prefix that lets a reader tell ciphertext from a
pre-migration plaintext row on sight.

Each test sets its OWN key (per the brief: "tests generate their own key") via monkeypatch on
config.PORTAL_BANK_KEY -- bank_crypto reads it fresh on every call, never caches it at import
time, so this is enough; nothing needs reloading.
"""
from cryptography.fernet import Fernet

import bank_crypto
import config


def _set_key(monkeypatch, key: str) -> None:
    monkeypatch.setattr(config, "PORTAL_BANK_KEY", key)


def test_encrypt_round_trips_through_decrypt(monkeypatch):
    _set_key(monkeypatch, Fernet.generate_key().decode("ascii"))
    ct = bank_crypto.encrypt("021000021")
    assert ct.startswith(bank_crypto.ENC_PREFIX)
    assert ct != "021000021"
    assert bank_crypto.decrypt(ct) == "021000021"


def test_ciphertext_is_not_the_same_token_twice(monkeypatch):
    """Fernet includes a random IV, so encrypting the same digits twice must not produce
    identical ciphertext -- if it did, two customers sharing a bank (or one customer's two
    deposits) would be visibly linkable from the stored bytes alone."""
    _set_key(monkeypatch, Fernet.generate_key().decode("ascii"))
    a = bank_crypto.encrypt("000123456789")
    b = bank_crypto.encrypt("000123456789")
    assert a != b
    assert bank_crypto.decrypt(a) == bank_crypto.decrypt(b) == "000123456789"


def test_key_configured_false_when_empty(monkeypatch):
    _set_key(monkeypatch, "")
    assert bank_crypto.key_configured() is False


def test_key_configured_false_when_garbage(monkeypatch):
    _set_key(monkeypatch, "not-a-valid-fernet-key")
    assert bank_crypto.key_configured() is False


def test_key_configured_true_for_a_real_key(monkeypatch):
    _set_key(monkeypatch, Fernet.generate_key().decode("ascii"))
    assert bank_crypto.key_configured() is True


def test_encrypt_raises_bankkeyerror_never_returns_plaintext(monkeypatch):
    """THE fail-closed guard. A caller that forgot to check key_configured() first must get an
    exception, not a value it could mistake for ciphertext and store as-is -- the one failure
    mode this whole module exists to rule out."""
    _set_key(monkeypatch, "")
    import pytest
    with pytest.raises(bank_crypto.BankKeyError):
        bank_crypto.encrypt("021000021")


def test_decrypt_raises_bankkeyerror_when_key_missing(monkeypatch):
    _set_key(monkeypatch, Fernet.generate_key().decode("ascii"))
    ct = bank_crypto.encrypt("021000021")
    _set_key(monkeypatch, "")
    import pytest
    with pytest.raises(bank_crypto.BankKeyError):
        bank_crypto.decrypt(ct)


def test_decrypt_raises_bankdecrypterror_under_the_wrong_key(monkeypatch):
    """Distinct from BankKeyError: the key IS configured and valid-shaped, it is just not the key
    this value was encrypted under (rotation, or a copy-paste from another environment)."""
    _set_key(monkeypatch, Fernet.generate_key().decode("ascii"))
    ct = bank_crypto.encrypt("021000021")
    _set_key(monkeypatch, Fernet.generate_key().decode("ascii"))   # a DIFFERENT valid key
    import pytest
    with pytest.raises(bank_crypto.BankDecryptError):
        bank_crypto.decrypt(ct)


def test_decrypt_is_tolerant_of_legacy_plaintext(monkeypatch):
    """A pre-migration row has no enc:v1: prefix at all -- decrypt() must hand it back unchanged
    rather than raising, so the reveal endpoint keeps working in the window before the migration
    script runs. Only a value that CLAIMS to be ciphertext (carries the prefix) can fail."""
    _set_key(monkeypatch, Fernet.generate_key().decode("ascii"))
    assert bank_crypto.decrypt("021000021") == "021000021"
    assert bank_crypto.decrypt(None) == ""
    assert bank_crypto.decrypt("") == ""


def test_is_encrypted(monkeypatch):
    assert bank_crypto.is_encrypted("enc:v1:abc") is True
    assert bank_crypto.is_encrypted("021000021") is False
    assert bank_crypto.is_encrypted(None) is False
    assert bank_crypto.is_encrypted("") is False


def test_mask_last4():
    assert bank_crypto.mask_last4("000123456789") == "••••6789"
    assert bank_crypto.mask_last4("021-000-021") == "••••0021"    # strips separators first
    assert bank_crypto.mask_last4("12") == "••••"                  # too short for a meaningful mask
    assert bank_crypto.mask_last4(None) == "••••"
