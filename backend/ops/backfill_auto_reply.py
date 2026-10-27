"""Mark the out-of-office replies recorded BEFORE remind-only shipped (fix round #13).

The inbound relay now flags an autoresponder's row with meta.auto_reply, and the follow-up schedule
ignores flagged rows when it looks for the customer's last reply (db._CUSTOMER_TOUCH_SQL): an
out-of-office is not the customer coming back to us, and counting it resets their reminder count
(decision 3). Rows the OLD relay stored carry no flag, so every out-of-office already in the
database still counts as a reply. This finds them and flags them.

    docker exec <container> python ops/backfill_auto_reply.py           # DRY RUN (the default)
    docker exec <container> python ops/backfill_auto_reply.py --apply   # write the flags

The dry run opens a READ-ONLY connection and prints how many rows would be flagged and every match.
--apply writes `meta.auto_reply = true` (plus `auto_reply_backfill` = today, so the change can be
found and undone) on exactly those rows, in ONE transaction. Running it again flags nothing new.

CONSERVATIVE ON PURPOSE. The headers the live check reads (inbound.is_auto_reply) were never stored,
and neither was the subject: an old row is a body and a sender. A false positive hides a real reply
from the schedule (the row stays in the thread either way), so a body must say BOTH "I am out of the
office / on vacation / on leave" AND something only an autoresponder says ("I will return on",
"limited access to email", "for urgent matters"), or carry an explicit "this is an automated reply",
and be short. Read the dry run's list before --apply.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import inbound  # noqa: E402

READ_ONLY = "-c default_transaction_read_only=on"
# Long bodies are conversations, not autoresponders.
MAX_LEN = 1200

_APOS = "['’]"
_STRONG = re.compile(r"\bthis is an? automat(?:ed|ic) (?:reply|response|message|e-?mail|notification)\b",
                     re.IGNORECASE)
_AWAY = re.compile(
    r"\b(?:I am|I" + _APOS + r"m|I will be|I" + _APOS + r"ll be|I shall be)\s+"
    r"(?:currently\s+|now\s+|presently\s+)?"
    r"(?:out of (?:the )?office|away from (?:the|my) (?:office|desk)|"
    r"on (?:annual |parental |maternity |paternity |sick |medical )?(?:vacation|holiday|leave|PTO))\b",
    re.IGNORECASE)
_BACK = re.compile(
    r"\b(?:(?:will|shall) (?:return|be back)(?: (?:to|in) (?:the|my) office)? (?:on|by|after|until)|"
    r"returning (?:to the office )?on|"
    r"back in the office|back in office|limited access to (?:my )?e-?mail|"
    r"no access to (?:my )?e-?mail|"
    r"(?:will|shall) (?:respond|reply|get back to you)[^.]{0,40}(?:upon|when|on) my return|"
    r"for (?:urgent|immediate) (?:matters|assistance|requests|issues|inquiries))\b",
    re.IGNORECASE)

SELECT_SQL = (
    "select id, proposal_id, created_at, body, meta from public.portal_questions "
    " where author_kind = 'customer' and coalesce(meta ->> 'source', '') = 'email' "
    "   and coalesce((meta ->> 'auto_reply')::boolean, false) is not true "
    " order by id")
APPLY_SQL = (
    "update public.portal_questions "
    "   set meta = coalesce(meta, '{}'::jsonb) || jsonb_build_object("
    "         'auto_reply', true, 'auto_reply_backfill', %s::text) "
    " where id = any(%s) and author_kind = 'customer' "
    "   and coalesce((meta ->> 'auto_reply')::boolean, false) is not true")


def _first_line(body: str) -> str:
    for line in (body or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def auto_reply_reason(subject, body) -> str | None:
    """Why this stored row looks like an autoresponder, or None. Pure."""
    if subject and inbound.is_auto_reply(str(subject)):
        return "subject"
    text = (body or "").strip()
    if not text or len(text) > MAX_LEN:
        return None
    if inbound.is_auto_reply(_first_line(text)) and (_AWAY.search(text) or _BACK.search(text)
                                                     or _STRONG.search(text)):
        return "first line"
    if _STRONG.search(text):
        return "automated-reply sentence"
    if _AWAY.search(text) and _BACK.search(text):
        return "away and return sentences"
    return None


def find(conn) -> list[dict]:
    """The unflagged inbound customer rows that look like autoresponders."""
    out = []
    for r in conn.execute(SELECT_SQL).fetchall():
        meta = r.get("meta") or {}
        subject = meta.get("subject") if isinstance(meta, dict) else None
        why = auto_reply_reason(subject, r.get("body"))
        if why:
            out.append({"id": r["id"], "proposal_id": r["proposal_id"],
                        "created_at": r["created_at"], "reason": why,
                        "subject": subject or _first_line(r.get("body") or "")[:120]})
    return out


def apply(conn, ids: list[int], stamp: str) -> int:
    """Flag `ids` in ONE transaction. Returns how many rows changed."""
    if not ids:
        return 0
    with conn.transaction():
        cur = conn.execute(APPLY_SQL, (stamp, list(ids)))
        return cur.rowcount


def run(url: str, do_apply: bool, out=None) -> dict:
    import psycopg
    from psycopg.rows import dict_row

    out = out or sys.stdout
    kwargs = {"row_factory": dict_row}
    if not do_apply:
        kwargs["options"] = READ_ONLY
    with psycopg.connect(url, **kwargs) as conn:
        matches = find(conn)
        print("%s: %d inbound customer row(s) look like automatic replies"
              % ("APPLY" if do_apply else "DRY RUN", len(matches)), file=out)
        for m in matches:
            print("  #%s  %s  %s  [%s]  %s" % (m["id"], m["proposal_id"], m["created_at"],
                                              m["reason"], m["subject"]), file=out)
        changed = 0
        if do_apply:
            changed = apply(conn, [m["id"] for m in matches], date.today().isoformat())
            print("flagged %d row(s) with meta.auto_reply" % changed, file=out)
        else:
            print("nothing written. Re-run with --apply to flag these rows.", file=out)
    return {"matches": matches, "changed": changed}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true",
                    help="write meta.auto_reply on the matched rows (default: dry run)")
    args = ap.parse_args(argv)
    import config

    run(config.DATABASE_URL, args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
