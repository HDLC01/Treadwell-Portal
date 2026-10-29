"""The follow-up schedule and its email wording, as settings rather than constants.

WHAT A FOLLOW-UP IS (Hanz, 2026-10-28): "the follow ups should be a follow up email to the
customer okay?" An automatic email to the customer, sent by the portal's worker, from the moment a
proposal is sent until it is approved. At most TWO per step:

    NOT OPENED YET        1st email N days after sending,         2nd email N days after the 1st
    OPENED, NO DECISION   1st email N days after sending,         2nd email N days after the 1st
                          (once they have opened it; never sooner than N days after the last
                          follow-up email the customer got — followup_rules.plan)
    ESTIMATOR             "Tell the estimator" N days after the last email of a step

Each of the five has its own ON/OFF switch and its own number of days. Nothing repeats: there is
no "then every", no "stop after N" and no recurring check-in. That is what replaced the old shape
(first_nudge_hours / second_nudge_hours / recurring_hours / max_recurring / staff_personal_hours /
deposit_max_recurring), which let an admin type 0 into "Then every" and have it silently become
"every 3 days, up to 20 times".

THE SAFETY POSTURE, because these settings send email to customers.

  * **A number out of range is REFUSED, with a sentence, never replaced.** The old module clamped
    and defaulted: 0 days became the shipped default and 200 became 90, and the page showed a
    value nobody typed. A refusal names the box and the range ("2nd email must be 1 to 90 days
    after the 1st"), and the page shows exactly what was saved. 0 is never "off" — every email has
    a switch for that.
  * **Old saved rows still read.** `merge` (the read path) maps an old-shape row onto the new one
    once, without error: both 1st emails take first_nudge_hours, both 2nd emails take
    second_nudge_hours, the estimator takes staff_personal_hours, every switch ON. The recurring
    and deposit keys are ignored. Reading never refuses — a hand-edited bad value falls back to
    the default with a warning, because a bad row must not silence every customer email.
  * **Templates are TEXT, not HTML**, rendered into the existing branded shell, and each one must
    keep its `{link}` (the button). Unchanged from before.

TOKENS. `{first_name}`, `{project}`, `{need}` and `{link}`. `{need}` is the deposit-conditional
phrase ("your signed approval and the deposit" / "your signed approval").
"""
from __future__ import annotations

import logging
import math
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

TABLE = "portal_settings"
ROW_ID = "followups"          # single row; this is one global schedule by design

# ── when automatic follow-ups were switched on (Hanz, 2026-10-29: "we push these auto follow ups
# changes and then we send the email at the time it was activated") ──────────────────────────────
# Its OWN row, not a key in ROW_ID: the editor's PUT replaces the whole ROW_ID value with what it
# validated, so a key kept there would be wiped by the next Save. Written once, by the first tick
# that runs with FOLLOWUP_AUTOMATION_ENABLED=true (or by ops/followup_activate.py); followup_rules
# starts every step's clock no earlier than it, so a proposal sent weeks before the release gets its
# 1st email N days after activation, not on the first tick.
ACTIVATION_ROW_ID = "followups_activation"
ACTIVATED_KEY = "activated_at"

# ── the schedule ──────────────────────────────────────────────────────────────
# The two customer steps, in the order the page shows them. Each step has a 1st and a 2nd email.
STEPS = ("not_opened", "opened")
ORDINALS = ("first", "second")

# Every schedule field, its default and its kind. The defaults are the approved mock (and what the
# live prod row maps to: 72h -> 3 days, 168h -> 7 days, 24h -> 1 day).
DEFAULTS: Dict[str, Any] = {
    "not_opened_first_on": True,
    "not_opened_first_days": 3,
    "not_opened_second_on": True,
    "not_opened_second_days": 7,
    "opened_first_on": True,
    "opened_first_days": 3,
    "opened_second_on": True,
    "opened_second_days": 7,
    "estimator_on": True,
    "estimator_days": 1,
    "send_start_hour": 8,         # customer emails go out in business hours, America/Chicago
    "send_end_hour": 18,
}
SWITCHES = tuple(k for k in DEFAULTS if k.endswith("_on"))
DAY_FIELDS = tuple(k for k in DEFAULTS if k.endswith("_days"))
HOUR_FIELDS = ("send_start_hour", "send_end_hour")
DAYS_MIN, DAYS_MAX = 1, 90

# The sentence each refusal says, in the page's own words. Quoted by the PUT's 400, so the page can
# show it next to the box.
_DAYS_SENTENCE = {
    "not_opened_first_days": "Not opened yet: the 1st email must be 1 to 90 days after sending.",
    "not_opened_second_days": "Not opened yet: the 2nd email must be 1 to 90 days after the 1st.",
    "opened_first_days": "Opened, no decision: the 1st email must be 1 to 90 days after sending.",
    "opened_second_days": "Opened, no decision: the 2nd email must be 1 to 90 days after the 1st.",
    "estimator_days": "Tell the estimator must be 1 to 90 days after the last email.",
}
_SWITCH_NAME = {
    "not_opened_first_on": "Not opened yet: the 1st email",
    "not_opened_second_on": "Not opened yet: the 2nd email",
    "opened_first_on": "Opened, no decision: the 1st email",
    "opened_second_on": "Opened, no decision: the 2nd email",
    "estimator_on": "Tell the estimator",
}

# The OLD shape's keys. Read on the way in (migration), never written back out.
LEGACY_KEYS = ("first_nudge_hours", "second_nudge_hours", "recurring_hours",
               "staff_personal_hours", "max_recurring", "deposit_max_recurring")

# ── the emails ────────────────────────────────────────────────────────────────
# One template per customer email. Three of them are the templates customers already received,
# mapped onto the new steps; the 2nd not-opened email is new (the old ladder repeated not_viewed):
#
#     not opened, 1st   not_viewed      (was "Not opened yet")
#     not opened, 2nd   not_viewed_2    (new)
#     opened, 1st       next_steps      (was "After they open it")
#     opened, 2nd       second_nudge    (was "Second reminder")
#
# The recurring check-in and the deposit reminder are no longer part of the cadence.
CADENCE_TEMPLATES: Dict[str, tuple] = {
    "not_opened": ("not_viewed", "not_viewed_2"),
    "opened": ("next_steps", "second_nudge"),
}
TEMPLATE_KEYS = ("not_viewed", "not_viewed_2", "next_steps", "second_nudge")

# The FIRST proposal email, editable on the same page. Not in TEMPLATE_KEYS, which is what the
# cadence sends with: the cadence must never send the here-is-your-proposal email as a reminder.
SENT_KEY = "sent"
ALL_TEMPLATE_KEYS = (SENT_KEY,) + TEMPLATE_KEYS

# Still readable, not editable and never sent by the cadence: old follow-up TASKS in the database
# carry these template names, and their compose box still opens with this wording.
LEGACY_TEMPLATE_KEYS = ("checkin", "deposit_nudge")
TOKENS = ("{first_name}", "{project}", "{need}", "{link}")

# What each email is CALLED on screen, in tab order. Quoted verbatim in validation refusals ("The
# “Opened, 2nd email” email needs {link}"), so short.
LABELS: Dict[str, str] = {
    "sent": "Proposal sent",
    "not_viewed": "Not opened, 1st",
    "not_viewed_2": "Not opened, 2nd",
    "next_steps": "Opened, 1st",
    "second_nudge": "Opened, 2nd",
}
_LEGACY_LABELS: Dict[str, str] = {
    "checkin": "Recurring check-in",
    "deposit_nudge": "Deposit reminder",
}

# The heading under the tabs once one is selected: when the email goes.
EDITOR_TITLES: Dict[str, str] = {
    "sent": "Proposal sent — the first email, when you publish it",
    "not_viewed": "Not opened yet — 1st email, days after sending",
    "not_viewed_2": "Not opened yet — 2nd email, days after the 1st",
    "next_steps": "Opened, no decision — 1st email, days after sending",
    "second_nudge": "Opened, no decision — 2nd email, days after the 1st",
}


def label(key: str) -> str:
    """The on-screen name of one email, for use in a message somebody has to act on."""
    return LABELS.get(key) or _LEGACY_LABELS.get(key) or str(key).replace("_", " ")


_MAX_SUBJECT = 200

# ONE subject for every customer email about a project (Hanz, 2026-08-11: "for all updates to one
# project can we have it in one email thread?"). Only {project} may appear in it.
DEFAULT_THREAD_SUBJECT = "Your Treadwell proposal — {project}"
THREAD_SUBJECT_TOKENS = ("{project}",)
_MAX_TITLE = 120
_MAX_BODY = 4000

# The wording as shipped. The first four are what customers have been receiving.
DEFAULT_TEMPLATES: Dict[str, Dict[str, str]] = {
    "sent": {
        "title": "Your proposal is ready",
        "body": ("Hi {first_name},\n\n"
                 "Your proposal for {project} is ready to review.\n\n"
                 "{link}\n\n"
                 "You can view it, ask questions, and approve it right on the page."),
        "cta": "View your proposal",
    },
    "not_viewed": {
        "title": "Your proposal is waiting",
        "body": ("Hi {first_name},\n\n"
                 "We sent over the proposal for {project} and wanted to make sure it reached "
                 "you.\n\n"
                 "{link}\n\n"
                 "Any questions at all, just reply to this email — it comes straight to us."),
        "cta": "View your proposal",
    },
    "not_viewed_2": {
        "title": "Checking your proposal reached you",
        "body": ("Hi {first_name},\n\n"
                 "We haven't seen the proposal for {project} opened yet, so we wanted to check it "
                 "didn't get lost in your inbox.\n\n"
                 "{link}\n\n"
                 "If somebody else should be looking at it, just reply and tell us who."),
        "cta": "View your proposal",
    },
    "next_steps": {
        "title": "Getting you on the schedule",
        "body": ("Hi {first_name},\n\n"
                 "Thanks for taking a look at the proposal for {project}.\n\n"
                 "Whenever you're ready, we need {need} before we can book your dates.\n\n"
                 "{link}\n\n"
                 "If anything needs changing first, reply and tell us — we'd rather adjust it "
                 "than have it sit."),
        "cta": "Review and approve",
    },
    "second_nudge": {
        "title": "Still holding your spot",
        "body": ("Hi {first_name},\n\n"
                 "Just a nudge that the proposal for {project} is still pending. We need {need} "
                 "to schedule the work.\n\n"
                 "{link}\n\n"
                 "Happy to walk through it or price an alternative — a reply is enough."),
        "cta": "Review and approve",
    },
    # Legacy (old follow-up tasks only).
    "checkin": {
        "title": "Checking in",
        "body": ("Hi {first_name},\n\n"
                 "Circling back on {project}. It's still open on our side and we need {need} "
                 "whenever the timing works.\n\n"
                 "{link}"),
        "cta": "View your proposal",
    },
    "deposit_nudge": {
        "title": "Reserving your dates",
        "body": ("Hi {first_name},\n\n"
                 "Thanks for approving {project} — we're glad to be doing it.\n\n"
                 "The deposit is what reserves your place on the schedule, so the sooner it's in, "
                 "the tighter we can hold the dates you wanted.\n\n"
                 "{link}\n\n"
                 "If it's already on its way, tell us there and we'll stop the reminders."),
        "cta": "Send your deposit",
    },
}
_STORED_TEMPLATE_KEYS = ALL_TEMPLATE_KEYS + LEGACY_TEMPLATE_KEYS


class ValidationError(ValueError):
    """A caller-fixable problem. The message reaches the user, so it says what to do."""


# ── reading one value ─────────────────────────────────────────────────────────

def _whole(raw: Any) -> Optional[int]:
    """A whole number from an int or a numeric string, or None. `True` is not a number here."""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw) if raw.is_integer() else None
    s = str(raw).strip()
    if re.fullmatch(r"[+-]?\d+", s):
        return int(s)
    if re.fullmatch(r"[+-]?\d+\.0*", s):
        return int(float(s))
    return None


def _switch(raw: Any) -> Optional[bool]:
    if isinstance(raw, bool):
        return raw
    s = str(raw).strip().lower() if raw is not None else ""
    if s in ("true", "on", "yes", "1"):
        return True
    if s in ("false", "off", "no", "0"):
        return False
    return None


def _days_ok(n: Optional[int]) -> bool:
    return n is not None and DAYS_MIN <= n <= DAYS_MAX


def _hours_from_legacy(raw: Any) -> Optional[int]:
    """An old `*_hours` value as whole days (rounded UP, so 25h is 2 days, never 1), or None."""
    if isinstance(raw, bool):
        return None
    try:
        h = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(h) or h <= 0:
        return None
    return max(DAYS_MIN, min(DAYS_MAX, int(math.ceil(h / 24.0))))


def _migrate_legacy(stored: dict) -> Dict[str, Any]:
    """The schedule an OLD-shape row means, as new-shape fields. Only the keys it can say anything
    about; every switch ON. recurring_hours, max_recurring and deposit_* mean nothing now."""
    out: Dict[str, Any] = {}
    first = _hours_from_legacy(stored.get("first_nudge_hours"))
    second = _hours_from_legacy(stored.get("second_nudge_hours"))
    staff = _hours_from_legacy(stored.get("staff_personal_hours"))
    if first is not None:
        out["not_opened_first_days"] = first
        out["opened_first_days"] = first
    if second is not None:
        out["not_opened_second_days"] = second
        out["opened_second_days"] = second
    if staff is not None:
        out["estimator_days"] = staff
    for sw in SWITCHES:
        out[sw] = True
    return out


def schedule_from(stored: Any) -> Dict[str, Any]:
    """The schedule a stored row (or an already-merged settings dict) means. NEVER raises.

    New-shape keys win; an old-shape row is migrated (`_migrate_legacy`); anything missing or
    unreadable is the default. Used by `merge` and by followup_rules, so a raw row and a merged one
    always mean the same schedule."""
    out = {k: DEFAULTS[k] for k in DEFAULTS}
    if not isinstance(stored, dict):
        return out
    has_new = any(k in stored for k in SWITCHES + DAY_FIELDS)
    if not has_new and any(k in stored for k in LEGACY_KEYS):
        out.update(_migrate_legacy(stored))
    for k in SWITCHES:
        if k in stored:
            v = _switch(stored.get(k))
            if v is None:
                log.warning("[followups] stored %s is unreadable; using the default", k)
            else:
                out[k] = v
    for k in DAY_FIELDS:
        if k in stored:
            n = _whole(stored.get(k))
            if _days_ok(n):
                out[k] = n
            else:
                log.warning("[followups] stored %s=%r is out of range; using the default",
                            k, stored.get(k))
    start, end = _whole(stored.get("send_start_hour")), _whole(stored.get("send_end_hour"))
    if start is not None and end is not None and 0 <= start < end <= 24:
        out["send_start_hour"], out["send_end_hour"] = start, end
    return out


# ── text ──────────────────────────────────────────────────────────────────────

def _clean_text(raw: Any, limit: int) -> str:
    """Plain text, collapsed. Any HTML tag is stripped rather than escaped."""
    s = str(raw or "")
    s = re.sub(r"<[^>]*>", "", s)                     # no HTML into a customer email
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    # Control characters out, keeping newlines and tabs: jsonb rejects a NUL, and email_sender
    # marks the {link} position with a control character, which is only safe because a stored
    # body can never contain one.
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)                  # cap the blank runs, keep paragraphs
    return s.strip()[:limit]


def validate_thread_subject(raw: Any) -> str:
    """The one subject every project email carries, cleaned. Empty -> the shipped wording."""
    subject = _clean_text(raw, _MAX_SUBJECT)
    if not subject:
        return DEFAULT_THREAD_SUBJECT
    unknown = set(re.findall(r"\{[a-z_]+\}", subject)) - set(THREAD_SUBJECT_TOKENS)
    if unknown:
        raise ValidationError(
            "The email subject cannot use %s — it is the same for every update about a project, "
            "so only %s makes sense there."
            % (", ".join(sorted(unknown)), ", ".join(THREAD_SUBJECT_TOKENS)))
    return subject


def validate_template(key: str, raw: Any) -> Dict[str, str]:
    """One template, cleaned. Raises for a missing {link}, an unknown token or a body too long."""
    if key not in _STORED_TEMPLATE_KEYS:
        raise ValidationError("There is no follow-up email called %r." % key)
    src = raw if isinstance(raw, dict) else {}
    base = DEFAULT_TEMPLATES[key]

    title = _clean_text(src.get("title"), _MAX_TITLE) or base["title"]
    cta = _clean_text(src.get("cta"), 60) or base["cta"]

    # Refused when too long rather than truncated: cutting it would usually cut the {link} too.
    raw_body = _clean_text(src.get("body"), _MAX_BODY * 4)
    if len(raw_body) > _MAX_BODY:
        raise ValidationError(
            "The “%s” email is %d characters — the limit is %d. Trim it rather than "
            "letting it be cut off mid-sentence." % (label(key), len(raw_body), _MAX_BODY))
    body = raw_body or base["body"]

    if "{link}" not in body:
        raise ValidationError(
            "The “%s” email needs {link} somewhere in the body — that is the button the "
            "customer clicks to see the proposal." % label(key))

    unknown = set(re.findall(r"\{[a-z_]+\}", body)) - set(TOKENS)
    if unknown:
        raise ValidationError(
            "Unknown placeholder %s. The ones available are %s."
            % (", ".join(sorted(unknown)), ", ".join(TOKENS)))
    return {"title": title, "body": body, "cta": cta}


# ── the whole row ─────────────────────────────────────────────────────────────

def validate(raw: Any, base: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """A whole settings payload from the page, checked. Raises ValidationError with a sentence.

    `base` is what is saved now (merged): a field the payload leaves out keeps its saved value, so a
    partial save never resets the rest. Out-of-range days, blank days and a switch that is neither
    on nor off are REFUSED — never clamped, never defaulted."""
    if not isinstance(raw, dict):
        raise ValidationError("Nothing to save.")
    current = merge(base) if base is not None else defaults()

    # An old page still posting the old shape would otherwise have its numbers ignored in silence.
    if (any(k in raw for k in LEGACY_KEYS)
            and not any(k in raw for k in SWITCHES + DAY_FIELDS)):
        raise ValidationError("This page is out of date — reload it to see the new follow-up "
                              "settings, then save again.")

    out: Dict[str, Any] = {}
    for k in SWITCHES:
        if k not in raw:
            out[k] = bool(current[k])
            continue
        v = _switch(raw.get(k))
        if v is None:
            raise ValidationError("%s must be switched on or off." % _SWITCH_NAME[k])
        out[k] = v
    for k in DAY_FIELDS:
        if k not in raw:
            out[k] = int(current[k])
            continue
        n = _whole(raw.get(k))
        if not _days_ok(n):
            raise ValidationError(_DAYS_SENTENCE[k])
        out[k] = n

    start = _whole(raw.get("send_start_hour")) if "send_start_hour" in raw \
        else int(current["send_start_hour"])
    end = _whole(raw.get("send_end_hour")) if "send_end_hour" in raw \
        else int(current["send_end_hour"])
    if start is None or end is None or not (0 <= start < end <= 24):
        raise ValidationError("The sending hours must start between 0 and 23, end between 1 and "
                              "24, and end after they start.")
    out["send_start_hour"], out["send_end_hour"] = start, end

    out["thread_subject"] = (validate_thread_subject(raw.get("thread_subject"))
                             if "thread_subject" in raw else current["thread_subject"])

    templates = raw.get("templates")
    src = templates if isinstance(templates, dict) else {}
    out["templates"] = {}
    for k in _STORED_TEMPLATE_KEYS:
        if k in src:
            out["templates"][k] = validate_template(k, src.get(k))
        else:
            out["templates"][k] = dict(current["templates"][k])
    return out


def defaults() -> Dict[str, Any]:
    """The shipped schedule, as a settings dict."""
    out = dict(DEFAULTS)
    out["thread_subject"] = DEFAULT_THREAD_SUBJECT
    out["templates"] = {k: dict(v) for k, v in DEFAULT_TEMPLATES.items()}
    return out


def merge(stored: Any) -> Dict[str, Any]:
    """Stored settings over the defaults, so a partial, corrupt or OLD-shape row still yields a
    full, usable schedule. Never raises. Only the new shape comes out."""
    out = defaults()
    if not isinstance(stored, dict):
        return out
    out.update(schedule_from(stored))
    if "thread_subject" in stored:
        try:
            out["thread_subject"] = validate_thread_subject(stored.get("thread_subject"))
        except ValidationError:
            log.warning("[followups] stored thread_subject is invalid; using the default")
    t = stored.get("templates")
    if isinstance(t, dict):
        for key in _STORED_TEMPLATE_KEYS:
            if not isinstance(t.get(key), dict):
                continue
            try:
                out["templates"][key] = validate_template(key, t[key])
            except ValidationError:
                log.warning("[followups] stored %s template is invalid; using the default", key)
    return out


def activated_at_from(stored: Any):
    """The activation moment a stored ACTIVATION_ROW_ID value holds, as an aware UTC datetime, or
    None when there is none (never activated, or unreadable). Never raises."""
    raw = stored.get(ACTIVATED_KEY) if isinstance(stored, dict) else None
    if isinstance(raw, datetime):
        at = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            at = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError:
            log.warning("[followups] stored %s=%r is unreadable", ACTIVATED_KEY, raw)
            return None
    else:
        return None
    return (at if at.tzinfo else at.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def activation_value(at, by: Optional[str] = None) -> Dict[str, Any]:
    """The ACTIVATION_ROW_ID value for `at` (an aware datetime): ISO UTC, plus who set it."""
    out: Dict[str, Any] = {ACTIVATED_KEY: at.astimezone(timezone.utc).isoformat()}
    if by:
        out["by"] = str(by)[:120]
    return out


def with_activation(cfg: Optional[Dict[str, Any]], at) -> Dict[str, Any]:
    """A copy of `cfg` carrying the activation moment followup_rules.plan reads. `at` None leaves
    it out (the schedule then runs from each step's own start, as before activation existed)."""
    out = dict(cfg or {})
    if at is None:
        out.pop(ACTIVATED_KEY, None)
    else:
        out[ACTIVATED_KEY] = at
    return out


def render(template: Dict[str, str], *, first_name: str, project: str, need: str,
           link_html: str) -> Dict[str, str]:
    """Fill a template's tokens. Returns {title, body} ready for the email shell."""
    def sub(s: str, link: str) -> str:
        return (s.replace("{first_name}", first_name or "there")
                 .replace("{project}", project or "your project")
                 .replace("{need}", need or "your signed approval")
                 .replace("{link}", link))

    return {
        "title": sub(template.get("title", ""), ""),
        "body": sub(template.get("body", ""), link_html),
    }


def preview(settings: Dict[str, Any], key: str) -> Dict[str, str]:
    """A template rendered with sample values, for the editor."""
    tpl = (settings.get("templates") or {}).get(key) or DEFAULT_TEMPLATES.get(key) or {}
    return render(tpl, first_name="Dave", project="Westport Retail Center",
                  need="your signed approval and the deposit",
                  link_html="[ %s ]" % (tpl.get("cta") or "View your proposal"))
