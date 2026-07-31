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

log = logging.getLogger("portal")

_START_LOCK = threading.Lock()
_started = False

# Read from the environment on EVERY tick, not at import: flipping automation off in
# production is an env change plus a restart, and the flag has to be believed
# immediately rather than at whatever value it held when the module loaded.
def _enabled() -> bool:
    raw = os.environ.get("FOLLOWUP_AUTOMATION_ENABLED",
                         "true" if getattr(config, "FOLLOWUP_AUTOMATION_ENABLED", True) else "false")
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
    """Everyone on the proposal, same as every other customer-facing email."""
    try:
        rec = db.get_recipients(p["proposal_id"]) or []
    except Exception:  # noqa: BLE001
        rec = []
    return rec or [e for e in [p.get("customer_email")] if e]


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


def _send_customer(p: dict, due) -> bool:
    token = p.get("token")
    url = f"{config.PUBLIC_BASE_URL}/p/{token}"
    reply_to = email_sender.proposal_reply_to(token)
    project = p.get("project_name") or "your project"
    name = p.get("customer_name") or ""
    ok = False
    for i, addr in enumerate(_customer_recipients(p)):
        try:
            sent = email_sender.send_followup(
                addr, url, project, due.template,
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
    amount = p.get("approved_total") or p.get("deposit_amount")
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


def _tick(now: datetime | None = None) -> None:
    if not _enabled():
        return
    now = now or datetime.now(timezone.utc)
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
            if fresh.get("followup_disabled_at") or \
                    (fresh.get("proposal_status") or "") not in ("sent", "viewed"):
                continue
            for due in rules.due_now(fresh, now):
                rid = db.reserve_followup(pid, due.rule_key, {
                    "audience": due.audience, "template": due.template,
                })
                if rid is None:
                    continue        # already sent — a prior tick, or the twin container
                ok = _send_customer(fresh, due) if due.audience == "customer" \
                    else _send_staff(fresh, due)
                if not ok:
                    # Nothing went out, so release the claim and let the next tick try.
                    # A systemic outage therefore retries at tick cadence rather than
                    # silently swallowing the whole cadence step.
                    db.delete_followup(rid)
                    log.info("[followup] %s %s not sent — reservation released",
                             pid, due.rule_key)
                else:
                    log.info("[followup] %s %s -> %s", pid, due.rule_key, due.audience)
        except Exception as exc:  # noqa: BLE001 — never let one row end the sweep
            log.warning("[followup] skipped %s: %s", pid, exc)
