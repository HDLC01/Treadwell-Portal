"""Multi-recipient publish — pure-logic invariants for `main._clean_emails`
(the recipient-list validator on /api/admin/publish). DB-backed reconcile /
access / dashboard paths are covered by the staging end-to-end smoke, per the
repo convention (see test_customer_auth.py)."""
import main

f = main._clean_emails


def test_absent_key_is_legacy():
    # No `emails` key at all → (None, None): admin_publish keeps today's behavior.
    assert f(None) == (None, None)


def test_non_list_is_error():
    assert f("a@x.com") == (None, "emails_must_be_list")
    assert f({"a": 1}) == (None, "emails_must_be_list")


def test_non_string_item_is_invalid():
    assert f([1]) == (None, "invalid_email")
    assert f(["a@x.com", None]) == (None, "invalid_email")


def test_lowercase_trim_dedupe_order_preserving():
    out, err = f([" A@x.com ", "a@X.COM", "b@Y.co", ""])
    assert err is None
    assert out == ["a@x.com", "b@y.co"]        # blanks skipped, dupes folded, order kept


def test_invalid_formats_rejected():
    for bad in ["no-at-sign", "a b@x.com", "a@b", "@x.com", "a@x.", "a@.com"]:
        assert f([bad])[1] == "invalid_email", bad


def test_no_whitespace_smuggling():
    # regex forbids internal whitespace/newlines → no header injection into Resend `to`.
    assert f(["a@x.com\nbcc:evil@x.com"]) == (None, "invalid_email")


def test_overlong_address_rejected():
    assert f(["a" * 250 + "@x.com"])[1] == "invalid_email"


def test_valid_list_passes_through():
    assert f(["a@x.com", "b@y.co"]) == (["a@x.com", "b@y.co"], None)


def test_empty_list_cleans_to_empty_not_error():
    # An all-blank list is not an error — admin_publish treats [] as "legacy".
    assert f([]) == ([], None)
    assert f(["", "  "]) == ([], None)


def test_max_recipients_constant():
    assert main.MAX_RECIPIENTS == 10
