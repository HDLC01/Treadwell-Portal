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


def occ(anchor, first_hours, n, recurring_hours=72):
    """The dedupe key suffix for recurring occurrence `n`, as a time.

    The key used to be the ordinal ("vr3"). That broke silently once the interval became
    editable: raising it made the same elapsed time yield a smaller n, landing on a key already
    consumed, so sends were deduped away for months. Keys are derived from WHEN the occurrence
    falls now, and these tests pin the same occurrences by computing that time."""
    at = anchor + timedelta(hours=first_hours) + timedelta(hours=recurring_hours) * n
    return at.astimezone(timezone.utc).strftime("%Y%m%dT%H")


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
    assert cust.rule_key.endswith(":nvr@" + occ(ENROLLED, 24, 1)), cust.rule_key


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
    v144 = fr._aware(p["cycle_viewed_at"])
    c = of(fr.due_now(p, at144), "customer")[0]
    assert c.template == "checkin" and c.include_status_ask is True
    assert c.rule_key.endswith(":vr@" + occ(v144, 72, 1)), c.rule_key

    p, at216 = _viewed(216)
    v216 = fr._aware(p["cycle_viewed_at"])
    assert of(fr.due_now(p, at216), "customer")[0].rule_key.endswith(
        ":vr@" + occ(v216, 72, 2))


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
    v = fr._aware(p["cycle_viewed_at"])
    key = of(fr.due_now(p, absurdly_late), "customer")[0].rule_key
    assert key.endswith(":vr@" + occ(v, 72, fr.MAX_RECURRING)), key
    # And it STAYS there. Asserting the ordinal only ever said "it is 20"; this says the series
    # stops advancing, which is what "capped" actually means.
    p2, even_later = _viewed(24 * 900)
    assert of(fr.due_now(p2, even_later), "customer")[0].rule_key.endswith(
        ":vr@" + occ(fr._aware(p2["cycle_viewed_at"]), 72, fr.MAX_RECURRING))


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


# ── next_due_at: the Follow-ups page's "Next reminder" column ───────────────
# It must mirror due_now exactly. If the two drift, the page states a schedule that
# exists nowhere else a human can check, and nobody would ever notice it was wrong.
def _row(**kw):
    base = {"followup_enrolled_at": None, "followup_disabled_at": None,
            "followup_paused_until": None, "cycle_viewed_at": None,
            "proposal_status": "sent"}
    base.update(kw)
    return base


def _ago(now, hours):
    return now - timedelta(hours=hours)


def test_nothing_is_due_when_nothing_is_chasing():
    now = datetime(2026, 8, 3, 15, 0, tzinfo=timezone.utc)
    assert fr.next_due_at(_row(), now) is None                    # not enrolled
    assert fr.next_due_at(
        _row(followup_enrolled_at=_ago(now, 100), followup_disabled_at=_ago(now, 1)), now) is None
    assert fr.next_due_at(
        _row(followup_enrolled_at=_ago(now, 100), proposal_status="approved"), now) is None
    assert fr.next_due_at(
        _row(followup_enrolled_at=_ago(now, 100), proposal_status="closed_lost"), now) is None


def test_the_first_reminder_lands_24h_after_the_send():
    now = datetime(2026, 8, 3, 15, 0, tzinfo=timezone.utc)
    sent = _ago(now, 2)
    assert fr.next_due_at(_row(followup_enrolled_at=sent), now) == sent + timedelta(hours=24)


def test_after_a_view_the_clock_restarts_on_the_view():
    """cycle_viewed_at is the anchor once they open it — that's what due_now uses, so a
    proposal viewed an hour ago is 24h from its next email regardless of when it was sent."""
    now = datetime(2026, 8, 3, 15, 0, tzinfo=timezone.utc)
    viewed = _ago(now, 1)
    got = fr.next_due_at(
        _row(followup_enrolled_at=_ago(now, 200), cycle_viewed_at=viewed,
             proposal_status="viewed"), now)
    assert got == viewed + timedelta(hours=24)


def test_a_pause_pushes_it_to_the_morning_after_the_window_closes():
    """The customer said "come back in two months". The cadence must not fire ON the last
    day of their window — is_paused covers the whole of that final day."""
    now = datetime(2026, 8, 3, 15, 0, tzinfo=timezone.utc)
    got = fr.next_due_at(
        _row(followup_enrolled_at=_ago(now, 100), followup_paused_until="2026-09-01"), now)
    assert got.date().isoformat() == "2026-09-02"
    assert fr.is_paused(
        _row(followup_paused_until="2026-09-01"),
        datetime(2026, 9, 1, 23, 0, tzinfo=timezone.utc)) is True


def test_it_always_points_at_a_future_occurrence_for_an_active_proposal():
    """The real contract, and it took a failing test to pin it down.

    `next_due_at` sees only the proposal row — never `portal_followups` — so it cannot
    know whether the occurrence that has just matured was already sent. It therefore
    reports the next time the cadence MATURES, which for any active proposal is always
    ahead of now: the worker ticks every 15 minutes, so anything ripe right now goes out
    within the quarter hour.

    That gives the column a second, useful meaning the page relies on: a date in the PAST
    means the cadence matured and nothing sent it — automation is off globally, or the
    worker is down. It is a stalled-sender warning, not a late-by-six-days complaint."""
    now = datetime(2026, 8, 3, 15, 0, tzinfo=timezone.utc)
    cases = [
        ("too soon to nudge", _row(followup_enrolled_at=_ago(now, 2))),
        ("unopened, first due", _row(followup_enrolled_at=_ago(now, 30))),
        ("unopened, recurring", _row(followup_enrolled_at=_ago(now, 100))),
        ("viewed, too soon", _row(followup_enrolled_at=_ago(now, 50),
                                  cycle_viewed_at=_ago(now, 2), proposal_status="viewed")),
        ("viewed, 24h due", _row(followup_enrolled_at=_ago(now, 50),
                                 cycle_viewed_at=_ago(now, 30), proposal_status="viewed")),
        ("viewed, 72h due", _row(followup_enrolled_at=_ago(now, 200),
                                 cycle_viewed_at=_ago(now, 100), proposal_status="viewed")),
    ]
    for name, row in cases:
        nxt = fr.next_due_at(row, now)
        assert nxt is not None and nxt > now, f"{name}: got {nxt}"


def test_the_next_occurrence_is_one_full_step_past_the_matured_one():
    """Concretely: unopened for 30h means the 24h nudge is ripe now, so the next
    scheduled one is the first recurring step — 24h + 72h from the send."""
    now = datetime(2026, 8, 3, 15, 0, tzinfo=timezone.utc)
    sent = _ago(now, 30)
    assert [d.template for d in fr.due_now(_row(followup_enrolled_at=sent), now)
            if d.audience == "customer"] == ["not_viewed"]
    assert fr.next_due_at(_row(followup_enrolled_at=sent), now) == sent + timedelta(hours=24 + 72)


def test_an_exhausted_cadence_reports_no_more():
    """MAX_RECURRING is the hard stop that keeps us from chasing somebody forever. The
    page has to say "—", not invent a reminder that will never be sent."""
    now = datetime(2026, 8, 3, 15, 0, tzinfo=timezone.utc)
    ancient = _ago(now, 24 + 72 * (fr.MAX_RECURRING + 2))
    assert fr.next_due_at(_row(followup_enrolled_at=ancient), now) is None


# ── an expired pause must RESUME the chase, not end it ────────────────────────
# From the adversarial audit. due_now emitted the staff "pause expired" note and returned, and
# nothing ever clears followup_paused_until, so every later tick took the same branch and returned
# the same note its own dedupe key had already consumed. The customer was never chased again —
# while the board showed a next-reminder date, because next_due_at fell through to normal
# scheduling. Every customer who clicked "Project delayed" left the chase permanently.
def _paused(days_ago_end, viewed_hours_before_pause=1):
    """A proposal whose pause ended `days_ago_end` days ago."""
    v = ENROLLED + timedelta(hours=viewed_hours_before_pause)
    ended = (ENROLLED + timedelta(days=60)).date()
    now = datetime.combine(ended, datetime.min.time(),
                           tzinfo=timezone.utc) + timedelta(days=days_ago_end, hours=18)
    return _p(proposal_status="viewed", cycle_viewed_at=v, followup_paused_until=ended), now, ended


def test_a_live_pause_still_silences_everything():
    p, _, ended = _paused(0)
    during = datetime.combine(ended, datetime.min.time(), tzinfo=timezone.utc) - timedelta(days=5)
    assert fr.due_now(p, during) == []


def test_an_expired_pause_tells_the_estimator_AND_resumes_the_customer_chase():
    p, now, ended = _paused(30)
    dues = fr.due_now(p, now)
    assert [d.template for d in of(dues, "staff")][:1] == ["staff_pause_expired"], (
        "the estimator is not told the customer's window closed")
    assert of(dues, "customer"), (
        "the chase did not resume — this customer would never be emailed again")


def test_the_paused_months_do_not_count_as_elapsed():
    """Otherwise a two-month pause drops the proposal straight past max_recurring, and somebody
    who asked for time is punished with "cadence exhausted, nothing more is coming"."""
    p, now, ended = _paused(1)
    resumed = datetime(ended.year, ended.month, ended.day, 8,
                       tzinfo=fr.BUSINESS_TZ) + timedelta(days=1)
    # One day after the pause ended, the clock has just restarted: the first post-pause reminder
    # is still ahead, so nothing recurring has matured.
    nxt = fr.next_due_at(p, now)
    assert nxt is not None, "the board says nothing is coming, so the cap was hit by the pause"
    assert nxt >= resumed, "the next reminder is dated before the pause even ended"


def test_the_board_and_the_worker_agree_after_a_pause_expires():
    """The specific inconsistency the audit found: the page showed a date the worker would never
    act on."""
    p, now, _ = _paused(30)
    nxt = fr.next_due_at(p, now)
    fired = of(fr.due_now(p, now), "customer")
    # next_due_at names the occurrence AFTER the one that has matured, so it is legitimately in
    # the future while due_now fires. What must never happen is the two disagreeing about whether
    # this proposal is being chased at all - which is what the audit found: the board showed a
    # date and the worker had quietly stopped for good.
    assert fired, "the worker is not chasing"
    assert nxt is not None, "the board says nothing is coming while the worker is chasing"
    assert nxt > now, "the next occurrence should be ahead of the one just fired"


# ── raising an interval must not silence the chase ────────────────────────────
def test_raising_the_interval_does_not_replay_a_consumed_key():
    """The ordinal bug. At 12 days elapsed, "every 3 days" is occurrence 3 and "every 5 days" is
    occurrence 1 — the old key format made the slower schedule collide with keys the faster one
    had already used, so the customer heard nothing until the count climbed past the old high
    water mark. Months, silently."""
    p, at = _viewed(24 * 12)          # whole days: same local hour, inside the send window
    fast = of(fr.due_now(p, at, {"recurring_hours": 72}), "customer")[0].rule_key
    slow = of(fr.due_now(p, at, {"recurring_hours": 120}), "customer")[0].rule_key
    assert fast != slow, (
        "the same key is reused across two different intervals, so the send is deduped away")


def test_the_key_is_the_occurrence_time_not_the_count():
    p, at = _viewed(24 * 12)
    key = of(fr.due_now(p, at), "customer")[0].rule_key
    assert "@" in key and key.split("@")[-1].isdigit() is False
    assert len(key.split("@")[-1]) == 11, "expected a YYYYMMDDTHH stamp, got %r" % key


def test_two_ticks_in_the_same_occurrence_still_dedupe_to_one_key():
    """The property the ordinal gave us for free, which the new scheme must keep: the worker runs
    every few minutes and must not email on each pass."""
    p, at = _viewed(150)
    a = of(fr.due_now(p, at), "customer")[0].rule_key
    b = of(fr.due_now(p, at + timedelta(minutes=20)), "customer")[0].rule_key
    assert a == b
