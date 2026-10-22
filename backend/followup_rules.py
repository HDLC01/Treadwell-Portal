"""When to chase a proposal, and with what. Pure functions — no I/O, no clock.

The cadence Hanz specified:

    sent, not viewed   → 24h: "it's ready when you are" (+ tell the estimator)
    viewed, not signed → 24h: what we need to schedule
                       → +48h: second reminder
                       → every 3 days after that, with a way out
    viewed, 48h, still pending → tell the estimator to make it personal
    approved, deposit not in    → 24h: the deposit reserves the dates, then every 3 days
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

import json
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
#
# Since 2026-10-23 this is the PRE-DECISION cap only: how many reminders a customer who has not
# decided yet is worth, counted since their last reply. It is the knob Kyle's stopgap lowers.
MAX_RECURRING = 20

# The DEPOSIT stage has its own cap, separate from the one above. Hanz, 2026-10-23 (decision 5):
# lowering "how many times do we nudge somebody before they decide" must never silence the
# reminder to chase a deposit on a job that is already signed. The two used to share
# MAX_RECURRING, so turning the pre-decision count down to 3 would also have stopped the
# estimator hearing about an unpaid won job after three reminders. 200 x 3 days is well over a
# year: effectively unbounded, but still a number, so a row nobody ever closes cannot loop forever.
DEPOSIT_MAX_RECURRING = 200

# ── follow-up MODE ────────────────────────────────────────────────────────────
# Hanz, 2026-10-23 (decision 1): v1 is REMIND-ONLY for every proposal, including the ones already
# mid-cadence when this ships (decision 2). When a reminder comes due, nothing is emailed to the
# customer: the estimator is told, and a draft waits for them to review, edit and send by hand.
#
# 'approve' (send a pre-filled template on a click) and 'auto' (the old ladder) are FUTURE modes.
# They are legal values of portal_proposals.followup_mode so the column does not need another
# migration when they arrive, but no code path reads or writes them in v1 — every proposal is
# treated as 'remind' whatever the column says.
MODE_REMIND = "remind"
FOLLOWUP_MODES = ("remind", "approve", "auto")


def followup_mode(p: dict) -> str:
    """The mode this proposal runs in. Always 'remind' in v1 — see FOLLOWUP_MODES."""
    return MODE_REMIND


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
                 "deposit_max", "start_hour", "end_hour")

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
        # Its own key and its own default — see DEPOSIT_MAX_RECURRING. Nothing that lowers
        # max_recurring (the settings page, a per-proposal stop-after) reaches this.
        self.deposit_max = whole("deposit_max_recurring", DEPOSIT_MAX_RECURRING, 1, 1000)
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


def deposit_outstanding(p: dict, *, for_customer: bool) -> bool:
    """Is this approved job still waiting on its deposit?

    Hanz, 2026-08-12: "followups should be automated until a deposit has been received." The
    cadence used to stop dead at approval, and nothing replaced it — an approved job got one
    invoice email and then silence on every channel.

    THE TWO AUDIENCES STOP AT DIFFERENT POINTS, which is the whole reason this takes a flag:

      * the CUSTOMER stops at `submitted`. Once they have entered ACH details or told us the
        cheque is posted, the money is in flight; chasing then reads as either a mistake or a
        second charge. Same judgement as the peer-notification receipt split.
      * the ESTIMATOR keeps going until `received`. A cheque that never arrives must not be able
        to go quiet on the staff side — somebody has to still be asking.

    `deposit_required is not False` because the flag is tri-state in practice: legacy rows have no
    value and DO collect one, and a job sent with no deposit has nothing to chase.
    """
    # `deposit_required is False` means the proposal went out without one — GC work usually does.
    # UNLESS an invoice was raised anyway: `deposit_requested_at` is staff deciding after the fact
    # that money is due, and money that has been asked for is money worth chasing. This is the rule
    # crm-core.depositSatisfied already used on the board, and the two disagreeing would have shown
    # a staff drawer reading "following up until the deposit is in" while the worker sent nothing.
    if p.get("deposit_required") is False and not p.get("deposit_requested_at"):
        return False
    status = str(p.get("deposit_status") or "").strip().lower()
    done = ("submitted", "received") if for_customer else ("received",)
    return status not in done


def in_scope(p: dict) -> bool:
    """Is there anything left to chase on this proposal at all?

    ONE definition, because there are three places that have to agree: the SQL that lists
    candidates, the worker's re-read a few minutes later, and the gate at the top of `due_now`.
    The worker used to spell its own version out inline (`status not in ("sent", "viewed")`), and
    widening the engine without it would have changed nothing observable — every approved row would
    have been dropped one line before the rules ran.

    Deliberately the STAFF reading of the deposit (the later of the two stops), because this only
    decides whether to look. Which audience is owed a send this tick is `due_now`'s call."""
    status = (p.get("proposal_status") or "")
    if status in ("sent", "viewed"):
        return True
    if status != "approved":
        return False
    return deposit_outstanding(p, for_customer=False)


def is_paused(p: dict, now: datetime) -> bool:
    """A pause covers the whole of its final day — the customer said "about two
    months", not "to the minute"."""
    until = _as_date(p.get("followup_paused_until"))
    return bool(until and business_today(now) <= until)


def _recurring_index(elapsed: timedelta, first: timedelta,
                     c: Optional["_Cadence"] = None, cap: Optional[int] = None) -> int:
    """How many recurring steps have matured past `first`. 0 = none yet.

    `cap` is which series' cap applies. It used to be max_recurring for every series, which is
    what tied the deposit chase to the pre-decision count (decision 5); the deposit stage now
    passes `c.deposit_max`."""
    c = c or _Cadence()
    if elapsed < first + c.recurring:
        return 0
    return min(c.max_recurring if cap is None else cap, int((elapsed - first) // c.recurring))


def _occurrence_key(ck: str, tag: str, anchor: datetime, first: timedelta,
                    n: int, c: "_Cadence") -> str:
    """Dedupe identity for one recurring occurrence, derived from WHEN it falls.

    It used to be the ordinal — `vr3`, the third recurring send. That silently broke the moment
    the interval became editable. Raise "every 3 days" to "every 5" and the same elapsed time
    yields a SMALLER n, so the key lands back on one already used; the send is deduped away and
    the customer hears nothing for as long as it takes the new, slower count to climb past the
    old one. Months, for a proposal that had been running a while — the exact opposite of what
    the editor promises, and invisible, because a deduped send logs nothing.

    Keying on the occurrence TIME instead means changing the interval moves the occurrences, so
    the new schedule cannot collide with the old one. Hour granularity is enough: the floor on
    every interval is four hours, so two occurrences can never share an hour.

    Migration note: this changes the key format, so a proposal already mid-cadence when the
    change deploys has no matching key for its next occurrence and may get one extra email. Once
    only, and `due_now` emits at most the latest matured occurrence, so it cannot cascade.
    """
    at = anchor + first + c.recurring * n
    return "%s:%s@%s" % (ck, tag, _stamp(at))


def _stamp(at: datetime) -> str:
    return at.astimezone(timezone.utc).strftime("%Y%m%dT%H")


def _resume_anchor(p: dict, now: datetime, c: "_Cadence") -> Optional[datetime]:
    """When an EXPIRED pause restarts the clock, or None if there is no expired pause.

    A pause is the customer saying "ask me in two months". Two things follow, and neither was
    true before:

    * The chase has to actually resume. `due_now` used to emit the staff "pause expired" note and
      `return` — and nothing ever clears `followup_paused_until` (only an explicit human action
      does), so every later tick took the same branch and returned the same note, which its own
      dedupe key had already consumed. The customer was never chased again. The board said
      otherwise, because `next_due_at` fell through to normal scheduling: it showed a date the
      worker was never going to act on.
    * The paused months must not count as elapsed. Counting them would drop the proposal straight
      into a high recurring ordinal — often past `max_recurring`, which reads as "cadence
      exhausted, nothing more is coming". Somebody who asked for time would be punished for it.
      The clock restarts the morning after their window closes, which is what the board has been
      promising all along.
    """
    until = _as_date(p.get("followup_paused_until"))
    if not until or business_today(now) <= until:
        return None
    return datetime(until.year, until.month, until.day,
                    c.start_hour, tzinfo=BUSINESS_TZ) + timedelta(days=1)


# ── touches: the human contact that re-anchors the schedule ───────────────────
# RJ's complaint, and the gap this closes: logging a call, a customer replying, an estimator
# answering in the chat — none of it moved the schedule. `due_now` was a pure function of the
# send/view/approval stamps, so the day after a forty-minute phone call the estimator was told to
# follow up, as if nothing had happened.
#
# A TOUCH is any of these, and only the latest one after the stage's own anchor counts:
#
#   followup_anchor_at      stamped by every decision on a follow-up task (send / skip / called /
#                           snooze) — the explicit human "I have dealt with this"
#   customer_touch_at       the newest customer-authored row in the thread: a chat message, an
#                           emailed reply (inbound relay), a status answer, a deposit submission.
#                           Out-of-office auto-replies are excluded where the row is written.
#   last_staff_followup_at  the newest call / email / text / note an estimator LOGGED (bookkeeping
#                           rows with an `action` key are not outreach and are excluded upstream)
#   last_staff_message_at   the newest message an estimator wrote to the customer in the thread,
#                           typed in the drawer or sent by email through the inbound relay
#
# These come from the DATA rather than from stamping a column at every write site, because there
# are at least six places a customer-authored row is written and a stamp each one had to remember
# is a stamp the seventh would forget. db.followup_signals (the worker) and the pipeline query
# (the board) both compute them with the same predicates.
TOUCH_KEYS = ("followup_anchor_at", "customer_touch_at",
              "last_staff_followup_at", "last_staff_message_at")

# Every field the two schedule functions read. The board's query is pinned against this list
# (test_admin_pipeline.py), because a field the worker sees and the board does not is exactly how
# the board came to show the not-viewed track's date for a viewed proposal: the pipeline query
# never selected cycle_viewed_at, so next_due_at was computing from a row with no view in it.
INPUT_KEYS = ("proposal_status", "followup_enrolled_at", "followup_disabled_at",
              "followup_paused_until", "cycle_viewed_at", "approved_at", "deposit_status",
              "deposit_required", "deposit_requested_at", "followup_next_at",
              "followup_interval_days", "followup_stop_after", "followup_reminder_times"
              ) + TOUCH_KEYS


def _ts(v: Any) -> Optional[datetime]:
    """A timestamp from the database OR from JSON, as an aware datetime; None if it isn't one.

    `_aware` takes real datetimes only, which is what psycopg hands back for a column. The
    reminder times arrive through jsonb_agg as ISO strings, and a stubbed row in a test may carry
    either, so this accepts both."""
    if isinstance(v, datetime):
        return _aware(v)
    if isinstance(v, str) and v.strip():
        try:
            d = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return None


def _latest(p: dict, keys: tuple, after: datetime) -> Optional[datetime]:
    """The newest of `keys` that falls strictly after `after`, or None."""
    best = None
    for k in keys:
        t = _ts(p.get(k))
        if t is not None and t > after and (best is None or t > best):
            best = t
    return best


def _reminders_since(p: dict, since: datetime) -> int:
    """How many reminders this proposal has had since `since`.

    `followup_reminder_times` is the creation time of every reminder in the current send cycle:
    each follow-up task that was not expired, plus each automated customer email the old ladder
    sent before remind-only shipped (so a proposal that was already nagged twenty times does not
    get a fresh twenty). Absent means "unknown" and counts as none, which leaves only the ladder's
    own index cap — exactly how the engine behaved before this count existed."""
    raw = p.get("followup_reminder_times") or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = []
    if not isinstance(raw, (list, tuple)):
        return 0
    n = 0
    for v in raw:
        t = _ts(v)
        if t is not None and t > since:
            n += 1
    return n


def _whole_or_none(v: Any, lo: int, hi: int) -> Optional[int]:
    if isinstance(v, bool) or v is None or v == "":
        return None
    try:
        n = int(float(str(v).strip()))
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return max(lo, min(hi, n))


def effective_cfg(cfg: Optional[dict], p: dict) -> dict:
    """The global cadence with THIS proposal's own schedule laid over it. Pure.

    Per-proposal wins where it is set, and only there:

      followup_interval_days  "follow up with this one every N days" — replaces recurring_hours,
                              which is also the gap after a touch
      followup_stop_after     "stop after N reminders" — replaces max_recurring, the PRE-DECISION
                              cap only. It never reaches deposit_max_recurring (decision 5): a
                              customer's nudge count is not a reason to stop chasing their deposit.

    `followup_next_at` (a snooze to a specific date) is not a cadence knob and is read directly by
    the schedule; see `_customer_plan`.

    Both schedule functions call this themselves, so the worker and the board cannot disagree about
    whether an override applies."""
    out = dict(cfg or {})
    days = _whole_or_none(p.get("followup_interval_days"), 1, 90)
    if days:
        out["recurring_hours"] = days * 24
    stop = _whole_or_none(p.get("followup_stop_after"), 1, 60)
    if stop:
        out["max_recurring"] = stop
    return out


def customer_stage(p: dict) -> Optional[str]:
    """Which customer reminder series this proposal is in: 'pre' (sent or viewed, not decided),
    'deposit' (approved, deposit not yet on its way), or None (nothing to remind the customer of)."""
    status = (p.get("proposal_status") or "")
    if status in ("sent", "viewed"):
        return "pre"
    if status == "approved" and deposit_outstanding(p, for_customer=True):
        return "deposit"
    return None


def task_stage(template: str) -> str:
    return "deposit" if template == "deposit_nudge" else "pre"


def task_is_live(detail: Optional[dict], p: Optional[dict], now: datetime) -> tuple:
    """May a pending follow-up task still be acted on? (ok, reason).

    The ONE answer for two callers: the worker's housekeeping, which expires a task whose
    proposal has moved on, and the send endpoint, which must never email a customer who signed,
    paid, asked for a pause or was closed out after the task was raised. A stale task in an
    estimator's queue is harmless; a stale task SENT is a nag to somebody who already answered."""
    d = detail or {}
    if not p:
        return False, "missing"
    if p.get("deleted_at"):
        return False, "deleted"
    if p.get("followup_disabled_at"):
        return False, "automation_off"
    enrolled = _aware(p.get("followup_enrolled_at"))
    if not enrolled:
        return False, "not_enrolled"
    if (p.get("proposal_status") or "") == "closed_lost":
        return False, "closed_lost"
    if not in_scope(p):
        return False, "finished"
    if is_paused(p, now):
        return False, "paused"
    stage = customer_stage(p)
    if stage is None:
        return False, "customer_done"
    if d.get("stage") and d.get("stage") != stage:
        return False, "stage_changed"
    if d.get("cycle") and d.get("cycle") != cycle_key(enrolled):
        return False, "resent"
    return True, ""


def _anchors(p: dict, now: datetime, c: "_Cadence") -> tuple:
    """(base, touch) for this proposal's current stage.

    `base` is the stage's own clock — the send, the first view of this send, or the approval —
    moved forward to an expired pause's restart. `touch` is the newest human contact AFTER base,
    or None. A touch before base belongs to an earlier stage and says nothing about this one."""
    enrolled = _aware(p.get("followup_enrolled_at"))
    viewed = _aware(p.get("cycle_viewed_at"))
    status = (p.get("proposal_status") or "")
    if status == "approved":
        stage_anchor = _aware(p.get("approved_at")) or viewed or enrolled
    else:
        stage_anchor = viewed or enrolled
    resume = _resume_anchor(p, now, c)
    base = resume if resume and resume > stage_anchor else stage_anchor
    return base, _latest(p, TOUCH_KEYS, base)


def _customer_plan(p: dict, now: datetime, c: "_Cadence") -> tuple:
    """(matured, next_at) for the CUSTOMER reminder series. The single source of both answers.

    `matured` is the latest occurrence that has come due (a Due, or None) — what due_now emits.
    `next_at` is the occurrence after it — what next_due_at reports. Computing both in one place
    is what keeps those two functions exact mirrors: they used to be two hand-maintained copies of
    the same branching, and the board drifted from the worker more than once.

    THREE SHAPES, in priority order:

    1. CAPPED. The reminders since the reset point (the stage anchor, or the customer's latest
       reply after it — decision 3: a reply puts the FULL count back) have reached the stage's cap.
       Nothing matures and nothing is next. Pre-decision stages use max_recurring (with a
       per-proposal stop-after laid over it); the deposit stage uses its own deposit_max.

    2. TOUCHED. Somebody has dealt with this proposal since its stage began. Reminders then fall at
       touch + interval, touch + 2*interval, ... — "follow up in N days" means N days after the
       last real contact, not N days after an anchor the contact never moved. A snooze to a specific
       date (`followup_next_at`) that is later than the touch wins instead, and the series runs on
       from that date.

       Keys are time-derived with their own tag, INCLUDING the first occurrence. The ladder below
       uses fixed per-cycle keys for its first one or two steps (nv1, v1, v2, dep1); if a touched
       schedule reused them, the first reminder after a touch would collide with the row the ladder
       already consumed, be deduped away in silence, and the real next reminder would land a whole
       interval later than "touch + N". That is the stress test's finding, and this is why the
       touched series never names a fixed key.

    3. LADDER. No touch yet: the cadence Hanz specified, exactly as it shipped — same times, same
       templates, same keys. That is deliberate, and it is what makes the switch to remind-only safe
       for proposals already mid-cadence (decision 2): the next occurrence's key is the one the old
       ladder would have used, so the shared dedupe index stops it duplicating a reminder that was
       already emailed, and nobody's estimator gets a burst of tasks the morning this ships.
    """
    enrolled = _aware(p.get("followup_enrolled_at"))
    ck = cycle_key(enrolled)
    status = (p.get("proposal_status") or "")
    viewed = _aware(p.get("cycle_viewed_at"))
    if status == "approved":
        if not deposit_outstanding(p, for_customer=True):
            return None, None
        oneshots = ((c.first, "dep1", "deposit_nudge", False),)
        # NO include_status_ask on the deposit stage, recurring or not — the one place in this
        # engine where the recurring stage does not offer it. That block asks "has your timeline
        # changed?" over two buttons, and the second reads "Not moving forward", which sets
        # closed_lost. Putting a one-click cancel on a job the customer has already signed and
        # been invoiced for is the worst button we could send them.
        rec = ("depr", "deposit_nudge", False)
        cap = c.deposit_max
    elif viewed is None:
        oneshots = ((c.first, "nv1", "not_viewed", False),)
        # Still unopened days later. This is exactly the proposal that needs the "delayed / not
        # moving forward" escape hatch offered.
        rec = ("nvr", "not_viewed", True)
        cap = c.max_recurring
    else:
        oneshots = ((c.first, "v1", "next_steps", False),
                    (c.second, "v2", "second_nudge", False))
        rec = ("vr", "checkin", True)
        cap = c.max_recurring

    base, touch = _anchors(p, now, c)

    # 1. capped — counted since the reset point. Only the CUSTOMER's reply resets the count; a
    # staff touch moves the date (shape 2) but not the count, or skipping a task would hand the
    # estimator a fresh full set of reminders every time and the cap would never bind.
    reset = _latest(p, ("customer_touch_at",), base) or base
    if _reminders_since(p, reset) >= cap:
        return None, None

    # 2. touched
    if touch is not None:
        step = c.recurring
        snooze = _ts(p.get("followup_next_at"))
        start = snooze if (snooze is not None and snooze > touch) else touch + step
        if now < start:
            return None, start
        k = min(cap, int((now - start) // step))
        at = start + step * k
        tag, tpl, ask = rec
        matured = Due("%s:%st@%s" % (ck, tag, _stamp(at)), "customer", tpl, ask)
        return matured, (None if k >= cap else at + step)

    # 3. ladder — the shipped cadence, unchanged
    elapsed = now - base
    rec_off = oneshots[-1][0]
    n = _recurring_index(elapsed, rec_off, c, cap)
    matured = None
    if n:
        tag, tpl, ask = rec
        matured = Due(_occurrence_key(ck, tag, base, rec_off, n, c), "customer", tpl, ask)
    else:
        for off, tag, tpl, ask in reversed(oneshots):
            if elapsed >= off:
                matured = Due("%s:%s" % (ck, tag), "customer", tpl, ask)
                break
    for off, _tag, _tpl, _ask in oneshots:
        if elapsed < off:
            return matured, base + off
    if n >= cap:
        return matured, None
    return matured, base + rec_off + c.recurring * (n + 1)


def next_due_at(p: dict, now: datetime, cfg: Optional[dict] = None) -> Optional[datetime]:
    """When the next CUSTOMER reminder is due, or None if none ever will be.

    Read-only companion to `due_now` for the Follow-ups page, which needs to say "next
    reminder: in 2 days" rather than only "one is due right now". Both read `_customer_plan`, so
    they cannot drift: if this disagreed with `due_now`, the page would state a schedule nobody
    can see any other way.

    In remind mode (every proposal, v1) "reminder" means the estimator is told and a draft waits
    for them — nothing reaches the customer on its own. The date is still the customer's: it is
    when they are next due to hear from us.

    Reports the next time the schedule MATURES, which for an active proposal is ahead of now.
    Ignores the 8am-6pm window on purpose: the window delays a reminder by hours, and rounding a
    date forward for it would make the column disagree with itself overnight.
    """
    c = _Cadence(effective_cfg(cfg, p))
    enrolled = _aware(p.get("followup_enrolled_at"))
    if not enrolled or p.get("followup_disabled_at"):
        return None                                     # not automated
    status = (p.get("proposal_status") or "")
    if status not in ("sent", "viewed", "approved"):
        return None                                     # closed lost
    # Approved counts only while the CUSTOMER is still being chased. The staff reminders run
    # past that point, but this column is the customer's schedule.
    if status == "approved" and not deposit_outstanding(p, for_customer=True):
        return None

    until = _as_date(p.get("followup_paused_until"))
    if until and business_today(now) <= until:
        # Paused. The cadence resumes the morning after the customer's window closes.
        return datetime(until.year, until.month, until.day,
                        c.start_hour, tzinfo=BUSINESS_TZ) + timedelta(days=1)

    return _customer_plan(p, now, c)[1]


def due_now(p: dict, now: datetime, cfg: Optional[dict] = None) -> list[Due]:
    """The rules that have matured for this proposal, at most one per audience.

    A CUSTOMER Due no longer means "email the customer". In remind mode (v1, every proposal) the
    worker turns it into a follow-up task for the estimator and emails nobody outside the company;
    see followup_worker._tick. The rules do not know that, on purpose: they decide WHEN, and the
    mode decides WHAT, so a later 'approve' or 'auto' mode needs no second schedule."""
    c = _Cadence(effective_cfg(cfg, p))
    enrolled = _aware(p.get("followup_enrolled_at"))
    if not enrolled or p.get("followup_disabled_at"):
        return []
    status = (p.get("proposal_status") or "")
    # APPROVED is in scope only while a deposit is still outstanding. Everything else (closed
    # lost, or approved-and-paid) is out. See in_scope / deposit_outstanding.
    if not in_scope(p):
        return []

    out: list[Due] = []

    # A pause expiring is the one thing that fires while paused — the estimator needs
    # to know the customer's window has closed and the cadence is live again.
    until = _as_date(p.get("followup_paused_until"))
    if until and business_today(now) <= until:
        return []
    if _resume_anchor(p, now, c):
        # Tell the estimator once (the key is the pause date, so later ticks dedupe), and then
        # CARRY ON. This used to `return` here, which is why an expired pause ended a customer's
        # chase for good — see _resume_anchor.
        out.append(Due("pe:%s" % until.isoformat(), "staff", "staff_pause_expired"))

    ck = cycle_key(enrolled)
    viewed = _aware(p.get("cycle_viewed_at"))
    base, touch = _anchors(p, now, c)
    # The estimator's own reminders run off the latest touch too: an estimator who rang the
    # customer this morning does not need telling tonight that "a call is worth more than another
    # email". Their keys stay fixed one-shots per cycle, so a touch can push one later but can
    # never make it fire twice.
    staff_anchor = touch or base

    matured, _ = _customer_plan(p, now, c)
    if matured is not None:
        out.append(matured)

    if status == "approved":
        # ── approved, deposit outstanding ────────────────────────────────────
        # The staff half runs on its own threshold and its own stop point — it keeps going after
        # the customer's stops, which is the case Hanz asked for: a cheque that never arrives. Its
        # cap is deposit_max, NOT max_recurring (decision 5).
        elapsed = now - staff_anchor
        if elapsed >= c.staff_personal:
            n = _recurring_index(elapsed, c.staff_personal, c, c.deposit_max)
            if touch is None:
                key = (_occurrence_key(ck, "depsr", staff_anchor, c.staff_personal, n, c) if n
                       else "%s:dep_staff" % ck)
            else:
                # Re-anchored by a touch: time-derived from the first occurrence on, for the
                # reason _customer_plan gives — "dep_staff" is already spent on this cycle.
                key = _occurrence_key(ck, "depst", staff_anchor, c.staff_personal, n, c)
            out.append(Due(key, "staff", "staff_deposit_outstanding"))
    elif viewed is None:
        if now - staff_anchor >= c.first:
            out.append(Due("%s:nv1_staff" % ck, "staff", "staff_not_viewed"))
    else:
        if now - staff_anchor >= c.staff_personal:
            out.append(Due("%s:v48_staff" % ck, "staff", "staff_personal_followup"))

    # Latest-only, per audience: never more than one customer reminder in one tick.
    if not in_send_window(now, cfg):
        out = [d for d in out if d.audience != "customer"]
    return out
