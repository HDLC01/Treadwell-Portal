"""The tick that sends the follow-up emails.

WHAT IT DOES (Hanz, 2026-10-28: "the follow ups should be a follow up email to the customer
okay?"). When a follow-up comes due (followup_rules.due_now) the worker EMAILS THE CUSTOMER itself:
the proposal's customer and every recipient on it who has not opted out of follow-ups, with the
saved template for that email, under the project's one email-thread subject, inside the Central
send window. When a step's emails are used up it tells the estimator once, if that switch is on.

This reverses the remind-only model of 2026-10-23 (portal #27), where a due reminder raised a
`followup_task` for the estimator and emailed nobody outside the company. Reminder tasks are no
longer raised; any still pending are marked expired by the housekeeping pass (`auto_followups`),
so an estimator cannot send by hand what the worker is about to send. The table and its rows stay
readable, the task endpoints still answer for them, and the scope-group DECISION task ("decide
what to do with the other proposals") is still raised and re-nagged — it is a staff job, not a
customer reminder.

THE SEND IS THE OLD AUTO-EMAIL PATH, reused: reserve, then send. The worker RESERVES the right to
send (a row in portal_followups, kind auto_email, under a unique (proposal, rule key) index) and
only then sends; a send that reached nobody releases the reservation so the next tick retries.
A deploy briefly runs two containers and a crash can restart one mid-send, so the dedupe cannot
live in memory: if the process dies between reserving and sending, the customer misses one email
(at-most-once is the right bias — there is no un-sending). The reserved row is also what the
schedule counts (db._SENT_HISTORY_SQL), so a restart cannot reset the 2-per-step cap.

Everything is per-proposal try/except: one malformed row must never stop the sweep for everyone
else.
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

# detail.engine on every row this worker reserves.
ENGINE_MARK = "auto-2026-10-28"


# Read from the environment on EVERY tick, not at import: flipping automation off in production is
# an env change plus a restart, and the flag has to be believed immediately.
def _enabled() -> bool:
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
    """Start the loop once, at app startup. Not under pytest, and not when automation is off."""
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
    """The contacts who get the follow-up emails — not simply everyone on the proposal.

    Per-contact opt-outs are honoured (db.get_followup_recipients): an accounts-payable address
    wants the invoice, not reminders. EMPTY and ALL-OPTED-OUT differ: a failed read falls back to
    the primary contact, but every contact opting out is a decision, and honouring it means
    sending nothing."""
    try:
        rec = db.get_followup_recipients(p["proposal_id"])
    except Exception:  # noqa: BLE001
        rec = None
    if rec is None:                      # the read failed — behave as it always did
        return [e for e in [p.get("customer_email")] if e]
    if rec:
        return rec
    try:
        if db.get_recipients(p["proposal_id"]):
            return []
    except Exception:  # noqa: BLE001
        pass
    return [e for e in [p.get("customer_email")] if e]


def _staff_recipients(p: dict) -> list[str]:
    """The assigned estimator owns the follow-up; older proposals fall back to the roster."""
    who = (p.get("assigned_estimator") or "").strip()
    if who:
        return [who]
    try:
        return email_sender._resolve_notify("general", p["proposal_id"]) or []
    except Exception:  # noqa: BLE001
        return []


def _settings() -> dict:
    """The schedule and wording staff have saved, over the shipped defaults. Never raises."""
    try:
        return followup_settings.merge(db.get_settings(followup_settings.ROW_ID))
    except Exception as exc:  # noqa: BLE001
        log.warning("[followup] could not read settings (%s) - using the shipped schedule", exc)
        return followup_settings.defaults()


def _send_customer(p: dict, due, templates: dict | None = None) -> bool:
    """One follow-up email to every follow-up recipient of `p`. True when at least one went."""
    token = p.get("token")
    url = f"{config.PUBLIC_BASE_URL}/p/{token}"
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


def _scope_names(scopes) -> str:
    return ", ".join(email_sender._esc(s.get("label") or "a scope")
                     for s in (scopes or []) if isinstance(s, dict))


# What the customer has (not) done, in the estimator's words, per step.
_STEP_SITUATION = {
    rules.NOT_OPENED: "still hasn't opened the proposal",
    rules.OPENED: "has opened the proposal but hasn't decided",
}

# Why a step's 2nd email was skipped (rules.CONTACT_KEYS' names), for the estimator's note.
_SKIP_WORDS = {
    "customer_reply": "the customer replied",
    "staff_contact": "a call, email or text with them was logged",
    "staff_message": "an estimator messaged them in the thread",
}


def _send_staff(p: dict, due, info: dict | None = None) -> bool:
    """An estimator email. `p` may be a scope group's synthetic proposal (rules.group_view): it then
    carries `scopes`, which the email lists, and `link_proposal_id`, an OPEN scope for the button.

    `info` is the plan's {step, sent} for the estimator's "follow-ups are done" note."""
    to = _staff_recipients(p)
    if not to:
        log.info("[followup] no staff recipient for %s", p["proposal_id"])
        return False
    pid = p["proposal_id"]
    link_pid = p.get("link_proposal_id") or pid
    project = p.get("project_name") or "this project"
    token = p.get("token")
    # NO CUSTOMER LINK IN A STAFF EMAIL (Hanz, 2026-08-24): /p/{token} is the customer's whole
    # credential. The button links the CRM card instead.
    crm_url = f"{config.PROPOSAL_TOOL_PUBLIC_URL}/portal.html?open={link_pid}"
    who = p.get("customer_name") or p.get("customer_email") or "the customer"
    esc = email_sender._esc

    if due.template == "staff_scope_decision":
        # Scope groups: one proposal was approved, the follow-ups on the others have stopped, and
        # somebody has to decide what happens to them. Never auto-closed.
        dec = p.get("_decision") or {}
        subject = f"Decide the other scopes — {project}"
        body = (f"<p><strong>{esc(who)}</strong> approved the "
                f"<strong>{esc(dec.get('approved_label') or 'one')}</strong> scope "
                f"of <strong>{esc(project)}</strong>.</p>"
                f"<p>Follow-up reminders for the other scopes have stopped: "
                f"<strong>{_scope_names(dec.get('scopes'))}</strong>.</p>"
                f"<p>Open it in the CRM to close them as lost, keep following up on them, or "
                f"leave them as they are. Nothing has been closed.</p>")
    else:   # staff_followups_done — the "Tell the estimator" switch
        info = info or {}
        amount = p.get("approved_total") or p.get("deposit_amount")
        amount_txt = f"${float(amount):,.2f}" if amount is not None else "—"
        situation = _STEP_SITUATION.get(info.get("step") or "", "hasn't decided yet")
        n = int(info.get("sent") or 0)
        if n:
            sent_line = ("Both follow-up emails have gone to them" if n >= 2
                         else "The follow-up email has gone to them")
            sent_line += ", and no more will be sent automatically."
        else:
            sent_line = "No follow-up emails are switched on for this step."
        if info.get("skipped_by"):
            # The 2nd was cancelled by contact after the 1st (rules.plan): say why it never went.
            sent_line += (" The second one was not sent because "
                          + _SKIP_WORDS.get(info["skipped_by"], "you are already in touch") + ".")
        subject = f"Follow-ups done — {project}"
        body = (f"<p><strong>{esc(who)}</strong> {situation} on "
                f"<strong>{esc(project)}</strong>.</p>"
                f"<p>{sent_line} A call is the next step.</p>"
                f"<ul>"
                f"<li>Customer: {esc(who)}</li>"
                f"<li>Email: {esc(p.get('customer_email') or '—')}</li>"
                f"<li>Amount: {amount_txt}</li>"
                f"</ul>")
    if p.get("scopes") and due.template != "staff_scope_decision":
        body += (f"<p>This project has several proposals. Still open: "
                 f"<strong>{_scope_names(p.get('scopes'))}</strong>.</p>")
    try:
        # notify_team returns the addresses it DELIVERED to; falsy means release and retry.
        return bool(email_sender.notify_team(
            subject, body, recipients=to, reply_link=crm_url, proposal_id=link_pid,
            reply_to=email_sender.proposal_reply_to(token)))
    except Exception as exc:  # noqa: BLE001
        log.warning("[followup] staff note failed for %s: %s", pid, exc)
        return False


# What each reminder says once it is in the conversation, written for the person who RECEIVED it.
# The CUSTOMER map: a plain row in the thread they read.
_ECHO = {
    "not_viewed": "Reminder sent — we emailed you a link to the proposal in case it got buried.",
    "not_viewed_2": "Reminder sent — we emailed you again in case the proposal got lost.",
    "next_steps": "Reminder sent — we emailed you about the next steps on this proposal.",
    "second_nudge": "Reminder sent — we emailed you again about this proposal.",
    # Legacy templates: an old task an estimator still sends by hand.
    "checkin": "Checking in — we emailed you to see where this stands.",
    "deposit_nudge": "Reminder sent — we emailed you about the deposit for this project.",
}

# The STAFF map: rows marked `internal`, so they stay in the staff drawer and out of the
# customer's copy of the thread (db.list_messages).
_ECHO_STAFF = {
    rules.ESTIMATOR_TEMPLATE: ("Follow-ups done — the follow-up emails for this step have all gone "
                               "and the customer hasn't decided. The estimator has been emailed; a "
                               "call is the next step."),
    "staff_scope_decision": ("Decision needed — a scope of this project was approved, so the "
                             "follow-up reminders for the other scopes have stopped. Nothing has "
                             "been closed; the estimator has been emailed to decide."),
}


def _echo_to_thread(pid: str, due, extra_meta: dict | None = None) -> None:
    """Put a sent reminder into the project's conversation, so both sides can see it happened.

    The audience decides the map and the map decides visibility: a customer reminder posts a plain
    row, a staff one an INTERNAL row. An unmapped template says nothing. `meta.followup` keeps
    these out of the customer's notification bell. Called only after a send succeeded.
    Best-effort: the email has gone, and nothing here is worth undoing it over."""
    audience = getattr(due, "audience", "")
    template = getattr(due, "template", "")
    if audience == "customer":
        body, meta = _ECHO.get(template), {"followup": True, "template": template}
    elif audience == "staff":
        body, meta = _ECHO_STAFF.get(template), {"followup": True, "template": template,
                                                 "internal": True}
    else:
        return
    if not body:
        return
    if extra_meta:
        meta = dict({k: v for k, v in extra_meta.items() if k not in meta}, **meta)
    try:
        db.add_message(pid, "staff", None, body, msg_type="system", meta=meta)
    except Exception as exc:  # noqa: BLE001 — the email is away; the record is a courtesy
        log.warning("[followup] could not echo %s to %s's thread: %s", template, pid, exc)


def send_task_email(p: dict, template: str, include_status_ask: bool, tpl: dict,
                    rule_key: str = "task") -> bool:
    """Send ONE follow-up an estimator reviewed (an old task, sent by hand). Same rendering path as
    the automatic email, so the shell, Reply-To, thread headers and opt-outs are identical."""
    due = rules.Due(rule_key, "customer", template, bool(include_status_ask))
    return _send_customer(p, due, {template: tpl})


def send_group_task_email(view: dict, tpl: dict) -> dict:
    """A scope group's follow-up: ONE email PER RECIPIENT, listing only the open proposals that
    recipient is on, with one button to the project page (as the combined send). Returns
    {email: [proposal_id, ...]} for every address it was DELIVERED to (empty = nothing went).
    Used by the worker's automatic group email and by an old group task sent by hand."""
    members = view.get("_open_members") or []
    per: dict[str, list[dict]] = {}
    for m in members:
        for e in _customer_recipients(m):
            e = (e or "").strip().lower()
            if e:
                per.setdefault(e, []).append(m)
    primary = (view.get("customer_email") or "").strip().lower()
    project = view.get("project_name") or "your project"
    delivered: dict[str, list[str]] = {}
    for e, mine in per.items():
        try:
            ok = email_sender.send_scope_group_followup(
                e, (view.get("customer_name") or "") if e == primary else "", project,
                [{"label": m.get("scope_label") or m.get("project_name") or "Proposal",
                  "url": f"{config.PUBLIC_BASE_URL}/p/{m.get('token')}", "approved": False}
                 for m in mine],
                tpl, deposit_required=view.get("deposit_required") is not False,
                # A reply lands in the thread of the first proposal THEY are on.
                reply_to=email_sender.proposal_reply_to(mine[0].get("token")),
                token=mine[0].get("token"))
        except Exception as exc:  # noqa: BLE001 — one bad address must not stop the others
            log.warning("[followup] group send to %s failed: %s", e, exc)
            ok = False
        if ok:
            delivered[e] = [m["proposal_id"] for m in mine]
    return delivered


# ── housekeeping ──────────────────────────────────────────────────────────────

def _expire_stale_tasks(now: datetime) -> None:
    """A pending REMINDER task is expired: the worker emails the customer itself now, and a task
    left open would let an estimator send the same reminder by hand. A scope-group DECISION task
    is judged against its siblings and re-nagged while it is live. Never raises."""
    try:
        pending = db.list_pending_followup_tasks()
    except Exception as exc:  # noqa: BLE001
        log.warning("[followup] could not list pending tasks: %s", exc)
        return
    for t in pending or []:
        try:
            d = t.get("detail") or {}
            if d.get("stage") != rules.DECISION_STAGE:
                db.decide_followup_task(t["id"], "expired", None, {"reason": "auto_followups"})
                log.info("[followup] task %s on %s expired (auto_followups)",
                         t["id"], t["proposal_id"])
                continue
            ok, why, _subject = judge_task(t, now)
            if not ok:
                db.decide_followup_task(t["id"], "expired", None, {"reason": why})
                log.info("[followup] task %s on %s expired (%s)", t["id"], t["proposal_id"], why)
            else:
                _renag_decision(t, now)
        except Exception as exc:  # noqa: BLE001
            log.warning("[followup] could not check task %s: %s", t.get("id"), exc)


# ── scope groups ──────────────────────────────────────────────────────────────
# A project priced as several separately approved proposals is followed up as ONE project: one
# email per recipient listing their open proposals, at most 2 per step for the whole group, filed on
# the group's HOME proposal. Approving any proposal stops the group and raises ONE "decide what to
# do with the others" task (main.api_approve); nothing is ever closed automatically.

# How often an unresolved decision task re-emails the estimator, and how many times at most.
DECISION_RENAG_DAYS = 3
DECISION_RENAG_MAX = 5


def is_group_task(detail) -> bool:
    d = detail or {}
    return bool(d.get("scope_group")) or d.get("stage") == rules.DECISION_STAGE


def group_members(group_id: str, strict: bool = True) -> list[dict]:
    """The LIVE members of one scope group (followup_rules.live_members), freshly read. Raises when
    the drafts cannot be read and `strict` (the worker: fail closed, email nobody)."""
    rows = db.list_scope_group(group_id)
    try:
        drafts = db.list_scope_group_draft_states(group_id)
    except Exception:  # noqa: BLE001
        if strict:
            raise
        drafts = []
    return rules.live_members(rows, drafts)


def _with_signals(members: list[dict], group_id: str) -> list[dict]:
    """Each member with its sent history (db.followup_signals) and the group's last 'keep
    following up' decision. Unguarded: without the history the cap would be guessed."""
    resumed = db.scope_group_resumed_at(group_id)
    out = []
    for m in members:
        sig = db.followup_signals(m["proposal_id"]) or {}
        row = dict(m, **{k: v for k, v in sig.items() if k != "pending_task"})
        row["scope_group_resumed_at"] = resumed
        out.append(row)
    return out


def _group_history(group_id: str, members: list[dict]) -> list[dict]:
    """The group's NON-live rows (rules.history_rows), each with its sent history: a closed or
    deleted former home still holds the group's sends."""
    rows = db.list_scope_group_history(group_id)
    live_ids = {m.get("proposal_id") for m in members}
    if not any(r.get("proposal_id") not in live_ids for r in rows or []):
        return []
    drafts = db.list_scope_group_draft_states(group_id)
    out = []
    for r in rules.history_rows(rows, drafts, live_ids):
        sig = db.followup_signals(r["proposal_id"]) or {}
        out.append(dict(r, **{k: v for k, v in sig.items() if k != "pending_task"}))
    return out


def group_state_for(group_id: str, now: datetime) -> tuple:
    """(view, reason, members) for one group, read fresh. Raises if it cannot be read."""
    members = _with_signals(group_members(group_id), group_id)
    view, why = rules.group_state(members, now, _group_history(group_id, members))
    return view, why, members


def judge_task(task: dict, now: datetime) -> tuple:
    """(ok, reason, subject): may this open task still be acted on, and against what? The ONE
    judgement for the housekeeping and every task endpoint."""
    d = task.get("detail") or {}
    gid = d.get("scope_group")
    if d.get("stage") == rules.DECISION_STAGE:
        if not gid:
            return False, "missing", None
        subject = {"scope_members": group_members(gid, strict=False)}
        ok, why = rules.task_is_live(d, subject, now)
        return ok, why, subject
    if gid:
        view, why, _members = group_state_for(gid, now)
        if view is None:
            return False, why or "finished", None
        if view.get("proposal_id") != task.get("proposal_id"):
            return False, "rehomed", view
        ok, why = rules.task_is_live(d, view, now)
        return ok, why, view
    p = db.get_proposal(task["proposal_id"])
    ok, why = rules.task_is_live(d, p, now)
    return ok, why, p


def _current_status(pid: str) -> str | None:
    """A proposal's status now; None for a missing or deleted row."""
    p = db.get_proposal(pid)
    return None if not p or p.get("deleted_at") else p.get("proposal_status")


def _renag_decision(task: dict, now: datetime) -> None:
    """Re-email the estimator about an unresolved decision task every DECISION_RENAG_DAYS, at most
    DECISION_RENAG_MAX times. Reserved like every send (rule key per task per occurrence)."""
    try:
        created = rules._ts(task.get("created_at"))
        if created is None:
            return
        n = int((now - created).total_seconds() // (DECISION_RENAG_DAYS * 86400))
        if n < 1:
            return
        n = min(n, DECISION_RENAG_MAX)
        pid = task["proposal_id"]
        key = "sdn:%s:%d" % (task["id"], n)
        rid = db.reserve_followup(pid, key, {"audience": "staff",
                                             "template": "staff_scope_decision"})
        if rid is None:
            return
        p = db.get_proposal(pid) or {"proposal_id": pid}
        d = task.get("detail") or {}
        p = dict(p, _decision={"approved_label": (d.get("approved_scope") or {}).get("label"),
                               "scopes": d.get("scopes") or []})
        ids = [s.get("proposal_id") for s in d.get("scopes") or []
               if isinstance(s, dict) and s.get("proposal_id")]
        try:
            p["link_proposal_id"] = rules.decision_link(
                ids, _current_status,
                (d.get("approved_scope") or {}).get("proposal_id") or pid)
        except Exception as exc:  # noqa: BLE001 — the link only
            log.warning("[followup] decision task %s: statuses unreadable: %s", task.get("id"), exc)
            p["link_proposal_id"] = ids[0] if ids else pid
        due = rules.Due(key, "staff", "staff_scope_decision")
        if not _send_staff(p, due):
            db.delete_followup(rid)
    except Exception as exc:  # noqa: BLE001
        log.warning("[followup] decision task %s re-nag failed: %s", task.get("id"), exc)


# How long a `sending` claim may stand before the tick decides the send is not in flight any more.
SENDING_STALE_MINUTES = 15


def _recover_stuck_sends() -> None:
    """Settle old tasks the send endpoint claimed (`sending`) and then lost its database before it
    could finish or reopen them. The staff_email row written after a REAL send -> finish as sent;
    otherwise -> back to pending, flagged `send_unconfirmed` (the expiry pass then retires it).
    Neither emails anybody. Never raises."""
    try:
        stuck = db.list_stale_sending_tasks(SENDING_STALE_MINUTES)
    except Exception as exc:  # noqa: BLE001
        log.warning("[followup] could not list unsettled sends: %s", exc)
        return
    for t in stuck or []:
        tid = t.get("id")
        try:
            if db.followup_task_send_recorded(tid):
                by = (t.get("detail") or {}).get("decided_by")
                db.decide_followup_task(tid, "sent", by, {"recovered": True},
                                        from_status="sending")
                log.info("[followup] task %s: unsettled send had gone — finished as sent", tid)
            else:
                db.reopen_followup_task(tid, {"send_unconfirmed": True})
                log.warning("[followup] task %s: unsettled send, no record it went — reopened",
                            tid)
        except Exception as exc:  # noqa: BLE001
            log.warning("[followup] could not settle task %s: %s", tid, exc)


# ── the tick ──────────────────────────────────────────────────────────────────

def _tick(now: datetime | None = None) -> None:
    if not _enabled():
        return
    now = now or datetime.now(timezone.utc)
    # One read per tick, so every proposal in this pass is judged by the same schedule.
    cfg = _settings()
    try:
        candidates = db.list_followup_candidates()
    except Exception as exc:  # noqa: BLE001
        log.error("[followup] could not list candidates: %s", exc)
        return

    _recover_stuck_sends()
    _expire_stale_tasks(now)

    singles, groups = _partition(candidates)
    for row in singles:
        pid = row.get("proposal_id")
        try:
            fresh = _fresh(pid, row)
            if fresh is None:
                continue
            _act_on_dues(fresh, pid, rules.due_now(fresh, now, cfg), cfg, now)
        except Exception as exc:  # noqa: BLE001 — never let one row end the sweep
            log.warning("[followup] skipped %s: %s", pid, exc)
    for gid in groups:
        try:
            _process_group(gid, now, cfg)
        except Exception as exc:  # noqa: BLE001 — never let one group end the sweep
            log.warning("[followup] skipped scope group %s: %s", gid, exc)


def _fresh(pid: str, row: dict) -> dict | None:
    """The proposal re-read with its sent history, or None if it has left the follow-up window.

    Re-read before acting: the candidate list may be minutes old, and a proposal approved or
    switched off in between must not get one last email. The history read is unguarded on purpose:
    without it the cap cannot be honoured, so a failed read skips this row rather than guessing."""
    fresh = db.get_proposal(pid) or row
    if fresh.get("deleted_at") or fresh.get("followup_disabled_at") or not rules.in_scope(fresh):
        return None
    signals = db.followup_signals(pid) or {}
    return dict(fresh, **{k: v for k, v in signals.items() if k != "pending_task"})


def _partition(candidates: list) -> tuple:
    """(singles, group ids). A candidate is a group candidate while it is an OPEN live member of
    its group; a scope the tool has taken out of the group is an ordinary proposal again; an
    archived one is followed up neither way. A group whose membership cannot be read is skipped
    this tick (fail closed) rather than emailed per proposal."""
    singles, groups, by_gid = [], [], {}
    for row in candidates or []:
        gid = row.get("scope_group")
        if gid and rules.is_open(row):
            by_gid.setdefault(gid, []).append(row)
        else:
            singles.append(row)
    for gid, rows in by_gid.items():
        try:
            all_rows = db.list_scope_group(gid)
            drafts = db.list_scope_group_draft_states(gid)
        except Exception as exc:  # noqa: BLE001
            log.warning("[followup] scope group %s unreadable, skipped this tick: %s", gid, exc)
            continue
        detached = rules.detached_ids(all_rows, drafts)
        live = {m["proposal_id"] for m in rules.live_members(all_rows, drafts)}
        grouped = False
        for r in rows:
            if r.get("proposal_id") in detached:
                singles.append(r)
            elif r.get("proposal_id") in live:
                grouped = True
        if grouped:
            groups.append(gid)
    return singles, groups


def _reserve(pid: str, due, extra: dict | None = None):
    detail = {"audience": due.audience, "template": due.template, "engine": ENGINE_MARK}
    if extra:
        detail.update(extra)
    return db.reserve_followup(pid, due.rule_key, detail)


def _act_on_dues(p: dict, pid: str, dues: list, cfg: dict, now: datetime) -> None:
    """Send what `dues` asks for on one ordinary proposal: reserve, send, release on failure."""
    templates = (cfg or {}).get("templates")
    for due in dues:
        if due.audience == "customer":
            if not _customer_recipients(p):
                # Every contact opted out of follow-ups: nothing to send, and nothing reserved, so
                # nothing is counted as sent either.
                log.info("[followup] %s %s: every contact opted out", pid, due.rule_key)
                continue
            rid = _reserve(pid, due)
            if rid is None:
                continue        # already sent — a prior tick, or the twin container
            ok = _send_customer(p, due, templates)
        else:
            rid = _reserve(pid, due)
            if rid is None:
                continue
            pl = rules.plan(p, now, cfg)
            ok = _send_staff(p, due, {"step": pl.step, "sent": pl.sent,
                                      "skipped_by": pl.skipped_by})
        if not ok:
            # Nothing went out: release the claim and let the next tick try.
            db.delete_followup(rid)
            log.info("[followup] %s %s not sent — reservation released", pid, due.rule_key)
            continue
        log.info("[followup] %s %s -> %s", pid, due.rule_key, due.audience)
        _echo_to_thread(pid, due)


def _process_group(gid: str, now: datetime, cfg: dict) -> None:
    """One scope group's pass: ONE email per recipient per due occurrence, reserved and recorded on
    the group's HOME proposal, and the estimator's note through the same synthetic proposal."""
    view, _why, _members = group_state_for(gid, now)
    if view is None:
        return
    home = view["proposal_id"]
    templates = (cfg or {}).get("templates") or {}
    for due in rules.due_now(view, now, cfg):
        extra = {"scope_group": gid, "scopes": view.get("scopes") or []}
        if due.audience == "customer":
            tpl = templates.get(due.template) or followup_settings.DEFAULT_TEMPLATES[due.template]
            rid = _reserve(home, due, extra)
            if rid is None:
                continue
            delivered = send_group_task_email(view, tpl)
            if not delivered:
                db.delete_followup(rid)
                log.info("[followup] group %s %s not sent — reservation released",
                         gid, due.rule_key)
                continue
            log.info("[followup] group %s %s -> %d recipient(s)", gid, due.rule_key,
                     len(delivered))
            reached = []
            for pids in delivered.values():
                for pid in pids:
                    if pid not in reached:
                        reached.append(pid)
            for pid in reached:
                _echo_to_thread(pid, due, {"scope_group": gid})
        else:
            rid = _reserve(home, due, extra)
            if rid is None:
                continue
            pl = rules.plan(view, now, cfg)
            if not _send_staff(view, due, {"step": pl.step, "sent": pl.sent,
                                           "skipped_by": pl.skipped_by}):
                db.delete_followup(rid)
                continue
            _echo_to_thread(home, due)


# ── the release-day dry run ───────────────────────────────────────────────────

def dry_run_count(now: datetime | None = None) -> dict:
    """What the NEXT tick would do, READ-ONLY: exactly which customers it would email, and every
    estimator email. Run it (backend/ops/followup_dry_run.py) before switching automation on.

    It evaluates what the tick evaluates — the same candidates, the same single/group partition,
    the same sent history, the same schedule, the same opt-outs — and reserves, sends, expires and
    writes NOTHING, whatever FOLLOWUP_AUTOMATION_ENABLED says. An occurrence whose rule key is
    already reserved is left out, as the dedupe index would leave it out.

    Returns:
      at, in_send_window
      customer_emails [{proposal_id, project_name, rule_key, template, step, ordinal,
                        recipients [...], scope_group, scopes}]
      customer_email_count            emails (one per proposal or group, each to its recipients)
      recipient_count                 addresses those emails go to
      opted_out [{proposal_id, rule_key}]   due, but every contact has opted out: nothing goes
      skipped_by_reply [{proposal_id, project_name, step, rule_key, template, contact,
                         contact_at, due_at, would_have_gone_now, scope_group}]
                                      a step's 2nd email cancelled by contact after its 1st
                                      (rules.plan); never sent
      staff_emails [{proposal_id, rule_key, template, scope_group}], staff_email_count
      tasks_to_expire [{task_id, proposal_id, template}]   pending reminder tasks the tick retires
      skipped [...]                   rows it could not evaluate
    `count` is customer_email_count, kept for callers of the old shape."""
    now = now or datetime.now(timezone.utc)
    cfg = _settings()
    out: dict = {"at": now.isoformat(), "in_send_window": rules.in_send_window(now, cfg),
                 "customer_emails": [], "opted_out": [], "staff_emails": [],
                 "skipped_by_reply": [], "tasks_to_expire": [], "skipped": []}

    def _used(pid: str, key: str) -> bool:
        try:
            return db.followup_rule_key_exists(pid, key)
        except Exception:  # noqa: BLE001 — unknown counts as new: over-reporting is the safe side
            return False

    def _simulate(p: dict, pid: str, gid=None, view=None) -> None:
        pl = rules.plan(p, now, cfg)
        if pl.skipped_key:
            out["skipped_by_reply"].append({
                "proposal_id": pid, "project_name": p.get("project_name"), "step": pl.step,
                "rule_key": pl.skipped_key, "template": pl.skipped_template,
                "contact": pl.skipped_by, "contact_at": pl.skipped_at.isoformat(),
                "due_at": pl.skipped_due_at.isoformat(),
                # True when this tick would have sent it, had nobody been in touch.
                "would_have_gone_now": bool(not pl.paused and now >= pl.skipped_due_at
                                            and rules.in_send_window(now, cfg)),
                "scope_group": gid})
        for due in rules.due_now(p, now, cfg):
            if _used(pid, due.rule_key):
                continue
            if due.audience == "customer":
                if view is not None:
                    rec = []
                    for m in view.get("_open_members") or []:
                        for e in _customer_recipients(m):
                            e = (e or "").strip().lower()
                            if e and e not in rec:
                                rec.append(e)
                else:
                    rec = list(_customer_recipients(p))
                if not rec:
                    out["opted_out"].append({"proposal_id": pid, "rule_key": due.rule_key})
                    continue
                out["customer_emails"].append({
                    "proposal_id": pid, "project_name": p.get("project_name"),
                    "rule_key": due.rule_key, "template": due.template, "step": pl.step,
                    "ordinal": (pl.next_ordinal or 0) + 1, "recipients": rec,
                    "scope_group": gid,
                    "scopes": [s.get("label") for s in (view or {}).get("scopes") or []]})
            else:
                out["staff_emails"].append({"proposal_id": pid, "rule_key": due.rule_key,
                                            "template": due.template, "scope_group": gid})

    candidates = db.list_followup_candidates()
    singles, groups = _partition(candidates)
    for row in singles:
        pid = row.get("proposal_id")
        try:
            fresh = _fresh(pid, row)
            if fresh is not None:
                _simulate(fresh, pid)
        except Exception as exc:  # noqa: BLE001
            out["skipped"].append({"proposal_id": pid, "error": str(exc)})
    for gid in groups:
        try:
            view, _why, _m = group_state_for(gid, now)
            if view is not None:
                _simulate(view, view["proposal_id"], gid, view)
        except Exception as exc:  # noqa: BLE001
            out["skipped"].append({"scope_group": gid, "error": str(exc)})
    try:
        pending_tasks = db.list_pending_followup_tasks() or []
    except Exception as exc:  # noqa: BLE001
        pending_tasks = []
        out["skipped"].append({"pending_tasks": True, "error": str(exc)})
    for t in pending_tasks:
        d = t.get("detail") or {}
        if d.get("stage") != rules.DECISION_STAGE:
            out["tasks_to_expire"].append({"task_id": t.get("id"),
                                           "proposal_id": t.get("proposal_id"),
                                           "template": d.get("template")})
            continue
        try:
            ok, _why, _subject = judge_task(t, now)
            created = rules._ts(t.get("created_at"))
            if not ok or created is None:
                continue
            n = int((now - created).total_seconds() // (DECISION_RENAG_DAYS * 86400))
            if n < 1:
                continue
            key = "sdn:%s:%d" % (t["id"], min(n, DECISION_RENAG_MAX))
            if not _used(t["proposal_id"], key):
                out["staff_emails"].append({"proposal_id": t["proposal_id"], "rule_key": key,
                                            "template": "staff_scope_decision",
                                            "scope_group": d.get("scope_group")})
        except Exception as exc:  # noqa: BLE001
            out["skipped"].append({"task_id": t.get("id"), "error": str(exc)})
    out["customer_email_count"] = len(out["customer_emails"])
    out["recipient_count"] = sum(len(e["recipients"]) for e in out["customer_emails"])
    out["staff_email_count"] = len(out["staff_emails"])
    out["count"] = out["customer_email_count"]
    return out
