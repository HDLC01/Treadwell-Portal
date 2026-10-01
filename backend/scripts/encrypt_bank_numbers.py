"""One-off migration: encrypt plaintext ACH routing/account numbers already sitting in
portal_deposits, and backfill routing_masked for rows that predate that column.

Hanz, 2026-09-29: bank_crypto.py stops NEW deposits from ever writing plaintext, but every ACH
deposit taken before this shipped has its routing_number/account_number sitting in the clear in
the same columns bank_crypto now expects to hold `enc:v1:` ciphertext. This script closes that gap
for existing rows, ONE TIME, run by hand -- it is not wired into app startup or schema.sql.

DRY RUN IS THE DEFAULT. Called with no flags, it prints how many rows would be migrated and
changes NOTHING. `--apply` performs the real UPDATE, inside ONE transaction (all matching rows or
none -- see run()), and is safe to run more than once: the candidate query excludes any row whose
routing_number is already `enc:v1:`-prefixed, so a second `--apply` finds zero rows and commits
nothing new. Existing `masked_ref`/`routing_masked` values are never overwritten (COALESCE).

RUN IT LIKE THIS, in the portal container, against the real database, by hand:
    cd /app/backend
    python scripts/encrypt_bank_numbers.py            # dry run -- prints counts only
    python scripts/encrypt_bank_numbers.py --apply     # the real thing

NEVER run this from a test or an agent session against a real database -- it reads DATABASE_URL
from the environment exactly like every other module in this app, so pointing it at anything is a
deliberate, by-hand act, not something a test suite should ever be able to trigger.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

# So `python scripts/encrypt_bank_numbers.py` works regardless of caller's cwd -- unlike `python
# -m X`, a plain script invocation puts only the SCRIPT's own directory on sys.path, not backend/
# itself, and this file's siblings (bank_crypto, db) live one level up.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bank_crypto  # noqa: E402 -- must follow the sys.path fix above
import db  # noqa: E402

log = logging.getLogger("portal.encrypt_bank_numbers")

# Only ACH rows with something in both number columns, and only those NOT already carrying the
# enc:v1: prefix, are candidates -- this single WHERE clause is what makes --apply idempotent.
_CANDIDATES_SQL = (
    "select id, routing_number, account_number, masked_ref, routing_masked "
    "from public.portal_deposits "
    "where method = 'ach' "
    "and routing_number is not null and account_number is not null "
    "and (routing_number not like 'enc:v1:%' or account_number not like 'enc:v1:%') "
    "order by id"
)

_UPDATE_SQL = (
    "update public.portal_deposits "
    "set routing_number = %s, account_number = %s, "
    "routing_masked = coalesce(routing_masked, %s), masked_ref = coalesce(masked_ref, %s) "
    "where id = %s"
)


def plan_update(row: dict[str, Any]) -> dict[str, Any]:
    """Pure function, no DB I/O (bank_crypto's key read aside) -- turns one candidate row into the
    values the UPDATE needs. Encrypts each of routing_number/account_number only if it is not
    ALREADY ciphertext, so a row left half-migrated by an interrupted previous --apply (one field
    encrypted, one not) is finished rather than double-encrypted.

    Masks are derived from the PLAINTEXT value, and only when there is a plaintext value to derive
    them from -- a field that arrives here already encrypted (the candidate query normally
    excludes fully-encrypted rows; this only matters for the mixed-state case above) has nothing
    left to mask, so its half of the mask falls back to whatever is already stored."""
    routing = row.get("routing_number")
    account = row.get("account_number")
    routing_was_plain = not bank_crypto.is_encrypted(routing)
    account_was_plain = not bank_crypto.is_encrypted(account)
    routing_enc = bank_crypto.encrypt(routing) if routing_was_plain else routing
    account_enc = bank_crypto.encrypt(account) if account_was_plain else account
    routing_masked = row.get("routing_masked") or (
        bank_crypto.mask_last4(routing) if routing_was_plain else None
    )
    masked_ref = row.get("masked_ref") or (
        bank_crypto.mask_last4(account) if account_was_plain else None
    )
    return {
        "id": row["id"],
        "routing_number": routing_enc,
        "account_number": account_enc,
        "routing_masked": routing_masked,
        "masked_ref": masked_ref,
    }


def run(apply: bool) -> dict[str, int]:
    """Dry run: SELECT the candidates, report the count, touch nothing else.

    --apply: SELECT + every UPDATE inside the SAME connection, which is the SAME transaction --
    `pool().connection()` commits on a clean `with` exit and rolls back on any exception, so a
    failure on row 50 of 100 leaves the database exactly as it was before this ran, not half
    migrated. This is why the loop below uses `conn.execute` directly rather than db.execute()
    (which opens and commits its OWN connection per call, one transaction per row)."""
    with db.pool().connection() as conn:
        rows = conn.execute(_CANDIDATES_SQL).fetchall()
        if not apply:
            return {"candidates": len(rows), "updated": 0}
        for row in rows:
            plan = plan_update(row)
            conn.execute(_UPDATE_SQL, (
                plan["routing_number"], plan["account_number"],
                plan["routing_masked"], plan["masked_ref"], plan["id"],
            ))
        return {"candidates": len(rows), "updated": len(rows)}


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually encrypt + update. Default is dry-run (prints counts, changes nothing).",
    )
    args = parser.parse_args()
    if not bank_crypto.key_configured():
        log.error("PORTAL_BANK_KEY is missing or invalid -- refusing to run")
        raise SystemExit(1)
    result = run(apply=args.apply)
    if args.apply:
        log.info("encrypted %d of %d candidate row(s)", result["updated"], result["candidates"])
    else:
        log.info(
            "DRY RUN: %d row(s) would be encrypted, 0 changed -- rerun with --apply to migrate",
            result["candidates"],
        )


if __name__ == "__main__":
    main()
