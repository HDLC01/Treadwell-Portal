"""Record WHEN automatic follow-ups were activated, for the release runbook.

Every follow-up step's clock starts no earlier than this moment (followup_rules, "THE CLOCK STARTS
AT ACTIVATION"): a proposal sent weeks before the release gets its 1st email N days after
activation, not on the first tick. The first tick that runs with FOLLOWUP_AUTOMATION_ENABLED=true
records its own clock when nothing is stored; this script records it explicitly instead, so the
runbook can fix the moment before the flag is switched on:

    docker exec <container> python ops/followup_activate.py --show
    docker exec <container> python ops/followup_activate.py --at 2026-10-30T08:00:00
    docker exec <container> python ops/followup_dry_run.py --now 2026-10-30T09:00:00

  --at <ISO>   the moment (default: now). Without an offset it is read as Central time.
  --show       print what is stored and change nothing.
  --replace    move a moment that is already stored. Without it a stored moment is never changed.
  --by <name>  recorded as portal_settings.updated_by (default: ops/followup_activate.py).

IDEMPOTENT: when nothing is stored it records --at; when the same moment is stored it says so and
changes nothing; when a DIFFERENT moment is stored it refuses (exit 3) unless --replace. Moving the
moment EARLIER can make old proposals due at once, which is what it exists to prevent. Every run
prints what it found and what it set. It never sends email.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

DEFAULT_BY = "ops/followup_activate.py"


def _opt(argv: list, name: str):
    """The value of `--name <v>` or `--name=<v>`, None when absent. ValueError when it has none."""
    raw = None
    for i, a in enumerate(argv):
        if a == name:
            if i + 1 >= len(argv) or argv[i + 1].startswith("--"):
                raise ValueError("%s needs a value" % name)
            raw = argv[i + 1]
        elif a.startswith(name + "="):
            raw = a[len(name) + 1:]
    return raw


def parse_at(raw: str) -> datetime:
    """An ISO datetime as an aware UTC datetime. No offset = Central (the business timezone)."""
    try:
        when = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("--at %r is not an ISO datetime, e.g. 2026-10-30T08:00:00" % raw) from None
    if when.tzinfo is None:
        import followup_rules
        when = when.replace(tzinfo=followup_rules.BUSINESS_TZ)
    return when.astimezone(timezone.utc)


def _fmt(at) -> str:
    if at is None:
        return "not yet activated"
    import followup_rules
    return "%s (%s Central)" % (at.isoformat(),
                                at.astimezone(followup_rules.BUSINESS_TZ).strftime("%Y-%m-%d %H:%M"))


def run(argv: list, now: datetime | None = None) -> int:
    """The command, against whatever database db.py points at. Returns the exit code."""
    try:
        raw_at = _opt(argv, "--at")
        by = _opt(argv, "--by") or DEFAULT_BY
        at = parse_at(raw_at) if raw_at is not None else \
            (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    except ValueError as exc:
        print("followup_activate: %s" % exc, file=sys.stderr)
        return 2
    import db
    import followup_settings
    import followup_worker

    stored = followup_worker.read_activation()
    print("stored activated_at: %s" % _fmt(stored))
    if "--show" in argv:
        return 0
    if stored is None:
        got = followup_worker.claim_activation(at, by)
        if got == at:
            print("set activated_at to %s" % _fmt(got))
            return 0
        # Somebody (the worker's first enabled tick) recorded one between our read and our write.
        print("not set: activated_at was recorded meanwhile as %s" % _fmt(got))
        return 3
    if stored == at:
        print("unchanged: activated_at is already %s" % _fmt(stored))
        return 0
    if "--replace" not in argv:
        print("refused: activated_at is already %s; pass --replace to move it to %s"
              % (_fmt(stored), _fmt(at)), file=sys.stderr)
        return 3
    db.save_settings(followup_settings.ACTIVATION_ROW_ID,
                     followup_settings.activation_value(at, by), by)
    got = followup_worker.read_activation()
    print("moved activated_at from %s to %s" % (_fmt(stored), _fmt(got)))
    if got is not None and got < stored:
        print("warning: the moment moved EARLIER; run ops/followup_dry_run.py before the next tick")
    return 0


def main(argv=None) -> int:
    return run(list(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    sys.exit(main())
