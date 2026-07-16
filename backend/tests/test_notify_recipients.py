"""Team-notification recipient resolution (email_sender.resolve_notify_recipients).
Pure-logic invariants; the DB-backed CRUD + admin endpoints are covered by the
staging end-to-end smoke, per the repo convention (see test_customer_auth.py)."""
import email_sender

f = email_sender.resolve_notify_recipients
ENV_G = ["bids@wetreadwell.com"]
ENV_D = ["deposits@wetreadwell.com"]


def test_general_uses_general_rows():
    assert f(["a@x.com"], [], "general", ENV_G, ENV_D) == ["a@x.com"]


def test_general_falls_back_to_env_when_empty():
    assert f([], [], "general", ENV_G, ENV_D) == ENV_G


def test_general_ignores_deposit_rows():
    assert f([], ["d@x.com"], "general", ENV_G, ENV_D) == ENV_G


def test_deposit_prefers_deposit_rows():
    assert f(["g@x.com"], ["d@x.com"], "deposit", ENV_G, ENV_D) == ["d@x.com"]


def test_deposit_falls_back_to_general_rows():
    assert f(["g@x.com"], [], "deposit", ENV_G, ENV_D) == ["g@x.com"]


def test_deposit_falls_back_to_env_when_no_rows():
    assert f([], [], "deposit", ENV_G, ENV_D) == ENV_D


def test_returns_copy_not_env_alias():
    out = f([], [], "general", ENV_G, ENV_D)
    out.append("x@x.com")
    assert ENV_G == ["bids@wetreadwell.com"]   # env list not mutated
