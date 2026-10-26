"""An in-memory stand-in for the database calls the combined scope-group send makes.

Shared by test_scope_groups.py and test_scope_group_send.py. The claim and retry functions run the
SAME pure planning code as db.py (scope_groups.plan_send / retry_targets) over in-memory marks, so
what is under test is the route and the planning rule, with only the SQL replaced.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

import scope_groups

T0 = datetime(2026, 10, 1, 15, 0, tzinfo=timezone.utc)


class FakeSendDB:
    def __init__(self, rows, drafts=None, recipients=None, now=None):
        self.rows = [dict(r) for r in rows]
        # Draft states: ids (all live, real) or full state dicts.
        self.drafts = [d if isinstance(d, dict) else {"id": d, "archived": False}
                       for d in (drafts if drafts is not None
                                 else [r["proposal_id"] for r in self.rows])]
        self.recipients = recipients or {}
        self.marks: list[dict] = []
        self.stashes: list[dict] = []
        self.now = now or (T0 + timedelta(days=1))
        self.draft_data: dict = {}
        self.calls = {"last_sent": [], "enrol": [], "messages": [], "claims": [],
                      "adopted": [], "settled": []}

    # ── reads ────────────────────────────────────────────────────────────────
    def row(self, pid):
        return next((r for r in self.rows if r["proposal_id"] == pid), None)

    def list_scope_group(self, gid):
        return [dict(r) for r in sorted(self.rows, key=lambda r: (r.get("created_at") or T0,
                                                                  r["proposal_id"]))
                if r.get("scope_group") == gid and not r.get("deleted_at")]

    def list_scope_group_draft_states(self, gid):
        return [dict(d) for d in self.drafts]

    def list_scope_group_draft_states_many(self, gids):
        return {g: self.list_scope_group_draft_states(g) for g in gids or []}

    def list_scope_group_drafts(self, gid):
        return scope_groups.split_members(self.list_scope_group_draft_states(gid))[0]

    def list_scope_group_marks(self, gids):
        return [copy.deepcopy(m) for m in self.marks
                if (m["detail"].get("scope_group") in set(gids or []))]

    def get_proposal(self, pid):
        r = self.row(pid)
        return dict(r) if r else None

    def get_draft_data(self, pid):
        return dict(self.draft_data.get(pid) or {})

    def get_recipients(self, pid):
        return list(self.recipients.get(pid, []))

    # ── claims ───────────────────────────────────────────────────────────────
    def _marks_for(self, gid, ids):
        return [m for m in self.marks
                if m["detail"].get("scope_group") == gid and m["proposal_id"] in ids]

    def _add_mark(self, pid, detail, created_at=None):
        mid = len(self.marks) + 1
        self.marks.append({"id": mid, "proposal_id": pid, "detail": detail,
                           "created_at": created_at or self.now})
        return mid

    def claim_scope_group_send(self, gid, pids, by=None, include_unknown=False):
        self.calls["claims"].append(list(pids))
        order = [p for p in dict.fromkeys(pids) if p]
        rows = [self.row(p) for p in order if self.row(p)]
        # Like db.claim_scope_group_send: the whole group's marks (they decide `first`).
        all_ids = {m["proposal_id"] for m in self.marks}
        plan = scope_groups.plan_send(rows, self._marks_for(gid, all_ids), self.now,
                                      include_unknown=include_unknown)
        if not plan["pending"]:
            return None
        n = max([int(m["detail"].get("send_no") or 0) for m in self.marks
                 if m["detail"].get("scope_group") == gid] or [0]) + 1
        revs = {}
        for pid in plan["pending"]:
            rev = self.row(pid).get("current_revision_no")
            revs[pid] = rev
            self._add_mark(pid, {"action": "scope_group_sent", "scope_group": gid, "send_no": n,
                                 "revision_no": rev, "kind": plan["kinds"][pid],
                                 "scopes": plan["pending"]})
        return {"send_no": n, "pending": plan["pending"], "kinds": plan["kinds"],
                "first": plan["first"], "revisions": revs}

    def mark_scope_group_attempt(self, gid, send_no, sent_with):
        self.calls.setdefault("attempts", []).append((send_no, copy.deepcopy(sent_with)))
        for m in self.marks:
            d = m["detail"]
            if (d.get("scope_group") == gid and d.get("send_no") == send_no
                    and m["proposal_id"] in sent_with):
                extra = sent_with[m["proposal_id"]] or {}
                d.update({"attempt_at": self.now.isoformat(), "note": extra.get("note"),
                          "files": list(extra.get("files") or [])})

    def release_scope_group_send(self, gid, send_no):
        self.marks = [m for m in self.marks
                      if not (m["detail"].get("scope_group") == gid
                              and m["detail"].get("send_no") == send_no)]

    # ── a grouped publish's note and files, kept for the combined email ──────
    def stash_scope_group_note(self, pid, gid, rev, note, files, by=None):
        self.stashes.append({"id": 1000 + len(self.stashes), "proposal_id": pid,
                             "detail": {"action": "scope_group_note", "scope_group": gid,
                                        "revision_no": rev, "note": note,
                                        "files": list(files or [])}})

    def list_scope_group_notes(self, gid, pids):
        return [copy.deepcopy(s) for s in self.stashes
                if s["detail"]["scope_group"] == gid and s["proposal_id"] in set(pids)]

    def settle_scope_group_send(self, gid, send_no, delivery):
        self.calls["settled"].append((send_no, copy.deepcopy(delivery)))
        for pid, dv in delivery.items():
            for m in list(self.marks):
                d = m["detail"]
                if (m["proposal_id"] == pid and d.get("scope_group") == gid
                        and d.get("send_no") == send_no):
                    if (dv or {}).get("delivered"):
                        d["delivery"] = {"delivered": list(dv["delivered"]),
                                         "failed": list(dv.get("failed") or [])}
                    else:
                        self.marks.remove(m)

    def claim_scope_group_retry(self, gid, pids):
        ids = set(pids)
        rows = [self.row(p) for p in sorted(ids) if self.row(p)]
        targets = scope_groups.retry_targets(rows, self._marks_for(gid, ids), self.now)
        for t in targets.values():
            m = next(m for m in self.marks if m["id"] == t["mark_id"])
            m["detail"]["retry_at"] = self.now.isoformat()
        return targets

    def settle_scope_group_retry(self, results):
        for mid, dv in results.items():
            m = next(m for m in self.marks if m["id"] == mid)
            m["detail"].pop("retry_at", None)
            m["detail"]["delivery"] = {"delivered": list(dv["delivered"]),
                                       "failed": list(dv["failed"])}

    def adopt_into_scope_group(self, pid, gid, label):
        self.calls["adopted"].append((pid, gid, label))
        r = self.row(pid)
        r["scope_group"], r["scope_label"] = gid, label
        if r.get("last_sent_at") and not self._marks_for(gid, {pid}):
            self._add_mark(pid, {"action": "scope_group_sent", "scope_group": gid, "send_no": 0,
                                 "revision_no": r.get("current_revision_no"), "adopted": True,
                                 "kind": "first", "scopes": [pid]})

    # ── writes the route makes after a send ─────────────────────────────────
    def mark_last_sent(self, pid):
        self.calls["last_sent"].append(pid)
        self.row(pid)["last_sent_at"] = self.now

    def enroll_followup(self, pid, keep_pause=False):
        self.calls["enrol"].append((pid, keep_pause))
        r = self.row(pid)
        r["followup_enrolled_at"] = self.now
        if not keep_pause:
            r["followup_paused_until"] = None

    def add_message(self, pid, kind, who, body, **k):
        self.calls["messages"].append({"pid": pid, "body": body, "meta": k.get("meta")})
        return {"id": 1}

    # ── a later publish, as admin_publish leaves the row ─────────────────────
    def publish_revision(self, pid, rev):
        r = self.row(pid)
        r["current_revision_no"] = rev
        r["proposal_status"] = "sent"

    def install(self, monkeypatch, main):
        for name in ("list_scope_group", "list_scope_group_draft_states",
                     "list_scope_group_draft_states_many", "list_scope_group_drafts",
                     "list_scope_group_marks", "get_proposal", "get_draft_data", "get_recipients",
                     "claim_scope_group_send", "settle_scope_group_send",
                     "mark_scope_group_attempt", "release_scope_group_send",
                     "stash_scope_group_note", "list_scope_group_notes",
                     "claim_scope_group_retry", "settle_scope_group_retry",
                     "adopt_into_scope_group", "mark_last_sent", "enroll_followup",
                     "add_message"):
            monkeypatch.setattr(main.db, name, getattr(self, name), raising=False)
        return self
