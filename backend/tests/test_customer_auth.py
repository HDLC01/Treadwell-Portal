"""Auth helper invariants (no DB needed). DB-backed verify_code/session paths
are covered by the staging end-to-end smoke."""
import hashlib

import customer_auth as ca


def test_generate_code_is_six_digits():
    for _ in range(50):
        c = ca.generate_code()
        assert len(c) == 6 and c.isdigit()


def test_hash_code_is_sha256():
    assert ca.hash_code("123456") == hashlib.sha256(b"123456").hexdigest()


def test_tokens_are_unguessable_and_unique():
    a, b = ca.new_session_token(), ca.new_session_token()
    assert a != b and len(a) > 20
    assert ca.new_proposal_token() != ca.new_proposal_token()


def test_google_verify_returns_none_when_disabled():
    # GOOGLE_CLIENT_ID is unset in the test env -> no network call, returns None.
    assert ca.verify_google_idtoken("any-token") is None
