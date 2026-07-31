"""The follow-up cadence. Pure functions, so these are the cheap, exhaustive tests.

The two properties worth most: a missed tick must never turn into a burst of emails
(latest-only), and a re-sent proposal must restart its own cadence without colliding
with the previous send's dedupe rows (cycle-scoped keys).
"""
from datetime import date, datetime, timedelta, timezone

import followup_rules as fr

TZ = fr.BUSINESS_TZ
# 10am Chicago — inside the send window, so customer rules aren't suppressed.
ENROLLED = datetime(2026, 8, 1, 15, 0, tzinfo=timezone.utc)


def _p(**over):
    p = {"proposal_id": "p1", "proposal_status": "sent",
         "followup_enrolled_at": ENROLLED, "followup_disabled_at": None,
         "followup_paused_until": None, "cycle_viewed_at": None}
    p.update(over)
    return p


def keys(dues):
    return [d.rule_key for d in dues]


def of(dues, audience):
    return [d for d in dues if d.audience == audience]


# ── not-viewed track ─────────────────────────────────────────────────────────
def test_nothing_before_the_first_day_is_up():
    assert fr.due_now(_p(), ENROLLED + timedelta(hours=23, minutes=59)) == []


def test_first_nudge_at_24h_goes_to_both_the_customer_and_the_estimator():
    dues = fr.due_now(_p(), ENROLLED + timedelta(hours=24, minutes=1))
    assert [d.template for d in of(dues, "customer")] == ["not_viewed"]
    assert [d.template for d in of(dues, "staff")] == ["staff_not_viewed"]
    # No status ask on the first nudge — it's too early to ask if they've given up.
    assert of(dues, "customer")[0].include_status_ask is False


def test_an_unopened_proposal_keeps_nudging_with_a_way_out():
    """Still unopened days later is exactly the proposal that needs the delayed /
    not-moving-forward escape hatch offered."""
    dues = fr.due_now(_p(), ENROLLED + timedelta(hours=24 + 72 + 1))
    cust = of(dues, "customer")[0]
    assert cust.template == "not_viewed" and cust.include_status_ask is True
    assert cust.rule_key.endswith(":nvr1")


# ── viewed track ─────────────────────────────────────────────────────────────
def _viewed(hours_ago, **over):
    v = ENROLLED + timedelta(hours=1)
    return _p(proposal_status="viewed", cycle_viewed_at=v, **over), v + timedelta(hours=hours_ago)


def test_viewed_track_walks_24h_then_48h_then_every_three_days():
    p, at24 = _viewed(24)
    assert [d.template for d in of(fr.due_now(p, at24), "customer")] == ["next_steps"]

    p, at72 = _viewed(72)
    assert [d.template for d in of(fr.due_now(p, at72), "customer")] == ["second_nudge"]

    p, at144 = _viewed(144)          # 72 + 72
    c = of(fr.due_now(p, at144), "customer")[0]
    assert c.template == "checkin" and c.include_status_ask is True
    assert c.rule_key.endswith(":vr1")

    p, at216 = _viewed(216)
    assert of(fr.due_now(p, at216), "customer")[0].rule_key.endswith(":vr2")


def test_the_estimator_is_told_to_get_personal_at_48h():
    p, at48 = _viewed(48)
    assert [d.template for d in of(fr.due_now(p, at48), "staff")] == ["staff_personal_followup"]


def test_only_one_customer_email_per_tick_however_late_we_are():
    """The safety property. Downtime, a restart, or the kill switch left off over a
    weekend must cost ONE email, not a burst of every reminder that matured."""
    p, way_late = _viewed(24 * 30)
    dues = fr.due_now(p, way_late)
    assert len(of(dues, "customer")) == 1
    assert len(of(dues, "staff")) == 1


def test_the_recurring_series_is_capped():
    p, absurdly_late = _viewed(24 * 400)
    key = of(fr.due_now(p, absurdly_late), "customer")[0].rule_key
    assert key.endswith(":vr%d" % fr.MAX_RECURRING)


# ── stop conditions ──────────────────────────────────────────────────────────
def test_approved_and_closed_proposals_are_left_alone():
    p, at72 = _viewed(72)
    for status in ("approved", "closed_lost"):
        assert fr.due_now(dict(p, proposal_status=status), at72) == []


def test_an_estimator_opt_out_stops_everything():
    p, at72 = _viewed(72)
    assert fr.due_now(dict(p, followup_disabled_at=ENROLLED), at72) == []


def test_an_unenrolled_proposal_is_never_chased():
    """Legacy rows published before automation existed have no anchor. Chasing them
    off created_at would fire weeks of overdue reminders at once."""
    p, at72 = _viewed(72)
    assert fr.due_now(dict(p, followup_enrolled_at=None), at72) == []


# ── pause ────────────────────────────────────────────────────────────────────
def test_a_pause_silences_everything_including_its_final_day():
    p, at72 = _viewed(72)
    today = fr.business_today(at72)
    assert fr.due_now(dict(p, followup_paused_until=today), at72) == []          # last day
    assert fr.due_now(dict(p, followup_paused_until=today + timedelta(days=30)), at72) == []


def test_an_expired_pause_reminds_the_estimator_and_nothing_else():
    p, at72 = _viewed(72)
    yesterday = fr.business_today(at72) - timedelta(days=1)
    dues = fr.due_now(dict(p, followup_paused_until=yesterday), at72)
    assert [(d.audience, d.template) for d in dues] == [("staff", "staff_pause_expired")]
    assert dues[0].rule_key == "pe:%s" % yesterday.isoformat()


# ── send window ──────────────────────────────────────────────────────────────
def test_customer_email_waits_for_business_hours_but_staff_notes_do_not():
    p = _p(proposal_status="viewed", cycle_viewed_at=ENROLLED)
    # 03:00 Chicago, two days later: both rules have matured.
    night = datetime(2026, 8, 4, 3, 0, tzinfo=TZ)
    dues = fr.due_now(p, night)
    assert of(dues, "customer") == []
    assert len(of(dues, "staff")) == 1
    # Same day at 09:00 the customer email is released — it was deferred, not lost.
    morning = datetime(2026, 8, 4, 9, 0, tzinfo=TZ)
    assert len(of(fr.due_now(p, morning), "customer")) == 1


def test_the_window_is_evaluated_in_chicago_not_utc():
    # 02:00 UTC is 21:00 the previous day in Chicago — outside the window either way,
    # but the point is that the answer comes from the business clock.
    assert fr.in_send_window(datetime(2026, 8, 4, 2, 0, tzinfo=timezone.utc)) is False
    assert fr.in_send_window(datetime(2026, 8, 4, 15, 0, tzinfo=timezone.utc)) is True


# ── cycle keys ───────────────────────────────────────────────────────────────
def test_resending_restarts_the_cadence_with_fresh_keys():
    """Same rule, different send — the keys must differ or the dedupe row from the
    first send would suppress the revision's reminder entirely."""
    p1, at = _viewed(24)
    later = ENROLLED + timedelta(days=10)
    p2 = dict(p1, followup_enrolled_at=later, cycle_viewed_at=later + timedelta(hours=1))
    k1 = of(fr.due_now(p1, at), "customer")[0].rule_key
    k2 = of(fr.due_now(p2, later + timedelta(hours=26)), "customer")[0].rule_key
    assert k1 != k2 and k1.endswith(":v1") and k2.endswith(":v1")


# ── month arithmetic for pauses ──────────────────────────────────────────────
def test_pause_months_clamp_to_the_end_of_short_months():
    assert fr.add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert fr.add_months(date(2026, 8, 31), 1) == date(2026, 9, 30)
    assert fr.add_months(date(2026, 11, 30), 2) == date(2027, 1, 30)   # crosses the year
    assert fr.add_months(date(2026, 8, 1), 4) == date(2026, 12, 1)
    assert fr.add_months(date(2026, 12, 15), 1) == date(2027, 1, 15)


def test_naive_timestamps_from_the_database_are_treated_as_utc():
    """psycopg normally hands back aware datetimes, but a stub or a plain column
    must not crash the tick."""
    naive = ENROLLED.replace(tzinfo=None)
    dues = fr.due_now(_p(followup_enrolled_at=naive), ENROLLED + timedelta(hours=25))
    assert [d.template for d in of(dues, "customer")] == ["not_viewed"]
