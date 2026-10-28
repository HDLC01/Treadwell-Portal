"""Release-day safety tooling for follow-ups: the dry-run entry point and the out-of-office backfill.

The schedule's own release-day guarantees (a proposal that already had 2 reminders in its step gets
no more; nothing re-sends a reminder already sent; the dry run lists exactly who would be emailed)
are tested in test_followup_auto_rules.py, test_followup_auto_worker.py and, against a real
Postgres, test_pg_followup_auto.py.
"""
import sys as _sys
from datetime import datetime, timezone

import pytest

import followup_worker as fw

_ops = __import__("pathlib").Path(__file__).resolve().parents[1] / "ops"
if str(_ops) not in _sys.path:
    _sys.path.insert(0, str(_ops))
import followup_dry_run                                                   # noqa: E402


def test_the_entry_point_evaluates_the_tick_at_now(monkeypatch, capsys):
    """Round 3 #5: `--now <ISO>` is the moment dry_run_count evaluates at; without it, the real
    clock (None). An offset-less value is Central time; a bad one exits 2 before any database."""
    seen = []
    monkeypatch.setattr(followup_dry_run, "install_read_only_pool", lambda url: None)
    monkeypatch.setattr(followup_dry_run, "refuse_email", lambda: None)
    monkeypatch.setattr(fw, "dry_run_count", lambda now=None: seen.append(now) or {
        "at": str(now), "tasks": []})
    assert followup_dry_run.main(["--json", "--now", "2026-10-27T12:00:00-05:00"]) == 0
    assert followup_dry_run.main(["--now=2026-10-27T17:00:00Z", "--json"]) == 0
    assert followup_dry_run.main(["--json", "--now", "2026-10-27T12:00:00"]) == 0
    assert followup_dry_run.main(["--json"]) == 0
    noon = datetime(2026, 10, 27, 17, 0, tzinfo=timezone.utc)
    assert seen[:3] == [noon, noon, noon] and seen[3] is None
    capsys.readouterr()
    assert followup_dry_run.main(["--now", "yesterday"]) == 2
    assert followup_dry_run.main(["--now"]) == 2
    assert len(seen) == 4, "a bad --now reached the database"
    assert "--now" in capsys.readouterr().err


def test_the_entry_point_opens_a_read_only_pool():
    assert followup_dry_run.READ_ONLY == "-c default_transaction_read_only=on"


# ── #13: the out-of-office backfill's body heuristics ─────────────────────────────
import backfill_auto_reply                                                # noqa: E402

AUTO_BODIES = [
    "I am currently out of the office and will return on Monday, October 28.",
    "Thank you for your email. I'm out of the office until 11/4 with limited access to email.",
    "I\u2019m on vacation this week. For urgent matters please call Dave at the office.",
    "I will be on leave from 10/1 to 10/15. I will respond to your message upon my return.",
    "This is an automated reply. Your message has been received.",
    "Automatic reply: Proposal for Acme HQ\nI am away from the office and will return on 10/30.",
]
REAL_BODIES = [
    "Looks good, we'd like to move forward with the epoxy option.",
    "I was out of the office last week, sorry. Yes, please send the contract.",
    "I'm out of the office today but go ahead and schedule us for November.",
    "We are on vacation next month, can you start after the 15th? I will return the signed copy.",
    "Out of office until Monday but approved - send the invoice",
    "Can you call me? " + "We have a few questions about the scope. " * 40
    + "I am out of the office and will return on Monday.",
    "",
]


@pytest.mark.parametrize("body", AUTO_BODIES)
def test_an_autoresponder_body_is_recognised(body):
    assert backfill_auto_reply.auto_reply_reason(None, body)


@pytest.mark.parametrize("body", REAL_BODIES)
def test_a_real_reply_is_never_flagged(body):
    """A false positive hides a real reply from the schedule, so these must all stay unflagged."""
    assert backfill_auto_reply.auto_reply_reason(None, body) is None


def test_a_stored_subject_uses_the_live_rule():
    assert backfill_auto_reply.auto_reply_reason("Out of Office: Re: Acme", "anything") == "subject"
    assert backfill_auto_reply.auto_reply_reason("Re: Acme proposal", REAL_BODIES[0]) is None
