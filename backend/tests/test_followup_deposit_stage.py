"""The cadence does not stop at approval — it stops at the deposit.

Hanz, 2026-08-12: **"remember followups should be automated until a deposit has been received."**

WHAT IT WAS BEFORE. `due_now` fired only while `proposal_status` was `sent` or `viewed`,
`db.list_followup_candidates` excluded approved rows in SQL, and the tool's 6am digest skipped them
with a comment claiming "the deposit column and its own reminders own that". There were no deposit
reminders. An approved job got one invoice email and then silence on every channel we have — which
is the exact failure the whole follow-up system exists to prevent, arriving one stage later.

THE TWO AUDIENCES STOP AT DIFFERENT POINTS. His choice, offered the alternatives:

  * the CUSTOMER stops at `submitted`. Once they have recorded ACH details or told us the cheque is
    posted, the money is in flight; chasing then reads as a mistake or a second charge.
  * the ESTIMATOR keeps going until `received`. A cheque that never arrives must not go quiet.

That split is the thing most likely to be "simplified" into one stop point by somebody reading this
engine later, so it is asserted from both sides.

WHY THE STAGE RETURNS INSTEAD OF FALLING THROUGH. An approved proposal has `cycle_viewed_at` set,
so the viewed branch would happily chase it with "getting you on the schedule — we need your signed
approval and the deposit". On a job that is already signed.

WHY THIS STAGE ALONE SUPPRESSES THE STATUS ASK. Every other recurring nudge offers "has your
timeline changed?", whose second button reads "Not moving forward" and sets closed_lost. On signed,
invoiced work that is a one-click cancel, and a stray tap kills a won job.
"""
import itertools
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import followup_rules as fr
import followup_settings as fs

ENROLLED = datetime(2026, 8, 1, 14, 0, tzinfo=timezone.utc)
APPROVED = datetime(2026, 8, 4, 15, 0, tzinfo=timezone.utc)


def _row(**kw):
    """An approved proposal with its deposit outstanding, at the default cadence."""
    row = dict(
        proposal_id="p1",
        proposal_status="approved",
        followup_enrolled_at=ENROLLED,
        cycle_viewed_at=ENROLLED + timedelta(hours=6),
        approved_at=APPROVED,
        deposit_status="pending",
    )
    row.update(kw)
    return row


def _at(hours):
    """A time `hours` after approval, inside the send window (10:00–13:00 Central)."""
    return APPROVED + timedelta(hours=hours)


def _due(row, at, cfg=None):
    return fr.due_now(row, at, cfg)


def _templates(dues):
    return [d.template for d in dues]


# ── the customer's half ──────────────────────────────────────────────────────
def test_an_approved_proposal_with_no_deposit_gets_chased():
    """The whole ask, in one assertion. 24h after approval at the shipped cadence."""
    assert "deposit_nudge" in _templates(_due(_row(), _at(25)))


def test_nothing_fires_before_the_first_interval():
    assert _templates(_due(_row(), _at(2))) == []


def test_the_clock_starts_AT_APPROVAL_not_at_the_send():
    """Anchored on approved_at, matching how every other move in this system re-anchors rather
    than replaying what was missed. Anchored on the send instead, a proposal approved on day 20
    would be "overdue" by twenty days and fire its whole recurring series at once."""
    row = _row(followup_enrolled_at=ENROLLED - timedelta(days=30),
               cycle_viewed_at=ENROLLED - timedelta(days=29))
    assert _templates(_due(row, _at(2))) == [], (
        "the deposit stage is reading an older anchor than the approval")
    assert "deposit_nudge" in _templates(_due(row, _at(25)))


def test_it_is_never_chased_as_a_proposal_awaiting_a_decision():
    """THE structural one. An approved row has cycle_viewed_at set, so a fall-through would chase
    a signed job with "we need your signed approval and the deposit before we can book"."""
    for hours in (25, 80, 200, 600):
        got = _templates(_due(_row(), _at(hours)))
        for wrong in ("next_steps", "second_nudge", "checkin", "not_viewed"):
            assert wrong not in got, (
                "an approved proposal is being chased with %s at %sh" % (wrong, hours))


def test_the_customer_stops_the_moment_they_tell_us_it_is_on_the_way():
    """`submitted` means ACH details entered or "the cheque is posted". Chasing then reads as
    either a mistake on our side or a second charge."""
    got = _templates(_due(_row(deposit_status="submitted"), _at(25)))
    assert "deposit_nudge" not in got, "the customer is still being chased after telling us"


def test_the_estimator_does_NOT_stop_when_the_customer_does():
    """The half Hanz specifically asked for: a cheque that never arrives has to stay somebody's
    problem. This is the assertion that fails if the two stop points get "simplified" into one."""
    got = _templates(_due(_row(deposit_status="submitted"), _at(60)))
    assert "staff_deposit_outstanding" in got, (
        "nobody is reminded about a deposit the customer said was on its way")


def test_the_money_arriving_stops_both_of_them():
    assert _due(_row(deposit_status="received"), _at(600)) == []


def test_a_job_sent_without_a_deposit_is_finished_at_approval():
    """`deposit_required is False` — GC work usually is. Chasing money nobody asked for would be
    worse than silence."""
    assert _due(_row(deposit_required=False), _at(600)) == []


def test_a_deposit_INVOICED_despite_the_flag_is_still_chased():
    """`deposit_required=False` says the proposal went out without one. `deposit_requested_at` says
    somebody raised an invoice anyway, and money that has been asked for is money worth chasing.

    This is the rule crm-core.depositSatisfied already used to decide what the BOARD says, so the
    engine reading the flag alone would have shown a staff drawer "following up until the deposit
    is in" while the worker sent nothing."""
    assert "deposit_nudge" in _templates(_due(
        _row(deposit_required=False, deposit_requested_at=APPROVED), _at(25)))


def test_a_legacy_row_with_no_deposit_flag_still_collects_one():
    """The column arrived after the rows did. Absent is not False: those jobs DID take a deposit,
    and reading a missing value as "none required" would silence the stage on all of them."""
    row = _row()
    row.pop("deposit_required", None)
    assert "deposit_nudge" in _templates(_due(row, _at(25)))


# ── the status ask, which this stage must not offer ──────────────────────────
def test_a_deposit_reminder_never_offers_the_not_moving_forward_button():
    """Every other recurring nudge offers "has your timeline changed?", and its second button sets
    closed_lost. On a job the customer has signed and been invoiced for, that is a one-click cancel
    of won work — the single most damaging thing this engine could put in an email."""
    for hours in (25, 100, 400, 1000):
        for d in _due(_row(), _at(hours)):
            if d.template == "deposit_nudge":
                assert d.include_status_ask is False, (
                    "the deposit reminder at %sh offers a one-click cancel of a won job" % hours)


def test_the_ask_is_still_offered_on_the_stages_it_belongs_to():
    """Guard against "fixing" the above by removing the escape hatch everywhere. A proposal nobody
    has opened for a week is exactly the one that needs a polite way out."""
    row = dict(proposal_id="p2", proposal_status="sent", followup_enrolled_at=ENROLLED)
    got = [d for d in _due(row, ENROLLED + timedelta(days=9)) if d.audience == "customer"]
    assert got and any(d.include_status_ask for d in got), (
        "the timeline-changed escape hatch is gone from the stages that should offer it")


# ── recurrence, dedupe and the caps ──────────────────────────────────────────
def test_it_repeats_rather_than_asking_once():
    """One email about money that never arrived is not a follow-up system."""
    keys = set()
    for day in range(1, 25):
        for d in _due(_row(), _at(24 * day)):
            if d.template == "deposit_nudge":
                keys.add(d.rule_key)
    assert len(keys) > 3, "the deposit reminder does not recur"


def test_the_same_tick_twice_reserves_the_same_key():
    """The worker dedupes on rule_key. A key with `now` in it would send on every tick."""
    a = [d.rule_key for d in _due(_row(), _at(100))]
    b = [d.rule_key for d in _due(_row(), _at(100) + timedelta(minutes=7))]
    assert a == b, "the rule key moves with the clock, so every tick would be a fresh send"


def test_the_recurring_series_respects_max_recurring():
    """Otherwise an unpaid job is chased for ever. The cap is the same knob the rest of the
    cadence uses, so setting it once bounds every stage."""
    cfg = fs.defaults()
    cfg["max_recurring"] = 2
    keys = set()
    for day in range(1, 60):
        for d in _due(_row(), _at(24 * day), cfg):
            if d.template == "deposit_nudge":
                keys.add(d.rule_key)
    assert len(keys) <= 4, "the deposit reminder ignores max_recurring: %s" % sorted(keys)


def test_a_revision_resets_the_stage():
    """Re-publishing moves followup_enrolled_at, which is what cycle_key reads. Every key here is
    cycle-scoped for that reason: the same reminder has to be sendable again against new numbers."""
    first = {d.rule_key for d in _due(_row(), _at(100))}
    again = {d.rule_key for d in _due(_row(followup_enrolled_at=ENROLLED + timedelta(days=40)),
                                     _at(100))}
    assert first and again and not (first & again), (
        "a revision re-send would be deduped against the previous cycle's reminders")


# ── the switches that outrank the stage ──────────────────────────────────────
def test_the_kill_switch_still_wins():
    assert _due(_row(followup_disabled_at=APPROVED), _at(600)) == []


def test_a_pause_the_customer_asked_for_still_wins():
    """They asked us to come back later. That it is now about money does not change the answer."""
    row = _row(followup_paused_until=(APPROVED + timedelta(days=40)).date())
    assert _due(row, _at(100)) == []


def test_a_closed_lost_job_is_not_chased_for_money():
    assert _due(_row(proposal_status="closed_lost"), _at(600)) == []


def test_the_send_window_holds_the_customer_and_not_the_estimator():
    """Same rule as every other stage: customers get business hours, staff mail does not wait.
    03:00 Central, which is outside the shipped 08:00–18:00 window."""
    at = datetime(2026, 8, 8, 8, 0, tzinfo=timezone.utc)   # 03:00 America/Chicago
    got = _due(_row(), at)
    assert "deposit_nudge" not in _templates(got), "a customer would be emailed at 3am"
    assert "staff_deposit_outstanding" in _templates(got)


# ── in_scope: one definition, three call sites ───────────────────────────────
def test_in_scope_is_what_the_worker_and_the_engine_both_ask():
    """The worker re-reads the row before acting and used to spell its own status test inline
    (`not in ("sent", "viewed")`). Widening the engine without it changed nothing observable:
    every approved row was dropped one line before the rules ran."""
    import followup_worker
    import inspect
    src = inspect.getsource(followup_worker._tick)
    assert "rules.in_scope(fresh)" in src, (
        "the worker judges scope on its own again, so it can disagree with the engine")
    assert '"sent", "viewed"' not in src, "the inline status test is back in the worker"


@pytest.mark.parametrize("row,expect", [
    (dict(proposal_status="sent"), True),
    (dict(proposal_status="viewed"), True),
    (dict(proposal_status="approved", deposit_status="pending"), True),
    (dict(proposal_status="approved", deposit_status="submitted"), True),
    (dict(proposal_status="approved", deposit_status="received"), False),
    (dict(proposal_status="approved", deposit_required=False), False),
    (dict(proposal_status="closed_lost"), False),
    (dict(proposal_status=""), False),
])
def test_in_scope_takes_the_staff_reading_of_the_deposit(row, expect):
    """The LATER of the two stops, deliberately: this only decides whether to look at a row at
    all. A `submitted` deposit is still in scope because the estimator is still owed a reminder,
    and `due_now` is what then declines to email the customer."""
    assert fr.in_scope(row) is expect


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
    """Is this one row a candidate? An approved job with its deposit outstanding, by default."""
    row = dict(proposal_id="one", proposal_status="approved", deposit_status="pending",
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
    chase that this query never returns means the deposit stage does not exist in production —
    silently, because every other test in this file is Python calling Python and still
    passes."""
    missing = _wanted(CORPUS) - admitted
    assert not missing, (
        "%d rows the cadence should chase are dropped before followup_rules ever sees them, so "
        "the deposit stage will never run on them in production:\n  %s\nquery: %s" % (
            len(missing), _describe(CORPUS, missing), candidate_sql))


def test_the_query_admits_nothing_the_rules_would_not_chase(admitted, candidate_sql):
    """The wasteful direction, and deliberately weaker than the one above.

    The query is allowed to admit rows in_scope declines, and on odd value spellings it does:
    deposit_outstanding strips and lowercases deposit_status, the SQL does not, and in_scope
    does not normalise proposal_status at all. That is what the looser-never-tighter test below
    covers. On the spellings the app ACTUALLY writes, though, the two sets have to match: a row
    the worker re-reads only to decline is a tick spent on nothing, and a candidate set drifting
    wider is how an `and` that became an `or` hides."""
    spurious = admitted - _wanted(CORPUS)
    assert not spurious, (
        "%d rows are candidates that in_scope declines, so every tick re-reads work it will "
        "never do:\n  %s\nquery: %s" % (
            len(spurious), _describe(CORPUS, spurious), candidate_sql))


def test_an_approved_job_with_its_deposit_outstanding_reaches_the_engine(candidate_sql):
    """The row the whole stage exists for. It used to be excluded right here, which is why
    widening followup_rules on its own changed nothing anybody could observe."""
    assert _admits_one(candidate_sql), (
        "approved rows are excluded again before the rules ever see them")


def test_a_received_deposit_stops_being_a_candidate(candidate_sql):
    """The last stop either audience has. Without it, paid jobs stay candidates for ever."""
    assert not _admits_one(candidate_sql, deposit_status="received")


def test_a_job_that_never_wanted_a_deposit_is_not_chased_for_one(candidate_sql):
    """`deposit_required is False` — GC work usually is."""
    assert not _admits_one(candidate_sql, deposit_required=False)


def test_a_no_deposit_job_INVOICED_ANYWAY_is_still_a_candidate(candidate_sql):
    """Same rule as test_a_deposit_INVOICED_despite_the_flag_is_still_chased, third
    implementation — and this is the half that was a grep for one substring of db.py until the
    two of them were replaced together, for the same reason."""
    assert _admits_one(candidate_sql, deposit_required=False, deposit_requested_at=STAMP)


def test_a_legacy_row_with_no_deposit_flag_is_still_a_candidate(candidate_sql):
    """Absent is not False. Reading a missing `deposit_required` as "none required" would
    silence the stage on every row that predates the column — the failure a grep for the word
    `coalesce` cannot see, because inverting its default keeps the word."""
    assert _admits_one(candidate_sql, deposit_required=None)


# The odd-spelling rows: in_scope strips and lowercases deposit_status, and does not normalise
# proposal_status at all. The SQL does neither. So the two can legitimately disagree here, and
# the DIRECTION is the invariant — SQL looser is harmless (the worker re-reads and in_scope
# declines), SQL tighter is the outage.
ODD_SPELLINGS = [
    dict(proposal_status=s, deposit_status=d)
    for s in ("approved", "Approved", "APPROVED", " approved ")
    for d in ("received", "Received", "RECEIVED", " received ", "received ", "pending", "Pending")
]


def test_the_query_is_looser_than_the_rules_and_never_tighter(candidate_sql):
    """The db.py docstring claims this clause is "deliberately coarser than the rules are".
    Coarser is fine and costs one wasted re-read. Finer is the stage silently not running, so
    the claim is pinned as an inequality rather than left as a comment."""
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
    ("the approved branch keyed on a status nothing ever writes", "'approved'", "'accepted'"),
    ("the deposit stop keyed on a status nothing ever writes", "'received'", "'paid'"),
    ("'sent' dropped from the statuses the cadence chases", "'sent'", "'issued'"),
)

# Alias-tolerant, and skipped rather than failed when the clause no longer spells the rule this
# way — the mandatory proofs above already keep the harness honest.
SHAPE_MUTATIONS = (
    ("the coalesce default for deposit_required inverted",
     r"coalesce\(((?:\w+\.)?deposit_required),\s*true\)", r"coalesce(\1, false)"),
    ("the invoiced exception read backwards",
     r"((?:\w+\.)?deposit_requested_at) is not null", r"\1 is null"),
    ("the enrolment guard inverted",
     r"((?:\w+\.)?followup_enrolled_at) is not null", r"\1 is null"),
)


def test_this_differential_would_notice_the_predicate_breaking(candidate_sql):
    """NON-NEGOTIABLE, and the reason this harness earns its length. It is the only thing
    standing between this file and a silent false green — a differential whose evaluator had
    quietly stopped evaluating would pass every assertion above without a murmur, which is
    precisely how the greps it replaced survived for as long as they did.

    So the query is deliberately broken, five ways that must always apply and three more that
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


# ── the board's next-follow-up column ────────────────────────────────────────
def test_next_due_at_mirrors_the_stage():
    """The board shows when the next email goes out. Saying "none" on a job that will be chased
    tomorrow is how staff learn to distrust the column."""
    when = fr.next_due_at(_row(), _at(2))
    assert when is not None, "the board would show no upcoming follow-up on an unpaid won job"
    assert when > _at(2)


def test_next_due_at_goes_quiet_once_the_customer_has_told_us():
    """It describes the CUSTOMER's schedule, which is what that column has always meant."""
    assert fr.next_due_at(_row(deposit_status="submitted"), _at(2)) is None
    assert fr.next_due_at(_row(deposit_status="received"), _at(2)) is None


# ── the wording it chases with ───────────────────────────────────────────────
def test_the_template_exists_and_is_editable():
    assert "deposit_nudge" in fs.TEMPLATE_KEYS
    t = fs.DEFAULT_TEMPLATES["deposit_nudge"]
    assert "{link}" in t["body"] and t["title"] and t["cta"]
    assert fs.LABELS["deposit_nudge"] == "Deposit reminder"
    assert "deposit" in fs.EDITOR_TITLES["deposit_nudge"].lower()


def test_the_shipped_wording_does_not_ask_again_for_the_approval_they_gave():
    """`{need}` renders as "your signed approval and the deposit". By the time this sends they
    have signed, and asking again reads as a mistake on our side."""
    assert "{need}" not in fs.DEFAULT_TEMPLATES["deposit_nudge"]["body"]


def test_the_reminder_links_to_the_deposit_step_not_the_top_of_the_proposal():
    """They have read and approved the proposal. Landing them back at the top of it makes them
    hunt for the one thing the email asked for."""
    import inspect

    import followup_worker
    src = inspect.getsource(followup_worker._send_customer)
    assert '"#proposal/deposit"' in src, "the deposit reminder opens the proposal, not the deposit"
    assert 'due.template == "deposit_nudge"' in src, (
        "the anchor is not gated on the deposit reminder, so every follow-up would deep-link")
