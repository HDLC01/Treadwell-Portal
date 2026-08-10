"""The tick. These guard the one thing that cannot be undone: emailing a customer
the same nag twice.

The worker reserves the right to send before sending. If it crashes in between, the
customer misses one nudge and the next cadence step covers it — recoverable. If it
sent first and crashed before recording, every restart would re-nag them, which is
not recoverable. So: reserve, then send, and release the reservation only when
nothing went out at all.
"""
from datetime import datetime, timedelta, timezone

import followup_rules as rules
import followup_worker as fw

NOW = datetime(2026, 8, 4, 15, 0, tzinfo=timezone.utc)     # 10am Chicago
ENROLLED = NOW - timedelta(hours=30)                       # first nudge is due


def _proposal(**over):
    p = {"proposal_id": "p1", "token": "tok", "customer_email": "c@x.com",
         "customer_name": "Cust", "project_name": "Westport",
         "proposal_status": "sent", "followup_enrolled_at": ENROLLED,
         "followup_disabled_at": None, "followup_paused_until": None,
         "cycle_viewed_at": None, "deposit_required": True,
         "assigned_estimator": "kyle@wetreadwell.com", "approved_total": 27653.0}
    p.update(over)
    return p


def _wire(monkeypatch, *, proposal=None, reserve=lambda pid, key, detail: 1,
          send_ok=True, staff_ok=True, enabled="true"):
    calls = {"reserved": [], "deleted": [], "customer": [], "staff": [], "candidates": 0}
    p = proposal if proposal is not None else _proposal()

    monkeypatch.setenv("FOLLOWUP_AUTOMATION_ENABLED", enabled)

    def _candidates():
        calls["candidates"] += 1
        return [p]

    monkeypatch.setattr(fw.db, "list_followup_candidates", _candidates)
    monkeypatch.setattr(fw.db, "get_proposal", lambda pid: p)
    monkeypatch.setattr(fw.db, "get_recipients", lambda pid: ["c@x.com", "b@x.com"])

    def _reserve(pid, key, detail):
        calls["reserved"].append(key)
        return reserve(pid, key, detail)

    monkeypatch.setattr(fw.db, "reserve_followup", _reserve)
    monkeypatch.setattr(fw.db, "delete_followup", lambda rid: calls["deleted"].append(rid))
    monkeypatch.setattr(fw.email_sender, "proposal_reply_to", lambda t: "proposals@notify.x")
    monkeypatch.setattr(fw.email_sender, "_resolve_notify", lambda kind, pid=None: ["bids@x.com"])
    monkeypatch.setattr(
        fw.email_sender, "send_followup",
        lambda addr, url, proj, tmpl, **k: calls["customer"].append(
            {"to": addr, "template": tmpl, "ask": k.get("include_status_ask"),
             "deposit": k.get("deposit_required")}) or send_ok)
    monkeypatch.setattr(
        fw.email_sender, "notify_team",
        lambda subject, body, **k: calls["staff"].append(
            {"subject": subject, "to": k.get("recipients")}) or staff_ok)
    return calls


def test_a_due_proposal_emails_every_recipient_and_tells_the_estimator(monkeypatch):
    calls = _wire(monkeypatch)
    fw._tick(NOW)
    assert [c["to"] for c in calls["customer"]] == ["c@x.com", "b@x.com"]
    assert calls["customer"][0]["template"] == "not_viewed"
    assert calls["staff"][0]["to"] == ["kyle@wetreadwell.com"]      # the assigned estimator
    assert calls["deleted"] == []


def test_the_reservation_is_taken_before_anything_is_sent(monkeypatch):
    """Ordering is the whole safety property, so assert it directly."""
    order = []
    calls = _wire(monkeypatch, reserve=lambda pid, key, detail: order.append("reserve") or 1)
    monkeypatch.setattr(fw.email_sender, "send_followup",
                        lambda *a, **k: order.append("send") or True)
    fw._tick(NOW)
    assert order[0] == "reserve" and "send" in order


def test_an_already_reserved_rule_sends_nothing(monkeypatch):
    """A prior tick, or the twin container during a deploy, already sent this one."""
    calls = _wire(monkeypatch, reserve=lambda pid, key, detail: None)
    fw._tick(NOW)
    assert calls["customer"] == [] and calls["staff"] == []
    assert calls["reserved"]              # it did try


def test_a_total_send_failure_releases_the_reservation_so_the_next_tick_retries(monkeypatch):
    calls = _wire(monkeypatch, send_ok=False, staff_ok=False)
    fw._tick(NOW)
    assert calls["deleted"], "a failed send must not leave the rule marked as sent"


def test_a_partial_send_keeps_the_reservation(monkeypatch):
    """One recipient bounced, another got it. Retrying would re-nag the one who did."""
    calls = _wire(monkeypatch)
    sent = {"n": 0}

    def _one_ok(addr, url, proj, tmpl, **k):
        sent["n"] += 1
        return sent["n"] == 1        # first succeeds, second fails

    monkeypatch.setattr(fw.email_sender, "send_followup", _one_ok)
    fw._tick(NOW)
    assert calls["deleted"] == []


def test_the_kill_switch_stops_everything(monkeypatch):
    calls = _wire(monkeypatch, enabled="false")
    fw._tick(NOW)
    assert calls["candidates"] == 0 and calls["customer"] == []


def test_the_flag_is_read_every_tick_not_at_import(monkeypatch):
    """Turning automation off in production is an env change plus a restart, so the
    worker must believe the environment now rather than at load time."""
    monkeypatch.setenv("FOLLOWUP_AUTOMATION_ENABLED", "false")
    assert fw._enabled() is False
    monkeypatch.setenv("FOLLOWUP_AUTOMATION_ENABLED", "true")
    assert fw._enabled() is True


def test_a_proposal_approved_since_the_list_was_built_is_skipped(monkeypatch):
    """The candidate list is minutes old by the time we reach a row. Someone who
    approved in between must not get one last reminder."""
    stale = _proposal()
    calls = _wire(monkeypatch, proposal=stale)
    monkeypatch.setattr(fw.db, "get_proposal",
                        lambda pid: dict(stale, proposal_status="approved"))
    fw._tick(NOW)
    assert calls["customer"] == [] and calls["reserved"] == []


def test_a_proposal_taken_off_automation_since_the_list_was_built_is_skipped(monkeypatch):
    stale = _proposal()
    calls = _wire(monkeypatch, proposal=stale)
    monkeypatch.setattr(fw.db, "get_proposal",
                        lambda pid: dict(stale, followup_disabled_at=NOW))
    fw._tick(NOW)
    assert calls["reserved"] == []


def test_one_bad_proposal_does_not_end_the_sweep(monkeypatch):
    good = _proposal(proposal_id="good")
    bad = _proposal(proposal_id="bad")
    calls = _wire(monkeypatch)
    monkeypatch.setattr(fw.db, "list_followup_candidates", lambda: [bad, good])

    def _get(pid):
        if pid == "bad":
            raise RuntimeError("row is a mess")
        return good

    monkeypatch.setattr(fw.db, "get_proposal", _get)
    fw._tick(NOW)
    assert calls["customer"], "the healthy proposal must still be chased"


def test_the_deposit_sentence_follows_the_proposal_flag(monkeypatch):
    """Telling a GC we need a deposit on a job sent without one is simply wrong."""
    viewed = _proposal(proposal_status="viewed", cycle_viewed_at=NOW - timedelta(hours=25),
                       deposit_required=False)
    calls = _wire(monkeypatch, proposal=viewed)
    fw._tick(NOW)
    assert calls["customer"][0]["deposit"] is False
    assert calls["customer"][0]["template"] == "next_steps"


def test_the_status_ask_appears_only_on_the_recurring_stage(monkeypatch):
    early = _proposal(proposal_status="viewed", cycle_viewed_at=NOW - timedelta(hours=25))
    calls = _wire(monkeypatch, proposal=early)
    fw._tick(NOW)
    assert calls["customer"][0]["ask"] is False

    late = _proposal(proposal_status="viewed", cycle_viewed_at=NOW - timedelta(hours=150))
    calls2 = _wire(monkeypatch, proposal=late)
    fw._tick(NOW)
    assert calls2["customer"][0]["ask"] is True


def test_an_unassigned_proposal_still_reaches_a_human(monkeypatch):
    """Proposals published before assignment was required have no owner; the note
    falls back to the notification roster rather than vanishing."""
    calls = _wire(monkeypatch, proposal=_proposal(assigned_estimator=None))
    fw._tick(NOW)
    assert calls["staff"][0]["to"] == ["bids@x.com"]


def test_the_worker_never_starts_under_pytest(monkeypatch):
    """Every test file builds a TestClient, which runs app startup. Without this the
    suite spawns a thread that blocks on a database the tests deliberately lack."""
    monkeypatch.setenv("FOLLOWUP_AUTOMATION_ENABLED", "true")
    assert fw.ensure_started() is False


def test_the_tick_interval_is_clamped(monkeypatch):
    monkeypatch.setenv("FOLLOWUP_TICK_SECONDS", "5")
    assert fw._interval() == 60
    monkeypatch.setenv("FOLLOWUP_TICK_SECONDS", "99999")
    assert fw._interval() == 3600
    monkeypatch.setenv("FOLLOWUP_TICK_SECONDS", "nonsense")
    assert fw._interval() == 900


# ── the default, pinned ───────────────────────────────────────────────
# Added 2026-08-04 on Hanz's instruction: "email follow ups should be automatically off."
#
# The two defaults are not symmetric. Default ON and be wrong, and automated follow-up mail
# goes to real customers from whatever box happens to be running. Default OFF and be wrong,
# and nothing sends until somebody notices a missing reminder. Nothing pinned this before,
# so it defaulted to "true" and production was safe only because the compose file happened
# to say otherwise.
def test_follow_up_automation_is_off_when_nobody_has_said_otherwise(monkeypatch):
    monkeypatch.delenv("FOLLOWUP_AUTOMATION_ENABLED", raising=False)
    assert fw._enabled() is False


def test_a_missing_config_attribute_does_not_re_enable_automation(monkeypatch):
    """The fallback used to be `getattr(config, ..., True)`, so a build where the config
    attribute went missing would have quietly switched customer email back ON — the one case
    you least want it guessing."""
    import config
    monkeypatch.delenv("FOLLOWUP_AUTOMATION_ENABLED", raising=False)
    monkeypatch.delattr(config, "FOLLOWUP_AUTOMATION_ENABLED", raising=False)
    assert fw._enabled() is False


def test_anything_that_is_not_an_explicit_yes_leaves_automation_off(monkeypatch):
    """Fails closed on junk, including near-misses: "of" and "truee" are typos somebody
    will make in a compose file, and neither should mail a customer."""
    for junk in ("", "of", "truee", "maybe", "disabled", "off", "0", "no"):
        monkeypatch.setenv("FOLLOWUP_AUTOMATION_ENABLED", junk)
        assert fw._enabled() is False, junk
