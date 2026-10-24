"""A THROWAWAY Postgres for the tests that execute the portal's SQL for real (tests/test_pg_*.py).

Everything else in this suite stubs the database, so a column name, a jsonb operator, a cast or an
ON CONFLICT that Postgres would reject has never been run anywhere before staging. These helpers
build a scratch database, apply backend/schema.sql through db.run_script (the SAME comment-strip and
semicolon split the boot path uses) on top of a minimal stub of the tool-owned tables, and point
db.py's connection pool at it.

SAFETY. Only ever a scratch server: the URL comes from PORTAL_TEST_PG_URL alone (never DATABASE_URL,
never .env), it must point at localhost, and every database created or dropped here is named
`tw_p3_<...>`, so nothing outside those names is ever touched. Unset, every test using it is
skipped and the default suite stays fast.
"""
from __future__ import annotations

import contextlib
import os
import pathlib
import uuid
from urllib.parse import urlsplit, urlunsplit

import pytest

ENV = "PORTAL_TEST_PG_URL"
PG_URL = (os.environ.get(ENV) or "").strip()
SCHEMA = pathlib.Path(__file__).resolve().parents[1] / "schema.sql"
PREFIX = "tw_p3_"

needs_pg = pytest.mark.skipif(
    not PG_URL, reason=f"{ENV} is not set (a throwaway local Postgres; see test_pg_scope_groups.py)")

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

# The tool-owned tables the portal reads, reduced to what its queries name. Same columns and types
# as the tool's own DDL (treadwell-proposal-tool backend/supabase_schema.sql and
# staging/schema_pg.sql): the portal reads drafts.id / data / owner_email / deleted_at, and
# draft_revisions.project_id / revision_no / data.
TOOL_STUB = (
    "create table if not exists public.drafts ("
    "  id text primary key, data jsonb not null default '{}'::jsonb, owner_email text,"
    "  created_at timestamptz not null default now(),"
    "  updated_at timestamptz not null default now(), deleted_at timestamptz)",
    "create table if not exists public.draft_revisions ("
    "  id bigint generated always as identity primary key, project_id text not null,"
    "  revision_no int not null, data jsonb not null default '{}'::jsonb,"
    "  created_at timestamptz not null default now(), unique (project_id, revision_no))",
)


def _server_url() -> str:
    parts = urlsplit(PG_URL)
    if parts.hostname not in _LOCAL_HOSTS:
        raise RuntimeError(f"{ENV} must point at a throwaway LOCAL Postgres, not {parts.hostname!r}")
    return PG_URL


def url_for(name: str) -> str:
    parts = urlsplit(_server_url())
    return urlunsplit((parts.scheme, parts.netloc, "/" + name, parts.query, parts.fragment))


def _admin():
    import psycopg

    return psycopg.connect(_server_url(), autocommit=True)


def ensure_roles() -> None:
    """The role schema.sql grants to (prod's least-privilege portal_app). Cluster-wide, so created
    once and left: it owns nothing and can log in nowhere."""
    import psycopg

    with _admin() as c:
        if c.execute("select 1 from pg_roles where rolname = 'portal_app'").fetchone():
            return
        try:
            c.execute("create role portal_app nologin")
        except psycopg.errors.DuplicateObject:
            pass


def create_database(tag: str) -> str:
    name = f"{PREFIX}{tag}_{uuid.uuid4().hex[:10]}"
    with _admin() as c:
        c.execute(f'create database "{name}"')
    return name


def drop_database(name: str) -> None:
    if not name.startswith(PREFIX):
        raise RuntimeError(f"refusing to drop {name!r}: not a scratch database")
    with _admin() as c:
        c.execute(f'drop database if exists "{name}" with (force)')


def stub_tool_tables(url: str) -> None:
    import psycopg

    with psycopg.connect(url, autocommit=True) as c:
        for stmt in TOOL_STUB:
            c.execute(stmt)


@contextlib.contextmanager
def portal_pool(url: str):
    """Point db.py at `url` for the duration (db.pool() hands out db._pool once it is set)."""
    import db
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    old = db._pool
    pool = ConnectionPool(url, min_size=1, max_size=8, kwargs={"row_factory": dict_row},
                          open=True, timeout=10)
    db._pool = pool
    try:
        yield pool
    finally:
        db._pool = old
        pool.close()


def apply_schema(text: str | None = None) -> None:
    """schema.sql through db.run_script, exactly as main.py's boot applies it."""
    import db

    db.run_script(text if text is not None else SCHEMA.read_text(encoding="utf-8"))


@contextlib.contextmanager
def scratch_portal_db(tag: str, *, schema: bool = True):
    """A fresh database with the tool stub (and, by default, schema.sql applied once), with db.py
    pointed at it. Dropped afterwards."""
    ensure_roles()
    name = create_database(tag)
    url = url_for(name)
    try:
        stub_tool_tables(url)
        with portal_pool(url) as pool:
            if schema:
                apply_schema()
            yield pool
    finally:
        drop_database(name)
