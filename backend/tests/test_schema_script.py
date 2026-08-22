"""schema.sql has to survive db.run_script's OWN transform, because that is how it is applied.

WHAT WENT WRONG. The 2026-08-21 `kind` widening was written as a `do $$ ... $ck$ ... $$` block, so
it dropped whatever the old CHECK was called rather than assuming a name. Perfectly reasonable
PL/pgSQL, and unrunnable here: run_script() strips `--` comments and SPLITS THE FILE ON ';', which
tore that block into five fragments. Three are nonsense alone and the first, 'do $$ declare c
record', is a syntax error.

WHY THAT IS NOT COSMETIC. main.py's startup applies schema.sql whenever APPLY_SCHEMA_ON_BOOT is
true, which is the DEFAULT for every non-production ENVIRONMENT, and docker-compose.staging.yml
sets ENVIRONMENT=staging. psycopg rolls the transaction back on the exception, so the next staging
deploy would apply NOTHING in this file: the CHECK never widens (every matrix save 500s),
cleanup_expired never runs, and a fresh local DB gets no tables at all. It surfaces as one
'startup failed' log line.

The file already carried the warning ("Written as plain statements, NOT a `do $$ ... $$` block")
above the deposit_status re-add. A comment is not a guard. This is the guard.

EXECUTED, NOT GREPPED, and specifically executed through the REAL run_script: a test that retyped
the regex and the split would be asserting against its own copy of the rule, and the copy is
exactly what drifts. The pool is stubbed, so the chunks are collected rather than sent anywhere.
"""
import pathlib

import pytest

import db

SCHEMA = pathlib.Path(__file__).resolve().parents[1] / "schema.sql"


@pytest.fixture
def chunker(monkeypatch):
    """Whatever run_script would hand to conn.execute(), in order, without a database.

    Stubbing `db.pool` rather than reimplementing the transform is the whole point: if somebody
    teaches run_script to understand dollar quoting, or to split differently, this follows."""
    def chunks(sql: str) -> list[str]:
        seen: list[str] = []

        class Conn:
            def execute(self, chunk):
                seen.append(chunk)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        class Pool:
            def connection(self):
                return Conn()

        monkeypatch.setattr(db, "pool", lambda: Pool())
        db.run_script(sql)
        return seen

    return chunks


def test_no_chunk_of_the_schema_carries_a_dollar_quote(chunker):
    """THE GUARD. A '$' surviving into a chunk means a dollar-quoted body was cut in half, and the
    whole file stops applying.

    Proven both ways when it was written: it passes on `git show HEAD:backend/schema.sql` (which
    has no dollar quoting) and fails on the working tree before the fix, reporting the three
    fragments 'do $$\\ndeclare c record', 'execute $ck$ ... $ck$' and 'end $$'.

    Comments are stripped BEFORE this looks, which is why the prose further down the file may go on
    naming `do $$ ... $$` as the thing not to write. Only executable text is checked."""
    bad = [c.strip() for c in chunker(SCHEMA.read_text(encoding="utf-8")) if "$" in c]
    assert not bad, "dollar-quoted SQL torn by run_script's split: %r" % (bad[:3],)


def test_the_guard_bites_on_a_dollar_quoted_block(chunker):
    """Proof the assertion above can actually fail, on a two-statement file small enough to read.
    A guard nobody has watched fail is a guard that might be checking nothing."""
    torn = chunker("create table t (a int);\ndo $$ begin perform 1; end $$;\n")
    assert len(torn) > 2, "the block survived the split, so the guard proves nothing"
    assert [c for c in torn if "$" in c]
    assert torn[1].strip() == "do $$ begin perform 1", "not the fragment the real bug produced"


def test_the_kind_widening_is_plain_statements_that_run_in_one_piece(chunker):
    """The widening itself: two whole statements, each arriving intact.

    Named constraint on purpose. Postgres auto-names a column-level check `<table>_<column>_check`,
    so the inline `check (kind in (...))` on the table is portal_notify_recipients_kind_check --
    the same convention the deposit_status and msg_type re-adds in this file already rely on
    against this database."""
    chunks = [" ".join(c.split()) for c in chunker(SCHEMA.read_text(encoding="utf-8"))]
    drop = [c for c in chunks if c == ("alter table public.portal_notify_recipients drop "
                                      "constraint if exists portal_notify_recipients_kind_check")]
    add = [c for c in chunks if c.startswith("alter table public.portal_notify_recipients add "
                                             "constraint portal_notify_recipients_kind_check")]
    assert len(drop) == 1, "the drop is missing or was split"
    assert len(add) == 1, "the add is missing or was split"
    assert chunks.index(drop[0]) < chunks.index(add[0]), "adding before dropping keeps the old one"
    for step in ("sent", "viewed", "question", "status_change", "approved",
                 "deposit_submitted", "deposit_received", "contacts", "feedback"):
        assert "'%s'" % step in add[0], step
    # 'general' is the floor and 'deposit' is the legacy value the resolver still fans out.
    assert "'general'" in add[0] and "'deposit'" in add[0]


def test_the_legacy_migration_copies_only_enabled_rows_and_deletes_only_what_it_copied(chunker):
    """The two data statements, and the two things that make them safe.

    `and r.enabled`: a disabled legacy row meant "never switched on", not "suppress me", so copying
    `enabled` verbatim would bake one dormant row into two permanent suppressions.

    The GUARDED delete: prod applies this out of band with psql, which without ON_ERROR_STOP
    carries on past a failed statement -- so an unguarded delete would still run after a failed
    insert and destroy the only record of who was on the deposit list."""
    chunks = [" ".join(c.split()) for c in chunker(SCHEMA.read_text(encoding="utf-8"))]
    ins = [c for c in chunks if c.startswith("insert into public.portal_notify_recipients")
           and "deposit_submitted" in c]
    dels = [c for c in chunks if c.startswith("delete from public.portal_notify_recipients")]
    assert len(ins) == 1 and len(dels) == 1
    assert "where r.kind = 'deposit' and r.enabled" in ins[0]
    assert "on conflict (kind, lower(email)) do nothing" in ins[0]
    assert "where d.kind = 'deposit'" in dels[0]
    for step in ("deposit_submitted", "deposit_received"):
        assert ("exists (select 1 from public.portal_notify_recipients" in dels[0]
                and "s.kind = '%s'" % step in dels[0]
                or "v.kind = '%s'" % step in dels[0]), step
    assert dels[0].count("exists (select 1 from public.portal_notify_recipients") == 2, (
        "the delete must require BOTH money rows to exist for that address")
