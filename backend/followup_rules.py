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


# ── the cadence as data ───────────────────────────────────────────────────────
# The constants above are the SHIPPED cadence and remain the defaults. Staff can now edit the
# intervals (followup_settings.py), so every function that uses them takes an optional `cfg`.
#
# Optional, rather than required, on purpose: `cfg=None` resolves to exactly the constants, so
# every existing caller and all 24 rule tests behave as they did. The alternative — module-level
# mutable config set once per tick — would have made those tests order-dependent and broken this
# module's "pure functions, no I/O" contract, which is what makes the cadence testable at all.
class _Cadence:
    """The seven numbers this module needs, resolved once per call."""

    __slots__ = ("first", "second", "recurring", "staff_personal", "max_recurring",
                 "start_hour", "end_hour")

    def __init__(self, cfg: Optional[dict] = None):
        c = cfg or {}

        def hours(key: str, fallback: timedelta) -> timedelta:
            v = c.get(key)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
                return fallback
            return timedelta(hours=float(v))

        def whole(key: str, fallback: int, lo: int, hi: int) -> int:
            v = c.get(key)
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                return fallback
            return max(lo, min(hi, int(v)))

        self.first = hours("first_nudge_hours", FIRST_NUDGE)
        self.second = hours("second_nudge_hours", SECOND_NUDGE)
        self.recurring = hours("recurring_hours", RECURRING)
        self.staff_personal = hours("staff_personal_hours", STAFF_PERSONAL)
        self.max_recurring = whole("max_recurring", MAX_RECURRING, 1, 200)
        self.start_hour = whole("send_start_hour", SEND_START_HOUR, 0, 23)
        self.end_hour = whole("send_end_hour", SEND_END_HOUR, 1, 24)
        # A window that ends before it opens would silence every customer email. Settings
        # validation already prevents it; this is the second line of defence, because a
        # hand-edited settings row reaches this code without passing through that validation.
        if self.end_hour <= self.start_hour:
            self.start_hour, self.end_hour = SEND_START_HOUR, SEND_END_HOUR


@dataclass(frozen=True)
class Due:
    rule_key: str          # dedupe identity: unique per proposal per occurrence
    audience: str          # "customer" | "staff"
    template: str
    include_status_ask: bool = False


def cycle_key(enrolled_at: datetime) -> str:
    """Namespace for one send's rule keys, so a re-send can fire the same rules."""
    return "c%d" % int(enrolled_at.timestamp())


def in_send_window(now: datetime, cfg: Optional[dict] = None) -> bool:
    """Is it a civil hour to email a customer, in Treadwell's timezone?"""
    c = _Cadence(cfg)
    return c.start_hour <= now.astimezone(BUSINESS_TZ).hour < c.end_hour


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


def _recurring_index(elapsed: timedelta, first: timedelta,
                     c: Optional["_Cadence"] = None) -> int:
    """How many recurring steps have matured past `first`. 0 = none yet."""
    c = c or _Cadence()
    if elapsed < first + c.recurring:
        return 0
    return min(c.max_recurring, int((elapsed - first) // c.recurring))


def next_due_at(p: dict, now: datetime, cfg: Optional[dict] = None) -> Optional[datetime]:
    """When the next CUSTOMER reminder is due, or None if none ever will be.

    Read-only companion to `due_now` for the Follow-ups page, which needs to say "next
    reminder: in 2 days" rather than only "one is due right now". Mirrors the same
    anchors and thresholds deliberately: if this drifts from `due_now`, the page lies
    about a schedule nobody can see any other way.

    Returns a time in the past when one is already overdue — the page renders that as
    "due now", and the difference between "overdue by six days" and "due tomorrow" is
    the whole reason to look at this screen. Ignores the 8am-6pm send window on purpose:
    the window delays an email by hours, and rounding a date forward for it would make
    the column disagree with itself overnight.
    """
    c = _Cadence(cfg)
    enrolled = _aware(p.get("followup_enrolled_at"))
    if not enrolled or p.get("followup_disabled_at"):
        return None                                     # not automated
    if (p.get("proposal_status") or "") not in ("sent", "viewed"):
        return None                                     # approved, or closed lost

    until = _as_date(p.get("followup_paused_until"))
    if until and business_today(now) <= until:
        # Paused. The cadence resumes the morning after the customer's window closes.
        return datetime(until.year, until.month, until.day,
                        c.start_hour, tzinfo=BUSINESS_TZ) + timedelta(days=1)

    viewed = _aware(p.get("cycle_viewed_at"))
    anchor = viewed or enrolled
    first = c.first if viewed is None else c.second
    elapsed = now - anchor

    if elapsed < c.first:
        return anchor + c.first                         # the first one hasn't matured
    if viewed is not None and elapsed < c.second:
        return anchor + c.second                        # viewed: first one done, second next

    # Into the recurring stage. `_recurring_index` is what due_now uses to decide which
    # occurrence has matured, so the next one is simply the step after it — capped, so a
    # proposal that has exhausted MAX_RECURRING correctly reports "no more".
    n = _recurring_index(elapsed, first, c)
    if n >= c.max_recurring:
        return None
    return anchor + first + c.recurring * (n + 1)


def due_now(p: dict, now: datetime, cfg: Optional[dict] = None) -> list[Due]:
    """The rules that have matured for this proposal, at most one per audience."""
    c = _Cadence(cfg)
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
        if elapsed >= c.first:
            n = _recurring_index(elapsed, c.first, c)
            if n:
                # Still unopened days later. This is exactly the proposal that needs
                # the "delayed / not moving forward" escape hatch offered.
                out.append(Due("%s:nvr%d" % (ck, n), "customer", "not_viewed", True))
            else:
                out.append(Due("%s:nv1" % ck, "customer", "not_viewed"))
            out.append(Due("%s:nv1_staff" % ck, "staff", "staff_not_viewed"))
    else:
        elapsed = now - viewed
        n = _recurring_index(elapsed, c.second, c)
        if n:
            out.append(Due("%s:vr%d" % (ck, n), "customer", "checkin", True))
        elif elapsed >= c.second:
            out.append(Due("%s:v2" % ck, "customer", "second_nudge"))
        elif elapsed >= c.first:
            out.append(Due("%s:v1" % ck, "customer", "next_steps"))
        if elapsed >= c.staff_personal:
            out.append(Due("%s:v48_staff" % ck, "staff", "staff_personal_followup"))

    # Latest-only, per audience: never send two customer emails in one tick.
    if not in_send_window(now, cfg):
        out = [d for d in out if d.audience != "customer"]
    return out
