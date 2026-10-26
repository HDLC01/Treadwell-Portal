"""Scope groups: who is a member, what the customer has been told, and what a scope is called.

Pure: no database, no clock of its own. db.py, main.py and followup_rules.py all read membership and
"has this scope been emailed" through these functions, so the combined send, the board, the
follow-up worker and the customer's money pages cannot disagree about either.

RJ's "one project, many proposals, one email" (build spec 2026-10-24, P2 send lifecycle).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

# ── membership ───────────────────────────────────────────────────────────────
# The tool's crm-core.js nameLooksLikeTest, character for character. It decides test-ness only when
# nobody has said: `data.is_test` true or false wins in both directions. Narrow on purpose -- "demo"
# lives inside "demolition".
_TEST_WORD = re.compile(r"\b(sample|test|verify|demo|qa|bugtest)\b", re.I)
_DELETE_ME = re.compile(r"delete me", re.I)
_ZZ = re.compile(r"^\s*zz", re.I)


def _flag(v: Any) -> Optional[bool]:
    if isinstance(v, bool):
        return v
    s = str(v if v is not None else "").strip().lower()
    if s in ("true", "t", "1", "yes"):
        return True
    if s in ("false", "f", "0", "no"):
        return False
    return None


def draft_is_test(state: dict) -> bool:
    """Is this draft a Test project? `state` carries the draft's raw `is_test` and `project_name`."""
    flag = _flag((state or {}).get("is_test"))
    if flag is not None:
        return flag
    name = str((state or {}).get("project_name") or "")
    return bool(_TEST_WORD.search(name) or _DELETE_ME.search(name) or _ZZ.search(name))


def split_members(states: Optional[list]) -> tuple[list, list]:
    """`(live, parked)` draft ids of one scope group. THE one membership rule.

    `states` is every NOT-DELETED draft carrying the group: `[{"id", "archived", "is_test",
    "project_name"}]`. A LIVE member is not archived and of the group's kind. A group is a REAL
    project as soon as one active draft in it is real, and then a Test draft in it is never a member
    (Hanz's decision 5: a Test project may not join a real customer's group); a group whose active
    drafts are all Test is a test group, and a real draft is never in one. PARKED drafts (archived,
    or the wrong kind) are still in the group -- never emailed on their own -- but are not chased,
    not expected by the combined send, and not emailed with it."""
    rows = [s for s in (states or []) if isinstance(s, dict) and s.get("id")]
    active = [s for s in rows if not _flag(s.get("archived"))]
    group_is_test = bool(active) and all(draft_is_test(s) for s in active)
    live = [s["id"] for s in active if draft_is_test(s) == group_is_test]
    taken = set(live)
    parked = [s["id"] for s in rows if s["id"] not in taken]
    return live, parked


# ── what the customer has been told ──────────────────────────────────────────
# A scope is ANNOUNCED when a `scope_group_sent` mark (a staff_note row, db.claim_scope_group_send)
# records that the combined email reached at least one person at the scope's CURRENT revision.
# A revision published after that is pending again, which is how "Email again" names only the new
# and the revised scopes (Hanz's decision 3).
INFLIGHT_TTL = timedelta(minutes=10)


def _ts(v: Any) -> Optional[datetime]:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, str) and v:
        try:
            t = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    return None


def _same_rev(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is None and b is None
    try:
        return int(a) == int(b)
    except (TypeError, ValueError):
        return False


def mark_status(mark: dict, now: datetime) -> Optional[str]:
    """'counts', 'inflight', 'unknown' or None for one scope_group_sent mark.

    - no `revision_no`: written before revisions were recorded. It counts, at an unknown revision.
    - `adopted`: a scope that had been emailed as an ordinary proposal before it joined the group.
    - `delivery` recorded: counts only if somebody actually got it.
    - neither: a send in progress. It holds the scope for INFLIGHT_TTL (so a double click cannot
      mail twice). After that it depends on `attempt_at`, stamped just before the first email:
      without it the send died before mailing anybody, and the mark is ignored; WITH it the emails
      may well have gone out and only the result was lost (a restart mid-loop, a database blip on
      settle). That is 'unknown': never announced, but never pending again on its own either --
      a person decides (the send's `resend_unknown`), because guessing wrong mails the customer
      the same proposals twice."""
    d = (mark or {}).get("detail") or {}
    if "revision_no" not in d:
        return "counts"
    if d.get("adopted"):
        return "counts"
    if "delivery" in d:
        return "counts" if ((d.get("delivery") or {}).get("delivered")) else None
    made = _ts((mark or {}).get("created_at"))
    if made is not None and now - made < INFLIGHT_TTL:
        return "inflight"
    if d.get("attempt_at"):
        return "unknown"
    return None


def scope_states(rows: list, marks: list, now: datetime) -> dict:
    """{proposal_id: {"announced", "inflight", "ever"}} for published scopes.

    `rows` are portal rows (proposal_id, current_revision_no, last_sent_at); `marks` the group's
    scope_group_sent marks for them. `ever` = the customer has had SOME version of this scope (a
    pending scope that has is REVISED, one that has not is NEW).

    A row with no mark at all but a `last_sent_at` was emailed as an ordinary proposal before it
    joined the group (a project sent, then given "New scope"). It counts as announced, at an
    unknown revision, the same as an old mark."""
    by: dict = {}
    for m in marks or []:
        by.setdefault((m or {}).get("proposal_id"), []).append(m)
    out = {}
    for r in rows or []:
        pid = r.get("proposal_id")
        ms = by.get(pid, [])
        rev = r.get("current_revision_no")
        counting = [m for m in ms if mark_status(m, now) == "counts"]
        inflight = any(mark_status(m, now) == "inflight" for m in ms)
        # Only an unsettled mark at the CURRENT revision: a lost result about revision 1 says
        # nothing about whether revision 2 has gone out.
        unknown = any(mark_status(m, now) == "unknown"
                      and _same_rev((m.get("detail") or {}).get("revision_no"), rev) for m in ms)
        announced = any("revision_no" not in (m.get("detail") or {})
                        or _same_rev((m.get("detail") or {}).get("revision_no"), rev)
                        for m in counting)
        ever = bool(counting)
        if not ms and r.get("last_sent_at"):
            announced = ever = True
        out[pid] = {"announced": announced, "inflight": inflight and not announced, "ever": ever,
                    "unknown": unknown and not announced and not inflight}
    return out


def plan_send(rows: list, marks: list, now: datetime, include_unknown: bool = False) -> dict:
    """What a combined send would email right now, for the group's published LIVE scopes `rows`.

    `pending`: not announced and not already being sent, in `rows` order. `first`: nothing in the
    group has ever reached the customer. `kinds`: per pending scope, 'first' on the first send,
    otherwise 'new' or 'revised'. `unknown`: scopes whose last send lost its result (mark_status);
    they are left out of `pending` unless `include_unknown` (a person said send them again).

    `first` also reads the marks of scopes NOT in `rows` (closed lost, deleted, taken out of the
    group since): once anything of the project has reached the customer, no later send is the
    first one again, even when every scope it announced has gone (round 3 contract)."""
    st = scope_states(rows, marks, now)
    first = (not any(s["ever"] for s in st.values())
             and not any(mark_status(m, now) == "counts" for m in marks or []))
    pending, kinds, unknown = [], {}, []
    for r in rows or []:
        pid = r.get("proposal_id")
        s = st.get(pid) or {}
        if s.get("announced") or s.get("inflight"):
            continue
        if s.get("unknown"):
            unknown.append(pid)
            if not include_unknown:
                continue
        pending.append(pid)
        kinds[pid] = "first" if first else ("revised" if s.get("ever") else "new")
    return {"pending": pending, "kinds": kinds, "first": first, "states": st, "unknown": unknown}


def retry_targets(rows: list, marks: list, now: datetime) -> dict:
    """{proposal_id: {"mark_id", "failed", "kind"}}: the addresses that did NOT get a scope's
    latest delivered email at its current revision, for `retry_failed`. Only those addresses are
    mailed again, never everybody (finding #16). A mark another retry is working on (`retry_at`
    within INFLIGHT_TTL) is left alone, so a double click cannot mail them twice."""
    by: dict = {}
    for m in marks or []:
        by.setdefault((m or {}).get("proposal_id"), []).append(m)
    out = {}
    for r in rows or []:
        pid = r.get("proposal_id")
        rev = r.get("current_revision_no")
        best = None
        for m in by.get(pid, []):
            d = m.get("detail") or {}
            if "delivery" not in d or mark_status(m, now) != "counts":
                continue
            if "revision_no" in d and not _same_rev(d.get("revision_no"), rev):
                continue
            if best is None or (m.get("id") or 0) > (best.get("id") or 0):
                best = m
        if best is None:
            continue
        d = best.get("detail") or {}
        failed = [e for e in ((d.get("delivery") or {}).get("failed") or []) if e]
        busy = _ts(d.get("retry_at"))
        if not failed or (busy is not None and now - busy < INFLIGHT_TTL):
            continue
        out[pid] = {"mark_id": best.get("id"), "failed": failed, "kind": d.get("kind") or "first",
                    "delivered": list((d.get("delivery") or {}).get("delivered") or []),
                    # What the original email carried (main._scope_send_extras), so a retry sends
                    # the same note and files rather than a bare list of links.
                    "note": d.get("note") or None, "files": list(d.get("files") or [])}
    return out


# ── the Files page's note and files for a scope, held for the combined email ────
# A grouped publish emails nobody (the combined send does), so the note and the files the estimator
# gave that publish are kept as a `scope_group_note` staff_note row (db.stash_scope_group_note) and
# picked up by the next combined send that names the scope (finding #17).
def stashed_extras(rows: list, stashes: list) -> dict:
    """{proposal_id: {"note", "files"}}: the NEWEST stash of each scope in `rows` made for its
    CURRENT revision. One made for an older revision belongs to an email that has gone, or never
    will; a stash with neither a note nor a file is nothing."""
    by: dict = {}
    for s in stashes or []:
        by.setdefault((s or {}).get("proposal_id"), []).append(s)
    out = {}
    for r in rows or []:
        pid = r.get("proposal_id")
        rev = r.get("current_revision_no")
        best = None
        for s in by.get(pid, []):
            d = s.get("detail") or {}
            if not _same_rev(d.get("revision_no"), rev):
                continue
            if best is None or (s.get("id") or 0) > (best.get("id") or 0):
                best = s
        if best is None:
            continue
        d = best.get("detail") or {}
        note = str(d.get("note") or "").strip() or None
        files = [f for f in (d.get("files") or []) if isinstance(f, dict) and f.get("id")]
        if note or files:
            out[pid] = {"note": note, "files": files}
    return out


def send_rows(rows: list, states: Optional[list]) -> list:
    """The group's portal rows that a combined send speaks for: published, not deleted, not closed
    lost, and a LIVE member (split_members). With no draft at all there is nothing to compare
    against and the stored rows stand, as everywhere else."""
    live, parked = split_members(states)
    live_set = set(live)
    known = live_set | set(parked)
    out = []
    for r in rows or []:
        if not r or r.get("deleted_at"):
            continue
        if (r.get("proposal_status") or "") == "closed_lost":
            continue
        if known and r.get("proposal_id") not in live_set:
            continue
        out.append(r)
    return out


# ── what a scope is called where money changes hands ─────────────────────────
def display_project_name(p: Optional[dict], name: Optional[str] = None) -> str:
    """"Acme HQ - Lab 1" for one scope of a multi-scope project; the plain project name otherwise.

    For the deposit reference, the invoice's job name, the signed-contract sentence and file name,
    and the approval emails (finding #21): three scopes of one project are three amounts, and a
    cheque or a contract that only says "Acme HQ" cannot say which. A plain hyphen, not a dash: the
    name ends up in a cheque memo and in a file name. `name` overrides the row's project name (the
    signing sentence prefers the draft's); "" when there is no name at all."""
    p = p or {}
    base = str(name if name is not None else (p.get("project_name") or "")).strip()
    label = str(p.get("scope_label") or "").strip() if p.get("scope_group") else ""
    if label and base and label.lower() != base.lower():
        return f"{base} - {label}"
    return base or label
