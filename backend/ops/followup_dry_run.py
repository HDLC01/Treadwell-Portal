"""Exactly which customers the follow-up worker's NEXT tick would email, without doing any of it.

Since 2026-10-28 a follow-up is an automatic EMAIL TO THE CUSTOMER (at most 2 per step, until the
proposal is approved). Run this BEFORE follow-up automation is switched on after a release: the
first tick runs 20 seconds after the container boots when FOLLOWUP_AUTOMATION_ENABLED is true, and
once a tick has run its sends are reserved and this reports nothing. So:

  1. deploy with FOLLOWUP_AUTOMATION_ENABLED=false in the box's .env;
  2. inside the running container (WORKDIR is /app/backend):
         docker exec <container> python ops/followup_dry_run.py
     add --json for the JSON alone, and --now <ISO datetime> to evaluate the tick at another
     moment (default: the real clock). Customer emails only go 8am-6pm Central: outside that
     window the list is empty, so pass e.g. --now 2026-10-29T09:00:00 to see the first in-window
     tick;
  3. read the list; when it looks right, set the flag to true and restart the container.

ACTIVATION. Every step's clock starts no earlier than the moment follow-ups were activated
(followup_rules). The summary's second line says which moment this run used: the stored one, or,
when none is stored yet, the --now itself ("not yet activated: the first enabled tick will set it
to ..."), which is what the first enabled tick records. Record it ahead of time with
ops/followup_activate.py --at <ISO>; right after activation the list holds nothing old.

READ-ONLY, three ways: followup_worker.dry_run_count reserves, sends and writes nothing; this
script opens its database pool with every transaction READ ONLY, so a write anywhere underneath
would fail instead of landing; and the one function that sends email is replaced with a refusal.
It works whatever FOLLOWUP_AUTOMATION_ENABLED says.
"""
from __future__ import annotations

import json
import os
import sys

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

READ_ONLY = "-c default_transaction_read_only=on"


def install_read_only_pool(url: str):
    """Point db.py at a pool whose every transaction is READ ONLY. Returns the pool."""
    import db
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    pool = ConnectionPool(url, min_size=1, max_size=2, open=True, timeout=30,
                          kwargs={"row_factory": dict_row, "options": READ_ONLY})
    db._pool = pool
    return pool


def refuse_email() -> None:
    import email_sender

    def _refused(*a, **k):
        raise RuntimeError("the follow-up dry run never sends email")

    email_sender._send = _refused


def table(result: dict) -> str:
    """The short human summary printed after the JSON: every customer the tick would email."""
    if result.get("activation_error"):
        activation = ("  activation time UNREADABLE: the tick would send nothing (%s)"
                      % result.get("activation_error"))
    elif result.get("activation_pending"):
        activation = ("  not yet activated: the first enabled tick will set it to %s"
                      % result.get("at"))
    else:
        activation = "  activated at ..................... %s" % result.get("activated_at")
    lines = [
        "follow-up dry run at %s (%s the customer send window)"
        % (result.get("at"), "inside" if result.get("in_send_window") else "OUTSIDE"),
        activation,
        "  customer emails it would send ..... %d (to %d addresses)"
        % (len(result.get("customer_emails") or []), int(result.get("recipient_count") or 0)),
        "  due but every contact opted out ... %d" % len(result.get("opted_out") or []),
        "  2nd email skipped, they replied ... %d (%d would have gone this tick)"
        % (len(result.get("skipped_by_reply") or []),
           sum(1 for s in result.get("skipped_by_reply") or [] if s.get("would_have_gone_now"))),
        "  estimator emails it would send .... %d" % len(result.get("staff_emails") or []),
        "  steps done before activation ...... %d (no estimator email for them)"
        % len(result.get("done_before_activation") or []),
        "  old reminder tasks it would expire  %d" % len(result.get("tasks_to_expire") or []),
        "  rows it could not evaluate ........ %d" % len(result.get("skipped") or []),
    ]
    by_template: dict = {}
    for e in result.get("staff_emails") or []:
        by_template[e.get("template")] = by_template.get(e.get("template"), 0) + 1
    for tpl in sorted(by_template):
        lines.append("    %-32s %d" % (tpl, by_template[tpl]))
    if result.get("customer_emails"):
        lines.append("")
        lines.append("  %-30s %-13s %-5s %s" % ("project", "template", "email", "to"))
        for e in result["customer_emails"]:
            lines.append("  %-30s %-13s %-5s %s" % (
                (e.get("project_name") or e.get("proposal_id") or "")[:30], e.get("template"),
                "%s" % e.get("ordinal"), ", ".join(e.get("recipients") or [])))
    if result.get("skipped_by_reply"):
        lines.append("")
        lines.append("  %-30s %-13s %-15s %s" % ("skipped (2nd email)", "template", "because",
                                                 "contact at"))
        for s in result["skipped_by_reply"]:
            lines.append("  %-30s %-13s %-15s %s" % (
                (s.get("project_name") or s.get("proposal_id") or "")[:30], s.get("template"),
                s.get("contact"), s.get("contact_at")))
    return "\n".join(lines)


def parse_now(argv: list):
    """The `--now <ISO datetime>` (or `--now=<ISO>`) option: the moment to evaluate the tick at,
    default None = the real clock. A value without an offset is read as Central time (the business
    timezone every user-facing date uses). Raises ValueError on a missing or unreadable value."""
    from datetime import datetime

    raw = None
    for i, a in enumerate(argv):
        if a == "--now":
            if i + 1 >= len(argv):
                raise ValueError("--now needs an ISO datetime, e.g. 2026-10-27T12:00:00-05:00")
            raw = argv[i + 1]
        elif a.startswith("--now="):
            raw = a[len("--now="):]
    if raw is None:
        return None
    try:
        when = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("--now %r is not an ISO datetime" % raw) from None
    if when.tzinfo is None:
        import followup_rules
        when = when.replace(tzinfo=followup_rules.BUSINESS_TZ)
    return when


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        now = parse_now(argv)
    except ValueError as exc:
        print("followup_dry_run: %s" % exc, file=sys.stderr)
        return 2
    import config
    import followup_worker

    install_read_only_pool(config.DATABASE_URL)
    refuse_email()
    result = followup_worker.dry_run_count(now)
    print(json.dumps(result, default=str, indent=2))
    if "--json" not in argv:
        print()
        print(table(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
