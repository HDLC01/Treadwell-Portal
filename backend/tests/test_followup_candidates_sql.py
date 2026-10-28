"""The follow-up worker's candidate SQL, RUN over a corpus and diffed against followup_rules.

Since 2026-10-28 follow-ups run only from "proposal sent" until approval: a candidate is a sent or
viewed proposal, enrolled, not switched off, not deleted. Nothing after approval is followed up
(there is no deposit chase), so an approved row is NOT a candidate.

Moved here from test_followup_deposit_stage.py (whose deposit-stage tests went with the stage);
the harness is unchanged, the single-row cases and the mutations now pin the new clause.
"""
import itertools
import json
import re
import sqlite3

import pytest

import followup_rules as fr

# ── the candidate SQL, run rather than read ──────────────────────────────────
# The third implementation of one rule. followup_rules.in_scope is Python and is called above;
# the worker's re-read calls it too; this query cannot, so the only honest way to know the two
# agree is to run BOTH over the same rows and diff the answers.
#
# Everything from here down is one harness and the tests that use it. Read
# test_no_row_the_rules_want_is_dropped_by_the_query first — it is the point, and it says what
# the three source greps that used to live here cost us.

CANDIDATE_COLUMNS = (
    "proposal_id", "proposal_status", "deposit_status", "deposit_required",
    "deposit_requested_at", "followup_enrolled_at", "followup_disabled_at", "deleted_at",
)
STAMP = "2026-08-04 15:00:00+00"

# Every spelling the app actually writes to these columns, crossed: 7 × 5 × 3 × 2⁴ = 1,680 rows.
# Wide enough that no single edit to the predicate can leave the two sides agreeing by luck,
# small enough to build, insert and query in about 20ms. `deposit_required` is tri-state on
# purpose — NULL is a row that predates the column, and those jobs DID take a deposit.
CORPUS = [
    dict(proposal_id="r%04d" % i, proposal_status=st, deposit_status=ds, deposit_required=req,
         deposit_requested_at=inv, followup_enrolled_at=enr, followup_disabled_at=off,
         deleted_at=gone)
    for i, (st, ds, req, inv, enr, off, gone) in enumerate(itertools.product(
        (None, "", "draft", "sent", "viewed", "approved", "closed_lost"),
        (None, "", "pending", "submitted", "received"),
        (None, True, False),
        (None, STAMP), (None, STAMP), (None, STAMP), (None, STAMP)))
]

_TO_JSONB_ARG = re.compile(r"to_jsonb\(\s*(\w+)\s*\)")
_JSON_ARROW = re.compile(r"(to_jsonb\(\s*\w+\s*\))\s*->>\s*'(\w+)'")


def _captured_sql():
    """The SQL db.list_followup_candidates really executes, whitespace-flattened.

    Through db.qall, the pattern test_delete_project.py established, and NOT
    inspect.getsource: getsource hands back Python — the string concatenation and the block of
    `#` comment threaded through this query — so anything matched against it is matched partly
    against prose. Restored in a `finally`, because a stubbed qall leaking into the
    rest of the file would turn a real database read into a silent empty list.

    Hand-rolled rather than monkeypatch so a module-scoped fixture can use it: rebuilding the
    corpus for each of the assertions below is pure waste."""
    import db
    seen = {}
    real = db.qall
    db.qall = lambda sql, params=(): seen.update(sql=sql) or []
    try:
        db.list_followup_candidates()
    finally:
        db.qall = real
    return " ".join(seen["sql"].split())


@pytest.fixture(scope="module")
def candidate_sql():
    return _captured_sql()


@pytest.fixture(scope="module")
def admitted(candidate_sql):
    return _admits(candidate_sql, CORPUS)


def _runnable(sql, con):
    """The same query in a form this runner's SQLite can parse — the OPERATOR rewritten if it
    has to be, never the predicate.

    Native `->>` landed in SQLite 3.38: ubuntu-latest ships 3.45 today, ubuntu-22.04 shipped
    3.37, and CI pins neither. Older runners get json_extract instead. If the rewrite cannot
    cover every `->>` in the query this fails, loudly and by name. It must never turn into a
    skipif: a test that quietly skips on CI reads as passing, which is worse than the grep
    this harness replaced."""
    if "->>" not in sql:
        return sql
    try:
        con.execute("select '{\"a\": 1}' ->> 'a'")
        return sql
    except sqlite3.OperationalError:
        pass
    shimmed = _JSON_ARROW.sub(r"json_extract(\1, '$.\2')", sql)
    assert "->>" not in shimmed, (
        "this runner's SQLite is %s, which predates the ->> operator, and the json_extract "
        "rewrite did not cover every use of it in: %s" % (sqlite3.sqlite_version, sql))
    return shimmed


def _admits(sql, rows, omit=()):
    """The set of proposal_ids this exact SQL text admits, evaluated over `rows` by SQLite.

    Two fixtures let the Postgres text run unedited, which is the whole point — a harness that
    rewrote the predicate to make it runnable would be testing its own rewrite:

      * `attach ... as public`, so the schema-qualified `public.portal_proposals` resolves
        natively instead of being regexed away;
      * to_jsonb's argument provisioned as a COLUMN carrying the row's own JSON, plus an
        identity to_jsonb, so `to_jsonb(x) ->> 'deleted_at'` is really evaluated. The name is
        read out of the query, so aliasing it (`to_jsonb(p)`) is a non-event here — while a
        MISTYPED real column is not healed and still raises "no such column"."""
    cols = [c for c in CANDIDATE_COLUMNS if c not in omit]
    composites = [n for n in sorted(set(_TO_JSONB_ARG.findall(sql))) if n not in cols]
    names = cols + composites
    con = sqlite3.connect(":memory:")
    con.execute("attach database ':memory:' as public")
    con.create_function("to_jsonb", 1, lambda v: v)
    # deposit_required is the one column read as a boolean; INTEGER affinity keeps True/False
    # out of TEXT, where SQLite would be judging the truthiness of the string '0'.
    con.execute("create table public.portal_proposals (%s)" % ", ".join(
        "%s %s" % (c, "int" if c == "deposit_required" else "text") for c in names))
    con.executemany(
        "insert into public.portal_proposals (%s) values (%s)" % (
            ", ".join(names), ", ".join("?" * len(names))),
        [[r.get(c) for c in cols] + [json.dumps({c: r.get(c) for c in cols})] * len(composites)
         for r in rows])
    try:
        cur = con.execute(_runnable(sql, con))
    except sqlite3.OperationalError as e:
        # Kept as an OperationalError so the ALTER test below can still assert on the type,
        # but carrying the two things a reader needs: which name, and which of the two causes.
        raise sqlite3.OperationalError(
            "%s -- the candidate query names something this table has not got. Either it is a "
            "typo, and Postgres would raise UndefinedColumn on every tick of the worker, or a "
            "real column joined the predicate and CANDIDATE_COLUMNS above has to learn about "
            "it.\nquery: %s" % (e, sql))
    pid = [d[0] for d in cur.description].index("proposal_id")
    return {r[pid] for r in cur.fetchall()}


def _in_cadence(row):
    """What the rules want out of this query.

    in_scope is the real shipped predicate. The three guards around it have no Python twin —
    a deleted, un-enrolled or switched-off row never reaches followup_rules at all — so they
    are spelled out once, here, and test_delete_project.py pins the deleted one structurally."""
    return (row.get("deleted_at") is None
            and row.get("followup_enrolled_at") is not None
            and row.get("followup_disabled_at") is None
            and fr.in_scope(row))


def _wanted(rows):
    return {r["proposal_id"] for r in rows if _in_cadence(r)}


def _describe(rows, ids, limit=6):
    """Name the offending rows by the fields that decide their fate, not by opaque ids."""
    by_id = {r["proposal_id"]: r for r in rows}
    keys = ("proposal_status", "deposit_status", "deposit_required", "deposit_requested_at")
    out = [" ".join("%s=%r" % (k, by_id[i].get(k)) for k in keys) for i in sorted(ids)[:limit]]
    if len(ids) > limit:
        out.append("... and %d more" % (len(ids) - limit))
    return "\n  ".join(out)


def _admits_one(sql, **kw):
    """Is this one row a candidate? A sent, enrolled proposal, by default."""
    row = dict(proposal_id="one", proposal_status="sent", deposit_status=None,
               deposit_required=None, deposit_requested_at=None,
               followup_enrolled_at=STAMP, followup_disabled_at=None, deleted_at=None)
    row.update(kw)
    return "one" in _admits(sql, [row])


def test_the_differential_corpus_is_not_vacuous(admitted):
    """Set equality holds trivially between two empty sets, and just as quietly between two
    full ones. This is the assertion that makes the two below mean something."""
    wanted = _wanted(CORPUS)
    assert 0 < len(wanted) < len(CORPUS), "the oracle admits everything or nothing"
    assert 0 < len(admitted) < len(CORPUS), "the query admits everything or nothing"


def test_no_row_the_rules_want_is_dropped_by_the_query(admitted, candidate_sql):
    """The catastrophic direction, and the reason this file no longer greps db.py.

    WHAT WAS HERE BEFORE. `inspect.getsource(db.list_followup_candidates)` and three `in`
    checks — for `'approved'`, for the received stop, for one coalesce. That is the mistake
    that took Active Projects down on production on 2026-08-12 with every test green
    (`STAGE_CREATED`: the string was in the source, the identifier was never bound), and the
    rule it produced is EXECUTE THE RENDERER, NOT ITS SOURCE. Here it failed in both
    directions at once: wrapping one line of the query broke the test, while flipping `<>` to
    `=`, deleting the approved branch, or dropping a coalesce so legacy NULL rows fall out
    all left those three strings sitting in the file and passed.

    So both sides are RUN, over the same rows, and their answers diffed. A row the rules would
    should follow up that this query never returns is never emailed in production — silently."""
    missing = _wanted(CORPUS) - admitted
    assert not missing, (
        "%d rows the rules follow up are dropped before followup_rules ever sees them, so "
        "nobody is emailed about them in production:\n  %s\nquery: %s" % (
            len(missing), _describe(CORPUS, missing), candidate_sql))


def test_the_query_admits_nothing_the_rules_would_not_chase(admitted, candidate_sql):
    """The wasteful direction, and deliberately weaker than the one above.

    The query is allowed to admit rows in_scope declines, and on odd value spellings it does:
    neither side normalises proposal_status. That is what the looser-never-tighter test below
    covers. On the spellings the app ACTUALLY writes, though, the two sets have to match: a row
    the worker re-reads only to decline is a tick spent on nothing, and a candidate set drifting
    wider is how an `and` that became an `or` hides."""
    spurious = admitted - _wanted(CORPUS)
    assert not spurious, (
        "%d rows are candidates that in_scope declines, so every tick re-reads work it will "
        "never do:\n  %s\nquery: %s" % (
            len(spurious), _describe(CORPUS, spurious), candidate_sql))


def test_a_sent_or_viewed_proposal_is_a_candidate(candidate_sql):
    assert _admits_one(candidate_sql, proposal_status="sent")
    assert _admits_one(candidate_sql, proposal_status="viewed")


@pytest.mark.parametrize("status", ["approved", "closed_lost", "draft", None])
def test_nothing_after_a_decision_is_a_candidate(candidate_sql, status):
    """Approved or lost: every follow-up email stops, so the worker never even reads the row.
    `approved` was a candidate until 2026-10-28 (the deposit chase)."""
    assert not _admits_one(candidate_sql, proposal_status=status)


def test_an_approved_job_with_its_deposit_outstanding_is_not_a_candidate(candidate_sql):
    """The row the old deposit chase existed for. No deposit chase from the cadence now."""
    assert not _admits_one(candidate_sql, proposal_status="approved", deposit_status="pending",
                           deposit_required=True)


def test_a_switched_off_or_unenrolled_proposal_is_not_a_candidate(candidate_sql):
    assert not _admits_one(candidate_sql, followup_disabled_at=STAMP)
    assert not _admits_one(candidate_sql, followup_enrolled_at=None)


# The odd-spelling rows: neither in_scope nor the SQL normalises proposal_status, so the two can
# legitimately disagree only in one DIRECTION, and the direction is the invariant — SQL looser is harmless (the worker re-reads and in_scope
# declines), SQL tighter is the outage.
ODD_SPELLINGS = [
    dict(proposal_status=s, deposit_status=d)
    for s in ("sent", "Sent", " sent ", "viewed", "VIEWED", "approved", "Approved")
    for d in ("received", "pending", None)
]


def test_the_query_is_looser_than_the_rules_and_never_tighter(candidate_sql):
    """Coarser than the rules is fine and costs one wasted re-read. Finer is follow-ups silently
    not running, so it is pinned as an inequality."""
    rows = [dict(proposal_id="o%02d" % i, deposit_required=None, deposit_requested_at=None,
                 followup_enrolled_at=STAMP, followup_disabled_at=None, deleted_at=None, **kw)
            for i, kw in enumerate(ODD_SPELLINGS)]
    tighter = _wanted(rows) - _admits(candidate_sql, rows)
    assert not tighter, (
        "the query is STRICTER than in_scope on %d rows, so the rules would chase them and "
        "never get the chance:\n  %s" % (len(tighter), _describe(rows, tighter)))


def test_the_query_still_runs_on_a_database_that_has_not_had_the_ALTER(candidate_sql):
    """Production applies its own DDL by hand (APPLY_SCHEMA_ON_BOOT is false when IS_PROD), so
    the code lands before the column does. This is the worker's ONLY query: a `deleted_at`
    named directly would raise UndefinedColumn on every tick until somebody noticed the
    cadence had gone quiet. An absent jsonb key reads as NULL, which is exactly "not deleted".

    The second half is what makes the first half worth having — it re-introduces the direct
    column reference and watches this harness raise, so the test is known to be able to tell
    the two apart."""
    try:
        live = _admits(candidate_sql, CORPUS, omit=("deleted_at",))
    except sqlite3.OperationalError as e:
        raise AssertionError(
            "the cadence query reads a column that production may not have yet (%s). Read it "
            "through to_jsonb, as list_all_portal_proposals does." % e)
    assert live, "nothing is a candidate on a database without deleted_at"

    named = _JSON_ARROW.sub(r"\2", candidate_sql)
    assert named != candidate_sql, (
        "the deleted_at guard no longer goes through to_jsonb, so a database without the ALTER "
        "takes the follow-up worker down on every tick")
    with pytest.raises(sqlite3.OperationalError):
        _admits(named, CORPUS, omit=("deleted_at",))


def test_the_json_extract_fallback_agrees_with_the_native_operator(candidate_sql, admitted):
    """_runnable's compat path for a runner whose SQLite predates `->>` (3.38: ubuntu-24.04
    ships 3.45, ubuntu-22.04 shipped 3.37, and CI pins neither). A fallback nothing exercises
    is a fallback that is wrong on the day it is first needed — and it would be wrong QUIETLY,
    since the rewrite only has to change the rows admitted to make every diff above lie. So it
    runs on every runner, native or not, and has to reach the same answer."""
    shimmed = _JSON_ARROW.sub(r"json_extract(\1, '$.\2')", candidate_sql)
    assert shimmed != candidate_sql, (
        "there is no `to_jsonb(...) ->> '...'` left in the query for the compat path to rewrite")
    assert _admits(shimmed, CORPUS) == admitted, (
        "json_extract and ->> disagree about this query, so the differential means one thing on "
        "a new runner and another on an old one")


# Constructs whose meaning differs between Postgres and the evaluator above, so a query using
# one would be judged against the wrong semantics. `'t'` is the sharp one: Postgres reads it as
# true, SQLite reads it as 0, so a coalesce default written that way would fail here for a
# reason that has nothing to do with the cadence.
UNMODELLED = ("ilike", " like ", "similar to", "::", " ~ ", "date_trunc", "is distinct from",
              "at time zone", "now()", "interval ", "'t'", "'f'", "'yes'", "'no'", "as boolean")


def test_the_clause_uses_no_construct_this_evaluator_cannot_model(candidate_sql):
    """The one text assertion left in this section, kept deliberately because it fails in the
    SAFE direction: it cannot pass a broken predicate, it can only ask a human to re-check the
    harness when the clause grows something SQLite reads differently."""
    found = [t for t in UNMODELLED if t in candidate_sql]
    assert not found, (
        "the candidate clause now uses %s, which Postgres and this SQLite harness do not agree "
        "about — the differential above is judging it against the wrong semantics. Re-verify "
        "the evaluator (or teach it the construct) before trusting a green run." % found)


def _swap_predicate(sql, predicate):
    """The same query with its WHERE clause replaced wholesale.

    Spelling-independent by construction, which is the point: a mutation written as a literal
    substring stops applying the moment somebody rewraps a line or aliases the table, and a
    meta-test that quietly stops mutating is the failure it exists to prevent."""
    assert " where " in sql and " order by " in sql, (
        "this query no longer has the shape (`... where ... order by ...`) the meta-test "
        "mutates, so the mutation below is silently a no-op: %s" % sql)
    return "%s where %s %s" % (sql[:sql.index(" where ")], predicate,
                              sql[sql.index(" order by "):].lstrip())


# Mutations that survive a refactor because they touch the VALUES the columns hold rather than
# the syntax around them: a status literal cannot be reworded without changing what the query
# means, whatever the clause looks like.
LITERAL_MUTATIONS = (
    ("'sent' dropped from the statuses followed up", "'sent'", "'issued'"),
    ("'viewed' dropped from the statuses followed up", "'viewed'", "'opened'"),
)

# Alias-tolerant, and skipped rather than failed when the clause no longer spells the rule this
# way — the mandatory proofs above already keep the harness honest.
SHAPE_MUTATIONS = (
    ("the switch-off guard inverted",
     r"((?:\w+\.)?followup_disabled_at) is null", r"\1 is not null"),
    ("the enrolment guard inverted",
     r"((?:\w+\.)?followup_enrolled_at) is not null", r"\1 is null"),
)


def test_this_differential_would_notice_the_predicate_breaking(candidate_sql):
    """NON-NEGOTIABLE, and the reason this harness earns its length. It is the only thing
    standing between this file and a silent false green — a differential whose evaluator had
    quietly stopped evaluating would pass every assertion above without a murmur, which is
    precisely how the greps it replaced survived for as long as they did.

    So the query is deliberately broken, four ways that must always apply and two more that
    apply while the clause is still spelled this way, and the harness is required to go red on
    every one. If review pressure trims this section for size, it must not be this."""
    wanted = _wanted(CORPUS)

    for why, predicate in (("a predicate that admits every row", "1=1"),
                           ("a predicate that admits none", "1=0")):
        assert _admits(_swap_predicate(candidate_sql, predicate), CORPUS) != wanted, (
            "the differential cannot tell %s from the real clause, so it is not evaluating "
            "anything" % why)

    for why, before, after in LITERAL_MUTATIONS:
        broken = candidate_sql.replace(before, after)
        assert broken != candidate_sql, (
            "%s is not in the query any more, so this mutation proves nothing. Either the rule "
            "changed and this list needs re-pointing, or the clause has stopped reading that "
            "column." % before)
        assert _admits(broken, CORPUS) != wanted, (
            "the differential does not notice %s, so it has stopped testing anything" % why)

    for why, pattern, repl in SHAPE_MUTATIONS:
        broken, n = re.subn(pattern, repl, candidate_sql)
        if not n:
            continue                      # refactored past this spelling; not a failure
        assert _admits(broken, CORPUS) != wanted, (
            "the differential does not notice %s, so it has stopped testing anything" % why)
