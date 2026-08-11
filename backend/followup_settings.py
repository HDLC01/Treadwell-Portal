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

# What the cadence chases with. `deposit_nudge` IS one of these — Hanz, 2026-08-12: "followups
# should be automated until a deposit has been received." Before that, approval ended the cadence
# and an approved job got one invoice email and then silence.
TEMPLATE_KEYS = ("not_viewed", "next_steps", "second_nudge", "checkin", "deposit_nudge")

# The FIRST proposal email, editable on the same page. Hanz, 2026-08-12: "Create the ability to
# change what the first proposal sent email looks like. from the heading to the content (this
# would be the global setting for the first proposal sent) Just like the emails for the follow
# ups."
#
# Deliberately NOT in TEMPLATE_KEYS. That tuple is what followup_rules and followup_worker walk
# to decide what to chase with; adding "sent" there would make the cadence send the
# here-is-your-proposal email as a reminder. So the editable set is a superset and the cadence set
# is untouched — every loop over the four stays a loop over the four.
SENT_KEY = "sent"
ALL_TEMPLATE_KEYS = TEMPLATE_KEYS + (SENT_KEY,)
TOKENS = ("{first_name}", "{project}", "{need}", "{link}")

# What each email is CALLED on screen. The keys are database identifiers, and a refusal that says
# "the not viewed email needs {link}" makes somebody hunt for a tab with that name — there isn't
# one. Naming lives here rather than in the page so the message and the tab cannot disagree; the
# editor reads these off the GET response.
#
# The ORDER is the order the tabs appear in, and it is chronological: the send, then the chase,
# then the deposit stage that only starts once they approve. The editor takes both the set and the
# order from here rather than keeping its own list, so a template added in this file shows up on
# the page without a second deploy.
LABELS: Dict[str, str] = {
    "sent": "Proposal sent",
    "not_viewed": "Not opened yet",
    "next_steps": "After they open it",
    "second_nudge": "Second reminder",
    "checkin": "Recurring check-in",
    "deposit_nudge": "Deposit reminder",
}


# The same four emails, described by WHEN THEY FIRE rather than named. Hanz, 2026-08-12: "each
# category in the emails should have different language or terms. For example if its in the Not
# opened yet category the Label would be 'First Reminder after not Opening'. Just to clearly show
# what category we are in."
#
# LABELS above stays as it is and stays short: it names tabs, and it is quoted verbatim in every
# validation refusal ("the Not opened yet email needs {link}"), where a sentence would read
# badly. These are the heading shown under the tabs once one is selected — the place there is
# room to say what the email actually is.
EDITOR_TITLES: Dict[str, str] = {
    "sent": "Proposal sent — the first email, when you publish it",
    "not_viewed": "First reminder — after not opening",
    "next_steps": "Next steps — after they open it",
    "second_nudge": "Second reminder — opened, still no decision",
    "checkin": "Recurring check-in — repeats until they decide",
    "deposit_nudge": "Deposit reminder — approved, deposit not yet in",
}


def label(key: str) -> str:
    """The on-screen name of one email, for use in a message somebody has to act on."""
    return LABELS.get(key, str(key).replace("_", " "))

_MAX_SUBJECT = 200

# ONE subject for every customer email about a project, editable on the Auto Followups page.
#
# Hanz, 2026-08-11: "for all updates to one project can we have it in one email thread?"
# Gmail groups by the References chain AND the subject, so a per-template subject line was
# what split a chased proposal into a conversation per email no matter what the headers said.
#
# This REPLACED a per-template "subject" field, which the editor still exposed as its own
# input. Leaving that field in place while ignoring it would have been the worst of the three
# options: somebody types a subject, saves, and nothing happens. So the field became this one,
# moved up to project level, and the per-template "Heading inside the email" — which was
# always separately editable — is what still varies per email.
DEFAULT_THREAD_SUBJECT = "Your Treadwell proposal — {project}"
# {first_name}/{need} are per-send and would make the subject differ between emails, which is
# the whole thing being fixed. {link} in a subject line is meaningless.
THREAD_SUBJECT_TOKENS = ("{project}",)
_MAX_TITLE = 120
_MAX_BODY = 4000

# The wording as shipped, lifted from email_sender.send_followup so the editor opens showing
# exactly what customers have been receiving rather than a blank box.
DEFAULT_TEMPLATES: Dict[str, Dict[str, str]] = {
    # The first send. Wording lifted from send_portal_link so the editor opens showing exactly
    # what customers have been receiving, the same rule the four below follow.
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
    "checkin": {
        "title": "Checking in",
        "body": ("Hi {first_name},\n\n"
                 "Circling back on {project}. It's still open on our side and we need {need} "
                 "whenever the timing works.\n\n"
                 "{link}"),
        "cta": "View your proposal",
    },
    # After approval. Deliberately does NOT use {need} — that phrase is "your signed approval and
    # the deposit", and by the time this sends they have already signed. Asking again for something
    # they have done is how a reminder reads as a mistake on our side.
    #
    # The last line is there because a cheque can be genuinely in the post while our column still
    # says nothing. The customer chase stops the moment they tell us (deposit submitted), but until
    # they do, we cannot tell "hasn't paid" from "hasn't mentioned it".
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


def validate_thread_subject(raw: Any) -> str:
    """The one subject every project email carries, cleaned.

    Empty falls back to the shipped wording rather than refusing: a blank subject is a broken
    email in every client, and the intent of clearing the box is "put it back how it was".

    {project} is the only token allowed. {first_name} and {need} vary per SEND, so allowing
    them would let one project's emails carry different subjects again — which is the exact
    splitting this field exists to stop. Refused loudly rather than stripped, because a subject
    reading "Your proposal, " with the token silently removed is worse than a save that failed.
    """
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
    """One template, cleaned. Raises only for a missing {link}."""
    if key not in ALL_TEMPLATE_KEYS:
        raise ValidationError("There is no follow-up email called %r." % key)
    src = raw if isinstance(raw, dict) else {}
    base = DEFAULT_TEMPLATES[key]

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

    unknown = set(re.findall(r"\{[a-z_]+\}", body)) - set(TOKENS)
    if unknown:
        raise ValidationError(
            "Unknown placeholder %s. The ones available are %s."
            % (", ".join(sorted(unknown)), ", ".join(TOKENS)))

    # No "subject": a project has ONE, at the top level. A stored template from before that
    # change still parses — its subject is simply dropped here rather than migrated.
    return {"title": title, "body": body, "cta": cta}


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

    out["thread_subject"] = validate_thread_subject(raw.get("thread_subject"))

    templates = raw.get("templates")
    src = templates if isinstance(templates, dict) else {}
    out["templates"] = {k: validate_template(k, src.get(k)) for k in ALL_TEMPLATE_KEYS}
    return out


def defaults() -> Dict[str, Any]:
    """The shipped cadence, as a settings dict."""
    out = dict(DEFAULTS)
    out["thread_subject"] = DEFAULT_THREAD_SUBJECT
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
    if "thread_subject" in stored:
        try:
            out["thread_subject"] = validate_thread_subject(stored.get("thread_subject"))
        except ValidationError:
            # Same posture as a bad template below: a hand-edited row must not silence every
            # customer email, it falls back to the shipped wording.
            log.warning("[followups] stored thread_subject is invalid; using the default")

    t = stored.get("templates")
    if isinstance(t, dict):
        for key in ALL_TEMPLATE_KEYS:
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
