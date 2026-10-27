"""What the follow-up worker's NEXT tick would do, without doing any of it.

Run it BEFORE follow-up automation is switched on after a deploy (fix round #12/#18/#4): the first
tick runs 20 seconds after the container boots when FOLLOWUP_AUTOMATION_ENABLED is true, and once a
tick has run, its tasks are reserved and this reports nothing. So:

  1. deploy with FOLLOWUP_AUTOMATION_ENABLED=false in the box's .env;
  2. inside the running container (WORKDIR is /app/backend):
         docker exec <container> python ops/followup_dry_run.py
     add --json for the JSON alone, and --now <ISO datetime> to evaluate the tick at another
     moment (default: the real clock; customer reminders only fall due 8am-6pm Central);
  3. read the counts; when they look right, set the flag to true and restart the container.

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
    """The short human summary printed after the JSON."""
    lines = [
        "follow-up dry run at %s" % result.get("at"),
        "  tasks the tick would raise ........ %d" % len(result.get("tasks") or []),
        "    of which touched (deploy burst) . %d" % len(result.get("touched_tasks") or []),
        "  estimator emails it would send .... %d" % len(result.get("staff_emails") or []),
        "  finished deposit chases restarted . %d" % len(result.get("deposit_restarts") or []),
        "  finished deposit chases held ...... %d" % len(result.get("deposit_held") or []),
        "  rows it could not evaluate ........ %d" % len(result.get("skipped") or []),
    ]
    by_template: dict = {}
    for e in result.get("staff_emails") or []:
        by_template[e.get("template")] = by_template.get(e.get("template"), 0) + 1
    for tpl in sorted(by_template):
        lines.append("    %-32s %d" % (tpl, by_template[tpl]))
    if result.get("tasks"):
        lines.append("")
        lines.append("  %-38s %-12s %s" % ("proposal", "template", "rule key"))
        for t in result["tasks"]:
            lines.append("  %-38s %-12s %s" % (t.get("proposal_id"), t.get("template"),
                                               t.get("rule_key")))
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
