"""When to email a customer about a proposal, and with what. Pure functions: no I/O, no clock.

THE SCHEDULE (Hanz, 2026-10-28). A follow-up is an automatic email to the customer. It runs from
"proposal sent" until the proposal is approved, and there are at most TWO per step:

    NOT OPENED YET        1st email N days after sending       2nd email N days after the 1st
    OPENED, NO DECISION   1st email N days after they open it  2nd email N days after the 1st
    ESTIMATOR             "Tell the estimator" N days after the last customer email of a step

Every one of the five has an ON/OFF switch (followup_settings). Nothing repeats.

IT STOPS when the proposal is approved or closed lost, while it is on hold (a pause:
`followup_paused_until`, which is what "Project delayed" and the two hold reasons set), and when the
project's Follow-ups switch is off (`followup_disabled_at`). There is no deposit chase.

THE COUNT IS THE SENT HISTORY, not a clock. `followup_sent_history` (db._SENT_HISTORY_SQL) lists
every customer reminder of the current send cycle that reached the customer: the automatic
customer emails of every engine that ever ran (`auto_email`, audience customer) and the follow-up
tasks an estimator sent or answered with a call. Each row's template says which step it belongs to.
So:

  * a proposal that ALREADY had 2 (or more) reminders in its current step gets no more, however
    they were sent — the first tick after release cannot restart a chase the old ladder finished;
  * a restart, a missed tick or the twin container during a deploy cannot double-send: the 2nd
    email's date is the 1st email's real send time plus N days, and every send is also reserved
    under a rule key first (followup_worker), which the database refuses twice.

A re-send of the proposal (a revision) starts a new cycle (`followup_enrolled_at` moves and
`cycle_viewed_at` is cleared), so the new version gets its own 2 + 2.

"NEXT EMAIL ON" (`followup_next_at`, the drawer's date) moves the NEXT customer email to that day,
earlier or later. It applies only while it is later than the newest reminder already sent, so once
that email has gone it is spent; it never adds an email past the cap.

A REPLY SKIPS THE 2ND (Hanz, 2026-10-28: "When the customer replies, or someone logs a call, that
step's remaining follow-up email is SKIPPED (they are talking to us already)"). Contact AFTER a
step's 1st email and before its 2nd has gone cancels the 2nd: the step counts as handled. No
re-anchor and no extra email; the estimator's note still follows its own switch, dated off the 1st.
Contact is CONTACT_KEYS: a customer-authored row that is not an auto-reply, a call / email / text
an estimator logged (not a note, not the row a reminder task's own send or call wrote), or an
estimator's message in the thread. Contact BEFORE the 1st email skips nothing: the 1st still goes.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import followup_settings
import scope_groups

try:                                      # 3.9+ stdlib; the container has it
    from zoneinfo import ZoneInfo
    BUSINESS_TZ = ZoneInfo("America/Chicago")
except Exception:                         # pragma: no cover - fallback keeps import safe
    BUSINESS_TZ = timezone.utc

# Customer email goes out in business hours only (the settings can move the window).
SEND_START_HOUR = followup_settings.DEFAULTS["send_start_hour"]
SEND_END_HOUR = followup_settings.DEFAULTS["send_end_hour"]

NOT_OPENED = "not_opened"
OPENED = "opened"

# The tag inside each rule key. `:a:` is this engine's namespace, so none of its keys can collide
# with a row an older engine reserved (the old ladder's `nv1`, `v1`, `v2`, a skipped task's key).
# Collisions would not be safe in either direction: a reserved key the new schedule needs would
# silently block that email for ever, and the history count already covers what the old rows sent.
KEY_NS = "a"
_TAGS = {NOT_OPENED: ("nv1", "nv2"), OPENED: ("v1", "v2")}
_EST_TAG = {NOT_OPENED: "nv_est", OPENED: "v_est"}

# The staff template of the estimator's note (followup_worker._send_staff).
ESTIMATOR_TEMPLATE = "staff_followups_done"

# Which step a sent-history row belongs to, by the template it was sent with. `checkin` is the old
# recurring check-in, which only ever ran on an opened proposal. `deposit_nudge` belongs to no step
# (it was sent after approval) and is not counted.
_STEP_OF_TEMPLATE = {
    "not_viewed": NOT_OPENED, "not_viewed_2": NOT_OPENED,
    "next_steps": OPENED, "second_nudge": OPENED, "checkin": OPENED,
}
_UNCOUNTED_TEMPLATES = ("deposit_nudge",)

# The older engines' estimator notes, by step: the ladder's "not opened yet" note and its "time for
# a personal follow-up" note. db._SENT_HISTORY_SQL carries them (audience staff) so the release-day
# tick does not tell an estimator a second time about a step the old engine already reported. They
# never count as customer emails.
_LEGACY_ESTIMATOR_SUFFIX = {NOT_OPENED: ":nv1_staff", OPENED: ":v48_staff"}

# ── follow-up MODE ────────────────────────────────────────────────────────────
# 'auto': the worker emails the customer itself. The remind-only mode of 2026-10-23 (a task for the
# estimator instead of an email) is retired from the cadence; its task rows stay readable.
MODE_AUTO = "auto"
FOLLOWUP_MODES = ("remind", "approve", "auto")

# Scope groups (see group_view below).
OPEN_STATUSES = ("sent", "viewed")
DECISION_STAGE = "decision"
GROUP_TASK_TEMPLATE_DECISION = "scope_decision"

# Every field the schedule reads. The board's query is pinned against this list
# (test_admin_pipeline.py): a field the worker sees and the board does not is how the board comes
# to show a date the worker will not act on.
#
# CONTACT: the customer and Treadwell talking, which skips a step's 2nd email. Each key is computed
# from the DATA by one SQL predicate shared by the worker (db.followup_signals) and the board
# (db.list_all_portal_proposals), so both see the same contact:
#   customer_touch_at      the newest customer-authored row, out-of-office auto-replies and the
#                          "Project delayed" click excluded (a pause is not a reply: the 2nd
#                          goes when it ends)
#   last_staff_contact_at  the newest call / email / text an estimator LOGGED (not a note, not a
#                          bookkeeping row, not the row a reminder task's own send or call wrote)
#   last_staff_message_at  the newest message an estimator wrote to the customer in the thread
# The pair is (key, what the dry run calls it).
CONTACT_KEYS = (("customer_touch_at", "customer_reply"),
                ("last_staff_contact_at", "staff_contact"),
                ("last_staff_message_at", "staff_message"))

INPUT_KEYS = ("proposal_status", "followup_enrolled_at", "followup_disabled_at",
              "followup_paused_until", "cycle_viewed_at", "viewed_at", "last_viewed_at",
              "followup_next_at", "followup_sent_history") + tuple(k for k, _ in CONTACT_KEYS)


def followup_mode(p: dict) -> str:
    """The mode this proposal runs in: always 'auto' (the worker emails the customer)."""
    return MODE_AUTO


@dataclass(frozen=True)
class Due:
    rule_key: str          # dedupe identity: unique per proposal per occurrence
    audience: str          # "customer" | "staff"
    template: str
    include_status_ask: bool = False


@dataclass(frozen=True)
class Plan:
    """Where one proposal stands. `reason` names why nothing is coming ('' while something is)."""
    step: Optional[str] = None             # NOT_OPENED | OPENED | None
    sent: int = 0                          # customer emails already sent in this step
    planned: int = 0                       # customer emails this step has switched on (0-2)
    next_at: Optional[datetime] = None     # the next customer email's date (None = none left)
    next_template: Optional[str] = None
    next_key: Optional[str] = None
    next_ordinal: Optional[int] = None     # 0 = the step's 1st email, 1 = its 2nd
    estimator_at: Optional[datetime] = None
    estimator_key: Optional[str] = None
    last_sent_at: Optional[datetime] = None
    paused: bool = False
    reason: str = ""
    # The step's 2nd email, cancelled by contact after the 1st (None = nothing skipped).
    skipped_at: Optional[datetime] = None  # the contact that skipped it
    skipped_by: str = ""                   # CONTACT_KEYS' name for it ("customer_reply", ...)
    skipped_due_at: Optional[datetime] = None   # when the 2nd would have gone
    skipped_key: Optional[str] = None
    skipped_template: Optional[str] = None


# ── small helpers ─────────────────────────────────────────────────────────────

def cycle_key(enrolled_at: datetime) -> str:
    """Namespace for one send's rule keys, so a re-send can fire the same rules."""
    return "c%d" % int(enrolled_at.timestamp())


def rule_key(enrolled_at: datetime, tag: str) -> str:
    return "%s:%s:%s" % (cycle_key(enrolled_at), KEY_NS, tag)


def _schedule(cfg: Optional[dict]) -> dict:
    return followup_settings.schedule_from(cfg or {})


def in_send_window(now: datetime, cfg: Optional[dict] = None) -> bool:
    """Is it a civil hour to email a customer, in Treadwell's timezone?"""
    s = _schedule(cfg)
    return s["send_start_hour"] <= now.astimezone(BUSINESS_TZ).hour < s["send_end_hour"]


def business_today(now: datetime) -> date:
    return now.astimezone(BUSINESS_TZ).date()


def add_months(d: date, months: int) -> date:
    """Month arithmetic without dateutil. Clamps to the month's length."""
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
    """A date column as a `date`, whatever the driver handed back."""
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


_as_date = as_date


def _ts(v: Any) -> Optional[datetime]:
    """A timestamp from the database OR from JSON, as an aware datetime; None if it isn't one."""
    if isinstance(v, datetime):
        return _aware(v)
    if isinstance(v, str) and v.strip():
        try:
            d = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return None


def _times_list(raw: Any) -> list:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    return list(raw) if isinstance(raw, (list, tuple)) else []


def is_paused(p: dict, now: datetime) -> bool:
    """A pause covers the whole of its final day."""
    until = _as_date(p.get("followup_paused_until"))
    return bool(until and business_today(now) <= until)


def _resume_at(p: dict, cfg: Optional[dict] = None) -> Optional[datetime]:
    """The morning after a pause ends (paused or expired), or None without a pause."""
    until = _as_date(p.get("followup_paused_until"))
    if not until:
        return None
    start = _schedule(cfg)["send_start_hour"]
    return datetime(until.year, until.month, until.day, start, tzinfo=BUSINESS_TZ) \
        + timedelta(days=1)


def in_scope(p: dict) -> bool:
    """Is this proposal still in the follow-up window at all? Sent or viewed, i.e. not decided.
    Approved and closed lost are out; so is everything else."""
    return (p.get("proposal_status") or "") in OPEN_STATUSES


def _cycle_rows(p: dict) -> list:
    """The history rows of the current cycle as [(at, template, audience, rule_key)]. A row with
    no audience is a customer reminder (a sent or called task carries none)."""
    enrolled = _aware(p.get("followup_enrolled_at")) or _ts(p.get("followup_enrolled_at"))
    out = []
    for row in _times_list(p.get("followup_sent_history")):
        if isinstance(row, dict):
            at, tpl = _ts(row.get("at")), (row.get("template") or "")
            aud, key = (row.get("audience") or "customer"), (row.get("rule_key") or "")
        else:                                   # a bare timestamp: template unknown
            at, tpl, aud, key = _ts(row), "", "customer", ""
        if at is None or (enrolled is not None and at < enrolled):
            continue
        out.append((at, tpl, aud, key))
    return out


def sent_history(p: dict) -> list:
    """The proposal's counted customer reminders as [(at, template)], oldest first, current cycle
    only. Accepts the jsonb list the database returns ({at, template, rule_key, audience}) or a
    JSON string. An estimator note in the list (audience staff) is never a customer email."""
    out = [(at, tpl) for at, tpl, aud, _key in _cycle_rows(p)
           if aud != "staff" and tpl not in _UNCOUNTED_TEMPLATES]
    out.sort(key=lambda r: r[0])
    return out


def _engine_key(key: str) -> bool:
    """Was this rule key reserved by THIS engine (the `:a:` namespace)?"""
    return (":%s:" % KEY_NS) in (key or "")


def legacy_estimator_told(p: dict, step: str) -> bool:
    """Did an older engine already send this cycle's estimator note for `step`, with none of the
    step's customer emails sent by this engine since? Then the release-day tick owes no new note:
    the estimator heard about this step from the old ladder, and nothing has happened since. Once
    this engine emails the customer in the step, the note after it is new news and goes."""
    suffix = _LEGACY_ESTIMATOR_SUFFIX.get(step)
    if not suffix:
        return False
    rows = _cycle_rows(p)
    if not any(aud == "staff" and key.endswith(suffix) for _at, _t, aud, key in rows):
        return False
    return not any(aud != "staff" and _engine_key(key) and _step_of(tpl, step) == step
                   for _at, tpl, aud, key in rows)


def opened_at(p: dict) -> Optional[datetime]:
    """When the customer opened THIS send, or None if they have not.

    `cycle_viewed_at` when it is stamped. A row whose status is 'viewed' without it was opened
    before that column existed (schema.sql adds it with no backfill) and IS opened: it is dated by
    the first view of this send we can still see (`viewed_at`, then `last_viewed_at`, whichever is
    not from before the send), else by the send itself. Never "not opened yet": that would email a
    customer who read the proposal that they have not opened it."""
    enrolled = _aware(p.get("followup_enrolled_at")) or _ts(p.get("followup_enrolled_at"))
    viewed = _ts(p.get("cycle_viewed_at"))
    if viewed is not None:
        return viewed
    if (p.get("proposal_status") or "") != "viewed":
        return None
    for k in ("viewed_at", "last_viewed_at"):
        at = _ts(p.get(k))
        if at is not None and (enrolled is None or at >= enrolled):
            return at
    return enrolled


def contact_after(p: dict, since: datetime) -> Optional[tuple]:
    """The newest contact (CONTACT_KEYS) strictly after `since`, as (at, name), or None."""
    best = None
    for key, name in CONTACT_KEYS:
        at = _ts(p.get(key))
        if at is not None and at > since and (best is None or at > best[0]):
            best = (at, name)
    return best


def _step_of(template: str, current: str) -> str:
    """A row's step. A row whose template is unknown counts against the CURRENT step: counting it
    can only ever send fewer emails, never more."""
    return _STEP_OF_TEMPLATE.get(template or "", current)


# ── the schedule ──────────────────────────────────────────────────────────────

def plan(p: dict, now: datetime, cfg: Optional[dict] = None) -> Plan:
    """Where `p` stands in the schedule at `now`. THE single source for due_now and next_due_at,
    so the board's date and the worker's send cannot drift apart."""
    enrolled = _aware(p.get("followup_enrolled_at")) or _ts(p.get("followup_enrolled_at"))
    if enrolled is None:
        return Plan(reason="not_enrolled")
    if p.get("deleted_at"):
        return Plan(reason="deleted")
    if p.get("followup_disabled_at"):
        return Plan(reason="followups_off")
    status = (p.get("proposal_status") or "")
    if status == "approved":
        return Plan(reason="approved")
    if status == "closed_lost":
        return Plan(reason="lost")
    if status not in OPEN_STATUSES:
        return Plan(reason="finished")

    s = _schedule(cfg)
    viewed = opened_at(p)                       # a 'viewed' row with no cycle stamp IS opened
    step = OPENED if viewed is not None else NOT_OPENED
    anchor = viewed if viewed is not None else enrolled
    if anchor < enrolled:                       # a view stamp from before this send: not this cycle
        anchor = enrolled
    pre = "%s_" % step
    on = (bool(s[pre + "first_on"]), bool(s[pre + "second_on"]))
    days = (timedelta(days=int(s[pre + "first_days"])),
            timedelta(days=int(s[pre + "second_days"])))
    planned = [i for i in (0, 1) if on[i]]

    hist = sent_history(p)
    mine = [at for at, tpl in hist if _step_of(tpl, step) == step]
    sent = len(mine)
    last = mine[-1] if mine else None
    last_any = hist[-1][0] if hist else None
    resume = _resume_at(p, cfg)
    paused = is_paused(p, now)

    common = dict(step=step, sent=sent, planned=len(planned), last_sent_at=last, paused=paused)
    skip: dict = {}
    if sent < len(planned):
        o = planned[sent]
        if o == 0:
            due = anchor + days[0]
        else:
            # The 2nd email runs from the 1st's REAL send time. With the 1st switched off it falls
            # where the 1st would have gone, plus the gap.
            due = (last if sent >= 1 and last is not None else anchor + days[0]) + days[1]
        nxt = _ts(p.get("followup_next_at"))
        if nxt is not None and nxt > enrolled and (last_any is None or nxt > last_any):
            due = nxt                               # "Next email on": moves this one email
        if resume is not None and due < resume:
            due = resume                            # never inside a pause; at its end instead
        template = followup_settings.CADENCE_TEMPLATES[step][o]
        key = rule_key(enrolled, _TAGS[step][o])
        # Contact after this step's 1st email skips its 2nd: they are talking to us already.
        # Before the 1st (sent == 0) nothing is skipped, whichever email comes first.
        contact = contact_after(p, last) if (o == 1 and sent >= 1 and last is not None) else None
        if contact is None:
            return Plan(next_at=due, next_template=template, next_key=key, next_ordinal=o,
                        reason="paused" if paused else "", **common)
        skip = dict(skipped_at=contact[0], skipped_by=contact[1], skipped_due_at=due,
                    skipped_key=key, skipped_template=template)

    # The step's emails are all sent, switched off or skipped: only the estimator's note is left.
    done = "skipped_by_reply" if skip else "step_done"
    if not s["estimator_on"]:
        return Plan(reason=done, **common, **skip)
    if legacy_estimator_told(p, step):          # the old ladder already told them; nothing since
        return Plan(reason=done, **common, **skip)
    est = (last if last is not None else anchor) + timedelta(days=int(s["estimator_days"]))
    if resume is not None and est < resume:
        est = resume
    return Plan(estimator_at=est, estimator_key=rule_key(enrolled, _EST_TAG[step]),
                reason="paused" if paused else done, **common, **skip)


def next_due_at(p: dict, now: datetime, cfg: Optional[dict] = None) -> Optional[datetime]:
    """When the next CUSTOMER email is due, or None if none ever will be (approved, lost, off, or
    the step's emails are used up). Ignores the send window on purpose: the window delays an email
    by hours, and rounding the date for it would make the column disagree with itself overnight."""
    return plan(p, now, cfg).next_at


def due_now(p: dict, now: datetime, cfg: Optional[dict] = None) -> list:
    """What is owed at `now`: at most one customer email and one estimator note. Nothing while the
    proposal is on hold. The customer email only inside the send window (the estimator's note goes
    to a work inbox and is not held)."""
    pl = plan(p, now, cfg)
    if pl.paused or pl.step is None:
        return []
    out = []
    if pl.next_at is not None and now >= pl.next_at and in_send_window(now, cfg):
        out.append(Due(pl.next_key, "customer", pl.next_template,
                       include_status_ask=(pl.next_ordinal == 1)))
    if pl.estimator_at is not None and now >= pl.estimator_at:
        out.append(Due(pl.estimator_key, "staff", ESTIMATOR_TEMPLATE))
    return out


# ── follow-up tasks (legacy reminder tasks, and the scope-group decision task) ───

def task_stage(template: str) -> str:
    if template == GROUP_TASK_TEMPLATE_DECISION:
        return DECISION_STAGE
    return "deposit" if template == "deposit_nudge" else "pre"


def task_is_live(detail: Optional[dict], p: Optional[dict], now: datetime) -> tuple:
    """May a pending follow-up task still be acted on? (ok, reason).

    The cadence no longer raises reminder tasks (the worker emails the customer itself), but rows
    raised before this change can still be open, and a scope group's decision task is still
    raised. A reminder task is live only while its proposal is still being followed up, in the
    same send cycle; a deposit task never is (there is no deposit chase)."""
    d = detail or {}
    if d.get("stage") == DECISION_STAGE:
        return decision_task_is_live(d, (p or {}).get("scope_members"))
    if not p:
        return False, "missing"
    if p.get("deleted_at"):
        return False, "deleted"
    if p.get("followup_disabled_at"):
        return False, "automation_off"
    enrolled = _aware(p.get("followup_enrolled_at"))
    if not enrolled:
        return False, "not_enrolled"
    status = (p.get("proposal_status") or "")
    if status == "closed_lost":
        return False, "closed_lost"
    if not in_scope(p):
        return False, "finished"
    if is_paused(p, now):
        return False, "paused"
    if d.get("stage") == "deposit":
        return False, "finished"
    if d.get("cycle") and d.get("cycle") != cycle_key(enrolled):
        return False, "resent"
    return True, ""


# ── scope groups: one project, several separately approved proposals ──────────
# RJ's "one project, many proposals": the customer is followed up about the PROJECT, not about each
# proposal. The group's live OPEN scopes are folded into one synthetic proposal the schedule above
# already understands (`group_view`), filed on a stable HOME scope: one email per recipient per due
# occurrence, at most 2 per step for the whole group. Approving any scope stops the group until
# somebody decides what happens to the others (the decision task).
def is_open(m: dict) -> bool:
    return (m.get("proposal_status") or "") in OPEN_STATUSES


def live_members(rows: list, drafts: Optional[list]) -> list:
    """The LIVE members of one scope group, from its portal rows and its drafts. Pure.

    `drafts` is `[{"id", "archived"}]` for every non-deleted draft that carries this group (the
    drafts decide membership, as in main._active_scope_group). A row whose draft is not among them
    has been taken out of the group in the tool and is an ordinary proposal again; a row whose
    draft is archived is in the group but not live. With no draft at all there is nothing to
    compare against and the stored rows stand. Deleted, closed-lost and automation-off rows are
    never live."""
    by_id = {d.get("id"): d for d in (drafts or []) if isinstance(d, dict)}
    live_ids = set(scope_groups.split_members(drafts)[0]) if by_id else set()
    out = []
    for r in rows or []:
        if not r or r.get("deleted_at") or r.get("followup_disabled_at"):
            continue
        if (r.get("proposal_status") or "") == "closed_lost":
            continue
        if by_id:
            if r.get("proposal_id") not in live_ids:
                continue
        out.append(r)
    return out


def detached_ids(rows: list, drafts: Optional[list]) -> set:
    """Rows that still carry the group on the portal but whose draft has left it: ordinary
    proposals again, followed up on their own. Archived drafts are NOT detached."""
    ids = {d.get("id") for d in (drafts or []) if isinstance(d, dict)}
    if not ids:
        return set()
    return {r.get("proposal_id") for r in rows or [] if r and r.get("proposal_id") not in ids}


def _sort_ts(v: Any) -> datetime:
    return _ts(v) or datetime.max.replace(tzinfo=timezone.utc)


def group_home(members: list) -> Optional[dict]:
    """The STABLE home of a group's follow-ups: the earliest (created_at, proposal_id) OPEN live
    member, or the earliest live member when none is open. The group's sends are reserved and
    recorded on it."""
    live = [m for m in members or [] if m and m.get("proposal_id")]
    if not live:
        return None
    pool = [m for m in live if is_open(m)] or live
    return min(pool, key=lambda m: (_sort_ts(m.get("created_at")), str(m.get("proposal_id"))))


GROUP_SCHEDULE_KEYS = ("followup_interval_days", "followup_stop_after", "followup_next_at")


def group_schedule(members: list, home: Optional[dict] = None) -> dict:
    """The group's own schedule fields: the HOME row's, and only the home's. Pure. Of these only
    `followup_next_at` ("Next email on") still moves anything; the other two are kept readable."""
    live = [m for m in members or [] if m and m.get("proposal_id")]
    home = home if home is not None else group_home(live)
    return {k: (home or {}).get(k) for k in GROUP_SCHEDULE_KEYS}


CARRIED_SCHEDULE_KEYS = ("followup_interval_days", "followup_stop_after")


def schedule_carry(former: Optional[dict], home: Optional[dict]) -> dict:
    """What 'keep' copies onto the group's NEW home: the former home's schedule fields the new home
    does not have. Pure. Only when `former` really was the home (it sorts before the new one)."""
    if not former or not home or former.get("proposal_id") == home.get("proposal_id"):
        return {}
    key = lambda m: (_sort_ts(m.get("created_at")), str(m.get("proposal_id")))  # noqa: E731
    if key(former) >= key(home):
        return {}
    return {k: former.get(k) for k in CARRIED_SCHEDULE_KEYS
            if former.get(k) is not None and home.get(k) is None}


def _min_ts(vals) -> Optional[datetime]:
    got = [t for t in (_ts(v) for v in vals) if t is not None]
    return min(got) if got else None


def _max_ts(vals) -> Optional[datetime]:
    got = [t for t in (_ts(v) for v in vals) if t is not None]
    return max(got) if got else None


def group_chase_stopped(members: list) -> bool:
    """Has a live member been approved since the last "keep following up the others" decision?

    Approving ANY scope stops the siblings' follow-ups until somebody decides what to do with them.
    Derived from data: an approved live member stops the group, and a `scope_group_resume` note
    (the decision task's 'keep' outcome, read into every member as `scope_group_resumed_at`)
    written after the latest approval starts it again."""
    approved = [m for m in members or [] if (m.get("proposal_status") or "") == "approved"]
    if not approved:
        return False
    latest = _max_ts(m.get("approved_at") for m in approved)
    resumed = _max_ts(m.get("scope_group_resumed_at") for m in members)
    if resumed is None:
        return True
    return latest is not None and resumed < latest


def history_rows(rows: list, drafts: Optional[list], live_ids) -> list:
    """The group rows that are NOT live members but still hold the group's follow-up history:
    closed, automation-off, archived and deleted scopes, and a former home taken out of the group
    (`group_tagged`). A scope taken out that never held the group's sends is left out. Pure."""
    detached = detached_ids(rows, drafts)
    out = []
    for r in rows or []:
        if not r or r.get("proposal_id") in live_ids:
            continue
        if r.get("proposal_id") in detached and not r.get("group_tagged"):
            continue
        out.append(r)
    return out


def _history_items(m: dict) -> list:
    return [r for r in _times_list(m.get("followup_sent_history"))]


def _item_at(item: Any) -> Optional[datetime]:
    return _ts(item.get("at")) if isinstance(item, dict) else _ts(item)


def group_state(members: list, now: datetime, history: Optional[list] = None) -> tuple:
    """(view, reason). See group_view; `reason` names why there is no view ('' when there is).

    `history`: rows of the group that are not live (history_rows), carrying their sent history. A
    group send is recorded on the HOME row, so when the home is closed or deleted the group must
    not forget the emails it already sent, or it would get a fresh 2 + 2."""
    live = [m for m in members or [] if m and not m.get("deleted_at")
            and not m.get("followup_disabled_at")
            and (m.get("proposal_status") or "") != "closed_lost"]
    if not live:
        return None, "no_members"
    if group_chase_stopped(live):
        return None, "scope_approved"
    if not any(is_open(m) for m in live):
        return None, "no_open_scope"
    # Only scopes the customer has been TOLD about: the combined send's enrolment is the
    # announcement. A scope published since is not in the follow-up.
    open_ = [m for m in live if is_open(m) and m.get("followup_enrolled_at")]
    if not open_:
        return None, "not_enrolled"
    enrolled = _min_ts(m.get("followup_enrolled_at") for m in open_)
    home = group_home(live)
    # Opened = a cycle stamp OR a 'viewed' status (a view from before cycle_viewed_at existed).
    opened = [t for t in (opened_at(m) for m in open_) if t is not None]
    view = dict(home)
    view.update({
        "proposal_status": "viewed" if opened else "sent",
        "followup_enrolled_at": enrolled,
        "cycle_viewed_at": min(opened) if opened else None,
        "followup_disabled_at": None,
        "deleted_at": None,
        "approved_at": None,
    })
    # The group is on hold only while EVERY open scope is; it resumes when the first of them does.
    pauses = [as_date(m.get("followup_paused_until")) for m in open_]
    view["followup_paused_until"] = (min(pauses) if pauses and all(pauses) else None)
    live_ids = {m.get("proposal_id") for m in live}
    # The cap is per GROUP: every open member's sent history and every history row's. An approved
    # member keeps only what was sent BEFORE its approval (it may have been the home then).
    folded = ([m for m in live if is_open(m)]
              + [h for h in history or [] if h and h.get("proposal_id") not in live_ids])
    items = []
    for m in folded:
        items.extend(_history_items(m))
    for m in live:
        if is_open(m):
            continue
        cut = _ts(m.get("approved_at"))
        items.extend(i for i in _history_items(m)
                     if cut is not None and _item_at(i) is not None and _item_at(i) <= cut)
    view["followup_sent_history"] = items
    # Contact on ANY scope's thread is contact about the project: the newest of each kind across
    # every live member and every history row (a reply into a closed former home's thread counts).
    for key, _name in CONTACT_KEYS:
        view[key] = _max_ts(m.get(key) for m in live + [h for h in history or [] if h])
    view.update(group_schedule(live, home))
    view["scope_group"] = home.get("scope_group")
    # A scope the customer paused is left out of the email while the others are followed up. Only
    # when every open scope is paused (the group itself is on hold) are they all kept, for the board.
    listed = [m for m in open_ if not is_paused(m, now)] or open_
    view["scopes"] = [{"proposal_id": m.get("proposal_id"),
                       "label": m.get("scope_label") or m.get("project_name") or "Proposal",
                       "status": m.get("proposal_status")} for m in listed]
    view["home_proposal_id"] = home.get("proposal_id")
    # Where a staff link should land: an OPEN scope, never a possibly-approved home.
    view["link_proposal_id"] = (home.get("proposal_id") if any(m is home for m in listed)
                                else listed[0].get("proposal_id"))
    view["_open_members"] = listed
    return view, ""


def group_view(members: list, now: datetime, history: Optional[list] = None) -> Optional[dict]:
    """One synthetic proposal for a scope group's follow-ups, or None when there is none.

    Input: the LIVE members of one group (see live_members), each carrying its sent history and
    `scope_group_resumed_at`. Output: a dict `plan`, `due_now` and `next_due_at` already
    understand, filed under the HOME scope's proposal_id. None when any live member is approved
    (and nobody has said "keep following up the others" since), or when no scope is open."""
    return group_state(members, now, history)[0]


def decision_link(listed_ids: list, status_of, fallback: Optional[str]) -> Optional[str]:
    """Where a decision task's link opens: the first sibling it lists that is STILL open by
    `status_of(proposal_id)`, else `fallback` (the approved scope)."""
    for pid in listed_ids or []:
        if pid and (status_of(pid) or "") in OPEN_STATUSES:
            return pid
    return fallback


def decision_task_is_live(detail: Optional[dict], members: Optional[list]) -> tuple:
    """A decision task is live while at least one sibling it lists is still open. (ok, reason)."""
    if members is None:
        return False, "missing"
    listed = {s.get("proposal_id") for s in (detail or {}).get("scopes") or []
              if isinstance(s, dict)}
    open_ids = {m.get("proposal_id") for m in members
                if m and is_open(m) and not m.get("deleted_at")}
    if listed & open_ids:
        return True, ""
    return False, "decision_moot"
