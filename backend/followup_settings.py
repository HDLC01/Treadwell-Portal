"""The follow-up cadence and its email wording, as settings rather than constants.

WHY THIS EXISTS. The cadence Hanz specified — 24h if not viewed, 24h after viewed, +48h, every
3 days thereafter — lived as `timedelta` constants in `followup_rules.py`. Changing "every 3 days"
to "every 5" meant a code edit and a deploy, and the wording of four customer-facing emails was
equally locked in f-strings. He asked for both to be editable, by any signed-in user, as ONE
global cadence.

THE SAFETY POSTURE, because these settings send email to customers.

A wrong value here is visible outside the company, which is a different class of mistake from a
mislabelled project. So:

  * **Every value is clamped, not merely validated.** An interval of "1 hour" would chase somebody
    24 times a day; the floor is 4 hours and the ceiling 90 days. Out-of-range input is pulled to
    the nearest legal value rather than rejected, because a form that refuses a number without
    explaining is how people give up and ask an engineer.
  * **Defaults are the current constants**, and an empty or unreadable settings row falls back to
    them silently. The cadence must keep working if this table is missing — which it will be on
    any environment where the DDL has not been applied yet.
  * **Templates are TEXT, not HTML.** The paragraphs get rendered into the existing branded shell.
    Letting somebody paste HTML into a customer email invites a broken layout in Outlook that
    nobody sees until a customer does, and there is no upside: the shell already carries the
    letterhead, the button and the footer.
  * **A template must keep its link.** An email whose only job is "come and look at your proposal"
    is worthless without the button, so `{link}` is required and a template missing it is refused
    rather than clamped — this is the one case where silently fixing it would be worse than
    saying no.

TOKENS. `{first_name}`, `{project}`, `{need}` and `{link}`. `{need}` is the deposit-conditional
phrase ("your signed approval and the deposit" / "your signed approval") — it exists because
promising a deposit on a job sent without one would be wrong, and GC work usually is.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

TABLE = "portal_settings"
ROW_ID = "followups"          # single row; this is one global cadence by design

# ── the shipped cadence, and the bounds around it ──────────────────────────────
# Defaults ARE the constants followup_rules.py used, so an environment with no settings row
# behaves exactly as it did before this module existed.
DEFAULTS: Dict[str, Any] = {
    "first_nudge_hours": 24,      # after send, and after first view
    "second_nudge_hours": 72,     # 48h after the first viewed reminder
    "recurring_hours": 72,        # "every 3 days thereafter"
    "staff_personal_hours": 48,   # viewed but still pending -> tell the estimator
    "max_recurring": 20,          # ~two months past the last real signal
    "send_start_hour": 8,         # business hours, America/Chicago
    "send_end_hour": 18,
}

# (floor, ceiling) per numeric field. The floors are the interesting half: 4 hours is about as
# often as a person can be chased without it reading as harassment, and one recurring send is the
# minimum that still means "recurring".
BOUNDS: Dict[str, tuple] = {
    "first_nudge_hours": (4, 24 * 90),
    "second_nudge_hours": (4, 24 * 90),
    "recurring_hours": (4, 24 * 90),
    "staff_personal_hours": (1, 24 * 90),     # staff mail is unclamped by hours, so 1h is fine
    "max_recurring": (1, 60),
    "send_start_hour": (0, 23),
    "send_end_hour": (1, 24),
}

TEMPLATE_KEYS = ("not_viewed", "next_steps", "second_nudge", "checkin")
TOKENS = ("{first_name}", "{project}", "{need}", "{link}")

# What each email is CALLED on screen. The keys are database identifiers, and a refusal that says
# "the not viewed email needs {link}" makes somebody hunt for a tab with that name — there isn't
# one. Naming lives here rather than in the page so the message and the tab cannot disagree; the
# editor reads these off the GET response.
LABELS: Dict[str, str] = {
    "not_viewed": "Not opened yet",
    "next_steps": "After they open it",
    "second_nudge": "Second reminder",
    "checkin": "Recurring check-in",
}


def label(key: str) -> str:
    """The on-screen name of one email, for use in a message somebody has to act on."""
    return LABELS.get(key, str(key).replace("_", " "))

_MAX_SUBJECT = 200
_MAX_TITLE = 120
_MAX_BODY = 4000

# The wording as shipped, lifted from email_sender.send_followup so the editor opens showing
# exactly what customers have been receiving rather than a blank box.
DEFAULT_TEMPLATES: Dict[str, Dict[str, str]] = {
    "not_viewed": {
        "subject": "Your Treadwell proposal for {project} is ready when you are",
        "title": "Your proposal is waiting",
        "body": ("Hi {first_name},\n\n"
                 "We sent over the proposal for {project} and wanted to make sure it reached "
                 "you.\n\n"
                 "{link}\n\n"
                 "Any questions at all, just reply to this email — it comes straight to us."),
        "cta": "View your proposal",
    },
    "next_steps": {
        "subject": "Next steps for {project}",
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
        "subject": "Quick reminder — {project}",
        "title": "Still holding your spot",
        "body": ("Hi {first_name},\n\n"
                 "Just a nudge that the proposal for {project} is still pending. We need {need} "
                 "to schedule the work.\n\n"
                 "{link}\n\n"
                 "Happy to walk through it or price an alternative — a reply is enough."),
        "cta": "Review and approve",
    },
    "checkin": {
        "subject": "Checking in on {project}",
        "title": "Checking in",
        "body": ("Hi {first_name},\n\n"
                 "Circling back on {project}. It's still open on our side and we need {need} "
                 "whenever the timing works.\n\n"
                 "{link}"),
        "cta": "View your proposal",
    },
}


class ValidationError(ValueError):
    """A caller-fixable problem. The message reaches the user, so it says what to do."""


def _clamp_int(raw: Any, field: str) -> int:
    """A whole number pulled into range. Never raises for an out-of-range value.

    Clamping rather than rejecting is deliberate: somebody typing 2 hours meant "chase them
    sooner", and answering with a validation error teaches them the form is hostile. They get the
    fastest legal cadence and can see what it became.

    BUT zero and negatives fall back to the default instead of clamping to the floor. Clamping
    them would turn a typo into the most aggressive cadence the system allows — type 0 in the
    recurring box and customers get an email every four hours. A nonsense value is not an attempt
    at a fast schedule, so the safe reading is "they did not mean to change this".
    """
    lo, hi = BOUNDS[field]
    if isinstance(raw, bool) or raw in (None, ""):
        return int(DEFAULTS[field])
    try:
        n = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return int(DEFAULTS[field])
    # An hour field can legitimately be 0 only for send_start_hour (midnight).
    if n <= 0 and field != "send_start_hour":
        return int(DEFAULTS[field])
    return max(lo, min(hi, n))


def _clean_text(raw: Any, limit: int) -> str:
    """Plain text, collapsed. Any HTML tag is stripped rather than escaped.

    Stripping beats escaping here: escaping would show a customer a literal "&lt;b&gt;", which
    looks broken, whereas stripping quietly gives them the sentence somebody meant to write."""
    s = str(raw or "")
    s = re.sub(r"<[^>]*>", "", s)                     # no HTML into a customer email
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    # Control characters out, keeping newlines and tabs. Two reasons, both real: Postgres jsonb
    # rejects a NUL outright, so one pasted from a spreadsheet would fail the save with a
    # message about a missing table; and email_sender marks the {link} position with a control
    # character while escaping, which is only safe because a stored body can never contain one.
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)                  # cap the blank runs, keep paragraphs
    return s.strip()[:limit]


def validate_template(key: str, raw: Any) -> Dict[str, str]:
    """One template, cleaned. Raises only for a missing {link}."""
    if key not in TEMPLATE_KEYS:
        raise ValidationError("There is no follow-up email called %r." % key)
    src = raw if isinstance(raw, dict) else {}
    base = DEFAULT_TEMPLATES[key]

    subject = _clean_text(src.get("subject"), _MAX_SUBJECT) or base["subject"]
    title = _clean_text(src.get("title"), _MAX_TITLE) or base["title"]
    cta = _clean_text(src.get("cta"), 60) or base["cta"]

    # The body is REFUSED when it is too long, not truncated — unlike the short fields above.
    # Truncating it emails the customer half a sentence, and worse: the {link} is usually at the
    # end, so cutting the text silently removed the button and then the check below rejected the
    # save with a message about a missing link the author never removed. Confusing, and it hid the
    # real problem.
    raw_body = _clean_text(src.get("body"), _MAX_BODY * 4)
    if len(raw_body) > _MAX_BODY:
        raise ValidationError(
            "The “%s” email is %d characters — the limit is %d. Trim it rather than "
            "letting it be cut off mid-sentence." % (label(key), len(raw_body), _MAX_BODY))
    body = raw_body or base["body"]

    # The one hard rule. These emails exist to get somebody back to the proposal; without the
    # link there is nothing to click and the send is wasted. Fixing it silently would mean
    # appending a button somebody deliberately deleted, so this one says no.
    if "{link}" not in body:
        raise ValidationError(
            "The “%s” email needs {link} somewhere in the body — that is the button the "
            "customer clicks to see the proposal." % label(key))

    unknown = set(re.findall(r"\{[a-z_]+\}", subject + " " + body)) - set(TOKENS)
    if unknown:
        raise ValidationError(
            "Unknown placeholder %s. The ones available are %s."
            % (", ".join(sorted(unknown)), ", ".join(TOKENS)))

    return {"subject": subject, "title": title, "body": body, "cta": cta}


def validate(raw: Any) -> Dict[str, Any]:
    """A whole settings payload, clamped and cleaned."""
    if not isinstance(raw, dict):
        raise ValidationError("Nothing to save.")

    out: Dict[str, Any] = {}
    for field in DEFAULTS:
        out[field] = _clamp_int(raw.get(field), field)

    # A window that ends before it starts would silence every customer email. Rather than refuse
    # it, fall back to the shipped window — the intent was legible and the alternative is a
    # cadence that quietly sends nothing.
    if out["send_end_hour"] <= out["send_start_hour"]:
        out["send_start_hour"] = int(DEFAULTS["send_start_hour"])
        out["send_end_hour"] = int(DEFAULTS["send_end_hour"])

    templates = raw.get("templates")
    src = templates if isinstance(templates, dict) else {}
    out["templates"] = {k: validate_template(k, src.get(k)) for k in TEMPLATE_KEYS}
    return out


def defaults() -> Dict[str, Any]:
    """The shipped cadence, as a settings dict."""
    out = dict(DEFAULTS)
    out["templates"] = {k: dict(v) for k, v in DEFAULT_TEMPLATES.items()}
    return out


def merge(stored: Any) -> Dict[str, Any]:
    """Stored settings over the defaults, so a partial or corrupt row still yields a full,
    usable cadence. This is what makes the table optional."""
    out = defaults()
    if not isinstance(stored, dict):
        return out
    for field in DEFAULTS:
        if field in stored:
            out[field] = _clamp_int(stored.get(field), field)
    if out["send_end_hour"] <= out["send_start_hour"]:
        out["send_start_hour"] = int(DEFAULTS["send_start_hour"])
        out["send_end_hour"] = int(DEFAULTS["send_end_hour"])
    t = stored.get("templates")
    if isinstance(t, dict):
        for key in TEMPLATE_KEYS:
            if not isinstance(t.get(key), dict):
                continue
            try:
                out["templates"][key] = validate_template(key, t[key])
            except ValidationError:
                # A stored template that has gone bad (hand-edited row, older shape) must not
                # take the cadence down — it falls back to the shipped wording for that one email.
                log.warning("[followups] stored %s template is invalid; using the default", key)
    return out


def render(template: Dict[str, str], *, first_name: str, project: str, need: str,
           link_html: str) -> Dict[str, str]:
    """Fill a template's tokens. Returns {subject, title, body} ready for the email shell.

    `{link}` becomes the branded CTA button, not a bare URL — the button is ours and stays ours,
    so an edited template cannot produce an email that looks unlike Treadwell.
    """
    def sub(s: str, link: str) -> str:
        return (s.replace("{first_name}", first_name or "there")
                 .replace("{project}", project or "your project")
                 .replace("{need}", need or "your signed approval")
                 .replace("{link}", link))

    return {
        "subject": sub(template.get("subject", ""), ""),   # no button in a subject line
        "title": sub(template.get("title", ""), ""),
        "body": sub(template.get("body", ""), link_html),
    }


def preview(settings: Dict[str, Any], key: str) -> Dict[str, str]:
    """A template rendered with sample values, for the editor.

    The point is that nobody sends a broken one: an unfilled token or a missing button is obvious
    here and invisible in the form."""
    tpl = (settings.get("templates") or {}).get(key) or DEFAULT_TEMPLATES.get(key) or {}
    return render(tpl, first_name="Dave", project="Westport Retail Center",
                  need="your signed approval and the deposit",
                  link_html="[ %s ]" % (tpl.get("cta") or "View your proposal"))
