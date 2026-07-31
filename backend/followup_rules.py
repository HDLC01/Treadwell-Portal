"""When to chase a proposal, and with what. Pure functions — no I/O, no clock.

The cadence Hanz specified:

    sent, not viewed   → 24h: "it's ready when you are" (+ tell the estimator)
    viewed, not signed → 24h: what we need to schedule
                       → +48h: second reminder
                       → every 3 days after that, with a way out
    viewed, 48h, still pending → tell the estimator to make it personal
    a pause that has expired   → remind the estimator it's live again

Three design choices carry most of the weight.

**Anchors.** The "sent" clock is `followup_enrolled_at`, not `created_at`, because a
revision re-publish never moves created_at — a revised proposal would inherit the
original send's clock and fire every overdue reminder at once. The "viewed" clock is
`cycle_viewed_at`, not `viewed_at`, for the same reason: viewed_at is the first view
EVER (the board wants that), so after a revision it would already be days old.

**Latest-only.** `due_now` returns at most one customer rule and one staff rule, the
most recent that has matured — it never backfills. A tick that was missed (downtime,
a restart, the kill switch off over a weekend) therefore costs one email, not a
burst. This is what makes the whole thing safe to run without catch-up bookkeeping.

**Cycle-scoped keys.** Every rule key carries the enrolment timestamp, so re-sending
a proposal starts a fresh set of keys and the same rule can fire again for the new
version without colliding with the last one's dedupe row.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

try:                                      # 3.9+ stdlib; the container has it
    from zoneinfo import ZoneInfo
    BUSINESS_TZ = ZoneInfo("America/Chicago")
except Exception:                         # pragma: no cover - fallback keeps import safe
    BUSINESS_TZ = timezone.utc

# Customer-facing email is clamped to business hours. A nag at 3am reads as a robot
# and invites a spam complaint; staff notes are unclamped because they land in a
# work inbox and being timely matters more.
SEND_START_HOUR = 8
SEND_END_HOUR = 18

FIRST_NUDGE = timedelta(hours=24)      # after send, and after first view
SECOND_NUDGE = timedelta(hours=72)     # 48h after the first viewed reminder
RECURRING = timedelta(hours=72)        # "every 3 days thereafter"
STAFF_PERSONAL = timedelta(hours=48)   # viewed but still pending

# Cap the recurring series so a proposal nobody ever closes doesn't nag forever.
# 20 × 3 days ≈ two months past the last real signal, by which point the digest and
# the estimator's own judgement are the right instruments.
MAX_RECURRING = 20


@dataclass(frozen=True)
class Due:
    rule_key: str          # dedupe identity: unique per proposal per occurrence
    audience: str          # "customer" | "staff"
    template: str
    include_status_ask: bool = False


def cycle_key(enrolled_at: datetime) -> str:
    """Namespace for one send's rule keys, so a re-send can fire the same rules."""
    return "c%d" % int(enrolled_at.timestamp())


def in_send_window(now: datetime) -> bool:
    """Is it a civil hour to email a customer, in Treadwell's timezone?"""
    return SEND_START_HOUR <= now.astimezone(BUSINESS_TZ).hour < SEND_END_HOUR


def business_today(now: datetime) -> date:
    return now.astimezone(BUSINESS_TZ).date()


def add_months(d: date, months: int) -> date:
    """Month arithmetic without dateutil. Clamps to the month's length, so a pause
    started on the 31st lands on the 28th/30th rather than overflowing."""
    total = (d.year * 12 + (d.month - 1)) + months
    year, month = divmod(total, 12)
    month += 1
    if month == 12:
        nxt = date(year + 1, 1, 1)
    else:
        nxt = date(year, month + 1, 1)
    last_day = (nxt - timedelta(days=1)).day
    return date(year, month, min(d.day, last_day))


def _aware(v: Any) -> Optional[datetime]:
    """Coerce a DB timestamp to an aware datetime; None if it isn't one."""
    if not isinstance(v, datetime):
        return None
    return v if v.tzinfo else v.replace(tzinfo=timezone.utc)


def as_date(v: Any) -> Optional[date]:
    """A date column as a `date`, whatever the driver handed back.

    psycopg gives a real `date`, a stub or a JSON round-trip gives a string. Callers
    compare pause windows, so string-vs-date comparisons have to be impossible."""
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str) and v.strip():
        try:
            return date.fromisoformat(v.strip()[:10])
        except ValueError:
            return None
    return None


_as_date = as_date          # internal alias, kept so existing call sites read the same


def is_paused(p: dict, now: datetime) -> bool:
    """A pause covers the whole of its final day — the customer said "about two
    months", not "to the minute"."""
    until = _as_date(p.get("followup_paused_until"))
    return bool(until and business_today(now) <= until)


def _recurring_index(elapsed: timedelta, first: timedelta) -> int:
    """How many recurring steps have matured past `first`. 0 = none yet."""
    if elapsed < first + RECURRING:
        return 0
    return min(MAX_RECURRING, int((elapsed - first) // RECURRING))


def due_now(p: dict, now: datetime) -> list[Due]:
    """The rules that have matured for this proposal, at most one per audience."""
    enrolled = _aware(p.get("followup_enrolled_at"))
    if not enrolled or p.get("followup_disabled_at"):
        return []
    if (p.get("proposal_status") or "") not in ("sent", "viewed"):
        return []

    out: list[Due] = []

    # A pause expiring is the one thing that fires while paused — the estimator needs
    # to know the customer's window has closed and the cadence is live again.
    until = _as_date(p.get("followup_paused_until"))
    if until:
        if business_today(now) <= until:
            return []
        out.append(Due("pe:%s" % until.isoformat(), "staff", "staff_pause_expired"))
        return out

    ck = cycle_key(enrolled)
    viewed = _aware(p.get("cycle_viewed_at"))

    if viewed is None:
        elapsed = now - enrolled
        if elapsed >= FIRST_NUDGE:
            n = _recurring_index(elapsed, FIRST_NUDGE)
            if n:
                # Still unopened days later. This is exactly the proposal that needs
                # the "delayed / not moving forward" escape hatch offered.
                out.append(Due("%s:nvr%d" % (ck, n), "customer", "not_viewed", True))
            else:
                out.append(Due("%s:nv1" % ck, "customer", "not_viewed"))
            out.append(Due("%s:nv1_staff" % ck, "staff", "staff_not_viewed"))
    else:
        elapsed = now - viewed
        n = _recurring_index(elapsed, SECOND_NUDGE)
        if n:
            out.append(Due("%s:vr%d" % (ck, n), "customer", "checkin", True))
        elif elapsed >= SECOND_NUDGE:
            out.append(Due("%s:v2" % ck, "customer", "second_nudge"))
        elif elapsed >= FIRST_NUDGE:
            out.append(Due("%s:v1" % ck, "customer", "next_steps"))
        if elapsed >= STAFF_PERSONAL:
            out.append(Due("%s:v48_staff" % ck, "staff", "staff_personal_followup"))

    # Latest-only, per audience: never send two customer emails in one tick.
    if not in_send_window(now):
        out = [d for d in out if d.audience != "customer"]
    return out
