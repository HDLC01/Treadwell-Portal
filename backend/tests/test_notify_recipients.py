"""Team-notification recipient resolution (email_sender.resolve_notify_recipients).
Pure-logic invariants; the DB-backed CRUD + admin endpoints are covered by the
staging end-to-end smoke, per the repo convention (see test_customer_auth.py)."""
import email_sender

f = email_sender.resolve_notify_recipients
ENV_G = ["bids@wetreadwell.com"]
ENV_D = ["deposits@wetreadwell.com"]


# ── base resolution (unconfigured → env fallback; configured → rows) ──────────
def test_general_uses_general_rows():
    assert f(["a@x.com"], [], "general", ENV_G, ENV_D) == ["a@x.com"]


def test_general_falls_back_to_env_when_unconfigured():
    # No rows at all → fresh/unconfigured install → env fallback.
    assert f([], [], "general", ENV_G, ENV_D) == ENV_G


def test_deposit_prefers_deposit_rows():
    assert f(["g@x.com"], ["d@x.com"], "deposit", ENV_G, ENV_D) == ["d@x.com"]


def test_deposit_falls_back_to_general_rows():
    assert f(["g@x.com"], [], "deposit", ENV_G, ENV_D) == ["g@x.com"]


def test_deposit_falls_back_to_env_when_unconfigured():
    assert f([], [], "deposit", ENV_G, ENV_D) == ENV_D


def test_returns_copy_not_env_alias():
    out = f([], [], "general", ENV_G, ENV_D)
    out.append("x@x.com")
    assert ENV_G == ["bids@wetreadwell.com"]   # env list not mutated


# ── the anti-resurrection fix: CONFIGURED but empty bucket → NOBODY, not env ──
def test_configured_but_general_bucket_empty_is_silent():
    # Roster has rows (only deposit-kind here) → a general alert must NOT fall back
    # to the env inbox just because no general rows are enabled.
    assert f([], ["d@x.com"], "general", ENV_G, ENV_D) == []


def test_configured_all_disabled_is_silent():
    # Everyone toggled off (configured=True, both buckets empty) → send to nobody.
    assert f([], [], "general", ENV_G, ENV_D, configured=True) == []
    assert f([], [], "deposit", ENV_G, ENV_D, configured=True) == []


# ── per-project overrides: union adds, subtract mutes (mute wins) ─────────────
def test_add_extends_base():
    assert f(["a@x.com"], [], "general", ENV_G, ENV_D, adds=["c@x.com"]) == ["a@x.com", "c@x.com"]


def test_mute_removes_from_base():
    assert f(["a@x.com", "b@x.com"], [], "general", ENV_G, ENV_D, mutes=["b@x.com"]) == ["a@x.com"]


def test_mute_wins_over_add():
    assert f(["a@x.com"], [], "general", ENV_G, ENV_D, adds=["b@x.com"], mutes=["b@x.com"]) == ["a@x.com"]


def test_case_insensitive_dedupe():
    # Same address in base + adds (different case) → kept once, first-seen casing.
    assert f(["A@x.com"], [], "general", ENV_G, ENV_D, adds=["a@x.com"]) == ["A@x.com"]


def test_mute_is_case_insensitive():
    assert f(["A@x.com"], [], "general", ENV_G, ENV_D, mutes=["a@x.com"]) == []


def test_add_on_empty_configured_roster():
    # A per-project "add" can notify someone even when the global roster is all-off.
    assert f([], [], "general", ENV_G, ENV_D, adds=["c@x.com"], configured=True) == ["c@x.com"]
