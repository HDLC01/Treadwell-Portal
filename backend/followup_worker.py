"""The tick that actually chases proposals.

One daemon thread, started at boot. The portal runs a single uvicorn worker, so
there is exactly one of these per container and no cross-process coordination is
needed — but a DEPLOY briefly runs two containers, and a crash can restart one
mid-send, so the dedupe cannot live in memory. It lives in the database: the worker
RESERVES the right to send (a row in portal_followups, protected by a partial unique
index on the rule key) and only then sends.

That ordering is deliberate. If the process dies between reserving and sending, the
customer misses one nudge and the next cadence step covers it. If it sent first and
died before recording, the customer would get the same nag twice on every restart —
and there is no way to un-send an email. At-most-once is the right bias here.

Everything is per-proposal try/except: one malformed row must never stop the sweep
for everyone else.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone

import config
import db
import email_sender
import followup_rules as rules
import followup_settings

log = logging.getLogger("portal")

_START_LOCK = threading.Lock()
_started = False

# Read from the environment on EVERY tick, not at import: flipping automation off in
# production is an env change plus a restart, and the flag has to be believed
# immediately rather than at whatever value it held when the module loaded.
def _enabled() -> bool:
    # The fallback default is FALSE at both levels — the env var and the config attribute.
    # `getattr(..., True)` would have quietly re-enabled automation on any build where the
    # config attribute went missing, which is the one case you least want it guessing.
    raw = os.environ.get("FOLLOWUP_AUTOMATION_ENABLED",
                         "true" if getattr(config, "FOLLOWUP_AUTOMATION_ENABLED", False) else "false")
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _interval() -> int:
    try:
        n = int(os.environ.get("FOLLOWUP_TICK_SECONDS", "900"))
    except (TypeError, ValueError):
        n = 900
    return max(60, min(3600, n))


def ensure_started() -> bool:
    """Start the loop once. Called from app startup rather than lazily off a request:
    a proposal nobody is looking at is exactly the one that needs chasing, so waiting
    for traffic would mean the quiet proposals never get followed up.

    Two things stop it before it starts. Under pytest, because every test file builds
    a TestClient and would otherwise spawn a thread that blocks on a database the
    tests deliberately don't have. And when automation is off, because a sweep that
    can only return early is not worth a thread — `_tick` re-reads the flag anyway,
    so this only decides whether to bother spinning one up at boot."""
    global _started
    if "pytest" in sys.modules:
        return False
    if not _enabled():
        log.info("[followup] automation disabled — worker not started")
        return False
    with _START_LOCK:
        if _started:
            return False
        _started = True
    threading.Thread(target=_run, name="followup-worker", daemon=True).start()
    log.info("[followup] worker started (tick=%ss, enabled=%s)", _interval(), _enabled())
    return True


def _run() -> None:
    time.sleep(20)          # let the app finish booting before the first sweep
    while True:
        try:
            _tick()
        except Exception as exc:  # noqa: BLE001 — a bad sweep must not kill the thread
            log.error("[followup] tick failed: %s", exc)
        time.sleep(_interval())


def _customer_recipients(p: dict) -> list[str]:
    """The contacts who should be CHASED — not simply everyone on the proposal.

    Hanz, 2026-08-12: "just like the 25% deposit creat a checkbox for each contact if they will
    be able to receive the automated follow ups or no". An accounts-payable address wants the
    invoice and not four reminders.

    get_followup_recipients rather than get_recipients: every other customer-facing email still
    goes to everybody, because opting out of the chase is not opting out of the proposal.

    Falls back to the primary contact when the list is empty, exactly as before — but note the
    difference between EMPTY and ALL-OPTED-OUT. An empty list means we could not read the
    recipients, so the primary is the safe guess. Every contact opting out is a decision somebody
    made, and honouring it means sending nothing; that is what the explicit flag below is for.
    """
    try:
        rec = db.get_followup_recipients(p["proposal_id"])
    except Exception:  # noqa: BLE001
        rec = None
    if rec is None:                      # the read failed — behave as it always did
        return [e for e in [p.get("customer_email")] if e]
    if rec:
        return rec
    # Readable, and deliberately nobody. Distinguished from a failed read so a project whose
    # contacts have all opted out is left alone instead of falling back to the primary.
    try:
        if db.get_recipients(p["proposal_id"]):
            return []
    except Exception:  # noqa: BLE001
        pass
    return [e for e in [p.get("customer_email")] if e]


def _days_since(raw) -> int | None:
    """Whole days between a stored timestamp and now, or None if it cannot be read.

    None rather than 0: "approved 0 days ago" in a reminder about a job going quiet is worse than
    not saying when, and a missing or malformed `approved_at` must not stop the email — the point
    of the send is the outstanding deposit, not the date."""
    try:
        when = rules._aware(raw)
        if when is None and isinstance(raw, str) and raw.strip():
            # rules._aware takes a real datetime only, which is what psycopg hands back here. A
            # STRING reaches this on any path that has been through JSON — a stubbed row in a
            # test, or a row read back through PostgREST. Parsing it rather than giving up keeps
            # the sentence honest instead of silently printing "recently" for every send.
            when = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
            if not when.tzinfo:
                when = when.replace(tzinfo=timezone.utc)
        if when is None:
            return None
        return max(0, int((datetime.now(timezone.utc) - when).total_seconds() // 86400))
    except Exception:  # noqa: BLE001
        return None


def _staff_recipients(p: dict) -> list[str]:
    """The assigned estimator owns the follow-up. Proposals published before
    assignment was required fall back to the notification roster so the nudge still
    reaches a human."""
    who = (p.get("assigned_estimator") or "").strip()
    if who:
        return [who]
    try:
        return email_sender._resolve_notify("general", p["proposal_id"]) or []
    except Exception:  # noqa: BLE001
        return []


def _settings() -> dict:
    """The cadence and wording staff have saved, laid over the shipped defaults.

    Never raises. A settings table that does not exist yet, or a database blip, must not stop the
    cadence — it falls back to the cadence as shipped, which is what every environment ran before
    these settings existed. That is also what lets this code deploy before the DDL is applied."""
    try:
        return followup_settings.merge(db.get_settings(followup_settings.ROW_ID))
    except Exception as exc:  # noqa: BLE001
        log.warning("[followup] could not read settings (%s) - using the shipped cadence", exc)
        return followup_settings.defaults()


def _send_customer(p: dict, due, templates: dict | None = None) -> bool:
    token = p.get("token")
    url = f"{config.PUBLIC_BASE_URL}/p/{token}"
    # The deposit reminder lands them ON the deposit step rather than at the top of a proposal
    # they have already read and approved. Same anchor the bell links use, so `applyHashView`
    # already handles it — a made-up fragment would open the page and quietly do nothing.
    if due.template == "deposit_nudge":
        url += "#proposal/deposit"
    reply_to = email_sender.proposal_reply_to(token)
    project = p.get("project_name") or "your project"
    name = p.get("customer_name") or ""
    ok = False
    for i, addr in enumerate(_customer_recipients(p)):
        try:
            sent = email_sender.send_followup(
                addr, url, project, due.template, templates=templates,
                # Only the primary gets the first-name greeting, matching publish.
                name=name if i == 0 else "",
                deposit_required=p.get("deposit_required") is not False,
                reply_to=reply_to, token=token,
                include_status_ask=due.include_status_ask)
            ok = ok or bool(sent)
        except Exception as exc:  # noqa: BLE001
            log.warning("[followup] send to %s failed for %s: %s", addr, p["proposal_id"], exc)
    return ok


def _send_staff(p: dict, due) -> bool:
    to = _staff_recipients(p)
    if not to:
        log.info("[followup] no staff recipient for %s", p["proposal_id"])
        return False
    pid = p["proposal_id"]
    project = p.get("project_name") or "this project"
    token = p.get("token")
    portal_url = f"{config.PUBLIC_BASE_URL}/p/{token}"
    crm_url = f"{config.PROPOSAL_TOOL_PUBLIC_URL}/portal.html?open={pid}"
    who = p.get("customer_name") or p.get("customer_email") or "the customer"
    # For a deposit chase the figure that matters is the DEPOSIT, not the whole job — the estimator
    # is about to ask for one specific number, and quoting the contract value would have them
    # asking for the wrong one.
    amount = (p.get("deposit_amount") or p.get("approved_total")
              if due.template == "staff_deposit_outstanding"
              else p.get("approved_total") or p.get("deposit_amount"))
    amount_txt = f"${float(amount):,.2f}" if amount is not None else "—"

    if due.template == "staff_not_viewed":
        subject = f"Not viewed yet — {project}"
        body = (f"<p><strong>{email_sender._esc(who)}</strong> hasn't opened the proposal for "
                f"<strong>{email_sender._esc(project)}</strong> yet — it went out 24 hours ago.</p>"
                f"<p>A quick call often beats another email.</p>")
    elif due.template == "staff_pause_expired":
        until = p.get("followup_paused_until")
        subject = f"Follow-up reminder — {project}"
        body = (f"<p>The delay window {email_sender._esc(who)} asked for on "
                f"<strong>{email_sender._esc(project)}</strong> ended "
                f"{email_sender._esc(until)}. Automated follow-ups have resumed.</p>"
                f"<p>Worth a personal check-in before the reminders land.</p>")
    elif due.template == "staff_deposit_outstanding":
        # The half that keeps going after the customer's reminders stop. A cheque "in the post"
        # that never lands has to stay somebody's problem, so this says WHICH of the two it is
        # rather than one vague "deposit outstanding" — they are different phone calls.
        submitted = str(p.get("deposit_status") or "").strip().lower() == "submitted"
        days = _days_since(p.get("approved_at"))
        ago = f"{days} day{'' if days == 1 else 's'} ago" if days is not None else "recently"
        subject = (f"Deposit not in yet — {project}" if not submitted
                   else f"Deposit still unconfirmed — {project}")
        head = (f"<p><strong>{email_sender._esc(who)}</strong> approved "
                f"<strong>{email_sender._esc(project)}</strong> {ago}, and the deposit ")
        body = (head + ("has been recorded on their side but has not arrived yet.</p>"
                        if submitted else "has not come in.</p>")
                + f"<ul>"
                + f"<li>Customer: {email_sender._esc(who)}</li>"
                + f"<li>Email: {email_sender._esc(p.get('customer_email') or '—')}</li>"
                + f"<li>Amount: {amount_txt}</li>"
                + f"<li>Proposal: <a href=\"{portal_url}\">{portal_url}</a></li>"
                + f"</ul>"
                + ("<p>Their own reminders have stopped — they've told us it's on the way, so "
                   "this one is ours to chase. Mark it received in the CRM when it lands.</p>"
                   if submitted else
                   "<p>The customer is still being reminded automatically. Dates aren't held "
                   "until the deposit is in, so a call is worth more than the next email.</p>"))
    else:   # staff_personal_followup
        subject = f"Time for a personal follow-up — {project}"
        body = (f"<p><strong>{email_sender._esc(who)}</strong> has read the proposal for "
                f"<strong>{email_sender._esc(project)}</strong> but hasn't approved it.</p>"
                f"<ul>"
                f"<li>Customer: {email_sender._esc(who)}</li>"
                f"<li>Email: {email_sender._esc(p.get('customer_email') or '—')}</li>"
                f"<li>Amount: {amount_txt}</li>"
                f"<li>Proposal: <a href=\"{portal_url}\">{portal_url}</a></li>"
                f"</ul>"
                f"<p>Automated reminders continue, but this one is worth a call.</p>")
    try:
        return bool(email_sender.notify_team(
            subject, body, recipients=to, reply_link=crm_url, proposal_id=pid,
            reply_to=email_sender.proposal_reply_to(token)))
    except Exception as exc:  # noqa: BLE001
        log.warning("[followup] staff note failed for %s: %s", pid, exc)
        return False


# What each reminder says once it is in the conversation, written for the person who RECEIVED it.
#
# Hanz, 2026-08-19: "For the Email follow ups, can it appear in the ChatBox and a history of the
# follow ups." Until now a customer who reads the portal instead of their inbox watched the thread
# go silent while six emails went out — and staff had no shared record of what had been chased.
#
# The internal vocabulary stays internal. "Nudge", "second nudge", "chase", "cadence" and the rule
# keys are how we talk about this to each other; `not_viewed` rendered on a customer's own screen as
# "Not opened yet" reads like being told off. Each line below says what we did and why, in the words
# we would use to their face.
_ECHO = {
    "not_viewed": "Reminder sent — we emailed you a link to the proposal in case it got buried.",
    "next_steps": "Reminder sent — we emailed you about the next steps on this proposal.",
    "second_nudge": "Reminder sent — we emailed you again about this proposal.",
    "checkin": "Checking in — we emailed you to see where this stands.",
    "deposit_nudge": "Reminder sent — we emailed you about the deposit for this project.",
}


def _echo_to_thread(pid: str, due) -> None:
    """Put a sent reminder into the project's conversation, so both sides can see it happened.

    CUSTOMER SENDS ONLY, and that gate is the whole safeguard. Every staff template in
    `_send_staff` is written for us and not for them — "A quick call often beats another email",
    "this one is ours to chase", "Dates aren't held until the deposit is in" — plus the customer's
    own address, the amount owed and a CRM link. The thread has no per-message visibility flag: the
    customer endpoint returns every row. So an internal note posted here is an internal note the
    customer reads.

    `msg_type="system"` because both screens already render that as a card (app.js in the portal,
    portal.js in the staff drawer) and it sits inside the existing CHECK constraint — no new message
    type, so no migration. `meta.followup` marks these as machine-sent, which is what keeps them out
    of the customer's notification bell: being emailed and then pinged about having been emailed is
    one event, not two.

    Called only after a send actually succeeded, so a released reservation leaves no claim that we
    wrote to somebody we did not. Best-effort: the email has gone, and nothing here is worth undoing
    it over."""
    if getattr(due, "audience", "") != "customer":
        return
    body = _ECHO.get(getattr(due, "template", ""))
    if not body:
        return                              # an unmapped template says nothing rather than guessing
    try:
        db.add_message(pid, "staff", None, body, msg_type="system",
                       meta={"followup": True, "template": due.template})
    except Exception as exc:  # noqa: BLE001 — the email is away; the record is a courtesy
        log.warning("[followup] could not echo %s to %s's thread: %s", due.template, pid, exc)


def _tick(now: datetime | None = None) -> None:
    if not _enabled():
        return
    now = now or datetime.now(timezone.utc)
    # One read per tick, so every proposal in this pass is judged by the same cadence. Reading it
    # per row would let an edit landing mid-tick apply to half the candidates and not the rest,
    # which is the kind of inconsistency nobody would ever reproduce.
    cfg = _settings()
    try:
        candidates = db.list_followup_candidates()
    except Exception as exc:  # noqa: BLE001
        log.error("[followup] could not list candidates: %s", exc)
        return

    for row in candidates:
        pid = row.get("proposal_id")
        try:
            # Re-read before acting. The list may be minutes old by the time we reach
            # this row, and a proposal approved or taken off automation in between
            # must not get one last nag.
            fresh = db.get_proposal(pid) or row
            if fresh.get("followup_disabled_at") or not rules.in_scope(fresh):
                continue
            for due in rules.due_now(fresh, now, cfg):
                rid = db.reserve_followup(pid, due.rule_key, {
                    "audience": due.audience, "template": due.template,
                })
                if rid is None:
                    continue        # already sent — a prior tick, or the twin container
                ok = _send_customer(fresh, due, (cfg or {}).get("templates")) \
                    if due.audience == "customer" else _send_staff(fresh, due)
                if not ok:
                    # Nothing went out, so release the claim and let the next tick try.
                    # A systemic outage therefore retries at tick cadence rather than
                    # silently swallowing the whole cadence step.
                    db.delete_followup(rid)
                    log.info("[followup] %s %s not sent — reservation released",
                             pid, due.rule_key)
                else:
                    log.info("[followup] %s %s -> %s", pid, due.rule_key, due.audience)
                    _echo_to_thread(pid, due)
        except Exception as exc:  # noqa: BLE001 — never let one row end the sweep
            log.warning("[followup] skipped %s: %s", pid, exc)
